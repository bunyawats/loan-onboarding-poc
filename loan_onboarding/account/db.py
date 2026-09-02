"""The ONLY code touching the `accounts` table (CLAUDE.md's module
dependency graph). Owns a lazily-initialized connection pool of its
own -- same convention as customer/db.py, not a shared pool."""

from __future__ import annotations

import os
from uuid import UUID

import asyncpg

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    return _pool


async def create(customer_id: UUID, product_type: str) -> asyncpg.Record:
    """Always inserts a new row -- no find-or-create (an account is 1:1
    with an approved application, not 1:1 with a customer). NOT
    conflict-safe on its own: relies on the caller
    (application/activities.py's persist_decision) having already run
    account.service.has_active_account_of_type as a pre-check via
    application.service.check_decision_allowed. db/schema.sql's partial
    unique index (ux_accounts_customer_active_product_type) is the
    last-resort backstop if that check was skipped or raced -- this
    function deliberately does not catch that constraint violation, so
    it surfaces as a real error rather than being silently swallowed."""
    pool = await _get_pool()
    return await pool.fetchrow(
        """
        INSERT INTO accounts (customer_id, product_type)
        VALUES ($1, $2)
        RETURNING *
        """,
        customer_id,
        product_type,
    )


async def has_active_account_of_type(customer_id: UUID, product_type: str) -> bool:
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


async def get(account_id: UUID) -> asyncpg.Record | None:
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM accounts WHERE account_id = $1",
        account_id,
    )
