-- loan_onboarding database schema
--
-- Applied to the `loan_onboarding` database only (see db/init/*.sh,
-- which also creates the separate `temporal` database in the same
-- Postgres container -- Temporal manages its own schema there, this
-- file never touches it).
--
-- Three tables, one per owning module (customer/, account/,
-- application/ -- see CLAUDE.md "Data storage"). Deliberately NO
-- foreign keys between them, even though they live in one database:
-- a same-database FK would make it trivial to join across module
-- boundaries directly, which is exactly the coupling the module split
-- exists to prevent. Treat accounts.customer_id and
-- applications.customer_id/account_id as opaque strings resolved only
-- through the owning module's service.py -- never joined here.
--
-- Account-on-approval model (see CLAUDE.md "Applying without being a
-- customer yet"): most applicants aren't customers yet when they
-- apply, and an account is the OUTCOME of an approved loan, not a
-- precondition of filing one. So:
--   * applications.applicant_identifier is the durable, always-known
--     identity key (used for the customer-facing visibility filter).
--   * applications.customer_id/account_id are both nullable -- set at
--     submission only if an existing customer is recognized
--     (customer_id), and only ever set for account_id once the
--     application reaches terminal APPROVED.
--   * accounts.customer_id is NOT unique -- one customer can hold many
--     accounts (one per approved application over time).

-- ---------------------------------------------------------------
-- customers -- owned exclusively by loan_onboarding.customer.db
-- ---------------------------------------------------------------
CREATE TABLE customers (
    customer_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    applicant_identifier    TEXT NOT NULL,
    name                    TEXT,
    email                   TEXT,
    phone                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- get_or_create() (called only from application/activities.py on
-- approval, see CLAUDE.md) is a find-or-create keyed on this value --
-- must be unique so "find" is unambiguous, and so two applications
-- from the same brand-new applicant approved close together don't
-- race into two customer rows.
CREATE UNIQUE INDEX ix_customers_applicant_identifier
    ON customers (applicant_identifier);

-- ---------------------------------------------------------------
-- accounts -- owned exclusively by loan_onboarding.account.db
-- ---------------------------------------------------------------
CREATE TABLE accounts (
    account_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     UUID NOT NULL,   -- opaque string, NOT a FK -- see header
    product_type    TEXT NOT NULL
                        CHECK (product_type IN ('personal_loan', 'auto_loan', 'mortgage')),
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE', 'CLOSED'))
);

-- Deliberately NOT unique on customer_id alone -- a customer can hold
-- many accounts, one per approved application over time (PRD/CLAUDE.md
-- revision: an account is created BY approval, not auto-opened ahead
-- of it). Kept as a plain (non-unique) index purely for the staff-side
-- "this customer's other accounts" lookup.
CREATE INDEX ix_accounts_customer_id
    ON accounts (customer_id);

-- The actual business rule: a customer's ACTIVE accounts must never
-- share a product_type (two closed personal_loan accounts are fine;
-- two simultaneously ACTIVE ones are not). A partial unique index is
-- the authoritative, final enforcement of this -- account.service's
-- has_active_account_of_type() pre-check (CLAUDE.md's "Applying
-- without being a customer yet") is what gives a clean error instead
-- of a raw constraint violation in the normal path, but this index is
-- what actually guarantees the invariant even under a race.
CREATE UNIQUE INDEX ux_accounts_customer_active_product_type
    ON accounts (customer_id, product_type)
    WHERE status = 'ACTIVE';

-- ---------------------------------------------------------------
-- applications -- owned exclusively by loan_onboarding.application.db
-- ---------------------------------------------------------------
CREATE TABLE applications (
    application_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- The durable identity key -- always known at submission time,
    -- whether or not the applicant is a recognized customer yet. This
    -- is what the customer-facing visibility filter
    -- (list_for_applicant) is keyed on, NOT customer_id -- it has to
    -- work identically for a first-time applicant and a returning one.
    applicant_identifier      TEXT NOT NULL,

    -- Opaque references, NOT FKs -- see header. Both nullable:
    --   customer_id  -- set at submission IF an existing customer is
    --                   recognized via customer.service.find_by_identifier;
    --                   otherwise NULL until (and unless) this
    --                   application is later approved.
    --   account_id   -- NULL for the entire non-terminal lifetime of
    --                   an application; set exactly once, when
    --                   persist_decision provisions a new account on
    --                   a terminal APPROVED transition.
    -- Resolved only via customer.service.get()/account.service.get()
    -- when a name/detail is needed -- never joined here.
    customer_id               UUID,
    account_id                UUID,

    -- Nullable: unset until persist_application (the workflow's first
    -- activity) commits; cleared if a Temporal admin deletes the
    -- execution out from under a row (see CLAUDE.md's reconciliation note).
    workflow_id               TEXT,

    product_type              TEXT NOT NULL
                                   CHECK (product_type IN ('personal_loan', 'auto_loan', 'mortgage')),
    payload                   JSONB NOT NULL,

    -- Denormalized as-submitted identity + amount -- see CLAUDE.md
    -- "Denormalized applicant fields, on purpose". amount is a top-level
    -- column (not inside payload) because workflow/service.start_workflow
    -- takes it as a named argument for the escalation-threshold check
    -- (PRD §6.3) without the workflow inspecting payload.
    applicant_name             TEXT NOT NULL,
    applicant_email            TEXT NOT NULL,
    applicant_phone            TEXT NOT NULL,
    amount                     NUMERIC(14, 2) NOT NULL CHECK (amount > 0),

    status                     TEXT NOT NULL DEFAULT 'PENDING_UNDERWRITING'
                                   CHECK (status IN (
                                       'PENDING_UNDERWRITING',
                                       'MORE_INFO_REQUESTED',
                                       'PENDING_MANAGER_APPROVAL',
                                       'APPROVED',
                                       'REJECTED',
                                       'CANCELLED'
                                   )),

    -- underwriter_name/manager_name are authenticated Keycloak usernames
    -- (preferred_username), never client-submitted free text -- see
    -- CLAUDE.md "Identity".
    underwriter_name           TEXT,
    underwriter_comment        TEXT,
    underwriter_decided_at     TIMESTAMPTZ,

    manager_name               TEXT,
    manager_comment            TEXT,
    manager_decided_at         TIMESTAMPTZ,

    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- account_id must be set once (and only once) an application
    -- reaches terminal APPROVED -- catches a persist_decision bug
    -- (e.g. the idempotency check in CLAUDE.md's provisioning section
    -- being skipped) at the database level rather than silently.
    CONSTRAINT chk_approved_has_account
        CHECK (status <> 'APPROVED' OR account_id IS NOT NULL)
);

-- list_for_applicant(applicant_identifier, page, ...) -- the
-- customer-facing "My Applications" screen; keyed on
-- applicant_identifier, NOT customer_id, since customer_id may still
-- be NULL for an applicant with no approved application yet.
CREATE INDEX ix_applications_applicant_identifier_created_at
    ON applications (applicant_identifier, created_at DESC);

-- Staff-side "this customer's other applications" lookup once
-- customer_id is resolved; partial since it's NULL for most rows
-- pre-approval.
CREATE INDEX ix_applications_customer_id_created_at
    ON applications (customer_id, created_at DESC)
    WHERE customer_id IS NOT NULL;

-- list_by_status(status, page, ...) -- Underwriter/Manager queues.
CREATE INDEX ix_applications_status_created_at
    ON applications (status, created_at DESC);

-- signal_decision/signal_resubmit resolve workflow_id -> application_id
-- (e.g. to double check state after a signal) via this index; also
-- lets a reconciliation job find rows by workflow_id directly.
CREATE INDEX ix_applications_workflow_id
    ON applications (workflow_id)
    WHERE workflow_id IS NOT NULL;
