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
-- exists to prevent. Treat accounts.customer_id/application_id and
-- applications.customer_id as opaque strings resolved only through the
-- owning module's service.py -- never joined here.
--
-- Account-on-approval model (see CLAUDE.md "Applying without being a
-- customer yet"): most applicants aren't customers yet when they
-- apply, and an account is the OUTCOME of an approved loan, not a
-- precondition of filing one. So:
--   * applications.applicant_identifier is the durable, always-known
--     identity key (used for the customer-facing visibility filter).
--   * applications.customer_id is nullable -- set at submission only
--     if an existing customer is recognized.
--   * accounts.application_id (NOT NULL, UNIQUE) points at the
--     application that produced this account -- there is no
--     applications.account_id column; see CLAUDE.md's "Applying
--     without being a customer yet" for why the pointer runs this
--     direction (it also doubles as persist_decision's provisioning
--     idempotency guard).
--   * accounts.customer_id is NOT unique -- one customer can hold many
--     accounts (one per approved application over time).
--
-- Primary keys are short, human-readable, application-assigned strings
-- (`cus-`/`acc-`/`app-` + a random 9-digit number, minted by the shared
-- `idgen` module -- see CLAUDE.md's "Data storage") -- NOT
-- database-generated UUIDs. Every PRIMARY KEY column below is plain
-- TEXT with no DEFAULT; the inserting module's db.py always supplies
-- the value, retrying on a PK collision (see each module's db.py).

-- ---------------------------------------------------------------
-- customers -- owned exclusively by loan_onboarding.customer.db
-- ---------------------------------------------------------------
CREATE TABLE customers (
    customer_id             TEXT PRIMARY KEY,
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
    account_id      TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL,   -- opaque string, NOT a FK -- see header
    application_id  TEXT NOT NULL,   -- opaque string, NOT a FK -- the application that produced this account
    product_type    TEXT NOT NULL
                        CHECK (product_type IN ('personal_loan', 'auto_loan', 'mortgage')),
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE', 'CLOSURE_REQUESTED', 'CLOSED'))

    -- Closure request/decision history used to live directly on this
    -- row (Phase 18, "Account closure") -- a single current-request
    -- shape that overwrote its own history on every repeat request.
    -- Phase 22 ("Account closure request history (1:M)" -- see
    -- CLAUDE.md / IMPLEMENTATION_PLAN.md) moved it to its own table,
    -- account_closure_requests, below -- this column stays
    -- current-state only, same as it always was.
);

-- Deliberately NOT unique on customer_id alone -- a customer can hold
-- many accounts, one per approved application over time (PRD/CLAUDE.md
-- revision: an account is created BY approval, not auto-opened ahead
-- of it). Kept as a plain (non-unique) index purely for the staff-side
-- "this customer's other accounts" lookup.
CREATE INDEX ix_accounts_customer_id
    ON accounts (customer_id);

-- An account can be produced by at most one application, and this is
-- also persist_decision's idempotency guard: the INSERT into accounts,
-- once committed, IS the durable "already provisioned" marker for a
-- Temporal retry (account.service.get_by_application_id checks this
-- before provisioning again) -- see CLAUDE.md's "Applying without
-- being a customer yet".
CREATE UNIQUE INDEX ux_accounts_application_id
    ON accounts (application_id);

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
-- account_closure_requests -- owned exclusively by
-- loan_onboarding.account.db, same as accounts itself (Phase 22,
-- "Account closure request history (1:M)" -- see CLAUDE.md's "Account
-- closure" / IMPLEMENTATION_PLAN.md's Phase 22). One row per closure
-- request against an account -- a request and its eventual decision
-- are the same row, updated in place once a decision lands, not a
-- separate row. accounts.status stays current-state only
-- (ACTIVE/CLOSURE_REQUESTED/CLOSED); this table is where the history
-- across repeat requests (reject/cancel, then request again) lives.
-- ---------------------------------------------------------------
CREATE TABLE account_closure_requests (
    closure_request_id  TEXT PRIMARY KEY,
    account_id           TEXT NOT NULL,   -- opaque string, NOT a FK -- see accounts' own header
    workflow_id          TEXT NOT NULL,

    -- Temporal run id for the specific CloseAccountWorkflow execution
    -- that handled this request. workflow_id ALONE is not enough to
    -- disambiguate a repeat request against the same account --
    -- workflow.service._workflow_id_for_account_closure deterministically
    -- reuses the same workflow_id (`account-closure-<account_id>`) for
    -- every request against that account, relying on Temporal's default
    -- AllowDuplicate reuse policy (safe because a new request is only
    -- reachable once the prior execution has already reached a terminal
    -- state -- see account.service.request_closure's own docstring).
    -- Nullable until the code that captures it is written (P22-2/P22-3).
    workflow_run_id      TEXT,

    requested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    status               TEXT NOT NULL DEFAULT 'PENDING'
                              CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),

    -- Staff attestation text (e.g. "balance confirmed zero") -- this POC
    -- has no ledger, so this is a manual confirmation, not a computed
    -- check. NULL for a customer self-cancel (no staff decision made).
    decision_comment     TEXT,
    -- Authenticated Keycloak preferred_username of the deciding staff
    -- member (Underwriter or Manager, either role may decide -- no
    -- escalation tier for closure) -- never client-submitted free text,
    -- same discipline as applications.underwriter_name/manager_name.
    -- NULL for a customer self-cancel.
    decided_by           TEXT,
    decided_at           TIMESTAMPTZ
);

-- Per-account closure history view, newest first -- backs a
-- customer/staff-facing "this account's closure history" screen
-- (Phase 22, P22-4).
CREATE INDEX ix_closure_requests_account_id_requested_at
    ON account_closure_requests (account_id, requested_at DESC);

-- The actual business rule this table enforces: an account may have at
-- most one PENDING closure request outstanding at a time. A partial
-- unique index is the authoritative, final enforcement of this -- same
-- "a pre-check in service.py handles the normal path, this index is
-- the last-resort backstop under a race" split
-- ux_accounts_customer_active_product_type (above) already establishes
-- for a different rule.
CREATE UNIQUE INDEX ux_closure_requests_account_pending
    ON account_closure_requests (account_id)
    WHERE status = 'PENDING';

-- Backs the staff-side closure-request queue
-- (account.service.list_pending_closure_requests), ordered the same
-- FIFO way that queue has always read it.
CREATE INDEX ix_closure_requests_pending_requested_at
    ON account_closure_requests (requested_at)
    WHERE status = 'PENDING';

-- ---------------------------------------------------------------
-- applications -- owned exclusively by loan_onboarding.application.db.
-- The REAL loan application (Phase 23, "Split loan application request
-- data (1:1)" -- see CLAUDE.md / IMPLEMENTATION_PLAN.md): what was
-- requested, by whom, for how much. Onboarding-workflow tracking
-- (Temporal linkage, staff decisions, risk tier) lives in
-- loan_apply_requests below, 1:1 via application_id -- NOT folded back
-- in here, even though the two are read together via a LEFT JOIN on
-- almost every path. See that table's own header for why the split
-- runs this way and why `status` is the one column deliberately kept
-- on BOTH tables.
-- ---------------------------------------------------------------
CREATE TABLE applications (
    application_id          TEXT PRIMARY KEY,

    -- The durable identity key -- always known at submission time,
    -- whether or not the applicant is a recognized customer yet. This
    -- is what the customer-facing visibility filter
    -- (list_for_applicant) is keyed on, NOT customer_id -- it has to
    -- work identically for a first-time applicant and a returning one.
    -- Confirmed staying on THIS table (Phase 23) -- it's who's
    -- applying, not onboarding-workflow machinery.
    applicant_identifier      TEXT NOT NULL,

    -- Opaque reference, NOT a FK -- see header. Nullable: set at
    -- submission IF an existing customer is recognized via
    -- customer.service.find_by_identifier; otherwise NULL until (and
    -- unless) this application is later approved. Resolved only via
    -- customer.service.get() when a name/detail is needed -- never
    -- joined here. There is no account_id column here -- see
    -- accounts.application_id above for why the pointer runs the other
    -- direction. Confirmed staying on THIS table (Phase 23), same
    -- reasoning as applicant_identifier -- resolved DURING the
    -- workflow, but it's "who this loan is for," not tracking data.
    customer_id               TEXT,

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

    -- DELIBERATELY DUPLICATED on loan_apply_requests too (Phase 23) --
    -- this is NOT the redundancy-to-eliminate an earlier draft of this
    -- schema assumed; it's a resilience feature, same "denormalize on
    -- purpose, source of truth stays resolvable elsewhere" philosophy
    -- CLAUDE.md's "Denormalized applicant fields, on purpose" already
    -- documents for applicant_name/email/phone above. This column is
    -- what lets `applications` alone answer "what happened to this
    -- loan" even if loan_apply_requests is ever truncated --
    -- application/db.py's own write functions keep the two in sync on
    -- every transition (same transaction, never one without the
    -- other). loan_apply_requests.status is the day-to-day operational
    -- source of truth; this copy is the durable one.
    --
    -- DEFAULT 'PENDING_UNDERWRITING' is now unreachable in practice --
    -- Phase 21's application/db.py's insert() always passes an explicit
    -- status (PENDING_RISK_ASSESSMENT, workflows.py's own new initial
    -- state -- see CLAUDE.md's "Automated risk assessment via NATS" /
    -- the risk-assessment-nats skill). Left in place rather than
    -- dropped -- removing a column DEFAULT is a bigger, non-additive
    -- change out of proportion to this task.
    status                     TEXT NOT NULL DEFAULT 'PENDING_UNDERWRITING'
                                   CHECK (status IN (
                                       'PENDING_RISK_ASSESSMENT',
                                       'PENDING_UNDERWRITING',
                                       'MORE_INFO_REQUESTED',
                                       'PENDING_MANAGER_APPROVAL',
                                       'APPROVED',
                                       'REJECTED',
                                       'CANCELLED'
                                   )),

    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()

    -- No chk_approved_has_account CHECK anymore -- that invariant
    -- ("an APPROVED application has a matching account") now spans two
    -- tables (applications.status, accounts.application_id) once the
    -- account pointer moved to accounts, and can no longer be expressed
    -- as a single-table CHECK. This is a real, accepted reduction in
    -- the DB-level safety net -- see CLAUDE.md's "Known gaps".
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
-- Reads this column alone now (Phase 23) -- no join needed just to
-- filter/count by status, since the mirrored copy above already lives
-- here; a real query-cost win the mirrored column earns beyond just
-- truncation-survival.
CREATE INDEX ix_applications_status_created_at
    ON applications (status, created_at DESC);

-- ---------------------------------------------------------------
-- loan_apply_requests -- owned exclusively by
-- loan_onboarding.application.db, same as applications itself (Phase
-- 23, "Split loan application request data (1:1)" -- see CLAUDE.md /
-- IMPLEMENTATION_PLAN.md). Onboarding-workflow TRACKING data for one
-- application -- genuinely 1:1 (an application is submitted once; a
-- resubmission overwrites this row's status/decision fields in place,
-- same as it always has, no history added here) -- unlike
-- account_closure_requests' own 1:M shape, so application_id itself is
-- this table's PK, no separate id needed.
-- ---------------------------------------------------------------
CREATE TABLE loan_apply_requests (
    application_id  TEXT PRIMARY KEY,   -- opaque string, NOT a FK -- see applications' own header

    -- Nullable: unset until persist_application (the workflow's first
    -- activity) commits. **Never cleared afterward by any code in this
    -- codebase** -- corrected in P12-1 from an earlier draft of this
    -- comment, which claimed it gets cleared "if a Temporal admin
    -- deletes the execution out from under a row"; no such
    -- reconciliation job was ever built (confirmed by grepping for any
    -- write to this column outside persist_application -- there is
    -- none), so a terminated workflow or a deleted execution leaves
    -- this value pointing at a Temporal execution that no longer
    -- exists, permanently. See CLAUDE.md's "Known gaps".
    workflow_id     TEXT,

    -- The day-to-day operational source of truth -- applications.status
    -- (above) is kept in sync with this column on every write, not the
    -- other way around. Same CHECK enum, deliberately copied verbatim
    -- rather than left to drift.
    status          TEXT NOT NULL DEFAULT 'PENDING_UNDERWRITING'
                        CHECK (status IN (
                            'PENDING_RISK_ASSESSMENT',
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
    underwriter_name        TEXT,
    underwriter_comment     TEXT,
    underwriter_decided_at  TIMESTAMPTZ,

    manager_name            TEXT,
    manager_comment         TEXT,
    manager_decided_at      TIMESTAMPTZ,

    -- Written only by a risk-driven persist_decision (Phase 21, "Automated
    -- risk assessment via NATS" -- see CLAUDE.md / the risk-assessment-nats
    -- skill), never by a human decision, which leaves this NULL forever.
    -- NULL also covers every application that predates this column and
    -- every application still awaiting a decision of any kind.
    risk_tier               TEXT
                                CHECK (risk_tier IN ('LOW', 'MEDIUM', 'HIGH')),

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- signal_decision/signal_resubmit resolve workflow_id -> application_id
-- (e.g. to double check state after a signal) via this index; also
-- lets a reconciliation job find rows by workflow_id directly. Moved
-- here from applications (Phase 23) -- workflow_id no longer lives
-- there.
CREATE INDEX ix_loan_apply_requests_workflow_id
    ON loan_apply_requests (workflow_id)
    WHERE workflow_id IS NOT NULL;

-- ---------------------------------------------------------------
-- application_document / account_document / customer_document --
-- owned exclusively by loan_onboarding.document.db (Phase 24,
-- "Document metadata persistence in Postgres" -- see CLAUDE.md /
-- IMPLEMENTATION_PLAN.md). document/ previously had NO Postgres
-- persistence at all -- Mayan was the only record of what documents
-- exist, so a Mayan outage meant this app couldn't even list them.
-- These three tables are now the PRIMARY source of truth for "what
-- documents exist" (not a fallback cache) -- every document/service.py
-- read function queries these directly; Mayan stays the system of
-- record only for actual file bytes and the visual Index Template
-- tree staff browse. mayan_document_id is Mayan's own plain integer
-- document id (NOT a real UUID -- nothing in this codebase has ever
-- captured Mayan's actual uuid field; every existing caller, including
-- every preview route, is already built around this integer id, so
-- that's what these tables key on too). Same "no FKs anywhere, opaque
-- string reference, app-minted idgen primary key" discipline every
-- other table in this schema follows.
--
-- Write ordering (document/service.py's own discipline, not enforced
-- by anything in this schema): every write calls Mayan first, then
-- writes the Postgres mirror here -- never the reverse. A Postgres
-- write failing after Mayan succeeds leaves a real document "hidden"
-- (invisible to every read until reconciled) rather than Postgres
-- claiming a file that doesn't exist -- the safer of the two failure
-- modes, and a genuinely new dual-write consistency risk this phase
-- accepts rather than closes (see CLAUDE.md's Known Gaps).
-- ---------------------------------------------------------------

-- application_document -- documents uploaded during the application
-- flow (Government ID, Proof of Income, Bank Statements, Credit
-- Report, product-specific categories). No uniqueness beyond
-- mayan_document_id -- a category is satisfied by one or more
-- documents, unlimited accumulation is intentional (matches today's
-- multi-Bank-Statement behavior). account_id/customer_id start NULL
-- and are set in bulk by tag_application_documents on approval --
-- mirrors exactly what that function already tags onto Mayan's own
-- metadata today, just now also written here.
CREATE TABLE application_document (
    application_document_id  TEXT PRIMARY KEY,
    mayan_document_id        INTEGER NOT NULL,
    application_id           TEXT NOT NULL,   -- opaque string, NOT a FK -- see header
    applicant_identifier     TEXT NOT NULL,
    category                 TEXT NOT NULL,
    filename                 TEXT NOT NULL,
    account_id               TEXT,   -- opaque, NOT a FK -- set on approval
    customer_id              TEXT,   -- opaque, NOT a FK -- set on approval
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX ux_application_document_mayan_document_id
    ON application_document (mayan_document_id);

-- Backs list_documents/check_completeness -- every read this phase
-- adds filters on application_id first.
CREATE INDEX ix_application_document_application_id
    ON application_document (application_id);

-- account_document -- Welcome Letter (system-generated, exactly once
-- per account) and Consent (customer/staff-uploaded, re-uploadable).
-- Unique on (account_id, category) -- a re-upload updates the existing
-- row in place (Mayan's own document-versioning semantics for these
-- two categories already work this way; this table just makes the
-- "at most one current copy" rule explicit and enforced here too).
CREATE TABLE account_document (
    account_document_id   TEXT PRIMARY KEY,
    mayan_document_id     INTEGER NOT NULL,
    account_id            TEXT NOT NULL,   -- opaque string, NOT a FK -- see header
    applicant_identifier  TEXT NOT NULL,
    customer_id           TEXT NOT NULL,
    category              TEXT NOT NULL,   -- 'Welcome Letter' | 'Consent'
    filename              TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX ux_account_document_mayan_document_id
    ON account_document (mayan_document_id);

-- The actual business rule this table enforces: at most one current
-- document per (account, category). document/db.py's upsert functions
-- rely on this via INSERT ... ON CONFLICT (account_id, category) DO
-- UPDATE -- same "index is the authoritative backstop" pattern this
-- schema already uses elsewhere (e.g. ux_closure_requests_account_pending).
CREATE UNIQUE INDEX ux_account_document_account_category
    ON account_document (account_id, category);

-- customer_document -- the customer-level Government ID copy
-- (promote_government_id_to_customer_photo's genuine second Mayan
-- document, not a re-tagged original -- see CLAUDE.md's "Document
-- metadata assignment lifecycle"). Unique on (customer_id, category),
-- same update-in-place reasoning as account_document above -- this is
-- what makes has_id_photo/list_customer_documents a plain, unambiguous
-- table read instead of the old "carries customer_id but neither
-- application_id nor account_id" heuristic.
CREATE TABLE customer_document (
    customer_document_id  TEXT PRIMARY KEY,
    mayan_document_id     INTEGER NOT NULL,
    customer_id           TEXT NOT NULL,   -- opaque string, NOT a FK -- see header
    applicant_identifier  TEXT NOT NULL,
    category              TEXT NOT NULL,   -- always 'Government ID' today, kept general
    filename              TEXT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX ux_customer_document_mayan_document_id
    ON customer_document (mayan_document_id);

CREATE UNIQUE INDEX ux_customer_document_customer_category
    ON customer_document (customer_id, category);
