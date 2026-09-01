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
                    (customer_id/account_id passed in as       │
                     opaque strings -- bff_customer resolves    │
                     them via customer/account BEFORE calling   │
                     application/, so application/ never        │
                     imports customer/ or account/ itself)      │
                                                                 │
                        ┌────────────────────────────────────────┘
                        ▼
              bff_customer/          bff_backoffice/
              (imports customer/,    (imports application/,
               account/,              document/, workflow/,
               application/,          customer/, account/ --
               document/, workflow/)  for rendering detail)
                        │                      │
                        └──────────┬───────────┘
                                   ▼
                          app.py / worker_main.py
                        (composition roots -- see
                         "Breaking the application ↔
                         workflow cycle" below)
```

Rules, in order of how often a shortcut will tempt someone to break
them:

- **`customer/` and `account/` never import anything else in this
  codebase.** They're pure data modules — profile, account — with no
  business logic that reaches outside themselves.
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
  `signal_resubmit(...)`. It does **not** import `customer/` or
  `account/` — `customer_id`/`account_id` arrive as opaque strings from
  whichever caller (a BFF) already resolved them.
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

## Modules, in detail

### 1. `bff_customer/` — Customer BFF

Public-facing, mobile-first HTMX (PRD §8.1). No business logic or data
of its own — pure orchestration + presentation, calling straight into
the domain modules' `service.py` functions.

- Owns the customer self-identify session cookie (PRD §7.1) — signed
  Starlette `SessionMiddleware` cookie holding `applicant_identifier`,
  no password, no Redis (nothing token-shaped to store).
- Calls, all as direct in-process function calls:
  `customer.service.identify_or_create(...)`,
  `account.service.find_or_create_for_customer(customer_id)`,
  `application.service.create_application(...)` /
  `resubmit_application(...)` / `list_applications_for_customer(...)`,
  `document.service.upload(...)` / `list_documents(...)`,
  `workflow.service.signal_decision(..., decision="CANCELLED")`.

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
  (single-item and bulk decision signals), `customer.service` +
  `account.service` (render applicant/account detail in the review
  dialog).
- Owns the bulk-selection store — reuse the same Redis instance this
  module already needs for Keycloak sessions.

### 3. `customer/` — Customer module

Owns the customer profile: `customer_id` (UUID), `applicant_identifier`
(the email/phone the customer self-entered), `name`, `email`, `phone`,
`created_at`. Owns the `customers` table — the only module whose code
touches it.

- `service.identify_or_create(applicant_identifier) -> Customer` —
  find-or-create, idempotent (the cookie may call this repeatedly
  across visits).
- `service.get(customer_id) -> Customer`.

### 4. `account/` — Account module

Owns the account entity: `account_id`, `customer_id` (stored as a plain
column, **not** a database foreign key across module boundaries — see
"Data storage" below for why even a same-database FK is deliberately
avoided here), `opened_at`, `status`. Owns the `accounts` table
exclusively. One account per customer for this POC, auto-opened the
first time they start an application — modeling the banking
relationship an application is filed under, matching
`mayan-edms-customer-archive`'s "Account" level (its account-level
document example, Welcome Letter, is a real document produced when an
account relationship opens).

- `service.find_or_create_for_customer(customer_id) -> Account`.
- `service.get(account_id) -> Account`.

### 5. `application/` — Application module

Owns the loan application entity, its `applications` table
exclusively, and the **submission business rule** (the document-
completeness gate, PRD §6.4) — the direct successor to
`review-approval-temporal`'s `workflow/service.py`, scoped to the
application domain specifically now that other domains have their own
modules.

- `service.create_application(customer_id, account_id, product_type,
  payload, ...)` — generates `application_id` (UUID) first, validates
  `payload` against the `product_type`'s Pydantic schema (owned here, in
  `application/schemas.py`), calls `document.service.check_completeness(...)`;
  if satisfied, calls `workflow.service.start_workflow(application_id,
  product_type, payload, ...)`. **The actual `applications` row isn't
  written by this function directly** — `persist_application` is one of
  the three activities in `application/activities.py` (see "Breaking
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
- `service.get(application_id)`,
  `service.list_for_customer(customer_id, page, ...)`,
  `service.list_by_status(status, page, ...)` (staff queues). All three
  support the reference project's paginated, `query_id`-cached list
  pattern — see the `list-pagination-bulk-actions` skill.
- **`activities.py`** — the concrete Temporal activity implementations
  (see "Breaking the cycle" above): `persist_application`,
  `persist_decision`, `persist_resubmit`, one per state-changing
  operation (don't collapse into one generic activity — each has
  different column-update semantics, same reasoning as the reference
  project). This is where `underwriter_name`/`manager_name` actually get
  written, sourced from the `actor_name` the signal carried.

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

- `service.upload(application_id, category, file)` — create-document →
  upload-file (`action_name=replace`) → attach metadata (`customer_id`,
  `account_id`, `application_id`, `category`) → rebuild index. Same
  four-step sequence, and the same gotchas #1-4 below, as the reference
  project's upload path.
- `service.list_documents(application_id)`.
- `service.check_completeness(application_id, product_type) ->
  list[str]` (missing categories, empty if satisfied) — called by
  `application.service` at create/resubmit time.
- `service.preview(application_id, document_id)` — streams the file
  from Mayan for in-app viewing, so neither BFF template needs its own
  Mayan credentials.

Owns `scripts/setup_document_hierarchy.sh` (one-time, not idempotent)
and the Mayan Index Template definition.

### 7. `workflow/` — Workflow module

The direct promotion of `review-approval-temporal`'s `workflow/`
package, deliberately kept **generic** now that `application/` owns the
concrete activity implementations (see "Breaking the cycle" above) —
this module knows Temporal, not loan applications.

- `service.start_workflow(application_id, product_type, payload) ->
  workflow_id`.
- `service.signal_decision(workflow_id, actor_role, decision,
  actor_name, comment)` — called directly by `bff_backoffice`
  (Approve/Reject/RequestMoreInfo) and `bff_customer` (Cancel).
- `service.signal_resubmit(workflow_id, payload)` — called only by
  `application.service`.
- `service.bulk_signal_decision(workflow_ids, decision, actor_name,
  comment)` — fans out `asyncio.gather()` over the single-item signal
  path, same shape as the reference project's `bulk_submit_decision()`,
  cap at `_MAX_BULK_SIZE` (start at 50). Called only by
  `bff_backoffice`.
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
└── <customer_id>
       └── <account_id>
              └── <application_id>
                     ├── Government ID
                     ├── Proof of Income
                     ├── Bank Statements
                     ├── Credit Report
                     ├── Property Appraisal      (mortgage only)
                     └── Vehicle Title/Invoice   (auto_loan only)
```

Directly `mayan-edms-customer-archive`'s own `Customer → Account →
Application` shape. The same **five gotchas** documented in that
project's `docs/document-hierarchy-setup.md` apply unchanged — read
that file before touching the index template or `document/`'s setup
script:

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

### Customer side (`bff_customer/`) — still no real auth

Signed session cookie holding `applicant_identifier`, no password, no
Redis.

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

`underwriter_name`/`manager_name` come from the authenticated session's
`preferred_username`, passed through as `actor_name` on the decision
signal — never a client-submitted free-text field.

**Deliberately not built**: Keycloak protection for Temporal Web UI (the
reference project does this via a `TemporalAdmin` role; out of scope
for "back-office web application authentication" specifically), and
anything Keycloak-related on the customer side (a different,
purpose-built identity problem — PRD §7.1).

## Data storage

**One application database, `loan_onboarding`**, holding all three
domain tables — `customers` (owned by `customer/`), `accounts` (owned
by `account/`), `applications` (owned by `application/`) — plus a
separate `temporal` database for Temporal's own persistence, both in
the **same Postgres container**. This is exactly
`review-approval-temporal`'s own two-database-one-container pattern
(`db/init/*.sh` creates both), just with three app tables instead of
one.

**No foreign keys between `accounts.customer_id` /
`applications.customer_id` / `applications.account_id` and the tables
they reference**, even though they're physically in the same database
now — deliberately, to keep the module boundary meaningful. A same-
database FK would make it trivially easy (and someday tempting, under
deadline pressure) to write a query that joins across module
boundaries directly, silently reintroducing exactly the coupling the
module split exists to prevent. Treat the three tables as if they were
in separate databases even though they aren't; the only sanctioned way
to resolve a `customer_id` into a name is a call to
`customer.service.get(...)`.

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
    └── workflow/
        ├── workflows.py
        ├── worker.py            # bootstrap fn taking an activities list --
        │                        # imports nothing from application/
        ├── task_queues.py
        └── service.py
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

- No real customer authentication on `bff_customer/` (PRD §7.1) — still
  the standout risk; unaffected by `bff_backoffice/`'s real Keycloak
  auth, since they're separate surfaces with separate identity models.
- Module boundaries are enforced by import-linter config, not by a
  process/network boundary — a determined or careless change can still
  violate them if CI isn't actually wired to fail on a violation. Don't
  treat "we organized it into folders" as equivalent to "the boundary is
  enforced" until the lint step exists and is required.
- Same Keycloak-side gaps the reference project has and hasn't closed:
  `verify_aud=False` until a real audience is configured; no caching on
  permission checks (every mutating action is a live UMA exchange).
- No timeout on "wait for Underwriter/Manager decision."
- A Temporal *terminate* (vs. *cancel*) still can't be recovered from
  inside the workflow, structurally — no event is ever delivered to
  catch.
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
