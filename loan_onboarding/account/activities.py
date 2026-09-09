"""Concrete Temporal activity implementations for account closure
(Phase 18, "Account closure" -- see CLAUDE.md), registered under the
exact string names `workflow/workflows.py`'s `CloseAccountWorkflow`
calls by name -- the same "Breaking the application <-> workflow cycle"
split `application/activities.py` already established for loan
applications, just for `account/` instead. This is the one file in
`account/` allowed to import `notifications/` (plus
`workflow/workflows.py`'s own dataclasses) -- CLAUDE.md's "planned
exception" for `account/`, now built. `customer/` stays off-limits here,
same as everywhere else in `account/` -- see `account/service.py`'s
`request_closure()` docstring for how `applicant_identifier` reaches
this file without `account/` ever importing `customer/` to resolve
one."""

from __future__ import annotations

from datetime import datetime, timezone

from temporalio import activity

from loan_onboarding.account import db as account_db
from loan_onboarding.notifications import service as notifications_service
from loan_onboarding.workflow.workflows import (
    ACTIVITY_PERSIST_CLOSURE_DECISION,
    ACTIVITY_PERSIST_CLOSURE_REQUEST,
    DECISION_APPROVE,
    DECISION_CANCELLED,
    DECISION_REJECT,
    PersistClosureDecisionInput,
    PersistClosureRequestInput,
    STATUS_ACCOUNT_CLOSURE_REQUESTED,
)

# The closure_request's own status vocabulary (Phase 22, "Account
# closure request history (1:M)") is not the same as accounts.status
# (ACTIVE/CLOSURE_REQUESTED/CLOSED) -- this maps the workflow's
# decision string onto the row-level outcome recorded on this specific
# request.
_CLOSURE_REQUEST_STATUS_BY_DECISION = {
    DECISION_APPROVE: "APPROVED",
    DECISION_REJECT: "REJECTED",
    DECISION_CANCELLED: "CANCELLED",
}


@activity.defn(name=ACTIVITY_PERSIST_CLOSURE_REQUEST)
async def persist_closure_request(inp: PersistClosureRequestInput) -> str:
    """Returns the newly-minted (or, on a Temporal retry, the
    already-existing) `closure_request_id` -- `workflows.py`'s `run()`
    captures this and threads it into every later
    `PersistClosureDecisionInput` for this execution, since `workflow_id`
    alone can't disambiguate a repeat request against the same account
    (see `db/schema.sql`'s `account_closure_requests` comment)."""
    record = await account_db.create_closure_request(inp.account_id, inp.workflow_id, inp.workflow_run_id)
    await account_db.set_status(inp.account_id, STATUS_ACCOUNT_CLOSURE_REQUESTED)
    return record["closure_request_id"]


@activity.defn(name=ACTIVITY_PERSIST_CLOSURE_DECISION)
async def persist_closure_decision(inp: PersistClosureDecisionInput) -> str:
    """Returns the status actually written -- normally
    `inp.resulting_status` verbatim. `workflows.py`'s
    `submit_decision`/`cancel` use this return value (not their own
    pre-computed `resulting_status`) as the workflow's own
    `self._status`, same convention `application/activities.py`'s
    `persist_decision` already establishes.

    Idempotency guard: a Temporal retry of an already-decided execution
    must not re-send the closure-decision email a second time.
    `inp.closure_request_id`'s own row is the marker now (Phase 22) --
    `PENDING` is the only status a decision is ever made from, so a row
    that's already moved past it (a prior execution of this same
    activity already committed) means the write and the email have
    already happened; skip both, permanently, reading the account's
    current status instead of re-deriving it. Same "check current state
    before redoing a side effect" discipline `application/activities.py`'s
    own `persist_decision` already uses for its account/document
    provisioning."""
    existing = await account_db.get_closure_request(inp.closure_request_id)
    assert existing is not None, f"closure request {inp.closure_request_id} not found"

    if existing["status"] != "PENDING":
        current = await account_db.get(inp.account_id)
        assert current is not None, f"account {inp.account_id} not found"
        return current["status"]

    await account_db.update_closure_request_decision(
        inp.closure_request_id,
        status=_CLOSURE_REQUEST_STATUS_BY_DECISION[inp.decision],
        decision_comment=inp.comment,
        decided_by=inp.actor_name,
        decided_at=datetime.now(timezone.utc),
    )

    updated_account = await account_db.set_status(inp.account_id, inp.resulting_status)
    assert updated_account is not None, f"account {inp.account_id} not found"

    notifications_service.send_account_closure_decision(
        applicant_identifier=inp.applicant_identifier,
        account_id=inp.account_id,
        product_type=updated_account["product_type"],
        decision=inp.decision,
        comment=inp.comment,
    )
    return updated_account["status"]
