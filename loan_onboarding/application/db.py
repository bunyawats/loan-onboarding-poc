"""The ONLY code touching the `applications`/`loan_apply_requests`
tables (CLAUDE.md's module dependency graph). Owns a lazily-initialized
connection pool of its own -- same convention as customer/db.py and
account/db.py, not a shared pool.

Deliberately a thin data-access layer: `insert`/`update_decision`/
`update_resubmission` take already-resolved column values, they don't
decide *which* columns matter for a given decision (that branching --
"underwriter columns vs manager columns vs neither, for a CANCELLED
decision" -- lives in `application/activities.py`, per CLAUDE.md's
"Breaking the cycle": activities.py is where every write's business
logic lives, db.py just executes it).

**Phase 23, "Split loan application request data (1:1)"** (see
CLAUDE.md / IMPLEMENTATION_PLAN.md) split what used to be one table
into two: `applications` (the real loan application -- product_type,
payload, applicant identity/amount, plus a *mirrored* `status`) and
`loan_apply_requests` (onboarding-workflow tracking -- workflow_id,
the operational `status`, staff decisions, risk_tier), 1:1 via
`application_id`. Every write function below now writes both tables in
one transaction (`status` always goes to both -- that's the entire
point of the mirror); every read function does a `LEFT JOIN`,
deliberately not `JOIN` -- an `INNER JOIN` would make an application
vanish from every list/lookup the moment its `loan_apply_requests` row
is gone, defeating the reason `applications.status` is mirrored in the
first place. The public shape callers see (one merged record per
application) is unchanged from before this split -- `application/
service.py`, `application/activities.py`, and both BFFs need no
changes at all, only this file and `models.py` do."""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """asyncpg doesn't serialize dict <-> jsonb automatically -- register
    a codec per-connection so `payload` can be passed/read as a plain
    Python dict everywhere else in this module."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], init=_init_connection)
    return _pool


# Shared by every read function below. LEFT JOIN, deliberately -- see
# this module's own docstring. Column list is explicit (not `a.*, r.*`)
# so `application_id` (present on both tables, opaque link not a real
# FK -- see db/schema.sql's own header) is never ambiguous, and so
# `a.status` (the durable, mirrored copy) is always what a caller reads
# as `status`, never `r.status` (the operational copy -- identical in
# practice except in the one degraded case this whole design exists
# for: `loan_apply_requests` truncated, `r.*` all NULL).
_SELECT_JOINED = """
    SELECT
        a.application_id, a.applicant_identifier, a.customer_id, a.product_type,
        a.payload, a.applicant_name, a.applicant_email, a.applicant_phone,
        a.amount, a.status, a.created_at,
        r.workflow_id, r.underwriter_name, r.underwriter_comment, r.underwriter_decided_at,
        r.manager_name, r.manager_comment, r.manager_decided_at, r.risk_tier, r.updated_at
    FROM applications a
    LEFT JOIN loan_apply_requests r ON r.application_id = a.application_id
