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

**Built (Phase 18, P18-2, "Account closure" below)**: a sixth leaf
module, `notifications/`, same shape as `idgen/` — promoted out of
`bff_customer/notifications.py` (which `bff_customer/routes.py` now
imports from instead) so `account/activities.py` can send an email from
inside a Temporal activity without reaching into a BFF. **Built (Phase
19)**: `application/activities.py` gains this same exception too, so it
can send the Welcome Letter email — see "Applying without being a
customer yet" above and PRD §6.5.

**Also not drawn, and no longer accurate as drawn (Phase 18, P18-4,
built)**: `account/` is no longer a pure leaf — it now has its own
downward edges to `workflow/` (to start/signal `CloseAccountWorkflow`)
and to `notifications/` (for the closure-decision email), both from
`account/service.py`/`account/activities.py`. Left off the diagram
above rather than redrawn, same "keep it readable, state the rule in
the bullet list instead" reasoning `application/activities.py`'s own
not-drawn edge already uses — `account/` still never imports
`customer/`, `document/`, or `application/`, so its position in the
diagram (a box with nothing below it) is wrong in degree, not in kind.

Rules, in order of how often a shortcut will tempt someone to break
them:

- **`customer/` never imports anything else in this codebase except
  `idgen/`, for primary-key generation; `account/` has the same
  exception plus two more (built, P18-4 — see "Account closure" below):
  `workflow/` (to start/signal `CloseAccountWorkflow`, the same role
  `application/` already has) and the new shared `notifications/`
  leaf.** Corrected from an earlier draft of this rule, written before
  either module needed to generate its own id (Postgres did it via a
  `DEFAULT` — see "Data storage"). `idgen/` is deliberately minimal
  enough (no I/O, no other module's types, see next bullet) that
  depending on it doesn't compromise the "pure data module" claim this
  rule otherwise makes about `customer/` — it still has zero business
  logic reaching outside itself, unaffected by `account/`'s two new
  exceptions. `account/` no longer fits that "pure data module"
  description quite as cleanly (`account/activities.py` calls
  `notifications.service` directly), but still never imports
  `customer/`, `document/`, or `application/` — see `account/`'s own
  module section for exactly what the two new exceptions are for and
  why a straightforward widen-the-forbidden-list edit alone wasn't
  enough (the overall layers contract needed restructuring too).
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
- **`risk/` (built, Phase 21) never imports anything else in this
  codebase except `idgen/`.** A leaf module, thinner even than
  `document/`: no NATS connection of its own, just one `httpx.post` to
  the standalone `risk-adapter` service. See "Automated risk assessment
  via NATS" below for the full design.
- **`application/` imports `document/`, `workflow/`, and `risk/` (Phase
  21), never the reverse.** It calls
  `document.service.check_completeness(...)` directly (an in-process
  function call — no HTTP, no serialization boundary beyond normal
  Python objects) and
  `workflow.service.start_workflow(...)`/`signal_decision(...)`/
  `signal_resubmit(...)`. **Built (Phase 19)**: `application/activities.py`
  also gains `notifications/` (the Welcome Letter email) — the same
  justified, single-file-scoped exception `account/activities.py`
  already has from Phase 18, not a general "`application/` may import
  `notifications/`" opening (nothing
  in `application/service.py` needs it). **Built (Phase 21)**:
  `application/activities.py` also calls
  `risk.service.submit_risk_assessment(...)` from a new
  `submit_risk_assessment` activity — same "activities.py is where
  outbound calls to leaf integration modules happen" pattern
  `document/`/`workflow/`/`notifications/` already follow, triggered by
  the workflow's own `execute_activity(...)`-by-name call (see
  "Breaking the cycle"), not by `application/service.py` directly.
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
- **`app.py`, `worker_main.py`, and `reconcile.py` are the only files
  that import from every module** — same "one composition root"
  principle `review-approval-temporal`'s `app.py` already follows, just
  now covering three entrypoints instead of one (see next section for
  why `app.py`/`worker_main.py` need this, and "Document/database
  reconciliation" for why `reconcile.py` does too — it's a
  Phase-15 addition, not part of the original two). **No fourth
  composition root planned for Phase 21, corrected from an earlier
  design pass** — that pass had a `risk_listener_main.py` process inside
  this package subscribing to NATS and calling
  `workflow.service.signal_risk_decision(...)`; once NATS connectivity
  moved entirely to the standalone NATS Adapter service (see "Automated
  risk assessment via NATS"), there's no in-package NATS subscription
  left for a fourth composition root to own — the Adapter signals
  Temporal directly, from outside this package entirely.

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

Most applicants aren't customers yet: `applications.applicant_identifier`
is the durable key at submission time, `applications.customer_id` stays
`NULL` until (and unless) the application is approved, and
`accounts.application_id` (not the reverse) points at the application
that produced the account. `application/activities.py`'s
`persist_decision` provisions the customer/account only on terminal
`APPROVED`, idempotency-guarded via `account.service.get_by_application_id`
(a Temporal retry that finds an account already provisioned skips all
provisioning calls, permanently). The active-account-per-product-type
rule (`accounts.product_type` + a partial unique index) is enforced
proactively at intake by `application.service.get_available_product_types`
(the customer-facing product picker, a hard elimination with no "apply
anyway" override) and at approval time by
`check_decision_allowed`/`check_decision_allowed_bulk`. **Load the
`id-provisioning` skill** for the full provisioning sequence, the exact
write order, and the accepted cross-request race-window gap
(`persist_decision` converts the loser into a clean `REJECTED` rather
than a stuck workflow).

### Returning-customer profile refresh and ID reuse (built — Phase 14)

A customer's profile is seeded/refreshed from each approved
application's own fields (`customer.service.get_or_create`/
`update_profile` — unconditional overwrite on refresh, not
fill-blanks-only), the new-application wizard prefills from an existing
customer, and a returning customer can reuse their on-file Government ID
(`document.service.has_id_photo`, `check_completeness(...,
exclude_categories=...)`) instead of re-uploading — a customer choice
surfaced in the UI, never a silent skip. `resubmit_application`
deliberately does not get this parameter (see `PRD.md` §11). **Load the
`id-provisioning` skill** — it covers this together with "Applying
without being a customer yet" above, since both govern the same
`persist_decision` provisioning code path.

### Account closure (built and live-verified — Phase 18)

A third `accounts.status` value, `CLOSURE_REQUESTED`, plus a second
Temporal workflow (`CloseAccountWorkflow`), lets a customer request
closure of an `ACTIVE` account and either an Underwriter or Manager
decide it (approve → `CLOSED`, reject → back to `ACTIVE`), or the
customer cancel their own still-pending request. This is the "path
back" that stops the product picker's hard elimination (above) from
permanently locking a customer out of a product type once they hold
one — once `CLOSED`, `has_active_account_of_type` stops counting that
account and the product reappears in the picker automatically. It's
also what ended `account/`'s status as a pure leaf module (new edges to
`workflow/` and the new `notifications/` leaf, which was promoted out
of `bff_customer/notifications.py` so a Temporal activity can send
email without reaching into a BFF). **Load the `account-closure`
skill** for the full state machine, the workflow's signals, and the
staff/customer UI surfaces.

