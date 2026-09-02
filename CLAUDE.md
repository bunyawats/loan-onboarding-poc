# CLAUDE.md

This file provides guidance to Claude Code (or any implementer) when
building out this repository. It is written *before* implementation
(planning stage) — unlike the two reference projects' own `CLAUDE.md`
files, which document gotchas already hit, this one documents intended
architecture and explicitly says which reference project each decision
is borrowed from. Update it as real gotchas get found, following the
style of those two files.

Read [`PRD.md`](PRD.md) first — it has the product requirements this
file assumes. This file is the *how*, not the *what*.

## What this is

A loan onboarding POC, built as a **modular monolith**: one deployable
Python codebase, one Docker image, organized into **seven strictly
bounded modules** rather than either a tangled single app or seven
separately-deployed microservices:

| # | Module | Package | Type |
|---|---|---|---|
| 1 | Customer BFF | `bff_customer/` | public-facing HTMX (mobile-first) |
| 2 | Back-Office BFF (LOS) | `bff_backoffice/` | internal-facing HTMX, Keycloak-gated |
| 3 | Customer | `customer/` | domain module — profile |
| 4 | Account | `account/` | domain module — banking relationship |
| 5 | Application | `application/` | domain module — the loan application entity + its submission rule |
| 6 | Document | `document/` | Mayan EDMS integration |
| 7 | Workflow | `workflow/` | Temporal integration |

**This was a deliberate revision, not the first draft.** An earlier pass
built these seven as genuine microservices (separate processes, REST
calls, one Postgres database per service, a shared internal API key).
That's a legitimate architecture, but it's more operational weight than
this POC needs, and a modular monolith gets nearly all the same
discipline — one owner per concern, no reaching into another module's
data, an enforced one-way dependency graph — without the network hops,
the multi-container deploy, or the cross-process schema-drift risk the
microservices version had to work around with a dedicated contract-test
suite. Both reference projects (`review-approval-temporal`,
`mayan-edms-customer-archive`) are themselves modular monoliths, not
microservices — this reverts to being a direct structural descendant of
them, the way it started, rather than a departure from their pattern.

