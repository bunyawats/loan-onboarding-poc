"""LoanApplicationWorkflow tests via temporalio.testing.WorkflowEnvironment
(time-skipping) -- no real Temporal server, no Postgres. persist_application/
persist_decision/persist_resubmit are faked here (they just record their
calls), registered under the exact string names workflows.py calls by name
-- see CLAUDE.md's "Breaking the application <-> workflow cycle" for why
that's what lets this file exist before application/activities.py does.

A signal only confirms Temporal *accepted* it, not that the workflow has
finished processing it (same "confirm accepted != confirm applied" gap
workflow/service.py's own docstring calls out). Tests that need to observe
an intermediate (non-terminal) state, or need one signal's effects to have
landed before sending the next, poll for it via `_wait_for_status`/
`_wait_for_calls` rather than asserting immediately after `await
handle.signal(...)` -- an immediate assert here is exactly the kind of
race a real caller (a BFF) would also hit, which is why
application/service.py's own `_wait_until()` (Phase 6) exists.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from loan_onboarding.workflow.workflows import (
    MANAGER_ESCALATION_THRESHOLD_USD,
    ApplicationStatus,
    ApplicationWorkflowInput,
    CloseAccountStatus,
    CloseAccountWorkflow,
    CloseAccountWorkflowInput,
    LoanApplicationWorkflow,
    PersistApplicationInput,
    PersistClosureDecisionInput,
    PersistClosureRequestInput,
    PersistDecisionInput,
    PersistResubmitInput,
)

BELOW_THRESHOLD = MANAGER_ESCALATION_THRESHOLD_USD - 1_000
AT_OR_ABOVE_THRESHOLD = MANAGER_ESCALATION_THRESHOLD_USD

_POLL_TIMEOUT_S = 5.0
_POLL_INTERVAL_S = 0.05


@dataclass
class _RecordedCall:
    name: str
    inp: Any


def _make_fake_activities(calls: list[_RecordedCall]):
    # Typed `inp` params, deliberately -- Temporal's default data converter
    # needs the activity function's own type hint to decode the payload
    # back into PersistApplicationInput/etc rather than a plain dict.
    @activity.defn(name="persist_application")
    async def persist_application(inp: PersistApplicationInput) -> None:
        calls.append(_RecordedCall("persist_application", inp))

    @activity.defn(name="persist_decision")
    async def persist_decision(inp: PersistDecisionInput) -> str:
        # Mirrors the real activity's normal-path return (the status it
        # actually wrote) -- application/activities.py's own tests cover
        # the active-account-conflict path where this can differ from
        # inp.resulting_status; that's an application/ concern, not a
        # workflow-orchestration one, so it isn't faked here.
        calls.append(_RecordedCall("persist_decision", inp))
        return inp.resulting_status

    @activity.defn(name="persist_resubmit")
    async def persist_resubmit(inp: PersistResubmitInput) -> None:
        calls.append(_RecordedCall("persist_resubmit", inp))

    return [persist_application, persist_decision, persist_resubmit]


def _input(**overrides) -> ApplicationWorkflowInput:
    base: dict[str, Any] = dict(
        application_id=str(uuid.uuid4()),
        product_type="personal_loan",
        payload={"purpose": "debt_consolidation"},
        amount=BELOW_THRESHOLD,
        applicant_identifier="applicant@example.com",
        applicant_name="Jane Doe",
        applicant_email="applicant@example.com",
        applicant_phone="+15551234567",
        customer_id=None,
    )
    base.update(overrides)
    return ApplicationWorkflowInput(**base)


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        yield environment


async def _start(env: WorkflowEnvironment, task_queue: str, **overrides) -> WorkflowHandle:
    inp = _input(**overrides)
    return await env.client.start_workflow(
        LoanApplicationWorkflow.run,
        inp,
        id=f"wf-{inp.application_id}",
        task_queue=task_queue,
    )


def _names(calls: list[_RecordedCall]) -> list[str]:
    return [c.name for c in calls]


async def _wait_for_status(handle: WorkflowHandle, expected_status: str) -> ApplicationStatus:
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    while True:
        status = await handle.query(LoanApplicationWorkflow.get_status)
        if status.status == expected_status:
            return status
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"status never reached {expected_status!r}, last seen {status.status!r}"
            )
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _wait_for_call_count(calls: list[_RecordedCall], count: int) -> None:
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    while len(calls) < count:
        if time.monotonic() >= deadline:
            raise AssertionError(f"only {len(calls)} activity calls recorded, expected {count}")
        await asyncio.sleep(_POLL_INTERVAL_S)


async def test_happy_path_below_threshold(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=BELOW_THRESHOLD)
        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=["underwriter", "APPROVE", "u1", "looks fine"],
        )
        result = await handle.result()

    assert result.status == "APPROVED"
    assert result.closed_by == "u1"
    assert _names(calls) == ["persist_application", "persist_decision"]
    assert calls[1].inp.resulting_status == "APPROVED"
    assert calls[1].inp.actor_role == "underwriter"


async def test_happy_path_escalates_then_manager_approves(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=AT_OR_ABOVE_THRESHOLD)
        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=["underwriter", "APPROVE", "u1", "escalating"],
        )
        await _wait_for_status(handle, "PENDING_MANAGER_APPROVAL")

        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=["manager", "APPROVE", "m1", "approved by manager"],
        )
        result = await handle.result()

    assert result.status == "APPROVED"
    assert result.closed_by == "m1"
    assert _names(calls) == [
        "persist_application",
        "persist_decision",
        "persist_decision",
    ]
    assert calls[1].inp.resulting_status == "PENDING_MANAGER_APPROVAL"
    assert calls[2].inp.resulting_status == "APPROVED"
    assert calls[2].inp.actor_role == "manager"


@pytest.mark.parametrize(
    "amount,actor_role,actor_name",
    [(BELOW_THRESHOLD, "underwriter", "u1"), (AT_OR_ABOVE_THRESHOLD, "manager", "m1")],
)
async def test_reject_at_each_stage(
    env: WorkflowEnvironment, amount, actor_role, actor_name
):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=amount)
        if actor_role == "manager":
            # Get to PENDING_MANAGER_APPROVAL first.
            await handle.signal(
                LoanApplicationWorkflow.submit_decision,
                args=["underwriter", "APPROVE", "u1", "escalating"],
            )
            await _wait_for_status(handle, "PENDING_MANAGER_APPROVAL")
        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=[actor_role, "REJECT", actor_name, "not eligible"],
        )
        result = await handle.result()

    assert result.status == "REJECTED"
    assert result.closed_by == actor_name
    assert calls[-1].inp.resulting_status == "REJECTED"


async def test_request_more_info_then_resubmit_then_approve(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=BELOW_THRESHOLD)
        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=["underwriter", "REQUEST_MORE_INFO", "u1", "need bank statements"],
        )
        await _wait_for_status(handle, "MORE_INFO_REQUESTED")

        await handle.signal(
            LoanApplicationWorkflow.resubmit, args=[{"purpose": "home_improvement"}]
        )
        await _wait_for_status(handle, "PENDING_UNDERWRITING")

        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=["underwriter", "APPROVE", "u1", "now complete"],
        )
        result = await handle.result()

    assert result.status == "APPROVED"
    assert _names(calls) == [
        "persist_application",
        "persist_decision",
        "persist_resubmit",
        "persist_decision",
    ]
    assert calls[2].inp.payload == {"purpose": "home_improvement"}


@pytest.mark.parametrize(
    "setup_decision,expected_intermediate_status",
    [
        (None, None),  # cancel directly from PENDING_UNDERWRITING
        (("underwriter", "APPROVE", "u1"), "PENDING_MANAGER_APPROVAL"),
        (("underwriter", "REQUEST_MORE_INFO", "u1"), "MORE_INFO_REQUESTED"),
    ],
)
async def test_cancel_from_each_non_terminal_state(
    env: WorkflowEnvironment, setup_decision, expected_intermediate_status
):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=AT_OR_ABOVE_THRESHOLD)
        if setup_decision is not None:
            actor_role, decision, actor_name = setup_decision
            await handle.signal(
                LoanApplicationWorkflow.submit_decision,
                args=[actor_role, decision, actor_name, ""],
            )
            await _wait_for_status(handle, expected_intermediate_status)
        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=["customer", "CANCELLED", "applicant@example.com", "changed my mind"],
        )
        result = await handle.result()

    assert result.status == "CANCELLED"
    assert result.closed_by == "applicant@example.com"
    assert calls[-1].inp.resulting_status == "CANCELLED"


async def test_wrong_actor_role_for_current_state_is_rejected(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=BELOW_THRESHOLD)
        # PENDING_UNDERWRITING only accepts actor_role="underwriter".
        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=["manager", "APPROVE", "m1", "wrong role"],
        )
        with pytest.raises(WorkflowFailureError) as exc_info:
            await handle.result()

    assert isinstance(exc_info.value.cause, ApplicationError)
    # The rejected attempt never reached persist_decision.
    assert _names(calls) == ["persist_application"]


async def test_native_cancel_lands_on_cancelled_via_fake_persist_decision(
    env: WorkflowEnvironment,
):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=BELOW_THRESHOLD)
        # Wait for persist_application to land first -- cancelling before
        # it does would deliver the CancelledError to that activity await
        # instead of the wait_condition() this test means to exercise
        # (that's a separate, un-recovered path -- see CLAUDE.md's "Known
        # gaps": a terminate/very-early-cancel can't be recovered from
        # inside the workflow, structurally).
        await _wait_for_call_count(calls, 1)
        await handle.cancel()
        result = await handle.result()

    assert result.status == "CANCELLED"
    assert result.closed_by == "temporal-admin"
    assert _names(calls) == ["persist_application", "persist_decision"]
    assert calls[1].inp.decision == "CANCELLED"
    assert calls[1].inp.decided_at is not None


async def test_two_concurrent_terminal_signals_only_write_once(env: WorkflowEnvironment):
    """Exercises _claim_transition()'s guard against two near-simultaneous
    terminal transitions -- here, two competing submit_decision signals
    (APPROVE and REJECT) fired concurrently -- rather than racing a real
    Temporal-level cancel (whose exact delivery timing relative to an
    in-flight signal handler isn't something this test can control
    deterministically). Both are terminal transitions guarded by the same
    _busy flag; either race proves the same invariant: only the first to
    synchronously claim the transition ever gets to run persist_decision.
    """
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=BELOW_THRESHOLD)
        await _wait_for_call_count(calls, 1)  # persist_application landed first
        await asyncio.gather(
            handle.signal(
                LoanApplicationWorkflow.submit_decision,
                args=["underwriter", "APPROVE", "u1", "approve"],
            ),
            handle.signal(
                LoanApplicationWorkflow.submit_decision,
                args=["underwriter", "REJECT", "u1", "reject"],
            ),
        )
        result = await handle.result()

    decision_calls = [c for c in calls if c.name == "persist_decision"]
    assert len(decision_calls) == 1
    assert result.status == decision_calls[0].inp.resulting_status
    assert result.status in ("APPROVED", "REJECTED")


# ----------------------------------------------------------------------
# CloseAccountWorkflow (Phase 18, "Account closure") -- same
# WorkflowEnvironment (time-skipping) pattern as LoanApplicationWorkflow
# above, with persist_closure_request/persist_closure_decision faked
# under the same string names CloseAccountWorkflow calls by name -- this
# is what lets these tests exist before account/activities.py does (see
# CLAUDE.md's "Breaking the application <-> workflow cycle").
# ----------------------------------------------------------------------


def _make_fake_closure_activities(calls: list[_RecordedCall]):
    @activity.defn(name="persist_closure_request")
    async def persist_closure_request(inp: PersistClosureRequestInput) -> None:
        calls.append(_RecordedCall("persist_closure_request", inp))

    @activity.defn(name="persist_closure_decision")
    async def persist_closure_decision(inp: PersistClosureDecisionInput) -> str:
        calls.append(_RecordedCall("persist_closure_decision", inp))
        return inp.resulting_status

    return [persist_closure_request, persist_closure_decision]


async def _start_closure(
    env: WorkflowEnvironment,
    task_queue: str,
    account_id: str,
    applicant_identifier: str = "applicant@example.com",
) -> WorkflowHandle:
    return await env.client.start_workflow(
        CloseAccountWorkflow.run,
        CloseAccountWorkflowInput(account_id=account_id, applicant_identifier=applicant_identifier),
        id=f"account-closure-{account_id}",
        task_queue=task_queue,
    )


async def _wait_for_closure_status(
    handle: WorkflowHandle, expected_status: str
) -> CloseAccountStatus:
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    while True:
        status = await handle.query(CloseAccountWorkflow.get_status)
        if status.status == expected_status:
            return status
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"status never reached {expected_status!r}, last seen {status.status!r}"
            )
        await asyncio.sleep(_POLL_INTERVAL_S)


async def test_close_account_approve_path(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CloseAccountWorkflow],
        activities=_make_fake_closure_activities(calls),
    ):
        handle = await _start_closure(
            env, task_queue, "ACC-000000001", applicant_identifier="alice@example.com"
        )
        await _wait_for_call_count(calls, 1)  # persist_closure_request landed
        await handle.signal(
            CloseAccountWorkflow.submit_decision,
            args=["underwriter", "APPROVE", "u1", "balance confirmed zero"],
        )
        result = await handle.result()

    assert result.status == "CLOSED"
    assert result.closed_by == "u1"
    assert _names(calls) == ["persist_closure_request", "persist_closure_decision"]
    assert calls[1].inp.resulting_status == "CLOSED"
    assert calls[1].inp.account_id == "ACC-000000001"
    assert calls[1].inp.applicant_identifier == "alice@example.com"


async def test_close_account_reject_reverts_to_active(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CloseAccountWorkflow],
        activities=_make_fake_closure_activities(calls),
    ):
        handle = await _start_closure(env, task_queue, "ACC-000000002")
        await _wait_for_call_count(calls, 1)
        await handle.signal(
            CloseAccountWorkflow.submit_decision,
            args=["manager", "REJECT", "m1", "balance not yet zero"],
        )
        result = await handle.result()

    assert result.status == "ACTIVE"
    assert result.closed_by == "m1"
    assert calls[-1].inp.resulting_status == "ACTIVE"


async def test_close_account_customer_cancel_path(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CloseAccountWorkflow],
        activities=_make_fake_closure_activities(calls),
    ):
        handle = await _start_closure(env, task_queue, "ACC-000000003")
        await _wait_for_call_count(calls, 1)
        await handle.signal(CloseAccountWorkflow.cancel)
        result = await handle.result()

    assert result.status == "ACTIVE"
    assert result.closed_by == "customer"
    assert calls[-1].inp.decision == "CANCELLED"


async def test_close_account_concurrent_decision_and_cancel_only_write_once(
    env: WorkflowEnvironment,
):
    """Same _claim_transition() single-writer guard
    test_two_concurrent_terminal_signals_only_write_once exercises for
    LoanApplicationWorkflow -- here a staff decision races the
    customer's own cancel() signal. Only the first to synchronously
    claim the transition ever runs persist_closure_decision; the loser
    is silently ignored (both are terminal, so there's no valid "undo"
    once one has landed)."""
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CloseAccountWorkflow],
        activities=_make_fake_closure_activities(calls),
    ):
        handle = await _start_closure(env, task_queue, "ACC-000000004")
        await _wait_for_call_count(calls, 1)  # persist_closure_request landed first
        await asyncio.gather(
            handle.signal(
                CloseAccountWorkflow.submit_decision,
                args=["underwriter", "APPROVE", "u1", "confirmed"],
            ),
            handle.signal(CloseAccountWorkflow.cancel),
        )
        result = await handle.result()

    decision_calls = [c for c in calls if c.name == "persist_closure_decision"]
    assert len(decision_calls) == 1
    assert result.status == decision_calls[0].inp.resulting_status
    assert result.status in ("CLOSED", "ACTIVE")


async def test_close_account_wrong_actor_role_is_rejected(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CloseAccountWorkflow],
        activities=_make_fake_closure_activities(calls),
    ):
        handle = await _start_closure(env, task_queue, "ACC-000000005")
        await _wait_for_call_count(calls, 1)
        await handle.signal(
            CloseAccountWorkflow.submit_decision,
            args=["customer", "APPROVE", "applicant@example.com", "self-approving"],
        )
        with pytest.raises(WorkflowFailureError) as exc_info:
            await handle.result()

    assert isinstance(exc_info.value.cause, ApplicationError)
    assert _names(calls) == ["persist_closure_request"]
