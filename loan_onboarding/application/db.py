"""The ONLY code touching the `applications` table (CLAUDE.md's module
dependency graph). Owns a lazily-initialized connection pool of its
own -- same convention as customer/db.py and account/db.py, not a
shared pool.

Deliberately a thin data-access layer: `insert`/`update_decision`/
`update_resubmission` take already-resolved column values, they don't
decide *which* columns matter for a given decision (that branching --
"underwriter columns vs manager columns vs neither, for a CANCELLED
decision" -- lives in `application/activities.py`, per CLAUDE.md's
"Breaking the cycle": activities.py is where every write's business
logic lives, db.py just executes it)."""

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
) -> asyncpg.Record:
    """Written by `persist_application` (the workflow's first activity),
    never directly by `application.service.create_application` -- see
    CLAUDE.md's "Applying without being a customer yet" / the
    application module section for why.

    `ON CONFLICT (application_id) DO NOTHING` makes this safe against a
    Temporal activity retry (the workflow's `DEFAULT_RETRY_POLICY`
    allows up to 5 attempts) -- a raw duplicate `INSERT` on the primary
    key would otherwise surface as an unhandled
    `UniqueViolationError` on a retried-but-already-succeeded first
    attempt, same idempotency concern `review-approval-temporal`'s own
    `persist_request` activity already handles this exact way.

    `application_id` is caller-supplied (application/service.py's
    create_application generates it via idgen, not a database default),
    so there's no PK-collision retry loop here the way customer/db.py
    and account/db.py need -- a collision on this id would mean two
    different workflow executions somehow picked the same id, which
    `create_application`'s own idgen call already handles at its own
    generation site, not here."""
    pool = await _get_pool()
    record = await pool.fetchrow(
        """
        INSERT INTO applications (
            application_id, applicant_identifier, customer_id, workflow_id,
            product_type, payload, applicant_name, applicant_email,
            applicant_phone, amount
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (application_id) DO NOTHING
        RETURNING *
        """,
        application_id,
        applicant_identifier,
        customer_id,
        workflow_id,
        product_type,
        payload,
        applicant_name,
        applicant_email,
        applicant_phone,
        amount,
    )
    if record is not None:
        return record
    # A retry of an already-succeeded first attempt -- return the
    # existing row rather than None, so the activity still completes
    # normally.
    record = await get(application_id)
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
) -> asyncpg.Record:
    """Generic decision-outcome writer. Every column here is optional
    and preserved (via `COALESCE`) rather than overwritten with `NULL`
    when the caller doesn't pass it -- `application/activities.py`
    passes only the columns relevant to the specific decision being
    persisted (e.g. a CANCELLED decision passes neither
    underwriter_*/manager_* set; an underwriter REQUEST_MORE_INFO
    passes only the underwriter_* set; a terminal APPROVED decision
    additionally passes `customer_id` from the provisioning step). No
    `account_id` parameter -- there is no such column on `applications`
    anymore; `accounts.application_id` is the pointer now (see
    CLAUDE.md's "Applying without being a customer yet"). `updated_at`
    defaults to `now()` but can be overridden (native-Temporal-cancel
    path) to reflect the moment Temporal actually delivered the
    cancellation rather than whenever the (possibly retried) activity
    happens to execute."""
    pool = await _get_pool()
    return await pool.fetchrow(
        """
        UPDATE applications
        SET status = $2,
            underwriter_name = COALESCE($3, underwriter_name),
            underwriter_comment = COALESCE($4, underwriter_comment),
            underwriter_decided_at = COALESCE($5, underwriter_decided_at),
            manager_name = COALESCE($6, manager_name),
            manager_comment = COALESCE($7, manager_comment),
            manager_decided_at = COALESCE($8, manager_decided_at),
            customer_id = COALESCE($9, customer_id),
            updated_at = COALESCE($10, now())
        WHERE application_id = $1
        RETURNING *
        """,
        application_id,
        status,
        underwriter_name,
        underwriter_comment,
        underwriter_decided_at,
        manager_name,
        manager_comment,
        manager_decided_at,
        customer_id,
        updated_at,
    )


async def update_resubmission(application_id: str, payload: dict[str, Any], status: str) -> asyncpg.Record:
    """`status` is caller-supplied (application/activities.py passes
    workflow.workflows.STATUS_PENDING_UNDERWRITING), same
    already-resolved-column-values principle this module's own
    docstring states -- a resubmission always lands back at that status,
    but which status that is isn't this thin data-access layer's
    decision to hardcode."""
    pool = await _get_pool()
    return await pool.fetchrow(
        """
        UPDATE applications
        SET payload = $2, status = $3, updated_at = now()
        WHERE application_id = $1
        RETURNING *
        """,
        application_id,
        payload,
        status,
    )


async def get(application_id: str) -> asyncpg.Record | None:
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM applications WHERE application_id = $1",
        application_id,
    )


async def get_by_workflow_id(workflow_id: str) -> asyncpg.Record | None:
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM applications WHERE workflow_id = $1",
        workflow_id,
    )


async def list_for_applicant(applicant_identifier: str, limit: int, offset: int) -> list[asyncpg.Record]:
    pool = await _get_pool()
    return await pool.fetch(
        """
        SELECT * FROM applications
        WHERE applicant_identifier = $1
        ORDER BY created_at DESC
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
        """
        SELECT * FROM applications
        WHERE status = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        status,
        limit,
        offset,
    )


async def count_by_status(status: str) -> int:
    pool = await _get_pool()
    return await pool.fetchval(
        "SELECT count(*) FROM applications WHERE status = $1",
        status,
    )
