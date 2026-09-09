"""The ONLY code touching the `accounts` table (CLAUDE.md's module
dependency graph). Owns a lazily-initialized connection pool of its
own -- same convention as customer/db.py, not a shared pool."""

from __future__ import annotations

import os
from datetime import datetime

import asyncpg

from loan_onboarding.idgen import service as idgen_service

_pool: asyncpg.Pool | None = None

_ID_PREFIX = "ACC"
_ID_LENGTH = 9
_MAX_ID_COLLISION_RETRIES = 10


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    return _pool


async def create(customer_id: str, product_type: str, application_id: str) -> asyncpg.Record:
    """Always inserts a new row -- no find-or-create (an account is 1:1
    with an approved application, not 1:1 with a customer). NOT
    conflict-safe against the *business* rule on its own: relies on the
    caller (application/activities.py's persist_decision) having
    already run account.service.has_active_account_of_type as a
    pre-check via application.service.check_decision_allowed.
    db/schema.sql's partial unique index
    (ux_accounts_customer_active_product_type) is the last-resort
    backstop if that check was skipped or raced -- this function
    deliberately does not catch that constraint violation, so it
    surfaces as a real error rather than being silently swallowed.

    Separately, and unconditionally, this function DOES retry on its
    own generated `account_id` colliding with an unrelated row's
    primary key -- an engineering concern, not a business one, same
    pattern as customer/db.py's get_or_create."""
    pool = await _get_pool()
    for _ in range(_MAX_ID_COLLISION_RETRIES):
        account_id = idgen_service.generate_id(_ID_PREFIX, _ID_LENGTH)
        try:
            return await pool.fetchrow(
                """
                INSERT INTO accounts (account_id, customer_id, product_type, application_id)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                account_id,
                customer_id,
                product_type,
                application_id,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            if exc.constraint_name == "accounts_pkey":
                continue
            raise
    raise RuntimeError(
        f"failed to generate a unique account_id after {_MAX_ID_COLLISION_RETRIES} attempts"
    )


async def has_active_account_of_type(customer_id: str, product_type: str) -> bool:
    pool = await _get_pool()
    return await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM accounts
            WHERE customer_id = $1 AND product_type = $2 AND status = 'ACTIVE'
        )
        """,
        customer_id,
        product_type,
    )


async def get(account_id: str) -> asyncpg.Record | None:
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM accounts WHERE account_id = $1",
        account_id,
    )


async def get_by_application_id(application_id: str) -> asyncpg.Record | None:
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM accounts WHERE application_id = $1",
        application_id,
    )


async def set_status(account_id: str, status: str) -> asyncpg.Record | None:
    """The only place `accounts.status` gets written from the closure
    flow now that closure history moved to its own table (Phase 22,
    "Account closure request history (1:M)" -- see CLAUDE.md /
    IMPLEMENTATION_PLAN.md). Deliberately unconditional (no WHERE-clause
    status guard) -- a retry setting the same value again is harmless,
    and `account_closure_requests`' own PENDING-uniqueness index is what
    actually protects against a second concurrent request, not this
    function."""
    pool = await _get_pool()
    return await pool.fetchrow(
        "UPDATE accounts SET status = $2 WHERE account_id = $1 RETURNING *",
        account_id,
        status,
    )


_CLOSURE_ID_PREFIX = "ACR"
_CLOSURE_ID_LENGTH = 9


