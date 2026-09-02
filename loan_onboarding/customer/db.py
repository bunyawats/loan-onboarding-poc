"""The ONLY code touching the `customers` table (CLAUDE.md's module
dependency graph). Owns a lazily-initialized connection pool -- each
domain module manages its own pool rather than sharing one, matching
"each module owns its own data" taken down to the connection level."""

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


async def find_by_identifier(applicant_identifier: str) -> asyncpg.Record | None:
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM customers WHERE applicant_identifier = $1",
        applicant_identifier,
    )


async def get_or_create(applicant_identifier: str) -> asyncpg.Record:
    """Atomic find-or-create -- INSERT ... ON CONFLICT DO NOTHING closes
    the race a naive find-then-insert would have (two concurrent calls
    for the same identifier must never create two rows; see CLAUDE.md's
    "Applying without being a customer yet" idempotency note)."""
    pool = await _get_pool()
    record = await pool.fetchrow(
        """
        INSERT INTO customers (applicant_identifier)
        VALUES ($1)
        ON CONFLICT (applicant_identifier) DO NOTHING
        RETURNING *
        """,
        applicant_identifier,
    )
    if record is not None:
        return record
    # Someone else won the race between our INSERT and now.
    record = await pool.fetchrow(
        "SELECT * FROM customers WHERE applicant_identifier = $1",
        applicant_identifier,
    )
    assert record is not None, "row must exist after ON CONFLICT DO NOTHING"
    return record


async def get(customer_id: UUID) -> asyncpg.Record | None:
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM customers WHERE customer_id = $1",
        customer_id,
    )
