"""The ONLY code touching the `accounts` table (CLAUDE.md's module
dependency graph). Owns a lazily-initialized connection pool of its
own -- same convention as customer/db.py, not a shared pool."""

from __future__ import annotations

import os

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