"""


async def insert(
    application_id: str,
    applicant_identifier: str,
    customer_id: str | None,
    workflow_id: str,
    product_type: str,
    payload: dict[str, Any],
    applicant_name: str,
    applicant_email: str,
    applicant_phone: str,
    amount: Decimal,
    status: str,
) -> asyncpg.Record:
    """Written by `persist_application` (the workflow's first activity),
    never directly by `application.service.create_application` -- see
    CLAUDE.md's "Applying without being a customer yet" / the
    application module section for why.

    `status` is caller-supplied (Phase 21: `workflows.py`'s own
    `self._status`, `PENDING_RISK_ASSESSMENT`), not left to either
    table's own `DEFAULT` -- same "no implicit database default"
    discipline this codebase already applies to primary keys (see
    CLAUDE.md's "Data storage"). Written to *both* tables (Phase 23) --
    `applications.status` and `loan_apply_requests.status` start in
    sync and stay that way, every write from here on touching both.

    Both inserts use `ON CONFLICT (application_id) DO NOTHING`, in one
    transaction, making the whole operation safe against a Temporal
    activity retry (the workflow's `DEFAULT_RETRY_POLICY` allows up to
    5 attempts) -- a raw duplicate `INSERT` on either primary key would
    otherwise surface as an unhandled `UniqueViolationError` on a
    retried-but-already-succeeded first attempt, same idempotency
    concern `review-approval-temporal`'s own `persist_request` activity
    already handles this exact way. Wrapping both inserts in one
    transaction is what makes this safe under a retry -- a first
    attempt either commits both rows or neither, so a retry never finds
    one table's row present without the other's.

    `application_id` is caller-supplied (application/service.py's
    create_application generates it via idgen, not a database default),
    so there's no PK-collision retry loop here the way customer/db.py
    and account/db.py need -- a collision on this id would mean two
    different workflow executions somehow picked the same id, which
    `create_application`'s own idgen call already handles at its own
    generation site, not here."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO applications (
                    application_id, applicant_identifier, customer_id,
                    product_type, payload, applicant_name, applicant_email,
                    applicant_phone, amount, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (application_id) DO NOTHING
                """,
                application_id,
                applicant_identifier,
                customer_id,
                product_type,
                payload,
                applicant_name,
                applicant_email,
                applicant_phone,
                amount,
                status,
            )
            await conn.execute(
                """
                INSERT INTO loan_apply_requests (application_id, workflow_id, status)
                VALUES ($1, $2, $3)
                ON CONFLICT (application_id) DO NOTHING
                """,
                application_id,
                workflow_id,
                status,
            )
            record = await conn.fetchrow(_SELECT_JOINED + " WHERE a.application_id = $1", application_id)
    assert record is not None, "row must exist after ON CONFLICT DO NOTHING"
    return record


async def update_decision(
    application_id: str,
    *,
    status: str,
    underwriter_name: str | None = None,
    underwriter_comment: str | None = None,
    underwriter_decided_at: datetime | None = None,
    manager_name: str | None = None,
    manager_comment: str | None = None,
    manager_decided_at: datetime | None = None,
    customer_id: str | None = None,
    updated_at: datetime | None = None,
    risk_tier: str | None = None,
) -> asyncpg.Record:
    """Generic decision-outcome writer. Every optional column is
    preserved (via `COALESCE`) rather than overwritten with `NULL` when
    the caller doesn't pass it -- `application/activities.py` passes
    only the columns relevant to the specific decision being persisted
    (e.g. a CANCELLED decision passes neither underwriter_*/manager_*
    set; an underwriter REQUEST_MORE_INFO passes only the underwriter_*
    set; a terminal APPROVED decision additionally passes `customer_id`
    from the provisioning step). No `account_id` parameter -- there is
    no such column on either table; `accounts.application_id` is the
    pointer now (see CLAUDE.md's "Applying without being a customer
    yet").

    Two tables, one transaction (Phase 23): `applications` gets `status`
    (always -- the mirror) and `customer_id`; `loan_apply_requests` gets
    `status` (the operational copy) plus every workflow-tracking column.
    `updated_at` (now `loan_apply_requests`-only) defaults to `now()`
    but can be overridden (native-Temporal-cancel path) to reflect the
    moment Temporal actually delivered the cancellation rather than
    whenever the (possibly retried) activity happens to execute.
    `risk_tier` (Phase 21) is only ever passed for a risk-driven
    auto-decision (LOW/HIGH) -- `None` for every human decision, which
    leaves the column untouched (already `NULL` in that case, so
    `COALESCE` is a no-op, not just a safety net)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE applications SET status = $2, customer_id = COALESCE($3, customer_id) WHERE application_id = $1",
                application_id,
                status,
                customer_id,
            )
            await conn.execute(
                """
                UPDATE loan_apply_requests
                SET status = $2,
                    underwriter_name = COALESCE($3, underwriter_name),
                    underwriter_comment = COALESCE($4, underwriter_comment),
                    underwriter_decided_at = COALESCE($5, underwriter_decided_at),
                    manager_name = COALESCE($6, manager_name),
                    manager_comment = COALESCE($7, manager_comment),
                    manager_decided_at = COALESCE($8, manager_decided_at),
                    updated_at = COALESCE($9, now()),
                    risk_tier = COALESCE($10, risk_tier)
                WHERE application_id = $1
                """,
                application_id,
                status,
                underwriter_name,
                underwriter_comment,
                underwriter_decided_at,
                manager_name,
                manager_comment,
                manager_decided_at,
                updated_at,
                risk_tier,
            )
            record = await conn.fetchrow(_SELECT_JOINED + " WHERE a.application_id = $1", application_id)
    return record


async def clear_risk_assessment(application_id: str) -> asyncpg.Record:
    """Phase 21's MEDIUM-tier outcome: no decision was made (no
    underwriter/manager column to write, no `risk_tier` recorded --
    CLAUDE.md's "Automated risk assessment via NATS" is explicit that
    `risk_tier` is only ever set for an auto-*decided* LOW/HIGH
    outcome), just a plain status flip out of `PENDING_RISK_ASSESSMENT`
    into the existing `PENDING_UNDERWRITING` wait. A dedicated, minimal
    function rather than routing this through `update_decision` with
    every other column `None` -- there is no "decision" here for that
    function's own column semantics to attach to. Two tables, one
    transaction, same as every other write here (Phase 23)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE applications SET status = 'PENDING_UNDERWRITING' WHERE application_id = $1",
                application_id,
            )
            await conn.execute(
                "UPDATE loan_apply_requests SET status = 'PENDING_UNDERWRITING', updated_at = now() WHERE application_id = $1",
                application_id,
            )
            record = await conn.fetchrow(_SELECT_JOINED + " WHERE a.application_id = $1", application_id)
    return record


async def update_resubmission(application_id: str, payload: dict[str, Any], status: str) -> asyncpg.Record:
    """`status` is caller-supplied (application/activities.py passes
    workflow.workflows.STATUS_PENDING_UNDERWRITING), same
    already-resolved-column-values principle this module's own
    docstring states -- a resubmission always lands back at that status,
    but which status that is isn't this thin data-access layer's
    decision to hardcode. `payload` lives on `applications` now (Phase
    23); `status`/`updated_at` still split the same way every other
    write here does -- both tables, one transaction."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE applications SET payload = $2, status = $3 WHERE application_id = $1",
                application_id,
                payload,
                status,
            )
            await conn.execute(
                "UPDATE loan_apply_requests SET status = $2, updated_at = now() WHERE application_id = $1",
                application_id,
                status,
            )
            record = await conn.fetchrow(_SELECT_JOINED + " WHERE a.application_id = $1", application_id)
    return record


async def get(application_id: str) -> asyncpg.Record | None:
    pool = await _get_pool()
    return await pool.fetchrow(_SELECT_JOINED + " WHERE a.application_id = $1", application_id)


async def get_by_workflow_id(workflow_id: str) -> asyncpg.Record | None:
    pool = await _get_pool()
    return await pool.fetchrow(_SELECT_JOINED + " WHERE r.workflow_id = $1", workflow_id)


async def list_for_applicant(applicant_identifier: str, limit: int, offset: int) -> list[asyncpg.Record]:
    pool = await _get_pool()
    return await pool.fetch(
        _SELECT_JOINED
        + """
        WHERE a.applicant_identifier = $1
        ORDER BY a.created_at DESC
        LIMIT $2 OFFSET $3
        """,
        applicant_identifier,
        limit,
        offset,
    )


async def count_for_applicant(applicant_identifier: str) -> int:
    pool = await _get_pool()
    return await pool.fetchval(
        "SELECT count(*) FROM applications WHERE applicant_identifier = $1",
        applicant_identifier,
    )


async def list_by_status(status: str, limit: int, offset: int) -> list[asyncpg.Record]:
    pool = await _get_pool()
    return await pool.fetch(
        _SELECT_JOINED
        + """
        WHERE a.status = $1
        ORDER BY a.created_at DESC
        LIMIT $2 OFFSET $3
        """,
        status,
        limit,
        offset,
    )


async def count_by_status(status: str) -> int:
    """Single-table read against `applications` alone (Phase 23) -- no
    join needed just to filter/count by `status`, since the mirrored
    copy already lives there. A real query-cost win the mirrored column
    earns beyond just truncation-survival -- don't "fix" this into a
    join out of habit once every other read function here needs one."""
    pool = await _get_pool()
    return await pool.fetchval(
        "SELECT count(*) FROM applications WHERE status = $1",
        status,
    )
