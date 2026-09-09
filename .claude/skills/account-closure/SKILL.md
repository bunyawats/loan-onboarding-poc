---
name: account-closure
description: The Phase 18 account-closure feature in loan-onboarding-poc -- the ACTIVE/CLOSURE_REQUESTED/CLOSED state machine, the CloseAccountWorkflow Temporal workflow, why account/ stopped being a leaf module, the notifications/ leaf module, and the staff/customer UI surfaces -- plus Phase 22's redesign of closure-request history from a single overwritten record to a real 1:M account_closure_requests table. Triggers on "account closure", "CloseAccountWorkflow", "closure_requested", "request_closure", "CLOSURE_REQUESTED", "closure decision", "list_pending_closure_requests", "notifications/ module", "account/ leaf module", "account_closure_requests", "closure_request_id", "workflow_run_id", "closure history".
---

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
  shape as an application's existing Cancel action. **As originally
  built (Phase 18), the request/decision data lived directly on
  `accounts`** (`closure_workflow_id`, `closure_requested_at`,
  `closure_decision_comment`, `closure_decided_by`, `closure_decided_at`
  — nullable, only the *current* request's data kept, a later request
  overwriting rather than preserving history). **Phase 22 replaced this
  with a real one-row-per-request table** — see that section below;
  `accounts.status` itself is unchanged by that redesign, still
  current-state only.
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

### Account closure request history — 1:M (built and live-verified — Phase 22)

Requested and confirmed by the user after reviewing `db/schema.sql`'s
own comment on the Phase 18 columns, which already flagged the
single-current-request shape as a known, accepted simplification: a
customer genuinely *can* request closure again on the same account once
a prior request is `REJECTED` or `CANCELLED` (both revert
`accounts.status` back to `ACTIVE`, and `request_closure()` is
reachable again the moment it does) — a real 1:M relationship (one
account, many historical closure requests) that Phase 18 collapsed into
1:1-overwritten. Full task-by-task build history, DONE notes, and the
live-verification sweep live in `IMPLEMENTATION_PLAN.md`'s Phase 22
section and Session Log, not here.

