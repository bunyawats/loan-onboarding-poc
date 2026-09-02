"""The ONLY code touching the `customers` table (CLAUDE.md's module
dependency graph). Owns a lazily-initialized connection pool -- each
domain module manages its own pool rather than sharing one, matching
"each module owns its own data" taken down to the connection level."""

from __future__ import annotations

import os

import asyncpg

from loan_onboarding.idgen import service as idgen_service

_pool: asyncpg.Pool | None = None

_ID_PREFIX = "CUS"
_ID_LENGTH = 9
_MAX_ID_COLLISION_RETRIES = 10


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
    "Applying without being a customer yet" idempotency note).

    Two independent conflict paths on insert now that `customer_id` is
    an application-assigned string rather than a database default: the
    `ON CONFLICT (applicant_identifier) DO NOTHING` above (a real
    concurrent caller for the same identifier) and a freshly-generated
    `customer_id` colliding with an unrelated row's primary key --
    handled by regenerating and retrying, bounded at
    `_MAX_ID_COLLISION_RETRIES` attempts."""
    pool = await _get_pool()
    for _ in range(_MAX_ID_COLLISION_RETRIES):
        customer_id = idgen_service.generate_id(_ID_PREFIX, _ID_LENGTH)
        try:
            record = await pool.fetchrow(
                """
                INSERT INTO customers (customer_id, applicant_identifier)
                VALUES ($1, $2)
                ON CONFLICT (applicant_identifier) DO NOTHING
                RETURNING *
                """,
                customer_id,
                applicant_identifier,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            if exc.constraint_name == "customers_pkey":
                continue
            raise
        if record is not None:
            return record
        # Someone else won the race between our INSERT and now.
        record = await pool.fetchrow(
            "SELECT * FROM customers WHERE applicant_identifier = $1",
            applicant_identifier,
        )
        assert record is not None, "row must exist after ON CONFLICT DO NOTHING"
        return record
    raise RuntimeError(
        f"failed to generate a unique customer_id after {_MAX_ID_COLLISION_RETRIES} attempts"
    )


async def get(customer_id: str) -> asyncpg.Record | None:
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM customers WHERE customer_id = $1",
        customer_id,
    )
