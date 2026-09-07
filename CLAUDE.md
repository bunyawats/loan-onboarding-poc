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
- **`risk/` (planned — Phase 21, not yet built) never imports anything
  else in this codebase except `idgen/`, if it ends up needing one.** A
  leaf module, same shape as `document/`: it knows nothing about loan
  applications, just a NATS connection and a subject-naming convention.
  See "Automated risk assessment via NATS" below for the full design.
- **`application/` imports `document/`, `workflow/`, and (planned,
  Phase 21) `risk/`, never the reverse.** It calls
  `document.service.check_completeness(...)` directly (an in-process
  function call — no HTTP, no serialization boundary beyond normal
  Python objects) and
  `workflow.service.start_workflow(...)`/`signal_decision(...)`/
  `signal_resubmit(...)`. **Built (Phase 19)**: `application/activities.py`
  also gains `notifications/` (the Welcome Letter email) — the same
  justified, single-file-scoped exception `account/activities.py`
  already has from Phase 18, not a general "`application/` may import
  `notifications/`" opening (nothing
  in `application/service.py` needs it). **Planned (Phase 21)**:
  `application/activities.py` will also call
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
  Phase-15 addition, not part of the original two). **Planned (Phase
  21)**: a fourth composition root, `risk_listener_main.py` — imports
  `risk/` and `workflow/` only (not every module, unlike the other
  three), since its one job is turning an inbound NATS decision message
  into a `workflow.service.signal_risk_decision(...)` call, the same
  role `bff_backoffice`'s decision routes already play for a human
  decision, just triggered by a message instead of an HTTP POST.

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
  3. Call `document.service.tag_application_documents(application_id,
     account_id, customer_id)`,
     `document.service.promote_government_id_to_customer_photo(application_id,
     customer_id)`, and `document.service.generate_welcome_letter(applicant_identifier,
     account_id, customer_id, applicant_name, product_type, amount)` —
     see `document/`'s module section and "Document metadata assignment
     lifecycle" below for what each does and why all three, not just
     one. **A retry that finds an account already provisioned (the check
     above) skips all three of these calls entirely, permanently** — a
     smaller, manually-recoverable gap (some documents short a few
     metadata fields, no Welcome Letter) than a duplicated account, and
     consistent with this project's existing rare-enough-to-accept-for-
     a-POC stance elsewhere in this section. (This is the same tradeoff
     an earlier draft of this file already accepted; only the mechanism
     that makes the retry recognize "already provisioned" has moved,
     from a column on `applications` to the `accounts` row itself.)
     **Built (Phase 19)**: a fourth call in this same block,
     `notifications.service.send_welcome_letter_email(applicant_identifier,
     account_id, product_type, amount)`, right alongside
     `generate_welcome_letter` — the second of PRD §4's two narrow
     exceptions to the no-proactive-notification non-goal (the first,
     already built, is the account-closure-decision email — see
     "Account closure" below). Deliberately placed inside this same
     `existing_account is None` guard, not a separate check: same
     "a retry skips it, permanently, rather than resending" tradeoff
     this bullet already accepts for the other three calls, not a new
     idempotency mechanism — **confirmed live, not just in theory**,
     during P19-3's own verification sweep: an unrelated local-environment
     mistake (a worker process missing its Mayan env vars) made a real
     retry take exactly this skip path, proving the accepted tradeoff
     holds for the new call too, not only the original three. Requires a
     new import edge, `application/activities.py` → `notifications/` —
     `application/`
     doesn't have this exception yet (only `account/` does, from Phase
     18); no `.importlinter`/`pyproject.toml` layers restructuring
     needed to add it, unlike `account/`'s own case, since
     `application/` already sits above the bottom `idgen | notifications`
     tier in the existing layers contract.
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
- **The same rule is now also enforced proactively, at intake, not just
  at approval — a UX addition, not a second source of truth.**
  `application.service.get_available_product_types(applicant_identifier)
  -> list[str]` resolves the customer via the existing read-only
  `find_by_identifier`, then filters `workflow.task_queues.KNOWN_PRODUCT_TYPES`
  down to the ones `account.service.has_active_account_of_type(customer_id,
  product_type)` returns `False` for — the exact same read
  `check_decision_allowed` already relies on, so the two can never
  disagree. A first-time applicant (no resolvable customer yet) gets
  every product type back, since there's nothing yet to conflict with.
  `bff_customer`'s product picker (`GET /apply/new`) calls this to
  render only the eligible types — a **hard elimination, confirmed with
  the user as deliberate: no "apply anyway" override.** A customer
  already active in every product type sees an explanatory message
  instead of an empty list. `POST /apply/new/start` re-runs the same
  check server-side before accepting the submitted `product_type` — the
  picker only hides the option, it doesn't stop a direct POST past it.
  **`check_decision_allowed`'s approval-time gate is deliberately left
  completely unchanged and un-simplified by this** — it remains the
  sole authoritative enforcement; this new function only ever narrows
  what the customer is *offered*, it never replaces the backstop that
  catches a bypass. See `PRD.md` §8.1/§9.2 for the product framing and
  §11 for the real gap this surfaced: there is still no way to ever
  transition an `accounts.status` row from `ACTIVE` back to `CLOSED` in
  this codebase, so combined with this hard elimination, a customer
  approved once for a product type can now never apply for that type
  again through the UI — raised directly by the user as a "close
  account" feature worth designing, not designed or built here.
