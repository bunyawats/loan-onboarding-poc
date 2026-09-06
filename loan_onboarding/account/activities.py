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
    PersistClosureDecisionInput,
    PersistClosureRequestInput,
    STATUS_ACCOUNT_CLOSURE_REQUESTED,
)


@activity.defn
async def persist_closure_request(inp: PersistClosureRequestInput) -> None:
    await account_db.update_closure_request(inp.account_id, inp.workflow_id)


@activity.defn
async def persist_closure_decision(inp: PersistClosureDecisionInput) -> str:
    """Returns the status actually written -- normally
    `inp.resulting_status` verbatim. `workflows.py`'s
    `submit_decision`/`cancel` use this return value (not their own
    pre-computed `resulting_status`) as the workflow's own
    `self._status`, same convention `application/activities.py`'s
    `persist_decision` already establishes.

    Idempotency guard: a Temporal retry of an already-decided execution
    must not re-send the closure-decision email a second time.
    `accounts.status` itself is the marker -- `CLOSURE_REQUESTED` is the
    only status a decision is ever made from, so a record that's already
    moved past it (a prior execution of this same activity already
    committed) means the write and the email have already happened;
    skip both, permanently. Same "check current state before redoing a
    side effect" discipline `application/activities.py`'s own
    `persist_decision` already uses for its account/document
    provisioning."""
    record = await account_db.get(inp.account_id)
    assert record is not None, f"account {inp.account_id} not found"

    if record["status"] != STATUS_ACCOUNT_CLOSURE_REQUESTED:
        return record["status"]

    updated = await account_db.update_closure_decision(
        inp.account_id,
        status=inp.resulting_status,
        closure_decision_comment=inp.comment,
        closure_decided_by=inp.actor_name,
        closure_decided_at=datetime.now(timezone.utc),
    )
    assert updated is not None, f"account {inp.account_id} not found"

    notifications_service.send_account_closure_decision(
        applicant_identifier=inp.applicant_identifier,
        account_id=inp.account_id,
        product_type=updated["product_type"],
        decision=inp.decision,
        comment=inp.comment,
    )
    return updated["status"]