async def create_closure_request(
    account_id: str, workflow_id: str, workflow_run_id: str | None = None
) -> asyncpg.Record:
    """Called only from account/activities.py's persist_closure_request,
    the first activity CloseAccountWorkflow.run() executes (Phase 18,
    "Account closure"; this insert-based shape is Phase 22, "Account
    closure request history (1:M)" -- see CLAUDE.md /
    IMPLEMENTATION_PLAN.md). Always inserts a new row -- one per
    request, not one overwritten per account -- mints
    `closure_request_id` via `idgen`, same PK-collision-retry pattern
    `create()` already uses for `accounts.account_id`.

    Idempotent against a Temporal retry of this same activity execution:
    a second attempt hits `account_closure_requests`'
    `ux_closure_requests_account_pending` partial unique index (the
    real, DB-level "at most one PENDING request per account" rule), and
    when the existing PENDING row's own `workflow_run_id` matches this
    call's -- proving it's genuinely the same Temporal execution
    retrying, not a different one that raced past `request_closure`'s
    own `ACTIVE`-only precondition and Temporal's own
    `start_workflow`-level single-flight guarantee for this
    deterministic workflow id -- this function returns that existing row
    instead of raising, the same "check current state before redoing a
    side effect" idempotency discipline `application/activities.py`'s
    own `persist_decision` already uses. A conflicting row that does
    NOT match (which, per the reasoning above, should never actually
    happen through the normal call path) is deliberately left to
    propagate rather than silently attributed to the wrong execution."""
    pool = await _get_pool()
    for _ in range(_MAX_ID_COLLISION_RETRIES):
        closure_request_id = idgen_service.generate_id(_CLOSURE_ID_PREFIX, _CLOSURE_ID_LENGTH)
        try:
            return await pool.fetchrow(
                """
                INSERT INTO account_closure_requests (closure_request_id, account_id, workflow_id, workflow_run_id)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                closure_request_id,
                account_id,
                workflow_id,
                workflow_run_id,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            if exc.constraint_name == "account_closure_requests_pkey":
                continue
            if exc.constraint_name == "ux_closure_requests_account_pending":
                existing = await pool.fetchrow(
                    "SELECT * FROM account_closure_requests WHERE account_id = $1 AND status = 'PENDING'",
                    account_id,
                )
                assert existing is not None, f"race: PENDING closure request for {account_id} vanished"
                if existing["workflow_run_id"] == workflow_run_id:
                    return existing
                raise
            raise
    raise RuntimeError(
        f"failed to generate a unique closure_request_id after {_MAX_ID_COLLISION_RETRIES} attempts"
    )


async def get_closure_request(closure_request_id: str) -> asyncpg.Record | None:
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM account_closure_requests WHERE closure_request_id = $1",
        closure_request_id,
    )


async def get_pending_closure_request(account_id: str) -> asyncpg.Record | None:
    """Read-only. Backs the customer/staff-facing 'is there a request in
    flight right now, and what's its workflow_id' lookup -- callers that
    used to read `accounts.closure_workflow_id` directly now call this
    instead."""
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM account_closure_requests WHERE account_id = $1 AND status = 'PENDING'",
        account_id,
    )


async def update_closure_request_decision(
    closure_request_id: str,
    status: str,
    decision_comment: str,
    decided_by: str,
    decided_at: datetime,
) -> asyncpg.Record | None:
    """Called only from account/activities.py's persist_closure_decision,
    which is itself responsible for the idempotency check (this
    function has no WHERE-clause guard of its own -- unlike
    create_closure_request, its caller already decided, by reading this
    exact row's current status first, that this write should happen at
    all). Updates by `closure_request_id`, not `account_id` -- the row
    this decision applies to is now explicit (threaded through
    `PersistClosureDecisionInput`), not "whichever row happens to be
    PENDING for this account.\""""
    pool = await _get_pool()
    return await pool.fetchrow(
        """
        UPDATE account_closure_requests
        SET status = $2,
            decision_comment = $3,
            decided_by = $4,
            decided_at = $5
        WHERE closure_request_id = $1
        RETURNING *
        """,
        closure_request_id,
        status,
        decision_comment,
        decided_by,
        decided_at,
    )


async def list_by_status(status: str) -> list[asyncpg.Record]:
    """Unpaginated, deliberately -- Phase 18's closure-request queue
    (CLAUDE.md's "Account closure") is a supplementary staff screen, not
    a first-class high-volume queue like `applications`' own
    `list_by_status`-backed underwriter/manager screens (which need the
    full list-pagination-bulk-actions treatment). Closure requests are
    expected to be rare enough at POC scale that a single unpaginated
    list is the right-sized answer, not a missing feature.

    Joins `accounts` for `customer_id`/`product_type` -- a same-module
    join (`account_closure_requests` and `accounts` are both owned by
    this file), not the cross-module join `CLAUDE.md`'s "no real FKs"
    rule is actually about."""
    pool = await _get_pool()
    return await pool.fetch(
        """
        SELECT r.*, a.customer_id, a.product_type
        FROM account_closure_requests r
        JOIN accounts a ON a.account_id = r.account_id
        WHERE r.status = $1
        ORDER BY r.requested_at ASC
        """,
        status,
    )


async def list_closure_requests_for_account(account_id: str) -> list[asyncpg.Record]:
    """Read-only. Full closure history for one account, newest first --
    backs a customer/staff-facing history view (Phase 22, P22-4)."""
    pool = await _get_pool()
    return await pool.fetch(
        "SELECT * FROM account_closure_requests WHERE account_id = $1 ORDER BY requested_at DESC",
        account_id,
    )