**What "modular" has to actually mean for this to be worth doing** (not
just seven folders): each module owns its own data and is the only code
that touches it; modules talk to each other only through an explicit
`service.py` entry point (an in-process function call, not a shared
table, not reaching into another module's models); the dependency graph
between modules is a DAG, enforced by which packages are allowed to
import which — never inferred, never "just don't do it," actually
checked by import-linter or an equivalent tool in CI once this exists.
Import discipline is the *entire* enforcement mechanism now that there's
no network boundary forcing it — see "Enforcing the boundaries" below.

## Module dependency graph

*(Rendered version: [`docs/diagrams/application-modules.md`](docs/diagrams/application-modules.md).)*

```
                         customer/    account/    document/    workflow/
                         (leaf)       (leaf)      (leaf --      (leaf --
                                                    Mayan only)   generic Temporal
                                                                  orchestration only,
                                                                  no domain knowledge)
                              ╲           ╲             ╲            │
                               ╲           ╲             ╲           │  (schema registry
                                ╲           ╲             ╲          │   assert checks
                                 ╲           ╲             ╲         │   against this)
                                  ╲           ╲             ▼        ▼
                                   ╲           ╲       ┌───────────────┐
                                    ╲           ╲      │  application/  │
                                     ╲           ╲     └───────────────┘
                                      ╲           ╲            │
                    (application/service.py: READ-ONLY calls into  │
                     customer/ and account/ only (find_by_identifier,│
                     has_active_account_of_type, get). Writes       │
                     (get_or_create, create_account) happen only    │
                     in application/activities.py, on approval --   │
                     see "Applying without being a customer yet")   │
                                                                 │
                        ┌────────────────────────────────────────┘
                        ▼
              bff_customer/          bff_backoffice/
              (imports customer/     (imports application/,
               [read-only find_by_    document/, workflow/,
               identifier only],      customer/, account/ --
               application/,          for rendering detail,
               document/, workflow/;  possibly None pre-approval)
               no account/ import --
               nothing to find-or-
               create there anymore)
                        │                      │
                        └──────────┬───────────┘
                                   ▼
                          app.py / worker_main.py
                        (composition roots -- see
                         "Breaking the application ↔
                         workflow cycle" below)
```

**Not drawn above**: `application/activities.py` (not `service.py`)
also has a downward edge to `customer/` and `account/`, used only to
provision the account an approval produces — see "Applying without
being a customer yet" below. Left off the diagram to keep it readable;
the rule is stated explicitly in the bullet list below instead.

**Also not drawn**: `idgen/`, a fifth leaf module (`customer/`,
`account/`, `document/`, `workflow/`'s siblings) that every other
module imports for `generate_id(prefix, length) -> str` — the
human-readable primary keys described in "Data storage" below. Omitted
from the diagram because it fans out to literally everything (every
arrow above would grow a second, parallel arrow to `idgen/`), not
because the edge is unusual; it's the plainest possible leaf
dependency, a pure function with zero I/O and zero state.

Rules, in order of how often a shortcut will tempt someone to break
them:

- **`customer/` and `account/` never import anything else in this
  codebase, with one exception: `idgen/`, for primary-key generation.**
  Corrected from an earlier draft of this rule, written before either
  module needed to generate its own id (Postgres did it via a `DEFAULT`
  — see "Data storage"). `idgen/` is deliberately minimal enough (no
  I/O, no other module's types, see next bullet) that depending on it
  doesn't compromise the "pure data module" claim this rule otherwise
  makes about these two — they still have zero business logic reaching
  outside themselves.
- **`idgen/` never imports anything else in this codebase either** —
  the plainest leaf in the graph, one pure function
  (`generate_id(prefix, length) -> str`, `secrets.choice` over the
  digit alphabet, no DB, no other module's types). Every module that
  assigns a primary key (`customer/`, `account/`, `application/`)
  imports it; it imports nothing back.
- **`document/` never imports `application/` or `workflow/`.** It knows
  nothing about loan applications or Temporal — just Mayan, categories,
  and completeness checks against a category list it's handed.
- **`workflow/` never imports `application/`, `document/`, `customer/`,
  or `account/`.** It is generic Temporal orchestration: a workflow
  class, a worker bootstrap that takes an activities list as a
  parameter, task-queue naming keyed by a product-type string. It has
  no idea what "application" data looks like — see "Breaking the cycle"
  below for how its activities still end up writing application data
  without `workflow/` importing `application/` to do it.
- **`application/` imports `document/` and `workflow/`, never the
  reverse.** It calls `document.service.check_completeness(...)`
  directly (an in-process function call — no HTTP, no serialization
  boundary beyond normal Python objects) and
  `workflow.service.start_workflow(...)`/`signal_decision(...)`/
  `signal_resubmit(...)`.
- **`application/service.py` may only call `customer/` and `account/`'s
  read-only functions — never their writes.** Corrected from an earlier
  draft of this file, which claimed `service.py` "never imports
  `customer/` or `account/`" at all; that was already false the moment
  `create_application()` started calling
  `customer.service.find_by_identifier(...)` (a read-only lookup — see
  "Applying without being a customer yet" below), and it's more false
  now that `check_decision_allowed()` (§ below, PRD's active-account
  rule) also calls `account.service.has_active_account_of_type(...)`.
  The actual, consistent rule: `service.py` may read (`find_by_identifier`,
  `get`, `has_active_account_of_type`), never write (`get_or_create`,
  `create_account`). **`application/activities.py` is where every write
  happens** — because approval is what creates the banking relationship
  now (see "Applying without being a customer yet") — `persist_decision`
  has to call `customer.service.get_or_create(...)` and
  `account.service.create_account(...)` when a decision resolves to
  terminal `APPROVED`. Both files' imports are normal downward
  dependencies, not a cycle (`customer/`/`account/` don't import
  anything back); `service.py`'s remaining read paths (`get`,
  `list_for_applicant`, `list_by_status`) still don't need either
  module at all, using the denormalized applicant fields instead, same
  as always.
- **`bff_customer/` and `bff_backoffice/` may import any domain module**
  (`customer/`, `account/`, `application/`, `document/`, `workflow/`),
  never each other, and never get imported back by anything below them.
- **`app.py` and `worker_main.py` are the only files that import from
  every module** — same "one composition root" principle
  `review-approval-temporal`'s `app.py` already follows, just now
  covering two entrypoints instead of one (see next section for why).

### Breaking the application ↔ workflow cycle

There's a real two-way relationship here, not a hypothetical one:
`application/` needs to **call** `workflow/` to start a Temporal
workflow when a loan application is submitted, but `workflow/`'s
activities need to **write** application data (status, decision
columns) when a signal resolves. If both directions were literal
package imports, that's a cycle — Python doesn't allow it cleanly, and
even if it did, it would mean neither module's boundary actually means
anything.

The fix is the classic modular-monolith answer: **the module that would
otherwise need the back-reference doesn't own the concrete
implementation — it defines the shape, and something above it wires
the real implementation in.**

- `workflow/activities.py` doesn't exist as a set of concrete
  `@activity.defn` functions inside the `workflow/` package. Instead,
  **`workflow/workflows.py` calls activities by string name**
  (`workflow.execute_activity("persist_application", args, ...)`, not
  by importing a function reference) — this is what actually removes
  the need for `workflow/` to import `application/` at all, even at the
  type level. `workflow/worker.py`'s bootstrap function separately takes
  a **list of concrete activity callables to register** as a runtime
  parameter (needed so the `Worker` has something to run when a
  matching name comes in) — `workflow/` still never imports
  `application/` to get that list; `worker_main.py` supplies it. Because
  activities are referenced by name rather than import, `workflows.py`
  can be built and unit-tested (via `WorkflowEnvironment`, with small
  fake activities registered under the same string names) **before**
  `application/activities.py`'s real implementations exist — the two
  don't have to be built in a fixed order relative to each other, only
  wired together for real once both exist, inside `worker_main.py`.
- The actual activity implementations — the code that writes to the
  `applications` table — live in **`application/activities.py`**,
  inside the module that owns that data. `application/` imports
  `workflow/` (for the `@activity.defn` decorator, the task-queue
  naming, and whatever base types the signatures need), which is a
  perfectly normal downward dependency, not a cycle.
- **`worker_main.py`** (a composition root, not part of either module)
  imports both `workflow/` (to build the `Worker`) and
  `application/activities.py` (to supply the concrete activity list),
  and wires them together at process startup. This is exactly the same
  role `app.py` plays for the two BFFs — "the one file allowed to know
  about everything" — just for the worker process instead of the web
  process.

This gets you the same outcome the reference project achieved by
brute-force co-location (its `workflow/activities.py` could touch
Postgres directly because there was only one domain in that whole
app), but with a real module boundary between "the application entity"
and "Temporal orchestration mechanics" now that they're two named
modules per this project's requirements. **A genuine, and pleasant,
side effect of reverting to a modular monolith**: `application/`'s
payload-schema registry can go back to checking itself against
`workflow/`'s `KNOWN_PRODUCT_TYPES` with a plain `assert` at import
time — the same trick `review-approval-temporal` uses, which the
microservices version of this plan had to replace with a whole
`tests/contract/` suite because the two registries lived in separate
processes. One process again means one import-time check is enough.

### Applying without being a customer yet

The original design had `bff_customer` eagerly create a `customer` row
(and an `account` under it) the moment someone typed an identifier and
started an application — modeling a bank's *existing* customer applying
for another product. The actual product intent is closer to real loan
origination: **most applicants aren't customers yet, and an account is
the *outcome* of an approved loan, not something that pre-exists it.**

- **`applications.applicant_identifier`** (new column, `NOT NULL`) is
  the durable key for "which human is this," always known at
  submission regardless of whether they're a recognized customer — the
  same value `bff_customer`'s session cookie already holds (PRD §7.1).
  This is what the customer-facing visibility filter (PRD §10 success
  criterion 2) is keyed on now, **not** `customer_id` — it has to work
  identically for a first-time applicant (no `customer_id` yet) and a
  returning one.
- **`applications.customer_id` is nullable.** `application.service.create_application(...)`
  resolves it via a **read-only** lookup —
  `customer.service.find_by_identifier(applicant_identifier) ->
  Customer | None` — never a create. If the identifier matches an
  existing customer, the application is linked immediately; if not,
  `customer_id` stays `NULL` until (and unless) the application is
  approved.
- **`accounts.application_id` is `NOT NULL` and `UNIQUE`** — the
  account points at the application that produced it, not the other
  way around. **Corrected from an earlier draft of this file**, which
  had `applications.account_id` (nullable, set once at approval)
  instead; flipped because (a) there was previously no way, given an
  account, to find which application produced it, and (b) the reversed
  direction lets the `UNIQUE` constraint on `accounts.application_id`
  serve as `persist_decision`'s idempotency guard directly (see step 2
  below), instead of a separately-written, easy-to-get-wrong nullable
  column on `applications`. There is still no "auto-opened account" —
  `account.service.find_or_create_for_customer(...)` is gone, and
  `account/` still doesn't enforce one-account-per-customer (see
  `account/`'s module section).
- **Provisioning happens inside `application/activities.py`'s
  `persist_decision`, only on the transition to terminal `APPROVED`**
  (either the Underwriter's below-threshold approve, or the Manager's
  approve after escalation — *not* the intermediate
  `PENDING_MANAGER_APPROVAL` step, which isn't a terminal approval).
  **Idempotency check first, before anything else**: call
  `account.service.get_by_application_id(application_id)`. A non-`None`
  result means this activity execution is a Temporal retry of an
  already-provisioned application (successful-but-unacknowledged, or a
  genuine partial failure partway through a prior attempt) — skip
  straight to the final decision-column write below, using
  `existing_account.customer_id` in place of step 1. Otherwise:
  1. If `applications.customer_id` is still `NULL`, call
     `customer.service.get_or_create(applicant_identifier) ->
     Customer` (idempotent find-or-create — this is the *only* caller
     of this function left; `bff_customer`'s identify step no longer
     calls it, see `customer/`'s module section below).
  2. Call `account.service.create_account(customer_id, product_type,
     application_id) -> Account` — **always creates a new row**, no
     find-or-create semantics, since accounts are 1:1 with approved
     applications now, not 1:1 with customers (a customer can hold
     many accounts, one per approved loan — a plain, non-unique index
     on `accounts.customer_id`). `product_type` is a required column
     too — see the active-account rule immediately below. **This
     INSERT, once committed, is itself the durable idempotency
     marker** — no separate write back onto `applications` is needed
     the way the old `account_id`-on-`applications` design required
     (that write's entire reason to exist was giving a retry something
     to check; `accounts.application_id`'s own `UNIQUE` constraint does
     that job now, one step earlier and with nothing to get out of
     order).
  3. Call `document.service.promote_government_id_to_customer_photo(application_id,
     customer_id)` and `document.service.generate_welcome_letter(account_id,
     customer_id, applicant_name, product_type, amount)` — see
     `document/`'s module section for what each does. **A retry that
     finds an account already provisioned (the check above) skips both
     of these calls entirely, permanently** — a smaller,
     manually-recoverable gap (a missing Welcome Letter) than a
     duplicated account, and consistent with this project's existing
     rare-enough-to-accept-for-a-POC stance elsewhere in this section.
     (This is the same tradeoff an earlier draft of this file already
     accepted; only the mechanism that makes the retry recognize
     "already provisioned" has moved, from a column on `applications`
     to the `accounts` row itself.)
  4. Write `status` and the underwriter/manager decision columns on
     `applications` — `customer_id` travels along in this same
     `UPDATE` (harmless if it's already set: `COALESCE` preserves it
     either way). There is no `account_id` column on `applications`
     to write here anymore.
  - **This makes provisioning the one place in the whole codebase where
    a Temporal activity's idempotency actually matters in a way that
    can silently misbehave**: activities can be retried by Temporal
    after a successful-but-unacknowledged execution, *or* after a
    genuine partial failure partway through. Creating a new `account`
    row (or a second Welcome Letter, or re-promoting an
    already-promoted `id_photo`) unconditionally on every call would
    duplicate state on a retry — the `get_by_application_id` check
    above, backed by `accounts.application_id`'s `UNIQUE` constraint,
    is what makes the whole activity safe to run twice.
- **One customer, one active account per product type — enforced
  *before* the decision is signaled, not just inside provisioning.**
  `accounts.product_type` (new column) plus a partial unique index
  (`db/schema.sql`'s `ux_accounts_customer_active_product_type`, on
  `(customer_id, product_type) WHERE status = 'ACTIVE'`) is the
  authoritative enforcement — a customer can hold any number of
  `CLOSED` accounts of the same type, just never two `ACTIVE` ones at
  once. But by the time `persist_decision` runs, the decision has
  already been accepted by the workflow — there's no clean way to
  surface an error back to whoever clicked Approve from that deep
  inside activity execution. So the real gate is earlier:
  `application.service.check_decision_allowed(application_id, decision)
  -> list[str]` (empty = OK, same shape as `check_completeness`) —
  called by `bff_backoffice` **before** it calls
  `workflow.service.signal_decision(...)`, for both the single-item and
  bulk-approve paths (bulk approve pre-filters each selected
  application this way *before* collecting `workflow_ids` to hand to
  `bulk_signal_decision`; anything blocked is reported as a per-item
  failure, same shape as any other bulk partial-failure). Only relevant
  for `decision == "APPROVE"` — Reject/RequestMoreInfo/Cancel never
  create an account, so never conflict. This is what actually justifies
  `application/service.py`'s new read-only call into
  `account.service.has_active_account_of_type(customer_id,
  product_type)` (see the corrected module-boundary rule above). **A
  real, accepted gap in the window itself, not fully closed**: two
  different staff members approving two different applications for the
  same customer+product_type within the small-but-nonzero window
  between this pre-check passing and `persist_decision` actually
  writing the account can still both pass the check — the partial
  unique index is the backstop that stops the bad state from ever
  being written. What's no longer a gap is what happens to the loser
  when that's hit: `persist_decision` now converts it into a clean
  `REJECTED` outcome instead of a stuck application and a `FAILED`
  Temporal workflow — see "Known gaps" below for the full mechanism,
  the live repro, and why the window itself was left open on purpose.
- **Document hierarchy still gates submission on a two-level branch**
  (`<applicant_identifier> -> <application_id> -> category`, see
  "Document hierarchy" below) — document upload/completeness-check at
  submission time never needs an `account_id`, since no account can
  possibly exist before submission. Post-approval, the hierarchy gains
  a separate `<applicant_identifier> -> id_photo` node and an
  `<applicant_identifier> -> <account_id>` branch (Welcome Letter,
  Consent) — see "Document hierarchy" below for the full tree; the
  Account level isn't gone, it's just populated later than the
  Application level, by a different code path (provisioning, not
  submission).

## Modules, in detail

### 1. `bff_customer/` — Customer BFF

Public-facing, mobile-first HTMX (PRD §8.1). No business logic or data
of its own — pure orchestration + presentation, calling straight into
the domain modules' `service.py` functions.

- Owns the customer self-identify session cookie (PRD §7.1) — signed,
  holding `applicant_identifier`, no password, no Redis (nothing
  token-shaped to store). **Corrected from an earlier draft of this
  file**, which said this was a slot inside `bff_backoffice`'s
  Starlette `SessionMiddleware` session; built instead (Phase 11) as
  its own dedicated cookie, hand-rolled with `itsdangerous` directly in
  `bff_customer/identity.py` (the same library `SessionMiddleware` uses
  internally) — `.env.example`'s `CUSTOMER_SESSION_SECRET_KEY`, present
  since P5-1, already anticipated this as a value distinct from
  `BACKOFFICE_SESSION_SECRET_KEY`, and Starlette supports only one
  `SessionMiddleware`/cookie per app, which `bff_backoffice`'s Keycloak
  session id already occupies. **Setting this
  cookie is a pure client-side write — no database call at all**;
  `customer/`'s row doesn't get created until (and unless) an
  application under this identifier is approved (see "Applying without
  being a customer yet"). The new-application wizard's own multi-step
  draft state (product type, provisional `application_id`, in-progress
  field values) is a separate, lower-stakes concern that *does* still
  ride on `bff_backoffice`'s shared `SessionMiddleware` session, under
  its own key — ordinary UI flow state, not identity, so it doesn't
  need its own signing mechanism.
- **This cookie is now only ever set after email verification, not on
  the strength of a self-typed identifier alone.** Corrected after
  being flagged as this POC's standout risk (Known Gaps below): typing
  someone else's email used to be sufficient to see and act on every
  application filed under it. `/apply/identify`'s `POST` now generates
  a 6-digit code (`identity.generate_verification_code()`), "sends" it
  via `notifications.send_verification_code(...)` (fake/dev-only
  delivery — see that module's docstring for why and what a real
  provider integration would change), and stashes its *hash* (never
  the code itself) in a second, short-lived signed cookie
  (`identity.start_verification(...)`, 10-minute expiry). A new
  `/apply/identify/verify` route checks the submitted code against
  that hash (`identity.verify_code(...)`) before ever calling
  `set_applicant_identifier`; 5 wrong attempts
  (`identity.record_failed_verification_attempt(...)`) clears the
  pending cookie and forces a fresh code. **Phone-number identifiers
  were dropped along with this fix** — the identify form now only
  accepts an email address, since SMS delivery would need a real SMS
  provider this project has none of either; confirmed with the user as
  an accepted scope reduction of choosing email OTP specifically.
- Calls, all as direct in-process function calls:
  `customer.service.find_by_identifier(...)` (read-only, optional —
  e.g. for "welcome back" copy),
  `application.service.create_application(...)` /
  `resubmit_application(...)` / `list_for_applicant(applicant_identifier, ...)`,
  `document.service.upload(applicant_identifier, application_id, ...)` /
  `list_documents(...)`,
  `workflow.service.signal_decision(..., decision="CANCELLED")`. **No
  `account.service` call** — there's nothing for this BFF to
  find-or-create anymore; account creation happens only inside
  `application/activities.py` on approval.

### 2. `bff_backoffice/` — Back-Office BFF (the "LOS")

Internal-facing HTMX (PRD §8.2), Keycloak-gated. "LOS" (Loan Origination
System) is this module's working name.

- Owns the entire Keycloak integration for this project: Authorization
  Code flow, Redis-backed session store, `require_session_role()` /
  `require_permission()` — direct reuse of `review-approval-temporal`'s
  mechanism (see "Identity" below). **This is the only module with a
  Keycloak dependency** — no domain module validates a Keycloak token;
  they trust `bff_backoffice` to have already checked, because there's
  no network boundary between them for an unchecked call to cross in
  the first place (unlike the microservices version, this module
  doesn't need an internal API key to prove a call is legitimate —
  being in the same process *is* the proof).
- Calls `application.service` (paginated Underwriter/Manager queues,
  read), `document.service` (view documents), `workflow.service`
  (single-item and bulk decision signals), `customer.service.get()` +
  `account.service.get()` (render applicant/account detail in the review
  dialog — **both are conditional on the application actually having a
  `customer_id`/`account_id` set**; for a `PENDING_UNDERWRITING` or
  `PENDING_MANAGER_APPROVAL` application the applicant may not be a
  resolved customer yet, `account_id` is always `None` until terminal
  `APPROVED` — the dialog falls back to the application's own
  denormalized `applicant_name`/`applicant_email`/`applicant_phone`
  fields in that case rather than calling `customer.service.get(None)`).
- **Every Approve action — single-item or bulk — is pre-checked before
  calling `workflow.service.signal_decision(...)`/
  `bulk_signal_decision(...)`** (PRD's active-account rule, "Applying
  without being a customer yet") — the single-item route calls
  `application.service.check_decision_allowed(application_id,
  "APPROVE")`; bulk approve calls the batch-aware
  `check_decision_allowed_bulk(application_ids, "APPROVE")` instead,
  not a loop over the single-item function, since only the batch-aware
  version can catch two selected applications claiming the same
  applicant+product_type against *each other* (see that function's own
  docstring and the Known Gaps entry). A non-empty result blocks that
  application: single-item shows the reason as an error instead of
  sending the signal; bulk approve filters blocked applications out of
  the batch *before* collecting `workflow_ids` and reports each one as
  a per-item failure in the same result shape as any other bulk
  partial-failure — it never reaches `bulk_signal_decision` at all.
  Reject/RequestMoreInfo/Cancel skip this check entirely (both
  functions are a no-op unless `decision == "APPROVE"`).
- Owns the bulk-selection store — reuse the same Redis instance this
  module already needs for Keycloak sessions.

### 3. `customer/` — Customer module

Owns the customer profile: `customer_id`, `applicant_identifier`
(the email/phone the customer self-entered), `name`, `email`, `phone`,
`created_at`. Owns the `customers` table — the only module whose code
touches it. **A customer row no longer gets created on first visit** —
see "Applying without being a customer yet" above; this module's
create path only fires from inside an approval.

**`customer_id` is `CUS-` followed by a random 9-digit number
(`idgen.service.generate_id("CUS", 9)`), assigned by `db.get_or_create`
at insert time — not a database default.** Corrected from an earlier
draft of this file, which had Postgres generate a `UUID` via
`DEFAULT gen_random_uuid()`; every domain module's primary key moved to
this application-assigned, human-readable scheme at once (see "Data
storage" for the full rationale and the shared `idgen/` module). Because
`get_or_create` is already a find-or-create keyed on
`applicant_identifier` (its own unique index), it now has *two*
independent conflict paths to handle on insert: the existing
`ON CONFLICT (applicant_identifier) DO NOTHING` (a real concurrent
caller for the same identifier — unchanged), and a fresh
`UniqueViolationError` on the `customer_id` primary key itself (the
generated id happened to collide with an unrelated row's) — the second
one is handled by regenerating the id and retrying the insert, bounded
at 10 attempts.

- `service.find_by_identifier(applicant_identifier) -> Customer |
  None` — **read-only**, no side effects. Called by
  `application.service.create_application(...)` at submission time to
  link an application to an existing customer if one matches; also
  usable by `bff_customer` (e.g. to show "welcome back" copy) without
  ever writing a row.
- `service.get_or_create(applicant_identifier) -> Customer` —
  find-or-create, idempotent. **Called only from
  `application/activities.py`'s `persist_decision`**, at the moment an
  application resolves to terminal `APPROVED` and no existing customer
  was already linked. Not called by `bff_customer`'s identify step —
  the session cookie itself needs no database write at all now, it just
  holds whatever `applicant_identifier` the customer typed.
- `service.get(customer_id) -> Customer`.

### 4. `account/` — Account module

Owns the account entity: `account_id`, `customer_id` (stored as a plain
column, **not** a database foreign key across module boundaries — see
"Data storage" below for why even a same-database FK is deliberately
avoided here), `application_id` (same treatment — opaque, not a real FK
— see below), `product_type`, `opened_at`, `status`. Owns the
`accounts` table exclusively. **An account is the outcome of an
approved loan, not something a customer has going into one** — see
"Applying without being a customer yet" above. One customer can hold
**many** accounts (one per approved application, over time), so unlike
the original draft there's no one-account-per-customer uniqueness
constraint — **but a customer's `ACTIVE` accounts may never repeat a
`product_type`** (a customer can have a `CLOSED` and a new `ACTIVE`
`personal_loan` account, just never two `ACTIVE` ones), enforced by
`db/schema.sql`'s partial unique index on `(customer_id, product_type)
WHERE status = 'ACTIVE'`.

**`account_id` is `ACC-` + a random 9-digit number
(`idgen.service.generate_id("ACC", 9)`), assigned by `db.create` at
insert time**, same scheme and same PK-collision-retry handling as
`customer/`'s — see that module's section and "Data storage" below.

**`accounts.application_id` (`NOT NULL`, `UNIQUE`) points at the
application that produced this account — corrected from an earlier
draft of this file, which had the pointer the other way
(`applications.account_id`, nullable).** See "Applying without being a
customer yet" for the full reasoning (finding an application from its
account was previously impossible; the `UNIQUE` constraint here now
doubles as `persist_decision`'s idempotency guard).

- `service.create_account(customer_id, product_type, application_id) ->
  Account` — always creates a new row, no find-or-create semantics (an
  account isn't a singleton per customer anymore). **Called only from
  `application/activities.py`'s `persist_decision`**, exactly once per
  application that reaches terminal `APPROVED` — see that section's
  idempotency note on why `persist_decision` calls
  `get_by_application_id` before calling this, not unconditionally on
  every activity execution. Not itself conflict-safe against the
  *business* rule — relies on `check_decision_allowed` (below) having
  already blocked the decision if this would violate the active-account
  rule; the partial unique index is the last-resort backstop for that,
  not the primary defense. (Separately, and unconditionally, this
  function *does* retry on its own generated `account_id` colliding
  with an unrelated row — an engineering concern, not a business one.)
- `service.get_by_application_id(application_id) -> Account | None` —
  **read-only**, new. The reverse lookup the direction flip above
  exists to make possible; also what `persist_decision` calls first, as
  its idempotency check. Called by `bff_backoffice`'s review dialog to
  render an application's resulting account (replacing the old
  `application.account_id`-gated `account.service.get(...)` call).
- `service.has_active_account_of_type(customer_id, product_type) ->
  bool` — **read-only**, the one function that makes the active-account
  rule enforceable *before* a decision is signaled. Called by
  `application.service.check_decision_allowed(...)`, never directly by
  a BFF (mirrors `customer.service.find_by_identifier`'s role: a
  read-only check `application/service.py` is allowed to make).
- `service.get(account_id) -> Account`.

### 5. `application/` — Application module

Owns the loan application entity, its `applications` table
exclusively, and the **submission business rule** (the document-
completeness gate, PRD §6.4) — the direct successor to
`review-approval-temporal`'s `workflow/service.py`, scoped to the
application domain specifically now that other domains have their own
modules.

- `service.create_application(applicant_identifier, product_type,
  payload, applicant_name, applicant_email, applicant_phone, amount,
  application_id=None)` — **no `customer_id`/`account_id` params** —
  neither is guaranteed to exist yet (see "Applying without being a
  customer yet" above; there is no `account_id` column on `applications`
  at all anymore, see that section). **`application_id` is optional,
  corrected from an earlier draft of this file that gave this function
  no such parameter at all** — that draft said this function "generates
  `application_id` (a `UUID`) first," full stop, which quietly conflicts
  with the customer-facing flow it's paired with elsewhere in this same
  file: `document.service.upload(applicant_identifier, application_id,
  category, file)` needs an `application_id` to tag uploads with, and
  Phase 11's own New Application flow is specified as "document upload
  → review & submit, calling `application.service.create_application(...)`"
  — uploads happening *before* this call, against an id this function
  alone was supposed to mint, is not satisfiable. The fix: `bff_customer`
  mints a provisional `application_id` (`idgen.service.generate_id("APP",
  9)`, via the shared `application.service.APPLICATION_ID_PREFIX`/
  `APPLICATION_ID_LENGTH` constants both call sites reference — corrected
  from an earlier draft that used a plain `uuid4()`,
  before every id in this codebase moved to the shared human-readable
  scheme, see "Data storage") at the *start* of its wizard, threads it
  through every `document.service.upload(...)` call during the flow,
  and passes that same id to `create_application(...)` at final submit
  — this function uses it verbatim instead of minting its own. If
  omitted (a caller with no upload-first flow), this function generates
  a fresh one itself the same way, same as the original draft. Either way, the
  returned result always carries `application_id` (even in the
  missing-categories branch, which persists no row) so a caller that
  *didn't* pre-mint one can still learn what id its just-checked
  documents should be tagged under, then retry this same call once
  they're uploaded. Resolves
  `customer_id` via the **read-only** `customer.service.find_by_identifier(applicant_identifier)`
  (`None` if this is a new applicant — `account_id` is always `None` at
  this point, full stop, regardless), validates
  `payload` against the `product_type`'s Pydantic schema (owned here, in
  `application/schemas.py`), calls `document.service.check_completeness(...)`;
  if satisfied, calls `workflow.service.start_workflow(application_id,
  product_type, payload, amount, applicant_identifier, applicant_name,
  applicant_email, applicant_phone, customer_id)`. **`amount` is passed
  to `start_workflow` as its own argument, never folded into `payload`**
  — the workflow needs it to run PRD §6.3's escalation-threshold check
  at the Approve transition, but `payload` stays
  product-specific-fields-only and the workflow stays payload-agnostic
  (never inspects `payload` itself — `amount` is the one common field it
  *does* need to see, so it travels as a named parameter, not a payload
  lookup). `applicant_identifier`, `applicant_name`, `applicant_email`,
  `applicant_phone`, and the possibly-`None` `customer_id` travel the
  same way, purely so `persist_application` (the workflow's first
  activity) has them to write into the row — `workflow/` still never
  inspects any of them, just forwards them as opaque activity arguments.
  **The actual `applications` row isn't
  written by this function directly** — `persist_application` is one of
  the four activities in `application/activities.py` (see "Breaking
  the cycle"), invoked by the workflow's own `run()` method as its
  first step, the same way `review-approval-temporal`'s workflow calls
  `persist_request` at the start of `run()` rather than the caller
  writing Postgres before starting the workflow. So
  `create_application()` reuses the reference project's `_wait_until()`
  pattern after calling `start_workflow()` — bounded poll (~50ms/5s)
  against `application/db.py`'s own read, always returning whatever it
  last read even on timeout — for the same reason: `start_workflow()`
  only confirms Temporal *accepted* the start, not that
  `persist_application` has actually committed yet, and the caller
  (a BFF) immediately wants to show the created application. If
  documents are missing, return the specific missing categories without
  ever calling `workflow.service` — never start a workflow for an
  incomplete application.
- `service.resubmit_application(application_id, payload)` — same gate
  re-check, then `workflow.service.signal_resubmit(...)` against the
  *existing* `workflow_id` (the same running execution, still waiting
  from `MORE_INFO_REQUESTED` — not a new workflow start).
- `service.check_decision_allowed(application_id, decision) ->
  list[str]` — blocking-reason strings, `[]` if the decision may
  proceed (same shape as `check_completeness`). A no-op (`[]`
  immediately) unless `decision == "APPROVE"`. **Called by
  `bff_backoffice`'s single-item decision route before it calls
  `workflow.service.signal_decision(...)`** — never by
  `application/activities.py`, which has no clean way to surface an
  error back to a decision-maker from inside a running activity.
  Resolves the applicable `customer_id` via `find_by_identifier` when
  the row's own column is `NULL` (a since-approved sibling application
  under the same identifier may have already resolved one — trusting
  `NULL` alone as "no customer exists" was a real bug, found live and
  fixed) and calls the **read-only**
  `account.service.has_active_account_of_type(customer_id,
  product_type)`; if `True`, returns a message naming the conflicting
  product type. See "Applying without being a customer yet" for the
  full active-account rule and its accepted, narrower (cross-request
  only) race-window gap.
- `service.check_decision_allowed_bulk(application_ids, decision) ->
  dict[str, list[str]]` — the batch-aware sibling `bff_backoffice`'s
  bulk-approve route calls instead of looping the single-item function
  above. Tracks `(applicant_identifier, product_type)` pairs already
  claimed by an earlier, still-eligible item *in the same batch*,
  blocking a later item for the same pair before any signal for it is
  ever sent — this is what actually closes the in-batch half of the
  active-account race window (two applications for the same
  applicant+product_type, both selected into one bulk action, would
  otherwise both pass an independent per-item check, since neither
  one's account exists yet). See "Applying without being a customer
  yet" and the Known Gaps entry for the live repro and exactly what
  this does and doesn't close.
- `service.get(application_id)`,
  `service.list_for_applicant(applicant_identifier, page, ...)`,
  `service.list_by_status(status, page, ...)` (staff queues). **The
  customer-facing list is keyed on `applicant_identifier`, not
  `customer_id`** — it has to return an applicant's own applications
  even before any of them are approved and `customer_id` gets resolved
  (see "Applying without being a customer yet"). All three support the
  reference project's paginated, `query_id`-cached list pattern — see
  the `list-pagination-bulk-actions` skill.
- **`activities.py`** — the concrete Temporal activity implementations
  (see "Breaking the cycle" above): `persist_application`,
  `persist_decision`, `persist_resubmit`, one per state-changing
  operation (don't collapse into one generic activity — each has
  different column-update semantics, same reasoning as the reference
  project). This is where `underwriter_name`/`manager_name` actually get
  written, sourced from the `actor_name` the signal carried, **and**
  where `persist_decision` provisions the customer/account on a
  terminal `APPROVED` transition (see "Applying without being a
  customer yet" above for the exact sequence and its idempotency
  requirement) — the one file in this module allowed to import
  `customer/` and `account/`.

**Denormalized applicant fields, on purpose**: `applicant_name`/
`applicant_email`/`applicant_phone` are captured on the application
record at creation time, not read live from `customer/` on every list
render. This isn't a shortcut — an application should keep the identity
details *as submitted*, which shouldn't silently change if the customer
later edits their profile. `customer/` is the source of truth for the
customer's *current* profile; `application/` is the source of truth for
what a specific application *said* at submission time. This also means
`application/`'s list/get queries never need to call into `customer/`
at all for their own display fields — a nice side benefit, not just a
data-integrity one.

### 6. `document/` — Document module

The direct promotion of `mayan-edms-customer-archive`'s
`mayan_client.py` + a document-service layer into a module of this app.
No Postgres of its own — Mayan's own dedicated Postgres/Redis (see
"Data storage") is the only persistence behind it.

- `service.upload(applicant_identifier, application_id, category,
  file)` — create-document → upload-file (`action_name=replace`) →
  attach metadata (`applicant_identifier`, `application_id`, `category`)
  → rebuild index. Same four-step sequence, and the same gotchas #1-4
  below, as the reference project's upload path. **No `account_id` or
  `customer_id` param** — the document hierarchy is two levels now
  (`<applicant_identifier> -> <application_id> -> category`, see
  "Document hierarchy" below): there's no `account_id` to organize
  under at upload time (uploads happen before submission, before any
  account can possibly exist — see "Applying without being a customer
  yet") and `customer_id` may not exist yet either.
  `applicant_identifier` is required here, not resolved internally —
  `document/` is a leaf module and never imports `application/`, so the
  caller (`bff_customer`, which already has it from the session cookie)
  passes it straight through.
- `service.list_documents(application_id)`.
- `service.check_completeness(application_id, product_type) ->
  list[str]` (missing categories, empty if satisfied) — called by
  `application.service` at create/resubmit time. **A category is
  satisfied by one or more documents, not exactly one** — a customer
  can upload three separate PDFs under "Bank Statements" and the gate
  is satisfied the same as if they'd uploaded one; `upload()` is safe
  to call repeatedly for the same `application_id`/`category`, each
  call creating a distinct Mayan document, never overwriting a prior
  one. (This resolves "an application can have multiple financial-proof
  documents" — no renaming, no new category: "Proof of Income" already
  works this way and always was meant to.)
- `service.preview(application_id, document_id)` — streams the file
  from Mayan for in-app viewing, so neither BFF template needs its own
  Mayan credentials.

**Three more managed document types, beyond the submission-gate
categories above** — all system-triggered, none uploaded by a customer
through the application flow:

- `service.promote_government_id_to_customer_photo(application_id,
  customer_id) -> None` — **called only from
  `application/activities.py`'s `persist_decision`**, as one more step
  of the same APPROVE-provisioning sequence described in "Applying
  without being a customer yet" (guarded by the same `account_id IS
  NOT NULL` idempotency check — this whole block only runs once). Finds
  the just-approved application's "Government ID" document and attaches
  `customer_id` metadata to it (**re-tags the existing document, does
  not copy it** — one Mayan document, findable from both the
  application's node and the customer's `id_photo` node once the index
  rebuilds) rather than asking a brand-new customer to upload the same
  photo twice.
- `service.generate_welcome_letter(account_id, customer_id,
  applicant_name, product_type, amount) -> DocumentRef` — **called only
  from `application/activities.py`'s `persist_decision`**, immediately
  after `account.service.create_account(...)` succeeds, same
  provisioning block. Renders a simple templated PDF (no live data
  beyond the plain arguments passed in — `document/` doesn't import
  `application/`, `customer/`, or `account/` to go get anything itself)
  and uploads it tagged to the new `account_id`. System-generated, no
  human in the loop, exactly one per account.
- `service.upload_consent(account_id, file) -> DocumentRef` — **true
  Mayan document versioning, not a new document per call**: if the
  account already has a "consent" document, this uploads a new *file
  version* of that same document (Mayan retains the version history
  natively); if not, it creates the document first. Not restricted to
  one caller — either BFF can call it once `account_id` exists (both
  already import `document/`); which surface actually exposes a UI for
  this is not yet designed — see `PRD.md`'s open questions.
- `service.list_customer_documents(customer_id) -> list[DocumentRef]`,
  `service.list_account_documents(account_id) -> list[DocumentRef]` —
  for staff/customer viewing (`id_photo`; `welcome_letter` + `consent`
  respectively).

Owns `scripts/setup_document_hierarchy.sh` (one-time, not idempotent)
and the Mayan Index Template definition.

### 7. `workflow/` — Workflow module

The direct promotion of `review-approval-temporal`'s `workflow/`
package, deliberately kept **generic** now that `application/` owns the
concrete activity implementations (see "Breaking the cycle" above) —
this module knows Temporal, not loan applications. "Generic" is about
*imports and data shape* (no import of `application/`, `payload` is an
opaque `dict[str, Any]` never inspected), not about the state machine
itself — `LoanApplicationWorkflow`'s states and its escalation-threshold
check (PRD §6.2, §6.3) are loan-specific business rules that live here
because Temporal workflow code has to be colocated with its `run()`
method; the module boundary this module actually enforces is "doesn't
reach into `application/`'s table or types," not "contains zero
domain knowledge."

- `service.start_workflow(application_id, product_type, payload, amount,
  applicant_identifier, applicant_name, applicant_email,
  applicant_phone, customer_id) -> workflow_id` — `amount` is a plain
  `Decimal`/`float` argument, not read out of `payload`;
  `LoanApplicationWorkflow.run()` needs it to compare against
  `MANAGER_ESCALATION_THRESHOLD_USD` at the Approve transition (PRD
  §6.3). This is the one piece of loan-domain-shaped data `workflow/`
  handles directly — see the note on `workflow/`'s "generic" framing
  below. `applicant_identifier`, `applicant_name`, `applicant_email`,
  `applicant_phone`, and the possibly-`None` `customer_id` are opaque
  strings the workflow forwards to the `persist_application` activity
  by name, exactly like `amount`, `product_type`, and `payload` —
  `workflow/` never inspects any of them, it just carries them from
  `start_workflow`'s caller through to the activity call. **Corrected
  from an earlier draft of this file**, which omitted
  `applicant_name`/`applicant_email`/`applicant_phone` from this
  signature entirely — an oversight caught while implementing Phase 4
  (P4-2's `persist_application` activity input needs these three
  denormalized fields to write into the row, same as
  `applicant_identifier`/`customer_id` already did; there was no other
  path for them to reach `persist_application` once `payload` stays
  product-specific-fields-only per `application/`'s own module section
  below).
- `service.signal_decision(workflow_id, actor_role, decision,
  actor_name, comment)` — called directly by `bff_backoffice`
  (Approve/Reject/RequestMoreInfo) and `bff_customer` (Cancel).
- `service.signal_resubmit(workflow_id, payload)` — called only by
  `application.service`.
- `service.bulk_signal_decision(workflow_ids, actor_role, decision,
  actor_name, comment)` — fans out `asyncio.gather()` over the
  single-item signal path, same shape as the reference project's
  `bulk_submit_decision()`, cap at `_MAX_BULK_SIZE` (start at 50).
  Called only by `bff_backoffice`. **Also corrected from an earlier
  draft**, which omitted `actor_role` — `submit_decision` needs it for
  the same reason the single-item `signal_decision` above does (which
  role is deciding is what `LoanApplicationWorkflow._resolve_transition`
  validates against the application's current state), and every
  application in one bulk-approve batch is decided by the same
  signed-in staff member, so it travels once per batch, not once per
  item.
- **`workflows.py`** (`LoanApplicationWorkflow`) — payload-agnostic
  (`product_type: str` + `payload: dict[str, Any]`, never inspected),
  states per PRD §6.2, one `submit_decision(actor_role, decision,
  actor_name, comment)` signal plus a separate `resubmit(payload)`
  signal, `_claim_final()`-style guard against racing terminal
  transitions, native-Temporal-cancel recovery (`except
  asyncio.CancelledError` around the decision wait, run the
  terminal-persist activity with `decision="CANCELLED"`,
  `closed_by="temporal-admin"`, don't re-raise).
- **`worker.py`** — bootstrap function taking an activities list as a
  parameter (see "Breaking the cycle"); same `WORKER_MODE`
  (`both`/`workflow`/`activity`) and `LOAN_PRODUCT_TYPE` env vars as the
  reference project's `REVIEW_TYPE`, same reasoning.
- **`task_queues.py`** — `KNOWN_PRODUCT_TYPES` + queue naming, the
  canonical registry `application/schemas.py` asserts against at import
  time (see "Breaking the cycle").
- **No Postgres table of its own.** Temporal's own persistence (the
  `temporal` database) is managed by the Temporal server container, not
  by this module's code.

## Document hierarchy

```
Loan Onboarding Archive
└── <applicant_identifier>
       ├── <application_id>              (created at submission, always)
       │      ├── Government ID
       │      ├── Proof of Income
       │      ├── Bank Statements
       │      ├── Credit Report
       │      ├── Property Appraisal      (mortgage only)
       │      └── Vehicle Title/Invoice   (auto_loan only)
       ├── id_photo                      (appears only once an application under
       │                                  this applicant_identifier is approved --
       │                                  same Mayan document as that application's
       │                                  Government ID, re-tagged, not copied)
       └── <account_id>                  (appears only once an application under
              ├── Welcome Letter          this applicant_identifier is approved)
              └── Consent                 (single document, multiple file versions)
```

**Two required metadata types beyond the original three**
(`applicant_identifier`, `application_id`, `category`): **`account_id`**
(new — the account branch's node key, present only on `Welcome
Letter`/`Consent` documents, absent on everything under
`<application_id>`) and **`customer_id`** (attached to the promoted
`id_photo` document alongside its original `application_id`/category
metadata, purely so staff search can find it by customer — not itself
an index branch key, since `applicant_identifier` already plays that
role).

**Multi-leaf placement — empirically confirmed against a real instance
in P5-2 (2026-09-02), not just source-level confidence.** The
`id_photo` node depends on one Mayan document satisfying two different
leaf-node paths in the same index template at once — the
`<application_id> -> Government ID` path (via `application_id` +
`category` metadata) and the `<applicant_identifier> -> id_photo` path
(via `customer_id` metadata, no `application_id` in that leaf's
condition). A source read of
`mayan/apps/document_indexing/models/index_instance_models.py`'s
`_document_add()` had already shown it walks *every* child branch at
each tree level and links the document into *all* branches whose
conditions independently evaluate true — not just the first match.
P5-2's verification script (upload a Government ID document, attach
`applicant_identifier`+`application_id`+`category` metadata, rebuild,
confirm it's filed under `<application_id>/Government ID`; then attach
`customer_id` to the *same* document, rebuild again, and confirm it now
*also* appears under `<applicant_identifier>/id_photo`, same document
id in both leaves' `documents_url` listings) reproduced exactly this —
one document, two leaf memberships, confirmed via
`GET /index_instances/<id>/nodes/.../documents/` on a live
`docker compose`-run Mayan instance, not inferred. `promote_government_id_to_customer_photo`
(P5-5, P6-3) can be built as a pure re-tag (attach `customer_id`
metadata to the existing document) with no fallback-to-copy path
needed. **Cabinets were evaluated as an alternative and rejected as the
hierarchy's backbone** — they also support true multi-membership and
are synchronous (no Celery, unlike Index Templates), but the project's
actual usage pattern is automatic, upload-time classification via API,
which is Index Templates' idiomatic niche, not Cabinets' (a third-party
source describes Cabinets as manual, file-manager-style curation).

**A sharper, previously-implicit consequence of gotcha #2 (async
reindex)**: `document.service.check_completeness()` and
`list_documents()`/`list_customer_documents()`/`list_account_documents()`
**must query Mayan's document/metadata search API directly, filtering
on the relevant id + category metadata — never read the Index Template
tree.** Metadata attachment itself is synchronous; only the *index's*
recomputed tree membership is async (Celery-driven, per gotcha #2). If
`check_completeness` walked the index tree instead, a customer who
uploads their last required document and immediately hits Submit could
get a false "still missing" result purely from index lag — a real
correctness bug, not a hypothetical, since `create_application()` calls
`check_completeness()` synchronously right after the customer's last
upload (PRD §6.4). The Index Template tree exists for staff to browse
the archive visually in Mayan's own UI; it is not a data source for any
of this application's own logic.

**Two levels on the application branch, not `mayan-edms-customer-archive`'s three** — that
project's `Customer → Account → Application` shape assumed both a
customer and an account already existed at document-upload time. Here
neither does: documents are uploaded and the completeness gate checked
*before* submission (PRD §6.4), and an account is now the *outcome* of
an approved application, not a precondition of filing one (see
"Applying without being a customer yet" above). `applicant_identifier`
is used as the top level specifically because it's the one identity
value guaranteed to exist at upload time regardless of whether the
applicant is a recognized customer yet — using `customer_id` instead
would mean branching this index between "customer" and "prospect"
sub-trees, or re-parenting documents after approval (Mayan gotcha #2's
async reindex makes that a real cost, not just an inconvenience); a
flat `applicant_identifier` node sidesteps both. The same **five
gotchas** documented in `mayan-edms-customer-archive`'s
`docs/document-hierarchy-setup.md` still apply — read that file before
touching the index template or `document/`'s setup script (they're
about index-template mechanics, not the specific number of levels):

1. Empty index-node expressions don't prune the branch — every leaf
   condition must repeat the full ancestor requirement set.
2. Index updates are async (Celery) — always rebuild the index after
   attaching all metadata, wait ~10-15s before reading the tree.
3. `action_name` on file upload is a string ID (`replace`); an invalid
   value fails silently (HTTP 200, broken async task).
4. A file that passes magic-byte sniffing may still have zero
   extractable pages — verify real uploads actually render.
5. `GET /index_templates/<id>/nodes/` doesn't return a wrapped root —
   `results` *is* the children array.

`DELETE /api/v4/documents/{id}/` moves to Mayan's trash, not a hard
delete — confirmed via the endpoint's own OPTIONS description in the
reference project.

## Identity

Two completely different mechanisms — see PRD §7 for the product
framing.

### Customer side (`bff_customer/`) — email-verified, still no password

**Corrected from an earlier draft of this file**, which described this
surface as having no verification at all -- closed after being flagged
as this POC's standout risk (see Known Gaps below for the full
mechanism and the fix). Signed session cookie holding
`applicant_identifier`, no password, no Redis -- but the cookie is now
only ever set after the applicant proves ownership of that identifier
via a 6-digit one-time code, not on the strength of just typing it in.
See `bff_customer/identity.py`'s module docstring for the full design
(a second, short-lived signed cookie holding the code's *hash*, not
the code itself, still no Redis -- the same "no server-side state"
philosophy this module already had, just applied to a second cookie)
and `bff_customer/notifications.py`'s for why delivery is fake/dev-only
in this POC.

### Back-office side (`bff_backoffice/`) — real Keycloak, direct reuse

Load the **`keycloak-admin`** skill before touching any of this.
Directly reuses `review-approval-temporal`'s mechanism:

- **Realm** (`keycloak/import/loanrealm-realm.json`, `start-dev
  --import-realm`): two realm roles, **`Underwriter`**, **`Manager`**;
  one confidential client `loan-onboarding-backoffice`; one Resource,
  **`LoanApplication`**, with five Scopes — `UnderwriterApprove`,
  `UnderwriterReject`, `UnderwriterRequestMoreInfo`, `ManagerApprove`,
  `ManagerReject` — bound via two Policies (`Underwriter Policy`,
  `Manager Policy`) and five scope-type Permissions. Demo users:
  `underwriter1`/`underwriter2` (`Underwriter`),
  `manager1`/`manager2` (`Manager`), password `password`.
- **Five stage-specific scopes, not the reference project's shared
  `Approve`/`Reject`**: that project only had one approving role, so a
  shared scope name never crossed a privilege boundary. Here both
  Underwriter and Manager approve, at different stages — a shared scope
  would hand every Underwriter a permission that also satisfies the
  Manager-stage decision route's check.
- **Code**: `bff_backoffice/keycloak_auth.py` (JWT decode + UMA ticket
  exchange + token refresh), `bff_backoffice/session_store.py`
  (Redis-backed `/ui/*` sessions — needed because a real access+refresh
  token pair runs ~4.5KB signed, over the ~4KB real-browser cookie
  ceiling, measured directly in the reference project),
  `bff_backoffice/keycloak_session.py` (`get_session_user()`,
  `require_session_role()`, `require_permission()`/`check_permission()`).
  **Role gates screens, permission gates actions — no exceptions**,
  including no `require_session_role("manager")` pre-gate on the
  manager decision route itself (only the permission check) — a
  deliberate, audited correction in the reference project after an
  earlier version had both and produced two different 403 reasons for
  the same denied action.
- **`keycloak_session.py`'s functions take a plain `session_id: str |
  None`, never a FastAPI `Request`** — a deliberate adaptation from the
  reference project's own `bff/keycloak_session.py`, whose equivalent
  functions read `request.session`/`request.app.state.redis` directly.
  That coupling makes a function untestable without a real Starlette
  `Request`; since `app.py` didn't exist yet when this module was
  written (Phase 9, before Phase 10's routes), there was no reason its
  session-resolution *logic* should depend on a web framework to be
  unit-tested. `bff_backoffice/routes.py` (Phase 10) supplies the thin,
  framework-coupled layer on top — small dependency-wrapper functions
  (`_role_dependency(role)`, `_session_user_dependency`) that read
  `request.session.get(SESSION_KEY)` and delegate into
  `keycloak_session.py`'s plain functions. Same split
  `workflow/service.py` already uses relative to `worker_main.py` for
  an analogous reason (framework/runtime-agnostic core, a thin
  composition-root/route layer on top) — not a new pattern for this
  codebase, just applied one level down.

`underwriter_name`/`manager_name` come from the authenticated session's
`preferred_username`, passed through as `actor_name` on the decision
signal — never a client-submitted free-text field.

**Deliberately not built**: Keycloak protection for Temporal Web UI (the
reference project does this via a `TemporalAdmin` role; out of scope
for "back-office web application authentication" specifically), and
anything Keycloak-related on the customer side (a different,
purpose-built identity problem — PRD §7.1).

## Data storage

*(ER diagram: [`docs/diagrams/er-diagram.md`](docs/diagrams/er-diagram.md).)*

**One application database, `loan_onboarding`**, holding all three
domain tables — `customers` (owned by `customer/`), `accounts` (owned
by `account/`), `applications` (owned by `application/`) — plus a
separate `temporal` database for Temporal's own persistence, both in
the **same Postgres container**. This is exactly
`review-approval-temporal`'s own two-database-one-container pattern
(`db/init/*.sh` creates both), just with three app tables instead of
one.

**No foreign keys between `accounts.customer_id` /
`accounts.application_id` / `applications.customer_id` and the tables
they reference**, even though they're physically in the same database
now — deliberately, to keep the module boundary meaningful. A same-
database FK would make it trivially easy (and someday tempting, under
deadline pressure) to write a query that joins across module
boundaries directly, silently reintroducing exactly the coupling the
module split exists to prevent. Treat the three tables as if they were
in separate databases even though they aren't; the only sanctioned way
to resolve a `customer_id` into a name is a call to
`customer.service.get(...)`. (`accounts.application_id` still gets a
plain `UNIQUE` index — enforcing "at most one account per application"
is a within-table constraint, not a cross-module join, so it doesn't
raise the same concern a real FK would.)

**Primary keys are short, human-readable, application-assigned
strings — not database-generated `UUID`s.** Corrected from an earlier
draft of this file, which had every table's primary key as
`UUID PRIMARY KEY DEFAULT gen_random_uuid()`. Each of the three entity
types gets its own prefix plus a random 9-digit number, generated by a
new shared leaf module, `idgen/` (see "Module dependency graph"):

| Entity | Prefix | Example |
|---|---|---|
| `customers.customer_id` | `CUS-` | `CUS-483920174` |
| `accounts.account_id` | `ACC-` | `ACC-019283746` |
| `applications.application_id` | `APP-` | `APP-573920184` |

`idgen.service.generate_id(prefix, length) -> str` is a pure function
(`secrets.choice` over `0-9`, no I/O) — every module that assigns one
of these ids (`customer/db.py`, `account/db.py`, `application/service.py`,
and `bff_customer/routes.py` for its provisional pre-mint) calls it
directly and passes the result into its own `INSERT`; nothing reads a
database default anymore. **Collision handling lives at each insert
site, not inside `idgen`**: on a `UniqueViolationError` against the
table's own primary key specifically (never a business-rule constraint
like `ux_accounts_customer_active_product_type` or the
`applicant_identifier` unique index), the caller regenerates the id and
retries the insert, bounded at 10 attempts.

**This is a real, deliberate entropy tradeoff, not an oversight**: pure
digits at length 9 is `10^9` (1 billion) values per entity type —
comfortably enough for a POC, but the birthday-paradox collision
probability becomes non-trivial (not merely theoretical) somewhere in
the tens-of-thousands-of-rows range for a single table, which is why
the retry-on-collision behavior above is load-bearing rather than
defensive icing. A longer or alphanumeric id would close this gap
entirely; kept at 9 digits specifically so ids read like a familiar
account-number format. Worth revisiting under the same "if this ever
needs to scale past one team/one deploy cadence" framing this file
already applies to its other POC-scale tradeoffs (see "Known gaps").

Mayan has its own fully separate `mayan-db`/`mayan-redis` (third-party
app boundary — see `mayan-edms-customer-archive`'s own `CLAUDE.md`,
"not application code we maintain"). Keycloak uses its own in-memory H2
(`start-dev` mode) — no dedicated Postgres.

## Enforcing the boundaries

Nothing about Python stops `bff_customer/` from importing
`application/db` directly and skipping `application/service.py`, or
`customer/` from importing `application/` and creating the exact cycle
"Breaking the cycle" above exists to avoid. The reference projects got
away with informal discipline because each was small enough for one
person to hold the whole map in their head. **Once this codebase has
seven modules, add an import-linter (or equivalent) config as part of
setting up the project — not as a later hardening pass** — encoding
literally the dependency graph drawn above, and run it in CI. Treat a
lint failure here with the same seriousness the reference project treats
its `schemas.py`/`task_queues.py` assert firing: a real bug, not a style
nit.

## Repo layout

```
loan-onboarding-poc/
├── pyproject.toml            # single source of truth for deps + packaging --
│                              # ONE package, matching review-approval-temporal's
│                              # convention, not one per module
├── Dockerfile
├── docker-compose.yml
├── db/
│   ├── schema.sql             # customers, accounts, applications tables --
│   │                          # NOT part of the Python package, applied via
│   │                          # bind mount, same convention as the reference project
│   └── init/                  # creates the loan_onboarding + temporal databases
├── scripts/
│   └── setup_document_hierarchy.sh
├── keycloak/
│   └── import/loanrealm-realm.json
├── docs/
│   ├── api-specification.md   # internal service.py contracts (all 7 modules)
│   └── diagrams/               # rendered Mermaid versions of this file's
│                                # ASCII diagrams -- ER, system architecture,
│                                # module dependency graph
├── .importlinter              # or pyproject.toml [tool.importlinter] --
│                              # encodes the dependency graph above
├── tests/
│   ├── unit/                  # mirrors module structure, no live services
│   └── integration/           # needs the real local stack
└── loan_onboarding/
    ├── __init__.py
    ├── app.py                 # composition root: assembles bff_customer +
    │                          # bff_backoffice into the FastAPI app (see
    │                          # "Deployment" for the alternative split)
    ├── worker_main.py          # composition root: workflow/ Worker bootstrap +
    │                          # application/activities.py, see "Breaking the cycle"
    ├── bff_customer/
    │   ├── routes.py
    │   ├── identity.py
    │   ├── notifications.py    # fake/dev-only verification-code delivery
    │   └── templates/
    ├── bff_backoffice/
    │   ├── routes.py
    │   ├── keycloak_auth.py
    │   ├── session_store.py
    │   ├── keycloak_session.py
    │   ├── selection_store.py
    │   └── templates/
    ├── customer/
    │   ├── service.py
    │   ├── models.py
    │   └── db.py               # the ONLY code touching the `customers` table
    ├── account/
    │   ├── service.py
    │   ├── models.py
    │   └── db.py               # the ONLY code touching the `accounts` table
    ├── application/
    │   ├── service.py
    │   ├── schemas.py          # per-product-type payload registry, asserts
    │   │                      # against workflow.task_queues.KNOWN_PRODUCT_TYPES
    │   ├── activities.py        # concrete Temporal activities -- see "Breaking
    │   │                        # the cycle"
    │   ├── models.py
    │   └── db.py               # the ONLY code touching the `applications` table
    ├── document/
    │   ├── mayan_client.py
    │   └── service.py
    ├── workflow/
    │   ├── workflows.py
    │   ├── worker.py            # bootstrap fn taking an activities list --
    │   │                        # imports nothing from application/
    │   ├── task_queues.py
    │   └── service.py
    └── idgen/
        └── service.py           # generate_id(prefix, length) -- the only
                                  # function in this module, zero I/O, zero
                                  # state; every module that assigns a
                                  # primary key imports this one
```

Every module imports every other module it's allowed to by its full
package path (`from loan_onboarding.workflow import service as
workflow_service`), matching `review-approval-temporal`'s convention —
no `sys.path` manipulation.

## Deployment

Still genuinely "one deployable" in the sense that matters (one image,
one dependency set, in-process calls between modules) — but that
doesn't force literally one running process, the same way the reference
project's `worker.py` already runs as a process separate from its
`uvicorn` web process despite being "the same app":

- **`uvicorn loan_onboarding.app:app`** — the web process, serving both
  `bff_customer` and `bff_backoffice`'s routes from one FastAPI app
  (`app.py` mounts both routers). Simplest option, matches the
  reference project's own single-`bff`-service Compose default.
- **Optional split**: two thin entrypoint modules
  (`app_customer.py`/`app_backoffice.py`), each mounting only one BFF's
  router from the same shared package, run as two separate `uvicorn`
  processes/Compose services from the **same image**. Worth doing if
  public customer traffic and internal staff traffic end up needing
  different scaling profiles or exposure (public ingress vs.
  internal-only) — this is purely a deployment-time choice, the module
  boundaries and in-process calls underneath are identical either way.
  Exactly the same pattern the reference project already uses for
  splitting `worker-workflow`/`worker-activity` from one image via
  `WORKER_MODE`.
- **`python -m loan_onboarding.worker_main`** — the Temporal
  worker process(es), same `WORKER_MODE`/`LOAN_PRODUCT_TYPE`-driven
  split as the reference project.

## Docker Compose topology (local dev)

*(Rendered version: [`docs/diagrams/system-architecture.md`](docs/diagrams/system-architecture.md).)*

Much smaller than the microservices version of this plan — one app
image instead of seven:

- `mayan-db`, `mayan-redis`, `mayan` — copied wholesale from
  `mayan-edms-customer-archive/docker-compose.yml`, fully isolated (see
  "Data storage").
- `db` (one Postgres container, two databases: `loan_onboarding`,
  `temporal`, via `db/init/*.sh`) + `temporal` + `temporal-ui`.
- `keycloak` (`quay.io/keycloak/keycloak:26.0`, `start-dev
  --import-realm`, realm from `./keycloak/import`, port `8080`) +
  `backoffice-redis` (Keycloak sessions + bulk selection for
  `bff_backoffice` only — named distinctly from `mayan-redis`).
- `worker-workflow` / `worker-activity` — from `worker_main.py`, same
  `WORKER_MODE`-split pattern as the reference project.
- `app` — the single web process (or `app-customer` + `app-backoffice`
  if the split above is used), `depends_on: [db, temporal, keycloak,
  backoffice-redis, mayan]`.

Every env var pointing at another container uses its Docker-internal
service name — same discipline the reference project already documents
for `KEYCLOAK_ISSUER`.

## Known gaps to state explicitly once built

- **`applications`'s `chk_approved_has_account` DB-level check
  constraint is gone, not replaced.** An earlier draft of this file
  (back when `applications.account_id` existed) relied on
  `CHECK (status <> 'APPROVED' OR account_id IS NOT NULL)` as a
  database-enforced backstop against a `persist_decision` idempotency
  bug ever leaving an `APPROVED` application with no account. Once
  `account_id` moved to `accounts.application_id` (see "Data storage"),
  that invariant can no longer be expressed as a single-table `CHECK` —
  enforcing "an APPROVED application has a matching account" across
  two tables would need a trigger, which this POC deliberately doesn't
  add. The invariant is still true in practice (`persist_decision`'s
  logic guarantees it), but it moved from **DB-enforced** to
  **code-enforced-only** — a real, if narrow, reduction in the safety
  net, not a change with zero cost.
- **The 9-digit-numeric primary key format (`CUS-`/`ACC-`/`APP-` +
  `idgen.service.generate_id(prefix, 9)`) trades away collision-safety
  margin for a familiar, account-number-style look.** `10^9` values per
  entity type is real headroom for a POC, but nowhere near a `UUID`'s;
  see "Data storage" for the full entropy discussion and why the
  retry-on-collision insert logic in each module's `db.py` is load-
  bearing, not decorative. Revisit (longer id, or alphanumeric instead
  of digits-only) if this project ever needs to scale past POC data
  volumes.
- **Resolved in P12-3**: `app`'s planned host port `8000` (per this
  file's own `app.py` docstring example) would have collided with
  `mayan`'s already-published host port `8000` (flagged as a risk since
  P5-1). `docker-compose.yml`'s `app` service (added in P12-3, once
  both BFFs existed) publishes on `8001` instead, which was already
  registered as this project's native-dev redirect URI
  (`keycloak/import/loanrealm-realm.json`'s `http://localhost:8001/ui/callback`)
  — no realm changes were needed. A second, real bug was found and
  fixed in the same pass, invisible until the very first fully
  containerized `docker compose up --build` (every earlier phase tested
  `app.py` running natively on the host, where this never surfaced):
  `bff_backoffice/keycloak_session.py`'s two browser-redirect URLs
  (login, logout) and `keycloak_auth.py`'s token issuer-claim validation
  were built from `KEYCLOAK_ISSUER` (`http://keycloak:8080/...`,
  correct for this app container's own server-to-server calls to
  Keycloak, but meaningless to a browser outside the compose network,
  and mismatched against what Keycloak actually stamps into an issued
  token's `iss` claim, which mirrors the front-channel/browser-facing
  URL). Fixed with a new `KEYCLOAK_PUBLIC_ISSUER` env var (falls back
  to `KEYCLOAK_ISSUER` when unset, so the native/host-run case needs no
  config change) — see both modules' `_public_issuer()`/
  `_public_keycloak_issuer()` and `docker-compose.yml`'s `app` service
  comment. Verified via a real browser login/logout round trip through
  the fully containerized stack after the fix.
- **Resolved post-P12**: `db`'s published host port (`5432`, Postgres's
  own default) collides with a native, host-installed Postgres on a
  developer machine that's already bound `127.0.0.1:5432` — confirmed
  directly, twice, on this project's own dev machine: both a native
  `psql postgresql://postgres:postgres@localhost:5432/loan_onboarding`
  and pgAdmin (running natively, pointed at `localhost:5432`) silently
  connected to the *native* Postgres instead of this container's, and
  failed with `FATAL: role "postgres" does not exist` (a real,
  reproducible error, not a hypothetical one — the native instance has
  no `postgres` role, this container's does). `docker-compose.yml`'s
  `db` service now publishes on `5433` instead — every in-Compose
  service (`temporal`, `worker-activity`, `app`) is unaffected, since
  they all reach this container via the internal hostname `db:5432`,
  never the host-published port; only host-side tools (pgAdmin, a
  native `psql`, or `DATABASE_URL` for a natively-run `app.py`/
  `worker_main.py`, as several earlier phases' own manual verification
  needed) have to know to use `5433`. This is the same class of
  collision as the earlier `db`-vs-`mayan` port-8000 note above — a
  developer machine's own pre-existing services silently shadowing a
  container's published port, with no error until something actually
  tries to connect and gets someone else's server.
- **Mayan's default REST API rate limit (`REST_API_THROTTLING_RATE_USER`,
  20 req/sec out of the box) is real and gets hit at POC scale, not just
  in theory** — found in P5-4/P5-5 by actually running
  `document.service`'s functions against the real local Mayan instance
  in a realistic sequence (upload several required categories in a row,
  each doing create+upload+3×attach-metadata+rebuild, then
  `check_completeness`'s fetch-all-documents-then-per-document-metadata
  scan) and watching it 429. `mayan_client.py`'s `_request` now retries
  on 429 honoring the `Retry-After` header Mayan sends (confirmed
  present), bounded at `_MAX_429_RETRIES = 5` — not a full fix, just
  enough that a normal customer upload flow doesn't surface a raw HTTP
  error. `document/service.py`'s underlying approach
  (`_documents_matching`: fetch every document in the whole instance,
  then fetch each one's metadata individually, then filter in Python —
  inherited from `mayan-edms-customer-archive`'s identical pattern,
  needed because Mayan's advanced-search endpoint doesn't AND multiple
  metadata fields together) is still O(all documents in the instance)
  per call and will get slower and throttle more often as real data
  volume grows; fine for a POC, would need real server-side filtering
  (or caching, or a lower-frequency rebuild strategy) before this scaled
  past that.
- **Resolved, narrowed rather than fully closed.** `bff_customer/` used
  to accept a self-typed email or phone number with zero verification
  (PRD §7.1) — this POC's standout risk, called out repeatedly in this
  file and PRD.md as the single highest-priority gap. **Fixed** by
  requiring email verification (a 6-digit one-time code, entered back
  correctly) before the session cookie is ever set — see this file's
  "Identity" section and `bff_customer/identity.py`'s module docstring
  for the full mechanism, and `tests/unit/bff_customer/test_identity.py`
  for the regression tests (round-trip, tampered/garbage rejection, the
  5-attempt lockout). Confirmed live: entering a wrong code shows
  "Incorrect code. Try again."; 5 wrong attempts in a row clears the
  pending verification and the correct code no longer works afterward
  without restarting from `/apply/identify`; the correct code lands on
  "My Applications" as the verified identity. **What's still a real,
  accepted limitation, not a gap in the mechanism itself**: this POC
  has no real email/SMS provider (no SMTP, no Twilio/SendGrid/SES
  credentials anywhere in `.env.example`), so delivery is fake --
  `bff_customer/notifications.py` only prints the code server-side,
  and the verify-code page also shows it directly in the response
  (labeled as dev-only) since no real inbox will ever receive it
  otherwise. This proves the mechanism, not a production-ready login --
  see PRD §7.1 for the "treat any real deployment as non-public until a
  real provider replaces the fake one" framing this carries forward.
  Also dropped along with this fix: phone-number identifiers (SMS
  delivery would need a provider this project doesn't have either) --
  the identify form is email-only now, confirmed with the user as an
  accepted scope reduction, not an oversight.
- **Resolved (the "no graceful handling" half only — the race window
  itself is deliberately still open, see below).** The active-account-
  per-product-type rule (`accounts.product_type`'s partial unique
  index, "Applying without being a customer yet") is checked before a
  decision is signaled, but not atomically with it — two near-
  simultaneous Approve decisions for the same customer and product type
  can both pass `check_decision_allowed` before either commits. The
  database constraint always stopped the bad state from ever being
  *written*; what used to be missing was any graceful handling of the
  resulting write failure on the loser's side. Reproduced live via the
  underwriter's own Bulk Approve action (its pre-check loop runs
  `check_decision_allowed` for every selected item *before* any signal
  goes out, then fires all signals concurrently via `asyncio.gather` —
  the natural way to hit the real race, not a race condition needing
  threading tricks to simulate): two `personal_loan` applications for
  one identifier, both selected and bulk-approved together, one
  genuinely won the `ux_accounts_customer_active_product_type` race and
  the other's `persist_decision` hit the real
  `UniqueViolationError` — before the fix, that retried 5 times
  identically, failed the whole Temporal workflow (`Status: FAILED`),
  and left the application stuck at its pre-decision status forever,
  no error ever surfaced to staff. **Fixed** by having
  `persist_decision` catch that specific constraint violation and
  convert the loser's outcome into a clean `REJECTED` write (with a
  system-generated comment explaining the conflict) instead of letting
  the exception propagate — confirmed by design decision with the user
  (three other options were on the table: leave it, fail fast but still
  stuck, or add a distributed lock; converting to a clean terminal
  state was chosen as the one that actually closes the "stuck forever"
  outcome without the invasiveness of serializing the check-and-write).
  `persist_decision` now also returns the status it actually wrote, and
  `workflows.py`'s `submit_decision` uses that return value (not its
  own pre-computed `resulting_status`) for the workflow's own
  `self._status` — closing a related, previously-unnoticed
  inconsistency where the workflow's own `get_status` query could have
  disagreed with what Postgres actually held (nothing in this codebase
  queries workflow status directly today, so this was latent, not
  observed, but is now correct either way). Re-verified live against
  the exact repro above: the losing application lands cleanly on
  `REJECTED` with the auto-generated comment, its Temporal workflow
  ends `Status: COMPLETED` (not `FAILED`) with a query result that
  correctly reports `"status":"REJECTED"`, and the worker logs stay
  silent — no retries, no traceback. **The in-batch half of the window
  itself is now also closed**, in a follow-up fix: `bff_backoffice`'s
  bulk-approve route now calls a new
  `application.service.check_decision_allowed_bulk(application_ids,
  decision)` instead of looping the plain per-item
  `check_decision_allowed` — it tracks which
  `(applicant_identifier, product_type)` pairs an earlier, still-
  eligible item *in the same batch* has already claimed, and blocks a
  later item for the same pair before either signal is ever sent
  (keyed on `applicant_identifier`, not the resolved `customer_id`,
  since the more common trigger is two applications for an applicant
  with *no* customer row yet at all — both would resolve the same
  customer via the same idempotent `get_or_create` during provisioning
  either way). Re-verified live against a fresh repro of the exact
  same scenario: bulk-approving two sibling `personal_loan`
  applications together now reports `"1 succeeded, 1 failed:
  customer already has an active personal_loan account"` **immediately,
  synchronously, in the Bulk Approve Results dialog** — the blocked
  application is never signaled at all, stays at
  `PENDING_UNDERWRITING` for a human to look at again later (rather
  than being silently auto-rejected), and the worker logs stay
  completely silent (no activity ever ran for it). **What's still a
  deliberately-accepted gap**: only same-request concurrency is
  closed. Two independent decisions — a second bulk action, or a
  single-item Approve, submitted via *separate* HTTP requests close
  enough in time — can still both pass their own checks, since nothing
  tracks claims across requests. Closing that fully would need a lock
  spanning from the check (in `bff_backoffice`, a web process) through
  to the actual write (in a Temporal activity, a worker process,
  arbitrarily later) — a much larger, riskier change (lock lifetime,
  staleness, and deadlock handling across an async boundary with no
  natural upper bound) that wasn't undertaken here.
  `persist_decision`'s conflict-to-REJECTED handling (above) remains
  the backstop for that cross-request case, and is expected to still
  fire occasionally. See `application/service.py`'s
  `check_decision_allowed_bulk` and
  `tests/unit/application/test_service.py`'s
  `test_check_decision_allowed_bulk_blocks_second_sibling_in_same_batch`,
  and `application/activities.py`'s `persist_decision` plus
  `tests/unit/application/test_activities.py`'s
  `test_persist_decision_approve_converts_to_rejected_on_active_account_conflict`
  for the still-needed backstop.
- **Resolved, found live in Phase 13's P13-7 verification sweep, not
  just reasoned about.** `check_decision_allowed`'s short-circuit used
  to read `if record["customer_id"] is None: return []` — "a brand-new
  applicant can't conflict with an existing active account" — which is
  only correct for an applicant with *no other* application anywhere.
  Two applications submitted under the same `applicant_identifier`
  before either is decided both get `customer_id = NULL` at submission
  (per "Applying without being a customer yet"). If one is approved
  first (provisioning a customer + an `ACTIVE` `personal_loan` account)
  and the *other*, older sibling application — whose own `customer_id`
  column was never backfilled, since it's a different row — is later
  Approved, the old code still read that row's own `NULL` `customer_id`
  and returned `[]`, never resolving via `applicant_identifier` the way
  `persist_decision`'s own provisioning step does. The signal reached
  the workflow uncontested; `persist_decision` then hit the exact same
  `ux_accounts_customer_active_product_type` `UniqueViolationError` as
  the race above, but deterministically, no timing window required.
  Reproduced directly before the fix: two `personal_loan` applications
  submitted back to back under one identifier, the first Approved
  (provisioning `cus-911063467`/`acc-604713440`), the second's later
  Approve then failing every retry with
  `duplicate key value violates unique constraint
  "ux_accounts_customer_active_product_type"` — the activity's 5
  retries all failed identically, and the Temporal workflow itself
  ended in `FAILED` status with the application permanently stuck at
  `PENDING_UNDERWRITING`, no error ever surfaced to `bff_backoffice` or
  a staff member. **Fixed** by having `check_decision_allowed` resolve
  via `customer.service.find_by_identifier(application.applicant_identifier)`
  when `customer_id` is `NULL`, instead of trusting the column alone —
  a genuinely new applicant still short-circuits to `[]` (no customer
  resolves at all), but a since-approved sibling application is now
  found. Re-verified live against the exact repro above: the second
  Approve now returns a clean `"customer already has an active
  personal_loan account"` error in the review dialog, the application
  stays at `PENDING_UNDERWRITING` (not signaled), and no Temporal
  workflow is touched at all — confirmed via `psql` and the worker
  logs staying silent. See `application/service.py`'s
  `check_decision_allowed` and `tests/unit/application/test_service.py`'s
  `test_check_decision_allowed_resolves_by_identifier_when_customer_id_null_but_customer_exists`.
  The adjacent race-window gap immediately above this bullet is
  unaffected by this fix — it's a separate, narrower timing issue this
  change doesn't touch.
- Module boundaries are enforced by import-linter config, not by a
  process/network boundary — a determined or careless change can still
  violate them if CI isn't actually wired to fail on a violation. Don't
  treat "we organized it into folders" as equivalent to "the boundary is
  enforced" until the lint step exists and is required.
- Same Keycloak-side gaps the reference project has and hasn't closed:
  `verify_aud=False` until a real audience is configured; no caching on
  permission checks (every mutating action is a live UMA exchange).
- No timeout on "wait for Underwriter/Manager decision."
- **A Temporal *terminate* (vs. *cancel*) still can't be recovered from
  inside the workflow, structurally — no event is ever delivered to
  catch — and, worse than previously documented here, no reconciliation
  job exists anywhere in this codebase to catch it from the outside
  either.** An earlier draft of `db/schema.sql`'s `workflow_id` column
  comment (and `PRD.md`'s §9.3 data-model table) claimed this column
  "gets cleared if a Temporal admin deletes the execution," describing
  a reconciliation mechanism as if it existed — corrected in P12-1
  after grepping the codebase and finding no code anywhere writes to
  `workflow_id` after `persist_application` sets it. Both claims (the
  terminate gap, and the absence of any reconciliation) were verified
  for real in P12-1, not just reasoned about: a genuine
  `temporal workflow cancel` issued directly via the Temporal CLI
  (bypassing this app entirely) against a live `PENDING_UNDERWRITING`
  application correctly landed the Postgres row on `CANCELLED` (the
  `except asyncio.CancelledError` recovery path works against a real
  server, not just `WorkflowEnvironment`); a `temporal workflow
  terminate` against a second, otherwise-identical application left its
  row permanently stuck at `PENDING_UNDERWRITING` with no error raised
  anywhere — confirmed by checking Postgres afterward, not assumed. A
  human operator today has no query, alert, or job that would ever
  surface this stuck row; finding it requires manually cross-referencing
  Postgres against Temporal's own workflow list.
- No proactive notification (email/SMS) on status change.
- A product type present in `application/schemas.py`'s registry but
  missing from `workflow/task_queues.py`'s `KNOWN_PRODUCT_TYPES` is
  caught immediately by the import-time assert (see "Breaking the
  cycle") — but a product type with **no worker actually polling its
  queue** still leaves applications stuck at `PENDING_UNDERWRITING`
  forever with no error anywhere; the assert can't catch that one, same
  unaddressed gap the reference project documents for its own
  `KNOWN_REVIEW_TYPES`.
- **If this ever needs to scale past one team/one deploy cadence**, the
  module boundaries here are deliberately drawn so any of the seven
  could be extracted into a real service later with the *interface*
  already correct (`service.py`'s function signatures become the new
  HTTP contract) — the work left at that point is standing up the
  process/network boundary and picking a wire format, not rediscovering
  where the seams should be.

## Testing

`tests/unit/` (mirrors module structure, no live services — mock
`document.service`/`workflow.service` calls at the function-call level
for a module under test, the in-process equivalent of the reference
project's `respx`-mocked HTTP calls) and `tests/integration/` (needs the
real local stack, marked `@pytest.mark.integration`).

**One deliberate exception**: a module's own `db.py` tests (e.g.
`customer/db.py`'s `get_or_create`) run against a **real Postgres**, not
a mock — "no live services" is about not needing to fake *other*
modules' HTTP/service calls, not about a module faking its own
database. Idempotency and uniqueness guarantees (e.g. "two concurrent
`get_or_create` calls for the same identifier create exactly one row")
are statements about database state; a mock recording call order can't
verify them, only assert that `service.py` called `db.py` in some
order. These still live under `tests/unit/<module>/` (mirrors module
structure, matches each such task's own DoD, which isn't tagged
"integration-verify") — they just need `DATABASE_URL` pointing at a
database with `db/schema.sql` applied, not the *full* local stack
`tests/integration/` needs (Temporal, Keycloak, Mayan). CI provisions a
real Postgres service container for exactly this reason (see
`.github/workflows/ci.yml`) — these tests are not integration tests in
the "needs the whole stack" sense, but they were never really "unit"
tests in the "no I/O at all" sense either; call them what they are
rather than mislabeling either way.

Prefer `temporalio.testing.WorkflowEnvironment` (time-skipping) over a
real Temporal server for `workflow/`'s workflow/activity tests — inject
a fake/in-memory version of `application/activities.py`'s functions
here rather than hitting the real `applications` table, same "test the
orchestration, not the downstream write" split
`review-approval-temporal`'s own bulk-decision tests use
(`monkeypatching submit_decision() rather than faking Temporal`).

No `tests/contract/` needed anymore (see "Breaking the cycle") — the
`application/schemas.py` assert against
`workflow.task_queues.KNOWN_PRODUCT_TYPES` does that job at import time,
in every test run, for free.

## Build order and session-to-session progress

See **[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)** — a
phased, checkbox-tracked breakdown of every task in the order above,
designed specifically to survive being picked up by a fresh coding-agent
session with no memory of prior sessions. That file is the single
source of truth for *sequencing and progress*; this file stays the
source of truth for *architecture*. Don't let the two drift — if a
session makes a real architectural decision while executing a task from
that plan, it updates this file, not just that one.
