"""The ONLY code touching the `application_document`/`account_document`/
`customer_document` tables (CLAUDE.md's module dependency graph). Owns a
lazily-initialized connection pool of its own -- same convention as
`customer/db.py`/`account/db.py`/`application/db.py`, not a shared pool.

**Phase 24, "Document metadata persistence in Postgres"** (see
CLAUDE.md / IMPLEMENTATION_PLAN.md) is this module's first-ever
Postgres persistence -- previously `document/` had none at all, Mayan
was the only record of what documents exist. These three tables are
the PRIMARY source of truth for "what documents exist" now, not a
fallback cache -- `document/service.py` reads them directly, Mayan
stays the system of record only for actual file bytes and the visual
Index Template tree.

Two accumulation shapes, matching the schema: `application_document`
has no uniqueness beyond `mayan_document_id` (unlimited rows per
`(application_id, category)`, `insert_application_document` always
inserts a new row); `account_document`/`customer_document` are unique
on `(account_id, category)`/`(customer_id, category)` -- a re-upload
updates the existing row in place, so their write paths are upserts,
not plain inserts. Every primary key here is an app-minted `idgen` id,
same PK-collision-retry convention `account/db.py`'s `create()`/
`create_closure_request()` already establish -- `document/` joins the
list of `idgen`-importing modules for the first time this phase."""

from __future__ import annotations

import os

import asyncpg

from loan_onboarding.idgen import service as idgen_service

_pool: asyncpg.Pool | None = None

_MAX_ID_COLLISION_RETRIES = 10

_APPLICATION_DOCUMENT_ID_PREFIX = "APD"
_APPLICATION_DOCUMENT_ID_LENGTH = 9

_ACCOUNT_DOCUMENT_ID_PREFIX = "ACD"
_ACCOUNT_DOCUMENT_ID_LENGTH = 9

_CUSTOMER_DOCUMENT_ID_PREFIX = "CUD"
_CUSTOMER_DOCUMENT_ID_LENGTH = 9


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    return _pool


# ---------------------------------------------------------------
# application_document -- no uniqueness beyond mayan_document_id, so
# every call always inserts a new row (a category is satisfied by one
# or more documents -- CLAUDE.md's "a category is satisfied by one or
# more documents, not exactly one").
# ---------------------------------------------------------------


