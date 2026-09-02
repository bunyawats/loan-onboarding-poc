# ER diagram — `loan_onboarding` database

Source of truth: [`db/schema.sql`](../../db/schema.sql). This covers
the three Postgres tables only — Mayan's own documents live in a
completely separate database (`mayan-db`, not `loan_onboarding`; see
`CLAUDE.md`'s "Data storage") and aren't part of this diagram. For how
documents associate to these entities, see `CLAUDE.md`'s "Document
hierarchy" section.

**All three relationships below are dashed deliberately** — none of
them are real foreign keys. `CLAUDE.md`'s "Data storage" explains why:
a same-database FK would make it trivially easy to write a query that
joins across module boundaries directly, which is exactly the coupling
the module split (`customer/`, `account/`, `application/` each owning
one table) exists to prevent. Every one of these ids is resolved only
through the owning module's `service.py` — never a SQL join.

```mermaid
erDiagram
    CUSTOMERS ||..o{ ACCOUNTS : "customer.service.get_or_create (approval-time only)"
    CUSTOMERS ||..o{ APPLICATIONS : "customer.service.find_by_identifier (submission) or get_or_create (approval)"
    APPLICATIONS ||..o| ACCOUNTS : "ACCOUNTS.application_id (account.service.create_account, exactly once, at terminal APPROVED)"

    CUSTOMERS {
        string customer_id PK "cus- + random 9-digit number, app-assigned via idgen"
        text applicant_identifier UK "unique; find-or-create key"
        text name
        text email
        text phone
        timestamptz created_at
    }

    ACCOUNTS {
        string account_id PK "acc- + random 9-digit number, app-assigned via idgen"
        string customer_id "opaque, NOT a FK -- not unique, one customer can hold many accounts"
        string application_id UK "opaque, NOT a FK -- NOT NULL, unique; points at the owning application (corrected from an earlier draft, which had the pointer the other way)"
        text product_type "personal_loan | auto_loan | mortgage"
        timestamptz opened_at
        text status "ACTIVE | CLOSED"
    }

    APPLICATIONS {
        string application_id PK "app- + random 9-digit number, app-assigned via idgen"
        text applicant_identifier "NOT NULL -- durable key, always known at submission"
        string customer_id "opaque, NOT a FK -- nullable"
        text workflow_id "nullable -- Temporal's id, never cleared afterward (see CLAUDE.md's Known gaps)"
        text product_type "personal_loan | auto_loan | mortgage"
        jsonb payload "product-specific fields only"
        text applicant_name
        text applicant_email
        text applicant_phone
        numeric amount "> 0"
        text status "PENDING_UNDERWRITING | MORE_INFO_REQUESTED | PENDING_MANAGER_APPROVAL | APPROVED | REJECTED | CANCELLED"
        text underwriter_name
        text underwriter_comment
        timestamptz underwriter_decided_at
        text manager_name
        text manager_comment
        timestamptz manager_decided_at
        timestamptz created_at
        timestamptz updated_at
    }
```

## Reading this diagram

- **`CUSTOMERS ||..o{ ACCOUNTS`** — one customer, zero or many
  accounts. Not one account *per* customer: an account is created
  exactly once per approved application, so a customer who's had three
  loans approved over time holds three accounts (`CLAUDE.md`'s
  "Applying without being a customer yet").
- **`CUSTOMERS ||..o{ APPLICATIONS`** — one customer, zero or many
  applications, but the link isn't always present: `customer_id` on an
  `APPLICATIONS` row is `NULL` for any applicant who isn't a recognized
  customer yet. `applicant_identifier` (not `customer_id`) is the
  column every customer-facing query actually filters on, precisely
  because it's never `NULL`.
- **`APPLICATIONS ||..o| ACCOUNTS`** — one application, zero or one
  account. Zero for every application that hasn't reached terminal
  `APPROVED` yet (the overwhelming majority at any given time); exactly
  one once it has. **The pointer lives on `ACCOUNTS.application_id`
  (`NOT NULL`, `UNIQUE`), not the other way around** — corrected from an
  earlier draft of this diagram, which had a nullable `account_id` on
  `APPLICATIONS` instead. Flipped because there was previously no way,
  given an account, to find which application produced it, and because
  the `UNIQUE` constraint on `ACCOUNTS.application_id` now doubles as
  `persist_decision`'s own idempotency guard against a Temporal retry
  double-provisioning (`CLAUDE.md`'s "Applying without being a customer
  yet"). One consequence: the "exactly one once approved" half of this
  relationship is **no longer enforced at the database level** — an
  earlier draft's `chk_approved_has_account` `CHECK` constraint on
  `applications` can't be re-expressed as a single-table check once the
  column it referenced moved to the other table; see `CLAUDE.md`'s
  Known Gaps for this as an explicit, accepted reduction in the safety
  net.
- **No relationship line for Mayan documents** — `id_photo` (customer),
  `Welcome Letter`/`Consent` (account), and the submission-gate
  categories (application) all live in Mayan, associated by metadata
  tags, not by anything a Postgres FK or this ER diagram could express.
- **`ACCOUNTS.product_type` isn't just descriptive — it's constrained.**
  A customer's `ACTIVE` accounts may never repeat a `product_type` (a
  `CLOSED` and a new `ACTIVE` `personal_loan` account can coexist, two
  simultaneously `ACTIVE` ones can't). Enforced by a partial unique
  index — `ux_accounts_customer_active_product_type` on `(customer_id,
  product_type) WHERE status = 'ACTIVE'` — not visible as a line on
  this diagram (it constrains rows within one table, not a relationship
  between two), but load-bearing: it's the actual backstop behind
  `application.service.check_decision_allowed`'s pre-approval gate (PRD
  §9.2, `CLAUDE.md`'s "Applying without being a customer yet").