- **Document upload/completeness-check at submission time never needs
  an `account_id`**, since no account can possibly exist before
  submission — `document.service.upload(...)` attaches
  `applicant_identifier`/`application_id`/`category` (plus `customer_id`
  when the applicant already resolves to a customer) regardless. Post-
  approval, every document under the application gains `account_id` too
  (`tag_application_documents`, "Document metadata assignment
  lifecycle" below) — the account-level metadata isn't gone, it's just
  attached later, by a different code path (provisioning, not
  submission). See "Document hierarchy" below for how staff actually
  browse these fields — corrected there from an
  `applicant_identifier`-rooted single index to three separate
  entity-rooted indexes; that redesign changed nothing about *when*
  metadata gets attached, only how it's organized for browsing.

### Returning-customer profile refresh and ID reuse (built — Phase 14)

Built and live-verified. This section describes the design behind
`IMPLEMENTATION_PLAN.md`'s Phase 14, written first per this project's
own convention (architecture doc before implementation). Confirmed with
the user as a deliberate enhancement to "Applying without being a
customer yet" above, not a correction of it — everything in that
section still holds; this adds two things on top: (1) a customer's
profile actually gets populated and kept current, instead of staying
permanently `NULL`, and (2) a *returning* customer gets a materially
better experience — a prefilled form and the option to skip
re-uploading a Government ID they already have on file.

- **Customer profile used to be write-once-never-filled, a real gap
  found while designing this**: `customer.service.get_or_create(applicant_identifier)`
  used to take *only* the identifier — `customers.name`/`email`/`phone`
  stayed `NULL` forever, for every customer. Phase 14 fixed this two
  ways:
  1. `customer.service.get_or_create` gains three new parameters —
     `get_or_create(applicant_identifier, name, email, phone) ->
     Customer` — so the *first* approval that creates a customer row
     seeds it from that application's own denormalized
     `applicant_name`/`applicant_email`/`applicant_phone` instead of
     leaving the profile blank.
  2. A new `customer.service.update_profile(customer_id, name, email,
     phone) -> Customer` write path, called instead of `get_or_create`
     when `persist_decision` finds `applications.customer_id` already
     set (an existing customer's *later* application being approved).
     **Policy: unconditional overwrite, not fill-blanks-only** — the
     most recently *approved* application's submitted details always
     win. This is the direct, consistent reading of "Denormalized
     applicant fields, on purpose" above: `application/` is what was
     submitted at the time, `customer/` is the *current* profile, and
     an approved application is exactly the trust signal that makes
     "current" worth updating.
  3. `application/activities.py`'s `persist_decision` branches on
     `record["customer_id"]` to pick which of the two to call — both
     already sit in the one file in `application/` allowed to import
     `customer/`, so this is a same-file branch, not a new import.
- **Returning-customer form prefill**: `bff_customer`'s new-application
  wizard start step calls the existing **read-only**
  `customer.service.find_by_identifier(applicant_identifier)` (already
  used elsewhere for "welcome back" copy) and, if it resolves,
  prefills `applicant_name`/`applicant_email`/`applicant_phone` —
  still editable, and a correction made here is exactly what feeds
  back into `update_profile` above on this application's own eventual
  approval. No new `service.py` function needed; this is purely a
  `bff_customer/routes.py` + template change.
- **ID reuse can't be "re-tag the old document into the new
  application" — a real Mayan constraint rules that out, confirmed
  against `mayan_client.py`'s own `attach_metadata`/
  `update_metadata_entry`**: Mayan holds exactly **one value per
  (document, metadata_type)** — a document's `application_id` metadata
  entry can be created once and later *updated* in place, never
  duplicated. Re-pointing an existing `id_photo` document's
  `application_id` to a brand-new application would silently unfile it
  from the *old* application's own `<application_id> -> Government ID`
  index leaf — a real correctness break, not a cosmetic one. So reuse
  works the other way: **the new application's document gate is told
  to skip Government ID, not that some other document already
  satisfies it.**
  1. `document.service.check_completeness` gains an optional
     `exclude_categories: list[str] | None = None` parameter —
     `required = [c for c in REQUIRED_CATEGORIES[product_type] if c
     not in (exclude_categories or [])]`. A small, general parameter
     rather than a Government-ID-specific special case, even though
     Government ID is the only category this call site excludes today.
  2. A new **read-only** `document.service.has_id_photo(customer_id)
     -> bool` (a thin wrapper over the existing
     `list_customer_documents(customer_id)` — any result *is* the
     `id_photo`, per the one-per-customer invariant below).
  3. `application.service.create_application(...)` gains a new
     `reuse_existing_id_photo: bool = False` parameter. When `True`
     *and* the applicant resolves to an existing customer (via the
     read-only `find_by_identifier` lookup this function already does)
     *and* `document.service.has_id_photo(customer_id)` is `True`, it
     calls `check_completeness(application_id, product_type,
     exclude_categories=[document_service.CATEGORY_GOVERNMENT_ID])`
     instead of the bare call. **Reuse is a customer choice surfaced in
     the UI, not an automatic silent skip** — the wizard shows "We
     already have a Government ID on file for you" with an explicit
     "Upload a new one instead" override once `find_by_identifier` +
     `has_id_photo` both resolve true; nothing is skipped unless the
     customer actually leaves reuse selected.
  4. `resubmit_application` does **not** get this parameter in Phase
     14 — deliberately deferred (see `PRD.md` §11's open questions);
     a customer resubmitting from `MORE_INFO_REQUESTED` who never
     uploaded a Government ID for *this* application still has to
     upload one, even if they're a known returning customer. A smaller
     gap than leaving reuse unbuilt entirely, and resubmit's document
     gate re-check already only fires when the customer touches
     documents at all (see `application/`'s module section below).
- **`id_photo` is refreshed by a later approved application's fresh
  upload, not fixed forever — corrects a real, previously-undocumented
  gap found while designing this feature, not something this feature
  introduces.** `PRD.md` §6.5 used to state "the first one stands," but
  `document.service.promote_government_id_to_customer_photo` never
  actually enforced that — it unconditionally re-tagged whatever
  Government ID document existed under the just-approved application,
  with no check for a prior `id_photo`. This was invisible before Phase
  14 because nothing exercised a *second* approval, for an
  already-a-customer applicant, with a fresh Government ID upload —
  exactly the case this phase makes reachable. Phase 14 resolved the
  tension in favor of the new intent (refreshable, not frozen) and made
  the enforcement real:
  1. If no Government ID document exists under the just-approved
     `application_id` (the reuse path — nothing was uploaded), `promote_government_id_to_customer_photo`
     returns early, a no-op — **changed from this function's original
     behavior, which `raise`d `DocumentNotFound`** in this case; that
     exception was written under the old assumption that every approved
     application always has its own Government ID document, no longer
     true once reuse exists.
  2. If one *does* exist (a fresh upload — either a first-time
     applicant, or a returning customer who chose "upload a new one
     instead"), the function first finds any *other* document
     currently carrying this customer's `customer_id` metadata
     (excluding the one about to be tagged) and strips that metadata
     entry via a new `mayan_client.delete_metadata_entry(document_id,
     metadata_entry_id)` (a plain wrapper over the already-generic
     `self.delete(...)` — Mayan's create/update metadata calls already
     exist in `mayan_client.py`, delete was simply never needed until
     now), *then* tags the new one. **Exactly one current `id_photo`
     per customer, enforced for real** — a reused-ID application
     leaves the existing one untouched (step 1); a fresh-upload
     application supersedes it (step 2).
  3. `persist_decision` itself doesn't grow a new branch — it still
     calls `promote_government_id_to_customer_photo(application_id,
     customer_id)` unconditionally on every terminal `APPROVED`
     transition, same as today; the no-op-vs-supersede behavior above
     lives entirely inside `document/service.py`.

### Account closure (built and live-verified — Phase 18)

Built and live-verified against the real stack (P18-1 through P18-8) —
full build history, task-by-task DONE notes, and the final end-to-end
staff/customer verification sweep live in `IMPLEMENTATION_PLAN.md`'s
Phase 18 section and Session Log, not here. Raised directly by the user
as the natural follow-up to the product-picker's hard elimination (see
"Modules, in detail" → `application/`'s `get_available_product_types`):
once that shipped, a customer approved for a product type could never
apply for that type again, because nothing in this codebase could ever
move an `accounts.status` row from `ACTIVE` back to `CLOSED`. Three
scoping decisions were confirmed with the user before any design work:
(1) balance verification is a **staff attestation** (a comment field
staff fills in confirming the balance is zero), not a real ledger
computation — this POC has no ledger at all, consistent with PRD §4's
disbursement/servicing non-goal; (2) the closure-decision email is a
**narrow, deliberate reversal** of PRD §4's "no proactive notification"
non-goal, scoped to this one decision only, not a general notification
feature; (3) either the **existing `Underwriter` or `Manager` Keycloak
role** may decide a closure request — no new Keycloak Resource/Scope/
Policy/Permission, no escalation tier (there's no dollar amount to
escalate on for a closure, unlike the loan-approval threshold).

- **State machine — a third `accounts.status` value, not a separate
  entity.** `ACTIVE` → (customer requests) → `CLOSURE_REQUESTED` →
  (staff decides) → `CLOSED` (approved) or back to `ACTIVE` (rejected —
  a closure request has no terminal "rejected" status of its own; the
  account simply resumes being usable). The customer may also `CANCEL`
  their own still-`CLOSURE_REQUESTED` request back to `ACTIVE`, same
  shape as an application's existing Cancel action. New nullable
  `accounts` columns: `closure_workflow_id`, `closure_requested_at`,
  `closure_decision_comment` (the staff attestation text),
  `closure_decided_by`, `closure_decided_at` — only the *current*
  request's data is kept, same "a later request overwrites rather than
  preserves history" simplification `applications`' own decision
  columns already use.
- **A second Temporal workflow, `CloseAccountWorkflow`**, in
  `workflow/workflows.py` alongside `LoanApplicationWorkflow` — same
  "generic orchestration, concrete activities live in the owning domain
  module" split `application/`/`workflow/` already established (see
  "Breaking the application ↔ workflow cycle"). A single dedicated task
  queue (`task_queue_for_account_closure()`, not product-type-keyed —
  closure review doesn't vary by product), registered in
  `worker_main.py` alongside the per-product-type workers. One
  execution per closure *request*, not per account — a rejected or
  customer-cancelled request reverts the account to `ACTIVE` and
  completes; a later request starts a brand-new execution under the
  same deterministic `account-closure-<account_id>` workflow id (safe
  because `request_closure` is only reachable while `ACTIVE`, so
  there's never a live execution to collide with). Reuses
  `LoanApplicationWorkflow`'s own role/decision constants rather than a
  parallel taxonomy. Two signals: `submit_decision(actor_role,
  decision, actor_name, comment)` (staff only) and a no-argument
  `cancel()` the customer can send while still `CLOSURE_REQUESTED`,
  guarded by the same synchronous `_claim_transition()` single-writer
  pattern `LoanApplicationWorkflow` uses. Calls `persist_closure_request`/
  `persist_closure_decision` by string name, exactly like
  `LoanApplicationWorkflow` calls `persist_application`/`persist_decision`.
- **`account/` stops being a leaf module — a real, deliberate change to
  the dependency graph, not an oversight.** It gained two exceptions to
  "never imports anything else in this codebase": `workflow/` (to
  start/signal `CloseAccountWorkflow`, the same justified exception
  `application/` already has) and the new shared `notifications/` leaf
  (below). `customer/` is unaffected. The concrete activities
  (`persist_closure_request`, `persist_closure_decision` — the actual
  `UPDATE accounts SET status = ...` and the email trigger) live in a
  new `account/activities.py`, the same role `application/activities.py`
  already plays; `account/service.py` gained `request_closure(account_id,
  applicant_identifier)` — see `account/`'s own module section below
  for why the second, opaque `applicant_identifier` parameter turned
  out to be necessary. Neither BFF needed a thin `account.service`
  decision-signal wrapper — both call
  `workflow.service.signal_close_account_decision`/
  `signal_close_account_cancel` directly. `.importlinter`'s layers
  contract had to move `account/` to its own layer, below `customer |
  document` and above `workflow/`, rather than just widening its
  forbidden-imports list — a `layers` contract checks same-bar modules
  for mutual independence, and `account/` sat on the same bar as
  `workflow/` before this.
- **A new shared leaf module, `notifications/`** (same "zero dependency
  on anything else in this codebase" shape as `idgen/`), promoted out
  of `bff_customer/notifications.py` — needed because
  `persist_closure_decision` sends an email from inside a Temporal
  *activity*, and `account/activities.py` cannot reach into
  `bff_customer` (wrong direction — BFFs consume domain modules, never
  the reverse). `bff_customer`'s OTP flow now imports it too, so both
  share one mechanism instead of duplicating it. Gains
  `send_account_closure_decision(applicant_identifier, account_id,
  product_type, decision, comment)`, fake/dev-only exactly like the
  existing OTP delivery.
- **Staff review surface**: `GET /ui/{underwriter,manager}/closures`
  lists every account at `CLOSURE_REQUESTED`
  (`account.service.list_pending_closure_requests()` — deliberately
  **unpaginated, no bulk actions**, unlike the application queues; a
  closure request is expected to be rare enough at POC scale that a
  plain list is the right-sized answer). Each row is its own plain
  `<form>` (comment field required, Approve/Reject buttons sharing one
  `name="decision"`) — a **plain POST-redirect-GET**, not an htmx
  fragment swap, same pattern `bff_customer`'s Cancel/Resubmit actions
  use. Gated by **role only**, not a new Keycloak permission scope —
  same reasoning Consent-upload already uses. The decision route calls
  `workflow.service.signal_close_account_decision(...)` then
  `account.service.wait_for_status_change(...)` before redirecting; a
  stale page (already decided, or cancelled meanwhile) re-renders with
  an explanatory message instead of a raw error.
- **Customer-facing trigger**: a "Request account closure" action on
  `bff_customer`'s application detail page
  (`POST /apply/applications/{application_id}/closure/request`), shown
  only once `application.status == APPROVED` and the resolved account
  is `ACTIVE` (reusing the existing `_owned_account` ownership check) —
  hidden once a request is pending or the account is `CLOSED`. A
  pending request shows its status plus a Cancel action
  (`POST .../closure/cancel`). Both routes are plain
  POST-redirect-GET forms, not htmx, appropriate for a low-frequency
  customer action.
- **The payoff this exists for**: once `persist_closure_decision`
  writes `CLOSED`, `account.service.has_active_account_of_type` (and
  therefore `application.service.get_available_product_types`) stops
  counting this account at all — the product picker automatically
  re-offers that product type on the customer's next visit, with no
  change needed to either function. This is the "path back" PRD §11
  flagged as missing when the picker's hard elimination first shipped.

### Real email delivery via Gmail SMTP (built and live-verified — Phase 20)

Built and live-verified against the real stack (P20-1 through P20-3),
using the user's own Gmail account — full build history, the two real
gaps found and fixed along the way (Docker stdout buffering hiding
every `print()`-based delivery confirmation; a browser-automation-only
`confirm()`-dialog hang), and the live-verification sweep (all 3
real-send outcomes confirmed delivered: Welcome Letter,
closure-decision Approve, closure-decision Reject) all live in
`IMPLEMENTATION_PLAN.md`'s Phase 20 section and Session Log, not here.
Raised directly by the user: `notifications/service.py`'s own docstring
had said, since Phase 18, that fake `print()` delivery is "the one
thing that would need to change (same signatures, real bodies) if a
real provider is ever wired up" — this phase is that. The design below
wires it in as an **optional** real delivery path, confirmed with the
user as SMTP + a Gmail App Password (stdlib `smtplib`, zero new
dependencies), not the Gmail API/OAuth2 (heavier setup — a Google Cloud
project, an OAuth consent screen, token storage/refresh — out of
proportion to what this POC needs).

- **Scoped to exactly the two functions the user named — `send_account_closure_decision`
  and `send_welcome_letter_email` — not `send_verification_code`.**
  The OTP code stays fake-only, still shown directly in the verify-code
  page's own response (Phase 11's accepted, deliberate design — see
  "Identity" below): real delivery there isn't what was asked for, and
  changing it would remove the one way a tester without real inbox
  access can currently complete the identify flow at all.
- **Opt-in via env vars, never a required dependency.** Real sending
  fires only when both `SMTP_USERNAME` and `SMTP_PASSWORD` are set;
  otherwise both functions fall through to the exact same fake
  `print()` behavior this codebase already has, unchanged. This is
  load-bearing, not a nicety: the existing unit tests for both
  functions (`tests/unit/notifications/test_service.py`) assert on
  `capsys`-captured `print()` output and must keep passing with zero
  SMTP configuration in CI — real sending only ever gets exercised by a
  new, `smtplib`-mocked test plus this phase's own live-verification
  step, never by CI itself. New env vars (`.env.example`, and
  `docker-compose.yml`'s `worker-activity` service only — the one
  process that actually calls `persist_decision`/`persist_closure_decision`,
  same reasoning `MAYAN_*`'s placement there already follows;
  `worker-workflow` does no I/O and doesn't need them): `SMTP_HOST`
  (default `smtp.gmail.com`), `SMTP_PORT` (default `587`),
  `SMTP_USERNAME`, `SMTP_PASSWORD` (a Gmail **App Password**, not the
  account's real password — Google requires this for third-party SMTP
  auth once 2-Step Verification is on, which an App Password itself
  requires), `SMTP_FROM_ADDRESS` (defaults to `SMTP_USERNAME` if
  unset — Gmail's own SMTP relay requires the `From:` header to match
  the authenticated account, or a configured "Send As" alias, which
  this POC doesn't set up).
- **Recipient is `applicant_identifier` itself, no new parameter
  needed.** `bff_customer`'s identify flow has only ever accepted an
  email address since Phase 11's OTP fix (see "Identity" below) — the
  same value already threaded through both functions' existing
  signatures *is* the address to send to. This is what makes "simulate
  sending" work exactly as the user described: testing this POC by
  typing your own Gmail address as the applicant identifier sends the
  Welcome Letter/closure-decision email to that same inbox.
- **A real SMTP send failure must never fail the Temporal activity —
  caught and logged, not raised.** Both call sites
  (`account/activities.py`'s `persist_closure_decision`,
  `application/activities.py`'s `persist_decision`) sit inside
  idempotency-guarded provisioning blocks whose other three calls
  already accept "a retry skips this permanently, once the account/
  decision is already committed" as a smaller, more recoverable gap
  than letting a transient failure retry the whole activity (see
  "Applying without being a customer yet" and P19-3's own live
  confirmation of exactly this mechanism). A flaky Gmail connection is
  exactly the kind of transient failure that tradeoff already exists
  for — letting it propagate would risk turning a successful
  approval/closure-decision into the same class of stuck, `Failed`
  Temporal workflow this file's Known Gaps section already documents
  happening for a wholly unrelated reason (Postgres connection
  exhaustion) in an earlier session. `document.service`'s own calls in
  these same blocks are the deliberate counter-example, not a
  precedent to match: Mayan is this POC's actual document-of-record
  system, so a failure there *should* retry; a notification email
  failing to send is explicitly a best-effort, POC-fake concern by
  PRD §4's own framing, not critical infrastructure.
- **Implementation**: a new private `_send_email(to_address, subject,
  body)` helper inside `notifications/service.py` — checks the four
  env vars, sends via `smtplib.SMTP(host, port)` +
  `starttls()`/`login()`/`send_message()` (an `email.message.EmailMessage`,
  plain text) wrapped in a bare `try`/`except Exception`, or falls
  through to the module's existing `print(...)` path when unconfigured.
  `send_account_closure_decision`/`send_welcome_letter_email` call it
  instead of `print(...)` directly; `send_verification_code` is
  untouched. Zero new `pyproject.toml` dependencies — `smtplib`/`email`
  are Python stdlib.
- **Credentials never committed, never handled by the assistant on the
  user's behalf.** `.env.example` gets the two non-secret defaults
  (`SMTP_HOST`/`SMTP_PORT`) plus empty placeholders for
  `SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_FROM_ADDRESS` — the real App
  Password goes only into the user's own local, gitignored `.env`,
  added by the user directly (not pasted into a chat for an assistant
  to write down), same discipline this project already applies to
  every other real secret it has (`KEYCLOAK_CLIENT_SECRET`,
  `MAYAN_SERVICE_ACCOUNT_PASSWORD`, etc. all ship placeholder-only
  defaults in `.env.example`).

### Automated risk assessment via NATS (planned — Phase 21, not yet built)

This section describes the target design for `IMPLEMENTATION_PLAN.md`'s
Phase 21, written first per this project's own convention, before any
of it is implemented — nothing below is built yet; every "planned"
marker in this section and in "`risk/` — Risk assessment module" below
is literal, not a stale leftover. Raised directly by the user as a
future enhancement, distinct from Phase 18-20's account-closure/
notification work: simulate a genuinely **external, asynchronous**
system — a Risk Engine — consulted over a message broker (NATS) rather
than a synchronous HTTP call, and let it auto-decide the easy cases
(very low or very high risk) without a human ever touching them.

- **Where it sits in the state machine**: a new state,
  `PENDING_RISK_ASSESSMENT`, entered immediately after
  `persist_application` commits — *before* today's
  `PENDING_UNDERWRITING`. A new activity, `submit_risk_assessment`
  (owned by `application/activities.py`, called by the workflow's own
  `execute_activity(...)`-by-name, same mechanism `persist_application`/
  `persist_decision` already use — see "Breaking the cycle"), publishes
  the application's risk criteria (amount, product type, payload) over
  NATS. A new signal, `signal_risk_decision(risk_tier)`, is what moves
  the workflow out of this state — sent not by a BFF route handler (the
  source of every other signal today) but by a new standalone process,
  `risk_listener_main.py`, subscribed to the Risk Engine's decision
  subject.
- **Decision routing**: `LOW` risk auto-transitions straight to
  `APPROVED` — reusing the *exact same* `persist_decision` activity and
  provisioning block a human Underwriter's Approve already triggers
  (customer/account creation, Welcome Letter email, document tagging —
  see "Applying without being a customer yet"), just with
  `underwriter_name` set to a fixed marker value
  (`"risk-engine-auto"`) instead of an authenticated Keycloak username.
  **This is a deliberate, called-out exception** to the rule stated
  elsewhere in this file that `underwriter_name`/`manager_name` are
  "always an authenticated Keycloak username, never client-submitted
  free text" — an automated decision has no Keycloak session behind it
  by definition, so the invariant has to bend here on purpose, not by
  accident. `HIGH` risk auto-transitions straight to `REJECTED`, same
  `persist_decision` REJECT path. **`MEDIUM` risk gets no new branch at
  all** — it falls straight through into today's existing
  `PENDING_UNDERWRITING`, waiting on a human `submit_decision` signal
  exactly as it does today. Confirmed with the user: no risk-tier
  column or badge is surfaced anywhere in `bff_backoffice`'s UI for this
  phase — a `MEDIUM` application looks identical to any other row in
  the underwriting queue.
- **Transport: pure NATS both directions, for this phase.** The
  submission leg (`application/activities.py` → Risk Engine) is a NATS
  publish; the decision leg (Risk Engine → `risk_listener_main.py`) is
  also a NATS publish the listener subscribes to — no HTTP in either
  direction between this codebase and the Risk Engine. **A separate,
  later enhancement, not scoped or designed yet**: an open-source
  gateway component sitting between `risk/nats_client.py` and a *real*
  (non-mock) Risk Engine, translating NATS ↔ HTTP so a genuine
  third-party system speaking REST/webhooks could sit behind the same
  `risk/service.py` contract without this codebase's own NATS-facing
  code ever changing. Listed here only so a future session knows where
  it's meant to plug in — no gateway product has been chosen, and
  nothing about its shape is decided.
- **The Mock Risk Engine is a genuinely separate simulated external
  service, not an in-process module.** Its own container, its own
  process, its own NATS subscription — confirmed with the user directly
  over building it as Python code inside `loan_onboarding`, matching
  how Mayan and Keycloak are already treated as real external systems
  this codebase doesn't own (see "Data storage"'s framing for Mayan's
  own separate Postgres/Redis). Its decision rule for this phase is a
  deliberately simple, deterministic bucketing on `amount` — **assumed
  default, not yet confirmed, see `IMPLEMENTATION_PLAN.md`'s Decisions
  Needed**: `< $15,000 → LOW`, `$15,000–$50,000 → MEDIUM`,
  `≥ $50,000 → HIGH`. Picked so the mock is trivially testable (a
  known amount always produces a known tier) rather than trying to
  simulate a real scoring model.
- **At-least-once delivery means the signal handler needs a duplicate
  guard.** NATS core pub/sub (no JetStream needed for this phase — see
  below) doesn't promise exactly-once delivery, and neither does a
  listener process that might retry a failed
  `workflow.service.signal_risk_decision(...)` call. The workflow's
  handler for this new signal needs the same "ignore a signal once a
  decision is already claimed" guard `_claim_final()`-style logic
  already gives the human-decision path — a duplicate/redelivered risk
  decision must not be able to double-apply.
- **No timeout on the risk-engine callback — a known gap carried
  forward on purpose, not solved differently here.** Same accepted gap
  this file's Known Gaps section already documents for "no timeout on
  wait for Underwriter/Manager decision" — an application that never
  gets a risk decision (Risk Engine down, message lost) sits at
  `PENDING_RISK_ASSESSMENT` forever, same shape as the existing gap,
  not a new category of problem.
- **New Docker Compose services (planned)**: `nats` (official
  `nats:latest` image — core pub/sub only, JetStream not needed for
  this phase since neither leg needs replay/durability beyond what
  Temporal's own activity retry already gives the publishing side),
  `mock-risk-engine` (the standalone simulated external system above),
  `risk-listener` (`risk_listener_main.py`).

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
  `customer.service.find_by_identifier(...)` (the same read-only call
  already used for "welcome back" copy above) to prefill
  `applicant_name`/`applicant_email`/`applicant_phone` into the draft's
  `fields`, and — when that resolves *and*
  `document.service.has_id_photo(customer_id)` is `True` — the
  documents step shows a "We already have a Government ID on file"
  choice, defaulting to reuse with an explicit "Upload a new one
  instead" override, wiring the result into
  `application.service.create_application(...)`'s
  `reuse_existing_id_photo` parameter. See "Returning-customer profile
  refresh and ID reuse" above for the full design and why reuse can't
  be silent. Live-verified across three consecutive applications under
  one identifier (no prefill on the first, prefill+reuse on the second,
  prefill+fresh-upload superseding the old copy on the third) — full
  sweep moved to `IMPLEMENTATION_PLAN.md`'s Session Log, 2026-09-04
  docs-consolidation entry.
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
  section is (`_owned_account` — `APPROVED` application + an account
  that actually exists), with an extra `account.status` check each route
  makes itself (`!= "ACTIVE"` for request, `!= "CLOSURE_REQUESTED"` for
  cancel) since the template's own visibility rules are cosmetic, not
  enforcement. Calls `account.service.request_closure(account.account_id,
  applicant_identifier)` / `workflow.service.signal_close_account_cancel(...)`
  then `account.service.wait_for_status_change(...)`, both plain
  POST-redirect-GET forms like this module's existing Cancel/Resubmit
  actions. See "Account closure" above for the full design and
  `IMPLEMENTATION_PLAN.md`'s Phase 18 (P18-7) for the live-verification
  sweep.

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
- **Account closure review queue**: `GET /ui/{role}/closures` lists
  every account `account.service.list_pending_closure_requests()`
  returns, each row a plain `<form>` (POST-redirect-GET, not htmx) with
  a required attestation comment field and Approve/Reject submit
  buttons posting to `/ui/{role}/closures/{account_id}/decision`.
  **Gated by role only (`_role_dependency`), not a Keycloak
  permission** — same reasoning Consent-upload above already uses;
  either `Underwriter` or `Manager` may decide, no escalation tier. The
  decision route calls `workflow.service.signal_close_account_decision(...)`
  then `account.service.wait_for_status_change(...)` before redirecting
  back to the queue; a stale page (the request was already decided, or
  the customer cancelled it) re-renders the queue with an explanatory
  message instead of a raw error. See "Account closure" above for the
  full design and why this screen is deliberately unpaginated with no
  bulk actions, unlike the application queues; live-verification sweep
  in `IMPLEMENTATION_PLAN.md`'s Phase 18 (P18-8).

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
  (workflow id).** Starts `CloseAccountWorkflow` via
  `workflow.service.start_close_account_workflow(...)`, then waits for
  `persist_closure_request` to actually commit before returning (same
  `_wait_until`-style confirm-then-return pattern
  `application.service.create_application` already uses). Raises the
  new `AccountNotActive` if the account isn't currently `ACTIVE` —
  same "the UI hides it, the service still enforces it" discipline
  `application.service`'s product-type picker already follows, and what
  makes `workflow.service`'s deterministic `account-closure-<account_id>`
  workflow id safe to reuse across a later request (there's never a
  live execution under that id when this check passes). **This is what
  ends `account/`'s status as a pure leaf module** — `.importlinter`'s
  layers contract moved `account/` below `customer | document` and
  above `workflow/` (siblings in a `layers` contract are checked for
  mutual independence, so `account/` importing a same-bar sibling would
  have broken the contract even though the module-specific "never
  imports" contract already allowed it) to make room for this.
  **`applicant_identifier` is an opaque pass-through parameter, not
  resolved internally**: `accounts` carries no `applicant_identifier`
  column of its own, only `customer_id`, and `account/` isn't granted a
  `customer/` import (only `workflow/` and `notifications/` are the
  exceptions) — so this module can't resolve `customer_id ->
  applicant_identifier` itself the way
  `notifications.service.send_account_closure_decision` needs it.
  Threaded through as an opaque string instead, the same role
  `ApplicationWorkflowInput`'s own `applicant_*` fields already play for
  `LoanApplicationWorkflow` — `bff_customer` already holds this value
  from its own session cookie and passes it straight through;
  `CloseAccountWorkflowInput`/`PersistClosureDecisionInput` carry it
  across however many signals arrive, and `account/activities.py`'s
  `persist_closure_decision` is what actually forwards it to
  `notifications.service`.
- **`account/activities.py`** — the concrete Temporal activity
  implementations `CloseAccountWorkflow` calls by string name,
  same "Breaking the application ↔ workflow cycle" split
  `application/activities.py` already established: `persist_closure_request(account_id,
  workflow_id)` (idempotent on a Temporal retry — the `UPDATE`'s `WHERE`
  clause matches both `ACTIVE` and `CLOSURE_REQUESTED`, and
  `COALESCE(closure_requested_at, now())` keeps the original request
  timestamp rather than sliding it forward) and
  `persist_closure_decision(...)` — writes `CLOSED` or reverts to
  `ACTIVE` plus the `closure_decided_*` columns, then calls
  `notifications.service.send_account_closure_decision(...)`.
  **Idempotency guard, same "check current state before redoing a side
  effect" discipline `application/activities.py`'s own `persist_decision`
  already uses**: if the account's status has already moved past
  `CLOSURE_REQUESTED` when this activity runs (a retry of an
  already-decided execution), it returns the already-written status
  without writing again or re-sending the email — a duplicate
  closure-decision email would otherwise be a real, customer-visible
  side effect of a Temporal retry, not just a wasted write.

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
  **`customer_id` (see "Document metadata assignment lifecycle" below)**
  is optional and caller-supplied, not resolved internally — same
  "`document/` is a leaf module, never imports `application/`" reasoning
  `applicant_identifier` already follows: the caller (`bff_customer`)
  already knows it, when it's knowable at all (a returning applicant who
  already resolves to an existing customer), and passes it straight
  through; `None` for a brand-new applicant, same as today.
  `applicant_identifier` is required here, not resolved internally —
  `document/` is a leaf module and never imports `application/`, so the
  caller (`bff_customer`, which already has it from the session cookie)
  passes it straight through.
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

### 8. `risk/` — Risk assessment module (planned — Phase 21, not yet built)

*(See "Automated risk assessment via NATS" above for the full design
this module implements — this section covers only its own code shape,
same split every other module section follows.)*

The connector to the (mocked, for now) external Risk Engine, following
the same "thin async client + a `service.py` that owns the calling
convention" shape `document/mayan_client.py` already establishes for
Mayan.

- **`nats_client.py`** — a thin async wrapper around a NATS client
  (connect, publish, subscribe), configured via a `NATS_URL` env var
  (Docker-internal service name, `nats://nats:4222`, same "every env
  var pointing at another container uses its Docker-internal service
  name" discipline this file already documents for `KEYCLOAK_ISSUER`).
  The only code in this module that actually touches the network.
- `service.submit_risk_assessment(application_id, applicant_identifier,
  product_type, amount, payload) -> None` — publishes the application's
  risk criteria to a NATS subject. Exact subject-naming scheme (one
  shared subject with `application_id` in the message body, vs. a
  per-application subject) not yet decided — see
  `IMPLEMENTATION_PLAN.md`'s Decisions Needed. Called only from
  `application/activities.py`'s new `submit_risk_assessment` activity,
  same "activities.py is where outbound calls to leaf integration
  modules happen" pattern `document/`/`workflow/`/`notifications/` are
  already called from there.
- **No subscribe-side code for the *decision* leg lives here.** Turning
  an inbound NATS decision message into a
  `workflow.service.signal_risk_decision(...)` call needs `workflow/`,
  and `risk/` must never import it — same leaf discipline `document/`
  already keeps. That subscription lives in `risk_listener_main.py` (a
  composition root, not part of this module — see "Module dependency
  graph").
- **Never imports `application/`, `workflow/`, `customer/`,
  `account/`, or `document/`.** A leaf, same shape as `document/` and
  `workflow/` themselves — `idgen/` is the one exception every other
  leaf already gets, if this module ends up needing to mint its own id
  (e.g. a `risk_assessment_id` correlating a submission with its
  eventual decision message) — not yet decided whether one is needed.
- **No Postgres table of its own for this phase.** The risk tier a
  decision resolves to gets written onto a new, nullable
  `applications.risk_tier` column — `application/`'s own table, written
  by the same `persist_decision` activity that already writes every
  other decision-outcome column — not by `risk/` itself.

## Document hierarchy

**Corrected from an earlier draft of this file, which built one single
"Loan Onboarding Archive" index rooted at `applicant_identifier`.**
Replaced (not merely renamed) with **three separate index templates**,
each rooted at a different one of the three entity ids a document can
carry — three different entry points into the same document set,
confirmed with the user rather than assumed, since neither a single
index nor `applicant_identifier`-as-root turned out to be what staff
actually wanted to browse by:

**Corrected again, to a strict "exclusive placement" model** — a
document lives at exactly *one* leaf per index: the deepest entity it's
actually tied to, matching the real customer → account → application
hierarchy. Requested directly by the user after the multi-placement
design (branches 1/2/3, all shown at once) turned out to be confusing
to browse in practice — no more "the same document shows up in three
different places at once."

```
Customer Index (customer_id)
└── <customer_id>
       ├── <account_id>
       │      ├── <application_id>
       │      │      └── <category>       (e.g. Bank Statements --
       │      │                            docs with all three ids set)
       │      └── <category>               (account-only docs, e.g.
       │                                    Welcome Letter -- account_id
       │                                    + customer_id, no application_id)
       ├── <application_id>                (docs with application_id +
       │      └── <category>                customer_id but NO account_id
       │                                    yet -- pre-approval upload
       │                                    from a returning customer)
       └── <category>                      (the customer-level
                                             Government ID copy --
                                             customer_id only, no
                                             account_id/application_id
                                             at all; see "Document
                                             metadata assignment
                                             lifecycle" below)

Account Index (account_id)
└── <account_id>
       ├── <application_id>
       │      └── <category>               (docs with account_id +
       │                                    application_id)
       └── <category>                      (account-only docs, no
                                             application_id)

Application Index (application_id)
└── <application_id>
       └── <category>                      (application is already the
                                             deepest owning entity for
                                             its own documents in the
                                             real hierarchy -- no further
                                             branching needed, whether or
                                             not the application has also
                                             gained account_id)
```

**No more cross-reference branches** (Account Index's old "customer"
sibling, Application Index's old "customer"/"account" siblings) — each
of those would have needed to either duplicate placement (the exact
thing this redesign removes) or dead-end with no documents under it, so
they're gone entirely rather than kept as inert navigation. A customer
looking to browse by account or application uses Customer Index (which
still nests both); Account Index and Application Index each answer only
"what does *this* account/application directly own." Every leaf
condition explicitly excludes the deeper case it doesn't own (e.g.
Customer Index's account-only leaf requires `account_id` present *and*
`application_id` absent) — Django's `{% if %}` supports `not` for this
(`{% if a and b and not c %}`), same tag used elsewhere in these
templates.

**The customer-level Government ID copy is exactly what makes Customer
Index's direct-category leaf unambiguous** (unlike the earlier
multi-placement design's version of this leaf, which matched *every*
document with `customer_id` — Proof of Income, Welcome Letters, all of
it): only the copy has `customer_id` with neither `account_id` nor
`application_id`, so it's the only thing that can ever land there. See
"Document metadata assignment lifecycle" below for why this document
exists as a genuine second Mayan document now, not a re-tagged original.

Live-verified end to end against a real instance (a customer with two
approved applications plus a rejected third, each landing in exactly
one place across the three indexes) — full sweep moved to
`IMPLEMENTATION_PLAN.md`'s Session Log, 2026-09-04 docs-consolidation
entry. `applicant_identifier` plays no role in any of the three trees —
it's still attached to every document (see `document/service.py`'s
`upload`) and still what `document.service.py`'s own queries filter on
(see the gotcha #2 consequence below), just never an index-tree
grouping key.

**A real, mid-build reliability wrinkle, not a template bug — hit twice,
in two different sessions, both from the same root cause: overlapping
`rebuild/` calls fired in quick succession race Mayan's own
reset-then-rebuild sequence and can leave a tree briefly at
`depth=0`/`node_count=0`, even when the template definitions are
correct.** Full repro moved to `IMPLEMENTATION_PLAN.md`'s Session Log
(2026-09-04 docs-consolidation entry). **The operating rule this
confirms, and the reason this paragraph stays in full here rather than
moving with the rest**: never fire a `rebuild/` call — for any index —
while a previous `rebuild/` call against *any* index might still be in
flight, and always confirm `node_count` stable across several polls
before trusting a rebuilt tree.

**A real, load-bearing bug found while deleting the old single index**:
`document/mayan_client.py`'s `rebuild_index()` used to look up a single
hardcoded slug, `INDEX_TEMPLATE_SLUG = "loan-onboarding-archive"` —
deleting that index without updating this constant would have made
every document upload in the whole application start failing. Fixed by
replacing the single slug with `INDEX_TEMPLATE_SLUGS = ("customer-index",
"account-index", "application-index")` and a new
`index_template_ids() -> list[int]` that `rebuild_index()` now loops
over, rebuilding all three. Deliberately still excludes "Creation
date" — nothing in this codebase rebuilt that index before this fix
either. Live-verified after the fix — see the Session Log entry above.

**Multi-leaf placement is real Mayan behavior, no longer exploited on
purpose — historical context, not current design.** Two earlier
drafts of this file relied on it (source-confirmed via
`mayan/apps/document_indexing/models/index_instance_models.py`'s
`_document_add()`, which walks *every* child branch at each tree level
and links a document into *all* branches whose conditions independently
evaluate true, not just the first match — full verification narrative
in the Session Log entry above). **Both uses are gone now** — the
exclusive-placement redesign above replaced them specifically because
multi-placement read as confusing when browsing (the user's own direct
feedback), and the customer-level Government ID copy (a genuine second
document, not a re-tagged original — see "Document metadata assignment
lifecycle" below) means no document needs to satisfy two leaves
simultaneously anymore. The mechanism itself is still true of Mayan and
worth knowing if a future design ever wants it back. **Cabinets were
evaluated as an alternative and rejected as the hierarchy's backbone**
— they also support true multi-membership and are synchronous (no
Celery, unlike Index Templates), but the project's actual usage pattern
is automatic, upload-time classification via API, which is Index
Templates' idiomatic niche, not Cabinets' (a third-party source
describes Cabinets as manual, file-manager-style curation).

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
upload (PRD §6.4). **This principle is exactly why swapping the index
templates out entirely (this section's redesign) required zero changes
to any of `document/service.py`'s query functions** — none of them ever
read the Index Template tree in the first place; the tree exists purely
for staff to browse the archive visually in Mayan's own UI, never as a
data source for this application's own logic.

The same **five gotchas** documented in `mayan-edms-customer-archive`'s
`docs/document-hierarchy-setup.md` still apply — read that file before
touching any index template or `document/`'s setup script (they're
about index-template mechanics, not any particular tree shape):

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

## Document metadata assignment lifecycle

**Five rules, confirmed with the user, that together describe exactly
which of `applicant_identifier`/`application_id`/`account_id`/
`customer_id` a document carries at every point in its life** — the
"Document hierarchy" section above describes the resulting tree shape;
this section describes *when* each metadata field actually gets
attached to make that shape happen.

1. **At upload time, `application_id` (and `applicant_identifier`,
   `category`) are always attached** — true since Phase 6, unchanged
   here. `document.service.upload(...)`'s first three metadata fields
   are never optional.
2. **At upload time, `customer_id` is attached too, but only when the
   applicant already resolves to an existing customer.** A returning
   applicant's `bff_customer` wizard already resolves `customer_id` via
   the read-only `customer.service.find_by_identifier(...)` lookup
   (Phase 14's prefill step) and holds it in the session draft —
   `new_application_upload` now passes it straight into
   `document.service.upload(...)`'s new `customer_id` parameter. The
   resubmit path (`upload_more_info_document`) passes the application
   row's own already-resolved `customer_id` column the same way. A
   brand-new applicant has no `customer_id` to pass — `None`, same as
   every upload before this existed — so their documents stay
   `customer_id`-less until approval, same as today.
3. **On approval, every document under the application — not just the
   Government ID one — gets `account_id` and `customer_id` attached.**
   `document.service.tag_application_documents(application_id,
   account_id, customer_id)` (new — see the `document/` module section
   above) does this in one pass across every category, in place, on
   the documents themselves. This runs alongside, not instead of,
   `promote_government_id_to_customer_photo` and
   `generate_welcome_letter` (whose own new document gets `customer_id`
   too, for consistency). All three calls sit inside `persist_decision`'s
   existing `account_id IS NOT NULL` idempotency guard — a Temporal
   retry that finds the account already provisioned skips all three,
   permanently, same accepted smaller-than-a-duplicated-account gap this
   file already documents for the other two.
4. **`promote_government_id_to_customer_photo` creates a genuine second
   Mayan document — a customer-level copy — rather than re-tagging the
   original, corrected from an earlier draft of this file.** The
   original design attached `customer_id` directly to the just-approved
   application's own Government ID document, making one Mayan document
   satisfy two index leaves at once (CLAUDE.md's old "multi-leaf
   placement"). Changed after a direct design request: the application's
   Government ID document is now left completely untouched (still owned
   only by its application, consistent with "Document hierarchy"'s
   exclusive-placement rule); a *new* document is created instead, with
   the same file content (`mayan_client.download_file`, a full in-memory
   read — POC-scale documents only, no streaming needed for the copy)
   but tagged with only `customer_id`/`applicant_identifier`/`category`
   — deliberately no `application_id`/`account_id` at all, so it lives
   purely at the customer level (Customer Index's own direct
   `Government ID` leaf). If the customer already had a previous copy
   (a fresh Government ID on a *later* approved application, the
   "Returning-customer profile refresh and ID reuse" refresh case), that
   old copy is trashed first (`DELETE /documents/{id}/`, Mayan's own
   soft-delete) — still never more than one copy per customer at a
   time, just via delete-then-create instead of strip-then-retag. The
   reuse path (no fresh Government ID under the just-approved
   application) is still a no-op, unchanged — the existing copy is
   already the customer's current photo.
5. **A rejected, cancelled, or still-pending application's documents
   never get `account_id` — this was already true by construction, not
   new behavior.** `account_id` is only ever attached inside the
   terminal-`APPROVED` branch of `persist_decision`'s provisioning
   block; no other decision outcome creates an account or calls
   `document/` for account-tagging at all. Stated explicitly here
   because it was asked about directly, not because anything had to
   change to make it true.

**Two real bugs found against the real stack (P16-4), neither caught by
the unit suite — both only surfacing against genuine Mayan behavior,
full repro/verification narrative moved to `IMPLEMENTATION_PLAN.md`'s
Session Log, 2026-09-04 docs-consolidation entry**:

1. **A document type can only carry metadata types it's been explicitly
   associated with** — `account_id` had never been associated with
   "Application Document", nor `customer_id` with "Account Document",
   so the new attaches above were rejected outright with a 400.
   `scripts/setup_document_hierarchy.sh` now attaches both associations
   (`required=false`, since neither exists at upload/create time).
2. **Mayan rejects a second `POST` for a metadata type a document
   already carries** with another 400 — `tag_application_documents` and
   `promote_government_id_to_customer_photo` used to both attach
   `customer_id` to the same Government ID document when a fresh upload
   was promoted; this file's own earlier draft wrongly called that
   second attach "a harmless idempotent no-op." Fixed with a
   `document/service.py`-internal `_set_metadata` helper (update-in-place
   via `update_metadata_entry` if the field already exists, plain create
   otherwise). **Superseded, not reverted, by the exclusive-placement
   redesign below** — `promote_government_id_to_customer_photo` no
   longer touches the same document `tag_application_documents` does at
   all (it creates a brand-new Mayan document instead), so this
   double-attach can't recur structurally, not just because
   `_set_metadata` guards it. `_set_metadata` stays in use by
   `tag_application_documents`' own multi-category attach loop, where
   the original conflict-on-retry concern is still real.

**A follow-up session redesigned the three index templates from
multi-placement to exclusive placement** (a document lives at exactly
one leaf — the deepest entity it's actually tied to — never nested
under more than one branch at once) and, along with it, changed
`promote_government_id_to_customer_photo` from a re-tag-in-place
operation into a genuine file copy — see "Document hierarchy" above for
the resulting tree shape and rule 4 above for the copy mechanism
itself. A real correctness bug in `reconcile.py` was also found and
fixed while building this: `scan()` used to treat *every* stale
`customer_id` as a strippable secondary tag — true when `customer_id`
only ever rode alongside `application_id`, no longer true now that the
customer-level copy carries `customer_id` as its *only* metadata.
`scan()` now checks whether a document has `application_id`/
`account_id` at all before deciding orphaned-vs-stale (see rule 5 above
and `scan()`'s own docstring) — without this fix, a customer-level copy
whose owning customer row was deleted would have had its one
identifying tag stripped instead of the whole document being trashed,
leaving a permanently untethered, un-taggable document invisible to
every future reconciliation run. Both redesign passes were
live-verified end-to-end (244 tests + `lint-imports` green after the
second) — full sweep in the Session Log entry referenced above.

**Deliberately out of scope**: no backfill of documents belonging to
applications approved *before* this lifecycle existed — same "forward-
looking only" scope boundary Phase 14 already accepted for not
backfilling existing customer profiles. An application approved before
this shipped keeps whatever metadata its documents already had; only
approvals from this point forward get the full `account_id`/
`customer_id` tagging on every document.

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

## Document/database reconciliation

**A real, live-observed gap, not a hypothetical one**: `loan_onboarding`
(Postgres) and Mayan are two completely independent systems with no
foreign key, no cascade, and no transaction spanning them — the only
link is a plain string (`applicant_identifier`/`application_id`/
`account_id`/`customer_id`) attached to a Mayan document as metadata
(`document/service.py`'s `upload`/`generate_welcome_letter`/etc., see
"Document hierarchy" above). Nothing enforces that string actually
still resolves to a Postgres row. Confirmed live: `loan_onboarding`'s
three domain tables were cleared (by something outside this app
entirely — a script or process with direct database access, not any
code path this codebase owns) while Mayan's documents were completely
unaffected, leaving real orphaned documents (`application_id`/
`account_id` values pointing at rows that no longer existed) with
nothing in the codebase able to detect, let alone fix, that on its own.

**Two related but genuinely different problems, addressed separately —
don't conflate them**:

1. **Drift detection / reconciliation** (this section, built): Postgres
   and Mayan can each be modified independently of the other, by
   anything with direct access to either — not just this app. The only
   way to catch that is to periodically (or on-demand) walk every Mayan
   document and check whether the Postgres row it claims to belong to
   still exists. Nothing about *how* the row disappeared matters — a
   direct `DELETE`/`TRUNCATE`, a bug, an operator mistake, all look
   identical from Mayan's side: metadata pointing at nothing.
2. **Cascade-on-delete** (planned, not built yet — see Known Gaps):
   when *this app itself* deletes a `customer`/`account`/`application`
   row through its own service layer, the documents that belonged to it
   should go too. This only ever fires for deletes that go through
   `service.py` — it does nothing for the kind of external, direct-DB
   modification that reconciliation (above) exists to catch. Also
   presently blocked on a real, unresolved product question: there is
   no delete operation for any of these three entities in this codebase
   today, and whether a loan-onboarding system should ever hard-delete
   an approved customer/account/application (audit-trail implications)
   versus something like a status change is an open question, not yet
   decided.

**Reconciliation mechanism**: `loan_onboarding/reconcile.py`, a third
composition root alongside `app.py`/`worker_main.py` (see "Repo
layout") — the only files in this codebase allowed to import from every
domain module, because this is fundamentally a cross-cutting concern no
single module's own leaf-purity should absorb. `customer/`/`account/`
stay pure leaves; `reconcile.py` reaches into `customer/`, `account/`,
`application/`, and `document/` all at once, same as `app.py` already
does for the two BFFs.

For every document `document.service.list_all_documents()` returns
(a new, unfiltered public wrapper over the existing private
`_documents_matching({})` — an empty filter dict already matches every
document, that path just wasn't exposed before):

- **A document's primary owner** is whichever id its document type
  actually keys on — `application_id` for an Application Document
  (Government ID, Proof of Income, Bank Statements, Credit Report,
  Property Appraisal, Vehicle Title/Invoice), `account_id` for an
  Account Document (Welcome Letter, Consent). If that id doesn't
  resolve via the owning module's own `service.get(...)` (catching the
  `NotFound` each module already raises — `ApplicationNotFound`,
  `AccountNotFound` — no new "exists" check needed anywhere), the
  document is **orphaned**: its primary owner is gone, so the document
  itself should go.
- **`customer_id` is a secondary tag, not a primary owner** — only ever
  present on a promoted `id_photo` document (`document/`'s
  `promote_government_id_to_customer_photo`, Phase 14), layered on top
  of that document's own real ownership via `application_id`. A stale
  `customer_id` (the referenced `customers` row is gone, but the
  document's own `application_id` still resolves fine) is narrower than
  an orphan — deleting the whole document over a stale *secondary* tag
  would be wrong when its primary ownership is still intact. This is a
  **stale tag**, fixed by stripping just that one metadata entry
  (`mayan_client.delete_metadata_entry`, already built in Phase 14 for
  exactly this shape of operation), not by removing the document.

**Two modes, `--report` (default) and `--fix`**: `--report` scans and
prints findings, mutating nothing — safe to run at any time, including
production, to see what's actually orphaned before deciding to act.
`--fix` additionally moves every orphaned document to Mayan's trash
(`DELETE /documents/{id}/` — soft-delete, reversible, same as this
project's existing "moves to Mayan's trash, not a hard delete" note)
and strips every stale `customer_id` tag, then rebuilds the index once
at the end (same "rebuild once, not per-document" discipline every
other multi-document `document/service.py` operation already follows).

**Live-verified against a real orphaned state, not a synthetic one**
(27 real orphaned documents plus a deliberately constructed stale-tag
case; `--report` correctly separated the two categories, `--fix`
correctly cleaned up both) — full sweep moved to
`IMPLEMENTATION_PLAN.md`'s Session Log, 2026-09-04 docs-consolidation
entry.

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
    ├── risk/                     # planned, Phase 21, not yet built --
    │   ├── nats_client.py        # see "risk/ -- Risk assessment module"
    │   └── service.py
    └── risk_listener_main.py     # planned, Phase 21 -- composition root,
                                   # NATS decision subscriber, see
                                   # "Automated risk assessment via NATS"
```

**Also planned, Phase 21, sitting outside the `loan_onboarding` Python
package entirely**: `mock_risk_engine/`, the standalone simulated
external Risk Engine — deliberately not part of this package, same
"a real external system this codebase doesn't own" treatment Mayan and
Keycloak already get (see "Automated risk assessment via NATS").

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
- **Planned, Phase 21, not yet built**: `nats` (core pub/sub, no
  JetStream — see "Automated risk assessment via NATS"),
  `mock-risk-engine` (the standalone simulated external system,
  `depends_on: [nats]`), `risk-listener`
  (`risk_listener_main.py`, `depends_on: [nats, temporal]`).

Every env var pointing at another container uses its Docker-internal
service name — same discipline the reference project already documents
for `KEYCLOAK_ISSUER`.

## Known gaps to state explicitly once built

*(Every "Resolved" bullet below is trimmed to a current-state summary —
full repro/root-cause/reverification narrative for each lives in
`IMPLEMENTATION_PLAN.md`'s Session Log, 2026-09-04 docs-consolidation
entry, unless a more specific pointer is given.)*

- **`docker compose up -d` does not rebuild images, and Mayan's own
  index-template/metadata-type config can independently drift or
  reset.** Both hit live, in the same session — full repro in
  `IMPLEMENTATION_PLAN.md`'s "2026-09-04 (new session)" Session Log
  entry. **The operating rule this confirms**: this file's "already
  built and live-verified" describes a point in time, not a durable
  guarantee — re-verify both the running image and Mayan's live config
  directly before trusting a "clear test data and re-verify" pass to
  exercise current code.
- **This project has no schema migration tooling** — `db/schema.sql`
  changes only ever apply to a brand-new `db` volume
  (`db/init/01-init.sh`, first container start only), never to an
  already-running one. Bit for real after Phase 18 (a live `KeyError:
  'closure_workflow_id'` inside a Temporal activity, from an
  un-migrated `accounts` table — full repro and fix in
  `IMPLEMENTATION_PLAN.md`'s Session Log). **The operating rule this
  confirms**: a schema change landing in `db/schema.sql` is not
  "deployed" just because it's merged and the images are rebuilt — an
  existing `db` volume needs either a manual `ALTER TABLE` or a full
  `docker compose down -v` (destroying all data) before new code that
  assumes the new columns exist can run safely against it. No tooling
  in this project currently detects or prevents this mismatch.
- **A local `worker_main.py` process and the dockerized
  `worker-workflow`/`worker-activity` containers silently race each
  other for the same Temporal task queues if both are left running at
  once, pointed at different databases** — found live during Phase 19's
  verification, full repro in `IMPLEMENTATION_PLAN.md`'s Session Log.
  **The operating rule this confirms**: any local-worker verification
  session must stop *all three* of `app`/`worker-workflow`/
  `worker-activity`, not just `app` — the two worker containers hold no
  port to conflict with, so it's easy to forget they're still silently
  polling and racing.
- **Reconciliation (`reconcile.py`) only detects and fixes drift — it
  never prevents it, and nothing runs it automatically.** It has to be
  invoked by a human or a scheduled job, neither of which this project
  sets up. **Cascade-on-delete is deliberately not built** — there is no
  delete operation for `customer`/`account`/`application` anywhere in
  this codebase today, and whether a loan-onboarding system should ever
  hard-delete an approved entity (audit-trail implications) versus a
  status change is a real, unresolved product question, not a build gap
  — confirmed with the user as "reconciliation first," cascade
  deferred, not decided against.
- **`applications`'s old `chk_approved_has_account` DB-level check
  constraint is gone, not replaced.** Once the account pointer moved to
  `accounts.application_id` (see "Data storage"), "an APPROVED
  application has a matching account" can no longer be expressed as a
  single-table `CHECK` — enforcing it across two tables would need a
  trigger, which this POC deliberately doesn't add. The invariant is
  still true in practice (`persist_decision`'s logic guarantees it), but
  it moved from DB-enforced to code-enforced-only — a real, if narrow,
  reduction in the safety net.
- **The 9-digit-numeric primary key format (`CUS-`/`ACC-`/`APP-`) trades
  away collision-safety margin for a familiar, account-number-style
  look.** `10^9` values per entity type is real headroom for a POC but
  nowhere near a `UUID`'s — see "Data storage" for the entropy
  discussion and why the retry-on-collision insert logic in each
  module's `db.py` is load-bearing, not decorative. Revisit (longer id,
  or alphanumeric) if this ever needs to scale past POC data volumes.
- **Resolved (P12-3)**: `app`'s host port would have collided with
  `mayan`'s (both `8000`) — moved to `8001`. A second bug, invisible
  until the first fully containerized run, was found in the same pass:
  browser-redirect URLs and issuer-claim validation were built from the
  server-internal `KEYCLOAK_ISSUER`, mismatched against Keycloak's
  actual browser-facing `iss` claim — fixed with a new
  `KEYCLOAK_PUBLIC_ISSUER` env var.
- **Resolved (post-P12)**: `db`'s published host port `5432` collides
  with a native, host-installed Postgres on a dev machine — moved to
  `5433` on the host side only (in-Compose services reach `db:5432`
  internally, unaffected).
- **Mayan's default REST API rate limit (`REST_API_THROTTLING_RATE_USER`,
  20 req/sec) is real and gets hit at POC scale** (found in P5-4/P5-5
  running a realistic upload sequence against real Mayan).
  `mayan_client.py`'s `_request` retries on 429 honoring `Retry-After`,
  bounded at `_MAX_429_RETRIES = 5` — not a full fix.
  `document/service.py`'s `_documents_matching` (fetch every document,
  then every document's metadata, then filter in Python — Mayan's
  advanced-search endpoint doesn't AND multiple metadata fields
  together) is still O(all documents in the instance) per call and will
  throttle more as real data volume grows; fine for a POC, would need
  server-side filtering (or caching) before scaling past that.
- **Resolved, narrowed rather than fully closed.** `bff_customer/` used
  to accept a self-typed email/phone with zero verification (PRD §7.1)
  — this POC's standout risk. **Fixed** by requiring a 6-digit
  email-verification code before the session cookie is ever set — see
  "Identity" above and `bff_customer/identity.py`'s module docstring.
  **Still a real, accepted limitation**: no real email/SMS provider, so
  delivery is fake (`notifications/service.py` prints the code
  server-side, the verify page shows it directly, labeled dev-only) —
  this proves the mechanism, not a production-ready login. Phone-number
  identifiers were dropped along with this fix (SMS would need a
  provider this project has none of either), confirmed with the user as
  an accepted scope reduction.
- **Resolved (no-graceful-handling half only — the race window itself
  is deliberately still open).** The active-account-per-product-type
  rule is checked before a decision is signaled but not atomically with
  it — two near-simultaneous Approves for the same customer+product_type
  can both pass the check before either commits; the partial unique
  index always stopped the bad *write*, but the loser used to fail its
  whole Temporal workflow and get stuck forever with no error surfaced.
  **Fixed**: `persist_decision` now catches that specific constraint
  violation and converts the loser into a clean `REJECTED` (with a
  system-generated comment), chosen over two other options (fail fast,
  or a distributed lock) as the one that closes "stuck forever" without
  serializing the check-and-write. **The in-batch half of the window is
  now also closed** by `check_decision_allowed_bulk`, which tracks
  `(applicant_identifier, product_type)` pairs an earlier item in the
  same batch already claimed. **What's still deliberately accepted**:
  only same-request concurrency is closed — two decisions from
  *separate* HTTP requests close enough in time can still both pass
  their own checks; closing that fully would need a lock spanning from
  the web-process check to the worker-process write, a much larger,
  riskier change not undertaken here. `persist_decision`'s
  conflict-to-REJECTED handling remains the backstop for that
  cross-request case. See `application/service.py`'s
  `check_decision_allowed_bulk` and `application/activities.py`'s
  `persist_decision`, plus each one's test coverage in
  `tests/unit/application/`.
- **Resolved, found live in Phase 13's P13-7 sweep.**
  `check_decision_allowed`'s short-circuit used to trust a `NULL`
  `applications.customer_id` as "no customer exists," which is wrong
  for a sibling application under the same identifier whose column was
  never backfilled after an *earlier* sibling's approval — this let a
  second Approve reach the workflow uncontested and then deterministically
  hit the same active-account unique-constraint violation. **Fixed** by
  having `check_decision_allowed` resolve via
  `customer.service.find_by_identifier(...)` when `customer_id` is
  `NULL`, instead of trusting the column alone. This is a separate,
  narrower fix from the race-window gap immediately above — unaffected
  by it.
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
  catch — and no reconciliation job exists anywhere in this codebase to
  catch it from the outside either.** An earlier draft of
  `db/schema.sql`'s `workflow_id` column comment (and `PRD.md` §9.3's
  data-model table) claimed this column "gets cleared if a Temporal
  admin deletes the execution," describing a reconciliation mechanism
  as if it existed — corrected in P12-1 after grepping the codebase and
  finding no code anywhere writes to `workflow_id` after
  `persist_application` sets it; `PRD.md` §9.3 still carries this
  correction inline in its own `workflow_id` row. Verified for real in
  P12-1: a genuine `temporal workflow cancel` correctly lands the
  Postgres row on `CANCELLED`, but a `temporal workflow terminate`
  leaves it permanently stuck with no error raised anywhere — a human
  operator today has no query, alert, or job that would ever surface
  this.
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

**When running these against a local `docker compose` stack you're also
using for live/manual verification, point `DATABASE_URL` at a separate
database (e.g. `loan_onboarding_test`), never the compose stack's own
`loan_onboarding`.** Found the hard way in P16-4: these tests' per-test
cleanup fixtures do a real `DELETE FROM applications`/`accounts`/
`customers` against whatever `DATABASE_URL` points at — running the
suite against the same live Postgres a `docker compose up -d app` is
using silently wipes every application/account/customer the live stack
had, mid-session, with no error. (The two databases live in the same
Postgres *container*, both reachable on the host's published `5433`
port — see "Data storage" — so pointing at the wrong one is an easy
mistake, not a hypothetical.) Create the test database once
(`CREATE DATABASE loan_onboarding_test;` then apply `db/schema.sql` to
it) and keep using it for every local unit-test run alongside a running
compose stack.

**A second, related real hazard found in a later session's clean-slate
E2E re-verification: ad hoc `docker exec <container> python3 -c
"asyncio.run(...)"` one-off scripts (used for manual document-service
recovery calls, or just to poke at a module directly) each create a
brand-new `asyncpg` pool via that module's own `_get_pool()` — and
`asyncpg.create_pool()` defaults to `min_size=10`, opening 10 real
connections per call.** Running several such one-off scripts across a
session (this one ran roughly a dozen over its course) can exhaust
Postgres's `max_connections` (100 by default) well before anything
looks obviously wrong — the symptom was every real request, browser-
driven or not, starting to fail with `asyncpg.exceptions.TooManyConnectionsError:
sorry, too many clients already`, including inside a running Temporal
activity (turning an in-progress approval into a genuinely stuck,
`FAILED` workflow — recovered by deleting that one workflow execution
via `temporal workflow delete` and its now-orphaned application row,
not by anything automatic). Each one-off process exiting *should*
release its connections via ordinary TCP teardown, but in practice the
connections lingered long enough to compound across many closely-spaced
invocations. Fixed by restarting `db` (safe — data lives on the
volume, not in the container) plus the app/worker containers whose own
pools were sitting on now-invalid connections after that restart.
**The operating rule this confirms**: prefer the running app/worker
containers' own long-lived pools (drive verification through the real
browser flow, or read state via `psql`/the Mayan REST API/`temporal`
CLI directly) over spinning up fresh one-off Python processes against
this codebase's own modules; if a one-off script is genuinely
necessary, keep it to one at a time and don't let more than a couple
accumulate across a session without restarting `db` in between.

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