- **New table, `account_closure_requests`** (owned by `account/`, same
  as `accounts` itself — a same-module join, not the cross-module kind
  `CLAUDE.md`'s "no real FKs" rule is about): `closure_request_id`
  (`ACR-` + 9 digits, minted via `idgen`, same collision-retry pattern
  every other entity's id uses), `account_id` (opaque, not a FK),
  `workflow_id`, `workflow_run_id`, `requested_at`, `status`
  (`PENDING`/`APPROVED`/`REJECTED`/`CANCELLED` — **a different
  vocabulary from `accounts.status`**, mapped in
  `account/activities.py`'s `_CLOSURE_REQUEST_STATUS_BY_DECISION`),
  `decision_comment`, `decided_by`, `decided_at`. A request and its
  eventual decision are the *same* row, updated in place once — this
  table is 1:M relative to `accounts`, not 1:M relative to itself per
  request. `accounts.status` lost nothing and gained nothing — still
  current-state only, now written by its own small
  `account/db.py:set_status(...)` rather than folded into the same
  query as the closure-request write.
- **Why `workflow_run_id` had to be added, and where it's captured.**
  `workflow.service._workflow_id_for_account_closure`'s deterministic
  `account-closure-<account_id>` id is reused for *every* request
  against one account (safe, relying on Temporal's default
  `AllowDuplicate` reuse policy) — so `workflow_id` alone can't tell two
  historical requests against the same account apart once there's more
  than one. `CloseAccountWorkflow.run()` captures
  `workflow.info().run_id` at the same point it already captures
  `workflow.info().workflow_id`, threading it into
  `PersistClosureRequestInput`.
- **Idempotency, and the one real design nuance found while building
  it**: `account/db.py:create_closure_request(...)` always tries a
  fresh `INSERT`; a retry of the same Temporal activity execution hits
  `account_closure_requests`' new `ux_closure_requests_account_pending`
  partial unique index (`account_id` `WHERE status = 'PENDING'` — the
  actual DB-level enforcement of "at most one pending request per
  account"). Rather than treating *any* hit on that constraint as "this
  must be my own retry," the function compares the existing row's
  `workflow_run_id` to the caller's own — only a match is treated as an
  idempotent no-op (returns the existing row); a genuine mismatch is
  left to propagate. Tracing when a mismatch could actually happen
  found it can't, through the normal `request_closure()` call path —
  the real reachable race (two near-simultaneous requests racing each
  other before either's activity runs) is caught one layer up, by
  Temporal's own `WorkflowAlreadyStartedError` at `client.start_workflow`
  time, since both attempts share the same deterministic workflow id
  and only one can be *running* at once. **This means
  `request_closure()`'s own race-safety is unchanged from Phase
  18 — a real, still-open, pre-existing gap this redesign didn't
  introduce or fix**: nothing today catches
  `WorkflowAlreadyStartedError` and converts it to a clean error;
  worth a future session.
- **Decision-time correctness**: `PersistClosureDecisionInput` gained
  `closure_request_id` (the one `run()` captured, threaded through both
  `submit_decision` and `cancel`), so `persist_closure_decision` updates
  the *correct* row explicitly rather than re-deriving "whichever
  request is currently PENDING for this account" at decision time — a
  meaningfully more correct idempotency guard now that more than one
  historical request can exist.
- **New service functions** (`account/service.py`):
  `get_pending_closure_request(account_id)` (replaces every place that
  used to read `Account.closure_workflow_id` directly — the field no
  longer exists on `Account`, which lost its five `closure_*` fields
  entirely), `list_closure_requests_for_account(account_id)` (full
  history, newest first), and a new `AccountClosureRequest` dataclass
  mapping 1:1 to the new table. `list_pending_closure_requests()` (the
  staff queue) now returns plain dicts — a same-module *view* joining
  closure-request fields with the owning account's
  `customer_id`/`product_type`, not a persisted entity in its own
  right.
- **UI (Phase 22, P22-4)**: a "Closure history" section on both
  `bff_customer`'s application detail page (a standalone card, shown
  whenever history exists, regardless of the account's *current*
  status — a `CLOSED` account's past requests are just as worth showing
  as an `ACTIVE` one's) and `bff_backoffice`'s staff review dialog
  (right after the existing Consent section). Deliberately not a new,
  dedicated history screen — a section on the existing detail page is
  proportionate to this POC's own closure volume, same reasoning
  `list_pending_closure_requests` staying unpaginated already uses.
- **Live-verified end to end, a fresh cycle, not reused e2e data**: a
  real `ACTIVE` account requested closure → staff `REJECT` (reverts to
  `ACTIVE`) → requested closure *again* on the same account → staff
  `APPROVE` (→ `CLOSED`). Confirmed via `psql`:
  `account_closure_requests` held exactly two rows for this account,
  distinct `closure_request_id`s, distinct real Temporal
  `workflow_run_id`s, correct independent `decision_comment`/
  `decided_by` per row — not one row silently overwritten. Confirmed
  the product-type-elimination payoff still works correctly off the
  *second* request's approval (the product reappeared in the picker
  once `CLOSED`, with the customer's other still-`ACTIVE` accounts in
  different product types correctly still eliminating those).
- **Live migration note, not a design decision**: the live stack's `db`
  volume already held real closure data from before this redesign (6
  rows, across two separate e2e sessions) — backfilled into the new
  table (`decided_by = 'customer'` → `CANCELLED`, prior
  `accounts.status = 'CLOSED'` → `APPROVED`, else `REJECTED`) before the
  old columns were dropped, rather than discarded. Worth knowing if a
  future schema change ever needs the same live-migration treatment:
  check for real data on the columns being changed *before* dropping
  them, not after.