### Real email delivery via Gmail SMTP (built and live-verified — Phase 20)

`send_account_closure_decision` and `send_welcome_letter_email` (only —
not `send_verification_code`, which stays fake/dev-only on purpose) can
send real email via Gmail SMTP when `SMTP_USERNAME`/`SMTP_PASSWORD` are
set in the environment; unset, both fall through to the original
`print()` behavior unchanged, so existing unit tests need zero SMTP
configuration. A send failure is caught and logged, never allowed to
fail the Temporal activity. **Load the `gmail-smtp-delivery` skill**
for the full design and the two real gotchas hit live (Docker stdout
buffering hiding every `print()`-based delivery confirmation in this
codebase, and a browser-automation-only `confirm()`-dialog hang).

### Automated risk assessment via NATS (built and live-verified — Phase 21)

A new `PENDING_RISK_ASSESSMENT` workflow state entered right after
`persist_application`, a standalone NATS Adapter service (`risk-adapter`)
as the *only* thing anywhere in this system that depends on the NATS
protocol, KrakenD fronting the Risk-Engine HTTP boundary in both
directions, and a `risk/` leaf module thinner than
`document/mayan_client.py` (one `httpx.post` call, no NATS awareness at
all, no import from `application/`/`workflow/`/`customer/`/`account/`/
`document/`). `LOW`/`HIGH` risk tiers auto-resolve through the existing
`persist_decision` APPROVE/REJECT paths, with `underwriter_name` set to
a fixed `"risk-engine-auto"` marker; `MEDIUM` falls straight through to
today's human `PENDING_UNDERWRITING` queue, unchanged. **Live-verified
end to end through the real customer UI**: three real applications (one
per amount bucket) submitted via a real browser session each resolved
correctly — `LOW` auto-approved (account/Welcome-Letter/document
provisioning all fired), `HIGH` auto-rejected, `MEDIUM` landed in the
Underwriter queue unchanged. **Load the `risk-assessment-nats` skill**
for the full design, including two real gotchas found while wiring
KrakenD up (a `depends_on` cycle in the original task wording; KrakenD
rejecting a `202` response by default) and why NATS connectivity was
moved entirely out of this codebase's own process.

## Modules, in detail

### 1. `bff_customer/` — Customer BFF

Public-facing, mobile-first HTMX (PRD §8.1). No business logic or data
of its own — pure orchestration + presentation, calling straight into
the domain modules' `service.py` functions.

- Owns the customer self-identify session cookie (PRD §7.1) — signed,
  holding `applicant_identifier`, no password, no Redis (nothing
  token-shaped to store). **Its own dedicated cookie, not a slot inside
  `bff_backoffice`'s Starlette `SessionMiddleware` session** — Starlette
  supports only one `SessionMiddleware`/cookie per app, which
  `bff_backoffice`'s Keycloak session id already occupies, so this is
  hand-rolled with `itsdangerous` directly in `bff_customer/identity.py`
  (the same library `SessionMiddleware` uses internally); `.env.example`'s
  `CUSTOMER_SESSION_SECRET_KEY` is distinct from
  `BACKOFFICE_SESSION_SECRET_KEY` for exactly this reason. **Setting this
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
  `workflow.service.signal_decision(..., decision="CANCELLED")`.
  **`account.service` is also called, read-only** — needed once the
  consent-upload feature below needed `account_id` to hand to
  `document.service.upload_consent(...)`:
  `account.service.get_by_application_id(application_id)`, resolving the
  account behind an `APPROVED` application. Still no *write* call —
  account creation happens only inside `application/activities.py` on
  approval, unchanged.
- **Consent upload (resolves PRD §11's "which surface" question for the
  customer half)**: the application detail page, once
  `application.status == APPROVED`, shows a Consent section —
  `_detail_context` resolves the account via
  `account.service.get_by_application_id(...)` and the current consent
  document (if any) via `document.service.list_account_documents(...)`
  filtered to `CATEGORY_CONSENT`. A plain (non-htmx) POST-redirect-GET
  form — same pattern as Cancel/Resubmit, not the one htmx widget this
  module otherwise uses — calls
  `document.service.upload_consent(applicant_identifier, account_id,
  customer_id, file)`; re-uploading versions the same Mayan document
  rather than creating a new one, so the page always links to one
  document regardless of how many times it's been replaced. A dedicated
  `document.service.preview_account_document(account_id, document_id)`
  (new — `preview(application_id, ...)` can't authorize an
  `application_id`-less document) backs the preview link, gated by the
  same applicant-owns-this-application check every other route here
  uses (`_owned_application`, wrapped for this case in a new
  `_owned_account` helper that also enforces `APPROVED` + an account
  actually existing). Live-verified end to end (document versioning,
  metadata attachment, cross-identity `404`s) — full sweep moved to
  `IMPLEMENTATION_PLAN.md`'s Session Log, 2026-09-04 docs-consolidation
  entry.
