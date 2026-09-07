---
name: account-closure
description: The Phase 18 account-closure feature in loan-onboarding-poc -- the ACTIVE/CLOSURE_REQUESTED/CLOSED state machine, the CloseAccountWorkflow Temporal workflow, why account/ stopped being a leaf module, the notifications/ leaf module, and the staff/customer UI surfaces. Triggers on "account closure", "CloseAccountWorkflow", "closure_requested", "request_closure", "CLOSURE_REQUESTED", "closure decision", "list_pending_closure_requests", "notifications/ module", "account/ leaf module".
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