async def insert_application_document(
    mayan_document_id: int,
    application_id: str,
    applicant_identifier: str,
    category: str,
    filename: str,
    customer_id: str | None = None,
) -> asyncpg.Record:
    """Called by `document.service.upload` after its Mayan sequence
    succeeds (Mayan first, per this phase's write-ordering rule -- see
    CLAUDE.md). `account_id` always starts `NULL` here -- there's no
    account to tag at upload time (see CLAUDE.md's "Applying without
    being a customer yet"); it's set later, in bulk, by
    `set_application_document_provisioning` on approval. `customer_id`
    may already be known at upload time (a returning applicant who
    already resolves to an existing customer) -- passed straight
    through, same as `document.service.upload`'s own optional
    parameter.

    Retries on its own generated `application_document_id` colliding
    with an unrelated row's primary key -- an engineering concern, same
    pattern `account/db.py`'s `create()` already uses; there is no
    business-rule constraint on this table to guard against, unlike
    `upsert_account_document`/`upsert_customer_document` below."""
    pool = await _get_pool()
    for _ in range(_MAX_ID_COLLISION_RETRIES):
        application_document_id = idgen_service.generate_id(
            _APPLICATION_DOCUMENT_ID_PREFIX, _APPLICATION_DOCUMENT_ID_LENGTH
        )
        try:
            return await pool.fetchrow(
                """
                INSERT INTO application_document (
                    application_document_id, mayan_document_id, application_id,
                    applicant_identifier, category, filename, customer_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                application_document_id,
                mayan_document_id,
                application_id,
                applicant_identifier,
                category,
                filename,
                customer_id,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            if exc.constraint_name == "application_document_pkey":
                continue
            raise
    raise RuntimeError(
        f"failed to generate a unique application_document_id after {_MAX_ID_COLLISION_RETRIES} attempts"
    )


async def set_application_document_provisioning(
    application_id: str, account_id: str, customer_id: str
) -> None:
    """Called by `document.service.tag_application_documents` on
    approval -- one `UPDATE` touching every `application_document` row
    for this application at once, a real simplification over today's
    per-document Mayan-metadata loop (which still runs separately, for
    the visual index tree -- see CLAUDE.md's "Document metadata
    assignment lifecycle"). Unconditional, no `WHERE ... IS NULL`
    guard -- a retry setting the same values again is harmless, same
    "idempotent by construction" reasoning `account/db.py`'s
    `set_status` already uses for the analogous case."""
    pool = await _get_pool()
    await pool.execute(
        """
        UPDATE application_document
        SET account_id = $2, customer_id = $3, updated_at = now()
        WHERE application_id = $1
        """,
        application_id,
        account_id,
        customer_id,
    )


async def get_application_documents(application_id: str) -> list[asyncpg.Record]:
    pool = await _get_pool()
    return await pool.fetch(
        "SELECT * FROM application_document WHERE application_id = $1 ORDER BY created_at",
        application_id,
    )


async def get_application_document_by_mayan_id(mayan_document_id: int) -> asyncpg.Record | None:
    """Read-only. Backs `document.service.preview`'s ownership check
    (Postgres now, replacing today's live Mayan metadata fetch)."""
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM application_document WHERE mayan_document_id = $1",
        mayan_document_id,
    )


# ---------------------------------------------------------------
# account_document / customer_document -- unique on
# (reference_id, category): a re-upload updates the existing row in
# place, matching Mayan's own document-versioning semantics for these
# categories (Welcome Letter, Consent, the customer-level Government ID
# copy). Both upserts are wrapped in the same PK-collision-retry loop
# `account/db.py`'s `create_closure_request()` already establishes --
# the `(reference_id, category)` conflict is the intended `DO UPDATE`
# path, never caught as an error; only a raw primary-key collision
# (the freshly generated id happening to match an unrelated row's) is
# caught and retried.
# ---------------------------------------------------------------


async def upsert_account_document(
    mayan_document_id: int,
    account_id: str,
    applicant_identifier: str,
    customer_id: str,
    category: str,
    filename: str,
) -> asyncpg.Record:
    """Called by `document.service.generate_welcome_letter`/
    `upload_consent` after their Mayan sequence succeeds. On a first
    upload for this `(account_id, category)` pair, inserts a new row
    under a freshly minted `account_document_id`. On a re-upload
    (`ux_account_document_account_category` fires), updates the
    existing row's `mayan_document_id`/`filename`/`updated_at` in place
    -- the existing row's own `account_document_id` is preserved,
    the freshly generated one from this call is simply discarded."""
    pool = await _get_pool()
    for _ in range(_MAX_ID_COLLISION_RETRIES):
        account_document_id = idgen_service.generate_id(_ACCOUNT_DOCUMENT_ID_PREFIX, _ACCOUNT_DOCUMENT_ID_LENGTH)
        try:
            return await pool.fetchrow(
                """
                INSERT INTO account_document (
                    account_document_id, mayan_document_id, account_id,
                    applicant_identifier, customer_id, category, filename
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (account_id, category) DO UPDATE
                SET mayan_document_id = EXCLUDED.mayan_document_id,
                    filename = EXCLUDED.filename,
                    updated_at = now()
                RETURNING *
                """,
                account_document_id,
                mayan_document_id,
                account_id,
                applicant_identifier,
                customer_id,
                category,
                filename,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            if exc.constraint_name == "account_document_pkey":
                continue
            raise
    raise RuntimeError(
        f"failed to generate a unique account_document_id after {_MAX_ID_COLLISION_RETRIES} attempts"
    )


async def get_account_documents(account_id: str) -> list[asyncpg.Record]:
    pool = await _get_pool()
    return await pool.fetch(
        "SELECT * FROM account_document WHERE account_id = $1 ORDER BY created_at",
        account_id,
    )


async def get_account_document_by_category(account_id: str, category: str) -> asyncpg.Record | None:
    """Read-only. Backs `document.service.upload_consent`'s "does a
    document already exist" check, replacing today's `_documents_matching`
    scan."""
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM account_document WHERE account_id = $1 AND category = $2",
        account_id,
        category,
    )


async def get_account_document_by_mayan_id(mayan_document_id: int) -> asyncpg.Record | None:
    """Read-only. Backs `document.service.preview_account_document`'s
    ownership check (Postgres now, replacing today's live Mayan
    metadata fetch)."""
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM account_document WHERE mayan_document_id = $1",
        mayan_document_id,
    )


async def upsert_customer_document(
    mayan_document_id: int,
    customer_id: str,
    applicant_identifier: str,
    category: str,
    filename: str,
) -> asyncpg.Record:
    """Called by `document.service.promote_government_id_to_customer_photo`
    after it trashes the customer's prior Mayan copy (if any) and
    creates a new one. Same insert-or-update-in-place shape as
    `upsert_account_document` above, keyed on `(customer_id, category)`
    instead -- this is what makes "exactly one current copy per
    customer" a real, enforced Postgres invariant rather than the
    Mayan-side trash-then-recreate sequence being the only thing
    holding that guarantee."""
    pool = await _get_pool()
    for _ in range(_MAX_ID_COLLISION_RETRIES):
        customer_document_id = idgen_service.generate_id(_CUSTOMER_DOCUMENT_ID_PREFIX, _CUSTOMER_DOCUMENT_ID_LENGTH)
        try:
            return await pool.fetchrow(
                """
                INSERT INTO customer_document (
                    customer_document_id, mayan_document_id, customer_id,
                    applicant_identifier, category, filename
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (customer_id, category) DO UPDATE
                SET mayan_document_id = EXCLUDED.mayan_document_id,
                    filename = EXCLUDED.filename,
                    updated_at = now()
                RETURNING *
                """,
                customer_document_id,
                mayan_document_id,
                customer_id,
                applicant_identifier,
                category,
                filename,
            )
        except asyncpg.exceptions.UniqueViolationError as exc:
            if exc.constraint_name == "customer_document_pkey":
                continue
            raise
    raise RuntimeError(
        f"failed to generate a unique customer_document_id after {_MAX_ID_COLLISION_RETRIES} attempts"
    )


async def get_customer_documents(customer_id: str) -> list[asyncpg.Record]:
    pool = await _get_pool()
    return await pool.fetch(
        "SELECT * FROM customer_document WHERE customer_id = $1 ORDER BY created_at",
        customer_id,
    )


async def get_customer_document_by_category(customer_id: str, category: str) -> asyncpg.Record | None:
    """Read-only. Backs `document.service.promote_government_id_to_customer_photo`'s
    "does a customer-level copy already exist" lookup, replacing
    today's `_documents_matching` scan."""
    pool = await _get_pool()
    return await pool.fetchrow(
        "SELECT * FROM customer_document WHERE customer_id = $1 AND category = $2",
        customer_id,
        category,
    )