- **(Phase 14, built)**: the new-application wizard's start step calls
  `customer.service.find_by_identifier(...)` to prefill
  `applicant_name`/`applicant_email`/`applicant_phone`, and — when that
  resolves and `document.service.has_id_photo(customer_id)` is `True` —
  offers Government ID reuse (explicit "Upload a new one instead"
  override) via `create_application(...)`'s `reuse_existing_id_photo`
  parameter. **See the `id-provisioning` skill** for the full design.
- **Built**: the product picker (`GET /apply/new`) calls
  `application.service.get_available_product_types(applicant_identifier)`
  and renders only the product types it returns — a **hard
  elimination** of any product type the applicant already holds an
  `ACTIVE` account for, confirmed with the user as deliberate, with no
  "apply anyway" override. Empty result (a customer active in every
  product type) renders an explanatory message instead of an empty
  list. `POST /apply/new/start` re-runs the same check before accepting
  the submitted `product_type`, so a direct POST past the picker still
  gets refused here — not just silently trusted because the UI happened
  to hide the option. See "One customer, one active account per product
  type" above for the full design and why `check_decision_allowed`'s
  approval-time gate is unaffected by this.
- **Account closure request/cancel**: the application detail page's
  Account closure section
  (`POST /apply/applications/{application_id}/closure/request` /
  `.../closure/cancel`), gated the same way Consent-upload's own account
  section is (`_owned_account`), calling
  `account.service.request_closure(...)` /
  `workflow.service.signal_close_account_cancel(...)`. **See the
  `account-closure` skill** for the full design.

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
- **Consent upload (staff half of PRD §11's "which surface" question,
  the customer half is `bff_customer`'s — see that module's section)**:
  the review dialog (`_detail_dialog.html`) shows a Consent section
  whenever `account` resolves (i.e. the application is `APPROVED`,
  mirroring `_application_detail_context`'s existing `customer`/
  `account` resolution) — an upload/replace form posting to a new
  `/ui/{role}/{application_id}/consent/upload`, backed by the same
  `document.service.upload_consent(...)` `bff_customer` calls. **Gated
  by role only (`_role_dependency`), not a Keycloak permission** —
  deliberately, same reasoning document preview already uses: this
  isn't one of the five decision scopes (Approve/Reject/RequestMoreInfo),
  it's a supplementary action available to anyone who can see the
  application at all. A new `document.service.preview_account_document(...)`
  (shared with `bff_customer`, see `document/`'s section) backs the
  preview link — `/ui/{role}/{application_id}/consent/{document_id}/preview`,
  ownership resolved via `account.service.get_by_application_id(...)`
  rather than trusting a raw `account_id` path param — full
  live-verification sweep (shared Mayan document, role-gating,
  non-`APPROVED` guard) in `IMPLEMENTATION_PLAN.md`'s Session Log,
  2026-09-04 docs-consolidation entry.
- **Account closure review queue**: `GET /ui/{role}/closures` (unpaginated,
  no bulk actions, gated by role only) and
  `POST /ui/{role}/closures/{account_id}/decision`, calling
  `workflow.service.signal_close_account_decision(...)`. **See the
  `account-closure` skill** for the full design.

### 3. `customer/` — Customer module

Owns the customer profile: `customer_id`, `applicant_identifier`
(the email/phone the customer self-entered), `name`, `email`, `phone`,
`created_at`. Owns the `customers` table — the only module whose code
touches it. **A customer row no longer gets created on first visit** —
see "Applying without being a customer yet" above; this module's
create path only fires from inside an approval.

**`customer_id` is `CUS-` followed by a random 9-digit number
(`idgen.service.generate_id("CUS", 9)`), assigned by `db.get_or_create`
at insert time — application-generated, not a Postgres `DEFAULT`** (see
"Data storage" for the full rationale and the shared `idgen/` module,
which every domain module's primary key uses). Because
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
- `service.get_or_create(applicant_identifier, name=None, email=None,
  phone=None) -> Customer` — find-or-create, idempotent. **Called only
  from `application/activities.py`'s `persist_decision`**, at the
  moment an application resolves to terminal `APPROVED` and no existing
  customer was already linked. Not called by `bff_customer`'s identify
  step — the session cookie itself needs no database write at all now,
  it just holds whatever `applicant_identifier` the customer typed.
  **(Phase 14, built)**: `name`/`email`/`phone` seed the row on a
  genuine first create — `persist_decision` passes the approving
  application's own denormalized fields, so a customer's profile no
  longer stays `NULL` forever (a real gap found while designing this).
  An existing row (the `ON CONFLICT ... DO NOTHING` path) is never
  touched by this function, no matter what's passed. See
  "Returning-customer profile refresh and ID reuse" above.
- `service.get(customer_id) -> Customer`.
- **(Phase 14, built)**: `service.update_profile(customer_id, name,
  email, phone) -> Customer` — a write path, called instead of
  `get_or_create` when `persist_decision` finds `applications.customer_id`
  already set (an existing customer's later application being
  approved). Unconditional overwrite, not fill-blanks-only — see
  "Returning-customer profile refresh and ID reuse" above for why.

### 4. `account/` — Account module

Owns the account entity: `account_id`, `customer_id` (stored as a plain
column, **not** a database foreign key across module boundaries — see
"Data storage" below for why even a same-database FK is deliberately
avoided here), `application_id` (same treatment — opaque, not a real FK
— see below), `product_type`, `opened_at`, `status`. Owns the
`accounts` table exclusively. **An account is the outcome of an
approved loan, not something a customer has going into one** — see
"Applying without being a customer yet" above. One customer can hold
**many** accounts (one per approved application, over time) — there's
no one-account-per-customer uniqueness constraint, **but a customer's
`ACTIVE` accounts may never repeat a
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
- **`service.request_closure(account_id, applicant_identifier) -> str`
  (workflow id).** Starts `CloseAccountWorkflow`, raises `AccountNotActive`
  if the account isn't currently `ACTIVE`. This is what ends `account/`'s
  status as a pure leaf module (new edges to `workflow/`/`notifications/`).
  **See the `account-closure` skill** for the full mechanism, including
  why `applicant_identifier` has to travel as an opaque pass-through
  parameter here.
- **`account/activities.py`** — the concrete `CloseAccountWorkflow`
  activities, called by string name: `persist_closure_request(...)`
  and `persist_closure_decision(...)` (idempotency-guarded — a retry
  after the status has already moved past `CLOSURE_REQUESTED` returns
  the already-written status without re-sending the closure-decision
  email). **See the `account-closure` skill** for the full design.

### 5. `application/` — Application module

Owns the loan application entity, its `applications` table
exclusively, and the **submission business rule** (the document-
completeness gate, PRD §6.4) — the direct successor to
`review-approval-temporal`'s `workflow/service.py`, scoped to the
application domain specifically now that other domains have their own
modules.

- `service.get_available_product_types(applicant_identifier) ->
  list[str]` — **read-only**, the proactive half of the
  one-active-account-per-product-type rule (PRD §9.2), called by
  `bff_customer`'s product picker before `create_application` is ever
  reached. See "One customer, one active account per product type"
  above for the full design and why this is a UX filter layered on top
  of `check_decision_allowed`'s approval-time gate, never a replacement
  for it.
- `service.create_application(applicant_identifier, product_type,
  payload, applicant_name, applicant_email, applicant_phone, amount,
  application_id=None)` — **no `customer_id`/`account_id` params** —
  neither is guaranteed to exist yet (see "Applying without being a
  customer yet" above; there is no `account_id` column on `applications`
  at all anymore). **`application_id` is optional, not always
  self-minted**, because `document.service.upload(...)` needs one to
  tag uploads with and Phase 11's flow uploads documents *before*
  calling this function: `bff_customer` mints a provisional
  `application_id` (`idgen.service.generate_id("APP", 9)`, via the
  shared `APPLICATION_ID_PREFIX`/`APPLICATION_ID_LENGTH` constants both
  call sites reference) at the start of its wizard, threads it through
  every upload, then passes that same id here, which this function uses
  verbatim; a caller with no upload-first flow omits it and this
  function mints its own the same way. Either way the returned result
  always carries `application_id` — even on the missing-categories
  branch, which persists no row — so a caller that didn't pre-mint one
  can still learn what id its just-checked documents were tagged under
  and retry once they're uploaded.
  Resolves `customer_id` via the **read-only**
  `customer.service.find_by_identifier(applicant_identifier)` (`None`
  for a new applicant), validates `payload` against the `product_type`'s
  Pydantic schema (owned here, in `application/schemas.py`), calls
  `document.service.check_completeness(...)`; if satisfied, calls
  `workflow.service.start_workflow(application_id, product_type,
  payload, amount, applicant_identifier, applicant_name,
  applicant_email, applicant_phone, customer_id)`. **`amount` travels as
  its own argument, never folded into `payload`** — the workflow needs
  it for PRD §6.3's escalation-threshold check at the Approve
  transition, but stays payload-agnostic otherwise (never inspects
  `payload` itself). `applicant_identifier`/`applicant_name`/
  `applicant_email`/`applicant_phone`/`customer_id` travel the same way,
  purely so `persist_application` (the workflow's first activity) has
  them to write into the row — `workflow/` never inspects any of them.
  **The `applications` row isn't written by this function directly** —
  `persist_application` is one of `application/activities.py`'s four
  activities (see "Breaking the cycle"), invoked by the workflow's own
  `run()` as its first step, same as `review-approval-temporal`'s
  `persist_request`. So `create_application()` reuses the reference
  project's `_wait_until()` pattern after `start_workflow()` — bounded
  poll (~50ms/5s) against `application/db.py`'s own read, since
  `start_workflow()` only confirms Temporal *accepted* the start, not
  that `persist_application` has committed, and the caller (a BFF)
  immediately wants to show the created application. If documents are
  missing, returns the specific missing categories without ever calling
  `workflow.service` — never start a workflow for an incomplete
  application. Also takes `reuse_existing_id_photo: bool = False` — when
  `True` *and* `customer_id` resolves *and*
  `document.service.has_id_photo(customer_id)` is `True`, the
  `check_completeness` call above passes
  `exclude_categories=[document_service.CATEGORY_GOVERNMENT_ID]`
  instead of the bare call. See "Returning-customer profile refresh and
  ID reuse" above for why this can't be a silent, automatic skip.
  `resubmit_application` below does *not* get this parameter — deferred
  on purpose, see that function's own note.
- `service.resubmit_application(application_id, payload)` — same gate
  re-check, then `workflow.service.signal_resubmit(...)` against the
  *existing* `workflow_id` (the same running execution, still waiting
  from `MORE_INFO_REQUESTED` — not a new workflow start). **Does not
  take `reuse_existing_id_photo`, deliberately deferred in Phase 14**
  (see `PRD.md` §11's open question) — a customer resubmitting from
  `MORE_INFO_REQUESTED` who never uploaded a Government ID for *this*
  application still has to upload one, even if they're a known
  returning customer with one already on file.
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
  `customer/` and `account/`. **(Phase 14, built)**: the provisioning
  block's customer step branches on `record["customer_id"]` —
  `customer.service.get_or_create(..., name, email, phone)` when `None`
  (first-ever customer, seeded from this application's own fields),
  `customer.service.update_profile(...)` when already set (an existing
  customer, unconditionally refreshed from this application's fields).
  See "Returning-customer profile refresh and ID reuse" above.
  **(Phase 19, built)**: `activities.py` also gains
  `notifications/` — same activities.py-only-exception shape as
  `customer/`/`account/` already are (unlike `document/`, which
  `application/` as a whole is already allowed to import) — used only
  for the one `notifications.service.send_welcome_letter_email(...)`
  call inside the same provisioning block — see "Applying without being
  a customer yet" above.

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

- `service.upload(applicant_identifier, application_id, category, file,
  customer_id=None)` — create-document → upload-file
  (`action_name=replace`) → attach metadata (`applicant_identifier`,
  `application_id`, `category`, and `customer_id` when given) → rebuild
  index. Same four-step sequence, and the same gotchas #1-4 below, as
  the reference project's upload path. **No `account_id` param** — there's
  no account to tag at upload time at all, uploads happen before
  submission, before any account can possibly exist (see "Applying
  without being a customer yet").
  Neither `applicant_identifier` nor `customer_id` (see "Document
  metadata assignment lifecycle" below) is resolved internally —
  `document/` is a leaf module and never imports `application/`, so
  both are caller-supplied: `bff_customer` already has
  `applicant_identifier` from the session cookie and passes it straight
  through (required); `customer_id` is optional, passed straight
  through too when it's knowable at all (a returning applicant who
  already resolves to an existing customer), `None` for a brand-new
  one.
- `service.list_documents(application_id)`.
- `service.check_completeness(application_id, product_type,
  exclude_categories=None) -> list[str]` (missing categories, empty if
  satisfied) — called by `application.service` at create/resubmit time.
  **A category is satisfied by one or more documents, not exactly
  one** — a customer can upload three separate PDFs under "Bank
  Statements" and the gate is satisfied the same as if they'd uploaded
  one; `upload()` is safe to call repeatedly for the same
  `application_id`/`category`, each call creating a distinct Mayan
  document, never overwriting a prior one. (This resolves "an
  application can have multiple financial-proof documents" — no
  renaming, no new category: "Proof of Income" already works this way
  and always was meant to.) **`exclude_categories` (Phase 14, built)**:
  `application.service.create_application`'s `reuse_existing_id_photo`
  path passes `[CATEGORY_GOVERNMENT_ID]` through it instead of
  requiring a fresh upload from a returning customer who already has
  one on file. See "Returning-customer profile refresh and ID reuse"
  above.
- `service.preview(application_id, document_id)` — streams the file
  from Mayan for in-app viewing, so neither BFF template needs its own
  Mayan credentials.

**Three more managed document types, beyond the submission-gate
categories above** — all system-triggered, none uploaded by a customer
through the application flow:

- **`service.tag_application_documents(application_id, account_id,
  customer_id) -> None`** — **called only from
  `application/activities.py`'s `persist_decision`**, the first of the
  three approve-provisioning document calls (see "Document metadata
  assignment lifecycle" below for the full design). Finds *every*
  document under `application_id` (all categories — Government ID,
  Proof of Income, Bank Statements, Credit Report, and whichever
  product-specific ones apply) and attaches `account_id` + `customer_id`
  to each, rebuilding the index once at the end. Deliberately separate
  from `promote_government_id_to_customer_photo` below, whose own job
  (re-tagging one specific document, possibly stripping a tag from a
  *different* application's document) is orthogonal — this function
  never looks outside `application_id`'s own documents, and re-attaching
  `customer_id` to the Government ID document a second time (once here,
  once via `promote_government_id_to_customer_photo`) is a harmless
  idempotent no-op, not a conflict.
- `service.promote_government_id_to_customer_photo(application_id,
  customer_id) -> None` — **called only from
  `application/activities.py`'s `persist_decision`**, as one more step
  of the same APPROVE-provisioning sequence described in "Applying
  without being a customer yet" (guarded by the same `account_id IS
  NOT NULL` idempotency check — this whole block only runs once).
  **Two paths (Phase 14, built)**: if no Government ID document exists
  under the just-approved `application_id` (the reuse path — nothing
  was uploaded), this is a no-op — **changed from this function's
  original behavior, which unconditionally `raise`d `DocumentNotFound`
  in this case**; that assumed every approved application always has
  its own Government ID document, no longer true once reuse exists. If
  one *does* exist (a fresh upload), it first strips `customer_id`
  metadata from any *other* document already carrying it for this
  customer (via `mayan_client.delete_metadata_entry`), then attaches
  `customer_id` metadata to the new one (**re-tags, does not copy** —
  one Mayan document, findable from both the application's node and the
  customer's `id_photo` node once the index rebuilds) — enforcing
  "exactly one current `id_photo` per customer" for real, which nothing
  did before this (see "Returning-customer profile refresh and ID
  reuse" above for why this is a correction of a real,
  previously-unenforced gap, not new behavior this feature
  introduces). Live-verified: promoting a second, fresh Government ID
  upload for the same customer stripped the first document's
  `customer_id` metadata entry while tagging the second.
- **(Phase 14, built)**: `service.has_id_photo(customer_id) -> bool` —
  **read-only**, a thin wrapper over `list_customer_documents(customer_id)`
  (any result *is* the `id_photo`, per the one-per-customer invariant
  above). Called by `application.service.create_application` to decide
  whether reuse is
  even offerable, and by `bff_customer` to decide whether to show the
  "already on file" choice at all.
- `service.generate_welcome_letter(applicant_identifier, account_id,
  customer_id, applicant_name, product_type, amount) -> DocumentRef` —
  **called only from `application/activities.py`'s `persist_decision`**,
  immediately after `account.service.create_account(...)` succeeds, same
  provisioning block. Renders a simple templated PDF (no live data
  beyond the plain arguments passed in — `document/` doesn't import
  `application/`, `customer/`, or `account/` to go get anything itself)
  and uploads it tagged to the new `account_id` (**and `customer_id`,
  see "Document metadata assignment lifecycle" below** — every other
  document tied to this account/application carries `customer_id` too;
  leaving the Welcome Letter as the one exception would have been
  inconsistent). System-generated, no human in the loop, exactly one per
  account. **`applicant_identifier` is a required field here, not
  optional** — `scripts/setup_document_hierarchy.sh` requires it on
  both document types, since gotcha #1 (leaf conditions don't inherit
  an ancestor's match) means an index branch nested under an applicant
  node needs every descendant document to carry
  `applicant_identifier` metadata itself or it lands under a top-level
  "None" bucket instead of the applicant's own branch — invisible to
  `FakeMayanClient`-backed unit tests, since Mayan's own
  required-metadata enforcement is what actually catches an omission.
  A document created before this was fixed stays orphaned under `None`
  — not retroactively backfilled.
- `service.upload_consent(applicant_identifier, account_id, customer_id,
  file) -> DocumentRef` — **true Mayan document versioning, not a new
  document per call**: if the account already has a "consent" document,
  this uploads a new *file version* of that same document (Mayan
  retains the version history natively) and returns that document's own
  already-attached metadata (including `customer_id`), not a
  freshly-constructed partial `DocumentRef`; if not, it creates the
  document first (attaching `applicant_identifier` too, same reasoning
  and same fix as `generate_welcome_letter` above — the new-version path
  doesn't re-attach metadata at all, so it needs nothing new).
  `customer_id` is attached alongside `account_id`/`applicant_identifier`
  on the create-first-version path, same as `generate_welcome_letter` —
  consent is an account-level document, exactly like Welcome Letter, so
  both carry `customer_id` for the same reason (Customer Index's
  `<customer_id>/<account_id>/<category>` branch needs it to nest either
  one under the customer). Not restricted to one caller — either BFF can
  call it once `account_id`/`customer_id` exist (both already import
  `document/`): `bff_customer`'s own application detail page, once
  `APPROVED`, plus `bff_backoffice`'s review dialog for staff to
  upload/replace on the customer's behalf — see both modules' sections
  below. Both write to the same document; a replace from either surface
  is immediately visible from the other.
- `service.preview_account_document(account_id, document_id) ->
  DocumentStream` — the account-scoped sibling of `preview(application_id,
  document_id)`, needed because an account-level document (Consent,
  Welcome Letter) carries no `application_id` at all, so `preview`
  itself can never authorize a request for one. Shares the actual
  Mayan-streaming call with `preview` via a private `_stream_document`
  helper; only the ownership check differs.
- `service.list_customer_documents(customer_id) -> list[DocumentRef]`,
  `service.list_account_documents(account_id) -> list[DocumentRef]` —
  for staff/customer viewing (`id_photo`; `welcome_letter` + `consent`
  respectively).

Owns `scripts/setup_document_hierarchy.sh` (one-time, not idempotent)
and the Mayan Index Template definitions (three — Customer/Account/
Application Index, see "Document hierarchy" below).

### 7. `workflow/` — Workflow module

*(Rendered version of the state machine:
[`docs/diagrams/loan-workflow-state-machine.md`](docs/diagrams/loan-workflow-state-machine.md).)*

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
  `start_workflow`'s caller through to the activity call.
  `applicant_name`/`applicant_email`/`applicant_phone` are in this
  signature because `persist_application`'s activity input needs these
  three denormalized fields to write into the row, same as
  `applicant_identifier`/`customer_id` — there's no other path for them
  to reach `persist_application` once `payload` stays
  product-specific-fields-only per `application/`'s own module section
  below.
- `service.signal_decision(workflow_id, actor_role, decision,
  actor_name, comment)` — called directly by `bff_backoffice`
  (Approve/Reject/RequestMoreInfo) and `bff_customer` (Cancel).
- `service.signal_resubmit(workflow_id, payload)` — called only by
  `application.service`.
- `service.bulk_signal_decision(workflow_ids, actor_role, decision,
  actor_name, comment)` — fans out `asyncio.gather()` over the
  single-item signal path, same shape as the reference project's
  `bulk_submit_decision()`, cap at `_MAX_BULK_SIZE` (start at 50).
  Called only by `bff_backoffice`. **Takes `actor_role`** for the same
  reason the single-item `signal_decision` above does (which role is
  deciding is what `LoanApplicationWorkflow._resolve_transition`
  validates against the application's current state) — every
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
  reference project's `REVIEW_TYPE`, same reasoning. **Gained a second,
  sibling bootstrap for account closure**:
  `_build_account_closure_worker`/`run_account_closure_worker`, a
  single non-product-type-keyed `Worker` (no per-product-type fan-out —
  see `task_queue_for_account_closure()`) registering
  `CloseAccountWorkflow` plus whatever activities `worker_main.py`
  supplies (`account/activities.py`'s two), under the same `WORKER_MODE`
  semantics as `_build_workers`/`run_worker`. Deliberately a separate
  function rather than a parameter grafted onto the existing one — the
  "one `Worker` per product type" shape `_build_workers`/`run_worker`
  are built around doesn't apply to a single fixed queue.
  `worker_main.py` (the composition root) `asyncio.gather()`s
  `run_worker(...)` and `run_account_closure_worker(...)` together in
  one process, one `WORKER_MODE` value governing both — this is exactly
  the kind of change (`asyncio.gather`ing a new call into an existing
  composition root's `main()`) whose test-suite gotcha
  (`IMPLEMENTATION_PLAN.md`'s Phase 18, P18-5: an unmocked
  `run_account_closure_worker` hung every existing `worker_main.main()`
  test indefinitely, since it connects to a real Temporal server) is
  worth knowing about before touching this file again.
- **`task_queues.py`** — `KNOWN_PRODUCT_TYPES` + queue naming, the
  canonical registry `application/schemas.py` asserts against at import
  time (see "Breaking the cycle").
- **No Postgres table of its own.** Temporal's own persistence (the
  `temporal` database) is managed by the Temporal server container, not
  by this module's code.

### 8. `risk/` — Risk assessment module (built and live-verified — Phase 21)

A thin leaf module (one `httpx.post` call to the NATS Adapter, no NATS
awareness of its own) — see "Automated risk assessment via NATS" above
and **load the `risk-assessment-nats` skill** for the full design,
including this module's own code shape.



## Document hierarchy

Three separate Mayan Index Templates (Customer/Account/Application
Index), each rooted at a different one of the three entity ids a
document can carry, with a strict **exclusive-placement** model — a
document lives at exactly one leaf per index, the deepest entity it's
actually tied to. Never read the Index Template tree from application
code (`check_completeness`/`list_*_documents` all query Mayan's
metadata search API directly) — the tree is async (Celery-driven) and
exists purely for staff to browse visually. **Load the
`document-hierarchy` skill** for the full tree diagrams, the five
index-template gotchas (inherited from `mayan-edms-customer-archive`),
and the "overlapping `rebuild/` calls race Mayan's reset-then-rebuild
sequence" operating rule.

## Document metadata assignment lifecycle

Five rules govern exactly when a document gains
`applicant_identifier`/`application_id`/`account_id`/`customer_id`:
both are attached at upload time; `customer_id` is attached at upload
time only when the applicant already resolves to an existing customer;
on approval, every document under the application gains `account_id` +
`customer_id` (`tag_application_documents`); the customer-level
Government ID copy (`promote_government_id_to_customer_photo`) is a
genuine *second* Mayan document, not a re-tagged original, so the
application's own copy stays exclusively owned by its application; and
a rejected/cancelled/still-pending application's documents never get
`account_id` at all, by construction. **Load the `document-hierarchy`
skill** for the full rule-by-rule design, two real Mayan-only bugs
found live (a document type can only carry metadata types it's been
explicitly associated with; Mayan rejects a second `POST` for a
metadata type a document already carries), and the `reconcile.py`
correctness fix this redesign required.

## Identity

Two completely different mechanisms — see PRD §7 for the product
framing.

### Customer side (`bff_customer/`) — email-verified, still no password

Signed session cookie holding `applicant_identifier`, no password, no
Redis — but the cookie is only ever set after the applicant proves
ownership of that identifier via a 6-digit one-time code, not on the
strength of just typing it in (this POC's standout risk before the fix
— see Known Gaps below). See `bff_customer/identity.py`'s module
docstring for the full design
(a second, short-lived signed cookie holding the code's *hash*, not
the code itself, still no Redis -- the same "no server-side state"
philosophy this module already had, just applied to a second cookie)
and `notifications/service.py`'s for why delivery is fake/dev-only
in this POC (promoted there from `bff_customer/notifications.py` in
Phase 18, P18-2 — see "Account closure").

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
strings — not database-generated `UUID`s.** Each of the three entity
types gets its own prefix plus a random 9-digit number, generated by a
shared leaf module, `idgen/` (see "Module dependency graph"):

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

## Document/database reconciliation

`loan_onboarding` (Postgres) and Mayan are two completely independent
systems with no foreign key, no cascade, and no transaction spanning
them — confirmed live: the three domain tables were cleared by
something outside this app entirely while Mayan's documents were
unaffected, leaving real orphaned documents with nothing able to detect
it. `loan_onboarding/reconcile.py` (a third composition root, alongside
`app.py`/`worker_main.py`) walks every Mayan document and checks
whether its primary-owner Postgres row still exists (`--report` prints
findings, `--fix` also trashes orphans and strips stale `customer_id`
tags). Cascade-on-delete (deleting an app-owned document when *this
app itself* deletes a customer/account/application) is deliberately not
built — there is no delete operation for any of these three entities in
this codebase today, and whether one should ever exist is an open
product question, not a build gap. **Load the `document-reconciliation`
skill** for the full orphaned-vs-stale-tag distinction and the live
27-document verification sweep.

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
    ├── reconcile.py             # composition root: cross-references Mayan
    │                          # documents against Postgres, see
    │                          # "Document/database reconciliation"
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
    │   ├── activities.py        # built, P18-4 -- concrete
    │   │                        # CloseAccountWorkflow activities, see
    │   │                        # "Account closure"
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
    ├── idgen/
    │   └── service.py           # generate_id(prefix, length) -- the only
    │                             # function in this module, zero I/O, zero
    │                             # state; every module that assigns a
    │                             # primary key imports this one
    ├── notifications/           # built, P18-2 -- promoted out of
    │   └── service.py           # bff_customer/notifications.py, see
    │                             # "Account closure"
    └── risk/                     # built, Phase 21 --
        └── service.py            # one httpx POST, no NATS client here --
                                   # see "risk/ -- Risk assessment module"
```

**Also sitting outside the `loan_onboarding` Python package entirely,
built in Phase 21**: `mock_risk_engine/` (the standalone simulated
external Risk Engine, HTTP-only) and `risk_adapter/` (the NATS Adapter
— the sole owner of NATS connectivity in this whole system, plus its
own small Temporal client) — both deliberately not part of this
package, same "a real external system this codebase doesn't own"
treatment Mayan and Keycloak already get (see "Automated risk
assessment via NATS"). No `risk_listener_main.py` — an earlier design
pass planned one, but there's no in-package NATS subscription left for
a composition root to own once the Adapter took over that job entirely.
Also built in Phase 21, at the repo root (sibling to
`loan_onboarding/`): `krakend/krakend.json`, the plain HTTP↔HTTP gateway
config fronting the Risk-Engine boundary.

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
- **Built, Phase 21**: `nats` (core pub/sub, no JetStream — see
  "Automated risk assessment via NATS"), `risk-adapter` (the NATS
  Adapter — sole owner of NATS connectivity, `depends_on: [nats,
  temporal]`), `krakend` (fronting the Risk-Engine boundary,
  `depends_on: [risk-adapter]`), `mock-risk-engine` (HTTP-only, no NATS
  — `depends_on: [krakend]`, since it calls the Adapter's webhook
  *through* KrakenD). **`depends_on` deliberately doesn't match a
  literal reading of "each service depends on the other two it talks
  to"** — `risk-adapter`↔`krakend` and `krakend`↔`mock-risk-engine`
  would each form a real cycle Docker Compose rejects outright; every
  cross-service call here is lazy (made well after startup), so neither
  direction was functionally needed anyway. See the `risk-assessment-nats`
  skill for the full story.

Every env var pointing at another container uses its Docker-internal
service name — same discipline the reference project already documents
for `KEYCLOAK_ISSUER`.

## Known gaps to state explicitly once built

**Load the `known-gaps-and-gotchas` skill before touching schema,
workers, or running ad hoc scripts against the local stack** — it's the
single source of truth for every accepted limitation and every real
operational gotcha hit while building this project, including: this
project has no schema migration tooling (`db/schema.sql` changes never
apply to an already-running `db` volume — this bit for real after Phase
18); a local `worker_main.py` process and the dockerized
`worker-workflow`/`worker-activity` containers silently race each other
if both are left running against different databases; the
active-account-per-product-type rule doesn't count `CLOSURE_REQUESTED`
as active, which sets up a real, unhandled `UniqueViolationError` crash
on a specific reject-after-second-approval sequence; a Temporal
*terminate* (vs. *cancel*) still can't be recovered from inside the
workflow, structurally; no timeout on "wait for Underwriter/Manager
decision"; and module boundaries are enforced only by import-linter
config, not by a process/network boundary, so don't treat "we organized
it into folders" as equivalent to "the boundary is enforced" until the
lint step exists and is required in CI.

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

**Two real, live-hit testing hazards to know about before running these
against a local `docker compose` stack you're also using for manual
verification**: pointing `DATABASE_URL` at the compose stack's own
`loan_onboarding` (instead of a separate `loan_onboarding_test`) lets
these tests' cleanup fixtures silently wipe the live stack's data, and
stacking up ad hoc `docker exec <container> python3 -c
"asyncio.run(...)"` one-off scripts can exhaust Postgres's
`max_connections` via `asyncpg`'s default `min_size=10` pool. **Load
the `known-gaps-and-gotchas` skill** for the full mechanism and recovery
steps for both.

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

**`tests/integration/test_document_service.py` (new) is this project's
first integration test to touch real Mayan** — every prior Mayan
verification (Phases 5, 14, 15, 16, the index redesigns) was a
documented manual sweep instead, and `test_end_to_end_workflow.py`
(the only other file in `tests/integration/`) deliberately stubs
`document_service` out to avoid needing Mayan at all. Added
specifically because `FakeMayanClient` can't catch what only real Mayan
enforces — a document type rejecting a metadata attach it was never
associated with (P16-4's real bug) and Mayan's own
reject-on-duplicate-attach behavior are exactly the two bugs this
project already hit for real that no unit test caught. Covers
`upload_consent`/`preview_account_document` (the account-level document
support the consent-upload feature added): a real create, a real
same-document re-version (not a duplicate, confirmed via
`list_account_documents` staying at one document), and a real streamed
download returning the latest version's actual bytes. Uses synthetic,
uuid4-based `account_id`/`customer_id` values with no real Postgres row
behind them — `document/` never imports `application/`/`account/`/
`customer/`, so it doesn't care whether they resolve to anything, only
that they're stable strings to tag and filter on; a `cleanup_documents`
fixture trashes every document a test creates afterward, same
soft-delete this codebase uses everywhere else. Needs
`docker compose up -d mayan` plus
`MAYAN_BASE_URL`/`MAYAN_SERVICE_ACCOUNT_USERNAME`/
`MAYAN_SERVICE_ACCOUNT_PASSWORD` set (same values `.env` already
carries) — run it on its own
(`pytest tests/integration/test_document_service.py -m integration`),
not mixed into one invocation with `tests/unit`: doing that once in the
same session produced an unrelated flake in
`tests/workflow/test_workflows.py`'s embedded time-skipping Temporal
test server that didn't reproduce running either suite alone,
consistent with CI's own separation (`.github/workflows/ci.yml` only
ever runs `pytest tests/unit`, never `tests/integration`).

## Build order and session-to-session progress

See **[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)** — a
phased, checkbox-tracked breakdown of every task in the order above,
designed specifically to survive being picked up by a fresh coding-agent
session with no memory of prior sessions. That file is the single
source of truth for *sequencing and progress*; this file stays the
source of truth for *architecture*. Don't let the two drift — if a
session makes a real architectural decision while executing a task from
that plan, it updates this file, not just that one.
