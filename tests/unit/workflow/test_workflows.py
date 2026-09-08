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
    ACTIVITY_PERSIST_APPLICATION,
    ACTIVITY_PERSIST_CLOSURE_DECISION,
    ACTIVITY_PERSIST_CLOSURE_REQUEST,
    ACTIVITY_PERSIST_DECISION,
    ACTIVITY_PERSIST_RESUBMIT,
    ACTIVITY_PERSIST_RISK_ASSESSMENT_CLEARED,
    ACTIVITY_SUBMIT_RISK_ASSESSMENT,
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
    PersistRiskAssessmentClearedInput,
    SubmitRiskAssessmentInput,
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
    @activity.defn(name=ACTIVITY_PERSIST_APPLICATION)
    async def persist_application(inp: PersistApplicationInput) -> None:
        calls.append(_RecordedCall(ACTIVITY_PERSIST_APPLICATION, inp))

    @activity.defn(name=ACTIVITY_PERSIST_DECISION)
    async def persist_decision(inp: PersistDecisionInput) -> str:
        # Mirrors the real activity's normal-path return (the status it
        # actually wrote) -- application/activities.py's own tests cover
        # the active-account-conflict path where this can differ from
        # inp.resulting_status; that's an application/ concern, not a
        # workflow-orchestration one, so it isn't faked here.
        calls.append(_RecordedCall(ACTIVITY_PERSIST_DECISION, inp))
        return inp.resulting_status

    @activity.defn(name=ACTIVITY_PERSIST_RESUBMIT)
    async def persist_resubmit(inp: PersistResubmitInput) -> None:
        calls.append(_RecordedCall(ACTIVITY_PERSIST_RESUBMIT, inp))

    # Phase 21 -- fakes for the two new risk-assessment activities.
    # submit_risk_assessment's real implementation calls out to
    # risk.service (an httpx POST) -- faked here as a no-op, same "test
    # the orchestration, not the downstream call" split every other
    # activity fake in this file already follows.
    @activity.defn(name=ACTIVITY_SUBMIT_RISK_ASSESSMENT)
    async def submit_risk_assessment(inp: SubmitRiskAssessmentInput) -> None:
        calls.append(_RecordedCall(ACTIVITY_SUBMIT_RISK_ASSESSMENT, inp))

    @activity.defn(name=ACTIVITY_PERSIST_RISK_ASSESSMENT_CLEARED)
    async def persist_risk_assessment_cleared(inp: PersistRiskAssessmentClearedInput) -> None:
        calls.append(_RecordedCall(ACTIVITY_PERSIST_RISK_ASSESSMENT_CLEARED, inp))

    return [
        persist_application,
        persist_decision,
        persist_resubmit,
        submit_risk_assessment,
        persist_risk_assessment_cleared,
    ]


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


async def _wait_for_call_count(
    calls: list[_RecordedCall], count: int, activity_name: str | None = None
) -> None:
    """Without `activity_name`, waits for `len(calls) >= count` (any
    activity). With it, waits for at least `count` calls *named*
    `activity_name` specifically -- used where a fixed positional count
    across all activities would be the wrong thing to wait for (see
    `_advance_past_risk_assessment`'s own docstring)."""
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    while True:
        matching = len(calls) if activity_name is None else sum(1 for c in calls if c.name == activity_name)
        if matching >= count:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"only {matching} matching activity calls recorded, expected {count} "
                f"(activity_name={activity_name!r}, all calls so far: {_names(calls)!r})"
            )
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _advance_past_risk_assessment(handle: WorkflowHandle, calls: list[_RecordedCall]) -> None:
    """Every application now starts at PENDING_RISK_ASSESSMENT (Phase
    21) -- tests exercising the pre-existing human-decision path (below)
    aren't about risk assessment at all, so they use this helper to get
    a MEDIUM resolution (the "falls straight through to today's
    unchanged behavior" tier) out of the way first, same shape a real
    NATS Adapter delivery would produce.

    Waits for run()'s own submit_risk_assessment activity to land before
    signalling -- a real, live-hit-in-this-test-suite race, not just
    theoretical: signal_risk_decision's guard only checks
    self._status == STATUS_PENDING_RISK_ASSESSMENT (true from __init__
    onward, before run() has done anything), so signalling immediately
    let the fake persist_risk_assessment_cleared activity's call land
    *before* submit_risk_assessment's, flipping their order in `calls`
    and breaking every positional assertion below. In real operation
    this ordering is enforced for real (the NATS Adapter can only send
    signal_risk_decision after a decided message arrives, which can only
    happen after submit_risk_assessment's own submission was made) --
    this wait reproduces that same real causal ordering here instead of
    racing it."""
    await _wait_for_call_count(calls, 1, activity_name=ACTIVITY_SUBMIT_RISK_ASSESSMENT)
    await handle.signal(LoanApplicationWorkflow.signal_risk_decision, "MEDIUM")
    await _wait_for_status(handle, "PENDING_UNDERWRITING")


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
        await _advance_past_risk_assessment(handle, calls)
        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=["underwriter", "APPROVE", "u1", "looks fine"],
        )
        result = await handle.result()

    assert result.status == "APPROVED"
    assert result.closed_by == "u1"
    assert _names(calls) == [
        ACTIVITY_PERSIST_APPLICATION,
        ACTIVITY_SUBMIT_RISK_ASSESSMENT,
        ACTIVITY_PERSIST_RISK_ASSESSMENT_CLEARED,
        ACTIVITY_PERSIST_DECISION,
    ]
    assert calls[3].inp.resulting_status == "APPROVED"
    assert calls[3].inp.actor_role == "underwriter"


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
        await _advance_past_risk_assessment(handle, calls)
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
        ACTIVITY_PERSIST_APPLICATION,
        ACTIVITY_SUBMIT_RISK_ASSESSMENT,
        ACTIVITY_PERSIST_RISK_ASSESSMENT_CLEARED,
        ACTIVITY_PERSIST_DECISION,
        ACTIVITY_PERSIST_DECISION,
    ]
    assert calls[3].inp.resulting_status == "PENDING_MANAGER_APPROVAL"
    assert calls[4].inp.resulting_status == "APPROVED"
    assert calls[4].inp.actor_role == "manager"


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
        await _advance_past_risk_assessment(handle, calls)
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
        await _advance_past_risk_assessment(handle, calls)
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
        ACTIVITY_PERSIST_APPLICATION,
        ACTIVITY_SUBMIT_RISK_ASSESSMENT,
        ACTIVITY_PERSIST_RISK_ASSESSMENT_CLEARED,
        ACTIVITY_PERSIST_DECISION,
        ACTIVITY_PERSIST_RESUBMIT,
        ACTIVITY_PERSIST_DECISION,
    ]
    assert calls[4].inp.payload == {"purpose": "home_improvement"}


@pytest.mark.parametrize(
    "setup_decision,expected_intermediate_status",
    [
        # A customer can also cancel directly from PENDING_RISK_ASSESSMENT
        # itself -- _resolve_transition's CANCELLED branch only excludes
        # TERMINAL_STATUSES, it doesn't check for a specific pending
        # state, so no risk-assessment advance is needed for this case.
        (None, None),
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
            await _advance_past_risk_assessment(handle, calls)
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
    # Not calls[-1]: the (None, None) case cancels immediately, which can
    # race run()'s own still-in-flight submit_risk_assessment activity
    # call (a real, harmless race -- CLAUDE.md's "no timeout" gaps
    # already accept a comparable class of benign concurrent-activity
    # ordering elsewhere) and land persist_decision before it in `calls`.
    decision_calls = [c for c in calls if c.name == ACTIVITY_PERSIST_DECISION]
    assert decision_calls[-1].inp.resulting_status == "CANCELLED"


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
        await _advance_past_risk_assessment(handle, calls)
        # PENDING_UNDERWRITING only accepts actor_role="underwriter".
        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=["manager", "APPROVE", "m1", "wrong role"],
        )
        with pytest.raises(WorkflowFailureError) as exc_info:
            await handle.result()

    assert isinstance(exc_info.value.cause, ApplicationError)
    # The rejected attempt never reached persist_decision.
    assert _names(calls) == [
        ACTIVITY_PERSIST_APPLICATION,
        ACTIVITY_SUBMIT_RISK_ASSESSMENT,
        ACTIVITY_PERSIST_RISK_ASSESSMENT_CLEARED,
    ]


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
        # Wait for both pre-wait_condition activities to land first
        # (persist_application, then Phase 21's submit_risk_assessment) --
        # cancelling before both do would deliver the CancelledError to
        # one of those activity awaits instead of the wait_condition()
        # this test means to exercise (that's a separate, un-recovered
        # path -- see CLAUDE.md's "Known gaps": a terminate/very-early-
        # cancel can't be recovered from inside the workflow,
        # structurally).
        await _wait_for_call_count(calls, 2)
        await handle.cancel()
        result = await handle.result()

    assert result.status == "CANCELLED"
    assert result.closed_by == "temporal-admin"
    assert _names(calls) == [ACTIVITY_PERSIST_APPLICATION, ACTIVITY_SUBMIT_RISK_ASSESSMENT, ACTIVITY_PERSIST_DECISION]
    assert calls[2].inp.decision == "CANCELLED"
    assert calls[2].inp.decided_at is not None


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
        await _advance_past_risk_assessment(handle, calls)
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

    decision_calls = [c for c in calls if c.name == ACTIVITY_PERSIST_DECISION]
    assert len(decision_calls) == 1
    assert result.status == decision_calls[0].inp.resulting_status
    assert result.status in ("APPROVED", "REJECTED")


# ----------------------------------------------------------------------
# Phase 21, "Automated risk assessment via NATS" -- the three risk-tier
# outcomes signal_risk_decision resolves, plus its duplicate-signal
# guard. Same WorkflowEnvironment/fake-activities pattern as every test
# above -- signal_risk_decision is sent directly here (never by a BFF,
# unlike every other signal in this file), same as the real NATS Adapter
# would, just without the NATS/HTTP hop itself.
# ----------------------------------------------------------------------


async def test_risk_low_auto_approves(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=BELOW_THRESHOLD)
        await _wait_for_call_count(calls, 1, activity_name=ACTIVITY_SUBMIT_RISK_ASSESSMENT)
        await handle.signal(LoanApplicationWorkflow.signal_risk_decision, "LOW")
        result = await handle.result()

    assert result.status == "APPROVED"
    assert result.closed_by == "risk-engine-auto"
    assert _names(calls) == [ACTIVITY_PERSIST_APPLICATION, ACTIVITY_SUBMIT_RISK_ASSESSMENT, ACTIVITY_PERSIST_DECISION]
    decision_call = calls[2]
    assert decision_call.inp.resulting_status == "APPROVED"
    assert decision_call.inp.actor_role == "underwriter"
    assert decision_call.inp.actor_name == "risk-engine-auto"
    assert decision_call.inp.risk_tier == "LOW"


async def test_risk_high_auto_rejects(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=BELOW_THRESHOLD)
        await _wait_for_call_count(calls, 1, activity_name=ACTIVITY_SUBMIT_RISK_ASSESSMENT)
        await handle.signal(LoanApplicationWorkflow.signal_risk_decision, "HIGH")
        result = await handle.result()

    assert result.status == "REJECTED"
    assert result.closed_by == "risk-engine-auto"
    decision_call = calls[2]
    assert decision_call.inp.resulting_status == "REJECTED"
    assert decision_call.inp.risk_tier == "HIGH"


async def test_risk_medium_falls_through_to_unchanged_underwriting(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=BELOW_THRESHOLD)
        await _advance_past_risk_assessment(handle, calls)
        status = await handle.query(LoanApplicationWorkflow.get_status)
        assert status.status == "PENDING_UNDERWRITING"

        # From here on, MEDIUM looks identical to any application that
        # never had a risk tier at all -- same human-decision path, same
        # assertion shape as test_happy_path_below_threshold.
        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=["underwriter", "APPROVE", "u1", "looks fine"],
        )
        result = await handle.result()

    assert result.status == "APPROVED"
    assert result.closed_by == "u1"
    assert _names(calls) == [
        ACTIVITY_PERSIST_APPLICATION,
        ACTIVITY_SUBMIT_RISK_ASSESSMENT,
        ACTIVITY_PERSIST_RISK_ASSESSMENT_CLEARED,
        ACTIVITY_PERSIST_DECISION,
    ]
    # MEDIUM itself never touches risk_tier (CLAUDE.md: only an
    # auto-*decided* LOW/HIGH outcome does) -- the eventual human
    # decision's own persist_decision call carries risk_tier=None too.
    assert calls[3].inp.risk_tier is None


async def test_signal_risk_decision_invalid_tier_is_rejected(env: WorkflowEnvironment):
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=BELOW_THRESHOLD)
        await _wait_for_call_count(calls, 1, activity_name=ACTIVITY_SUBMIT_RISK_ASSESSMENT)
        await handle.signal(LoanApplicationWorkflow.signal_risk_decision, "BOGUS")
        with pytest.raises(WorkflowFailureError) as exc_info:
            await handle.result()

    assert isinstance(exc_info.value.cause, ApplicationError)
    assert _names(calls) == [ACTIVITY_PERSIST_APPLICATION, ACTIVITY_SUBMIT_RISK_ASSESSMENT]


async def test_duplicate_signal_risk_decision_after_medium_is_ignored(env: WorkflowEnvironment):
    """NATS is at-least-once, and so is the Adapter's own retry of a
    failed signal call -- a second signal_risk_decision arriving after
    the first already resolved MEDIUM (a non-terminal transition, so
    _claim_transition() alone would happily succeed again) must be a
    silent no-op, not a second, incorrect transition. This is exactly
    what the `self._status != STATUS_PENDING_RISK_ASSESSMENT` half of
    signal_risk_decision's guard exists for, not just
    `self._finalized`."""
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=BELOW_THRESHOLD)
        await _advance_past_risk_assessment(handle, calls)

        # A duplicate delivery of the same (already-resolved) decision.
        await handle.signal(LoanApplicationWorkflow.signal_risk_decision, "MEDIUM")
        # Give the (should-be-ignored) signal a beat to misbehave, then
        # confirm nothing changed -- there is no observable state
        # transition to poll for on a correctly-ignored no-op.
        await asyncio.sleep(_POLL_INTERVAL_S * 2)
        status = await handle.query(LoanApplicationWorkflow.get_status)
        assert status.status == "PENDING_UNDERWRITING"

        await handle.signal(
            LoanApplicationWorkflow.submit_decision,
            args=["underwriter", "APPROVE", "u1", "looks fine"],
        )
        result = await handle.result()

    assert result.status == "APPROVED"
    assert _names(calls) == [
        ACTIVITY_PERSIST_APPLICATION,
        ACTIVITY_SUBMIT_RISK_ASSESSMENT,
        ACTIVITY_PERSIST_RISK_ASSESSMENT_CLEARED,
        ACTIVITY_PERSIST_DECISION,
    ]


async def test_two_concurrent_risk_decisions_only_write_once(env: WorkflowEnvironment):
    """Same invariant test_two_concurrent_terminal_signals_only_write_once
    already proves for two racing submit_decision signals, here for two
    racing signal_risk_decision signals instead -- a real, not just
    theoretical, possibility given NATS's at-least-once delivery plus
    the Adapter's own retry of a failed signal call (CLAUDE.md's
    "Automated risk assessment via NATS"). Note this does NOT exercise
    "signal a workflow after its execution has already closed" --
    Temporal itself rejects that at the server/RPC level (a real
    `temporalio.service.RPCError: Completed workflow`, confirmed while
    writing this test), not something signal_risk_decision's own
    in-workflow guard is ever asked to handle; the two-signals-in-flight
    race below is what's actually reachable and worth guarding."""
    task_queue = str(uuid.uuid4())
    calls: list[_RecordedCall] = []
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[LoanApplicationWorkflow],
        activities=_make_fake_activities(calls),
    ):
        handle = await _start(env, task_queue, amount=BELOW_THRESHOLD)
        await _wait_for_call_count(calls, 1, activity_name=ACTIVITY_SUBMIT_RISK_ASSESSMENT)
        await asyncio.gather(
            handle.signal(LoanApplicationWorkflow.signal_risk_decision, "LOW"),
            handle.signal(LoanApplicationWorkflow.signal_risk_decision, "HIGH"),
        )
        result = await handle.result()

    decision_calls = [c for c in calls if c.name == ACTIVITY_PERSIST_DECISION]
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
    @activity.defn(name=ACTIVITY_PERSIST_CLOSURE_REQUEST)
    async def persist_closure_request(inp: PersistClosureRequestInput) -> None:
        calls.append(_RecordedCall(ACTIVITY_PERSIST_CLOSURE_REQUEST, inp))

    @activity.defn(name=ACTIVITY_PERSIST_CLOSURE_DECISION)
    async def persist_closure_decision(inp: PersistClosureDecisionInput) -> str:
        calls.append(_RecordedCall(ACTIVITY_PERSIST_CLOSURE_DECISION, inp))
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
    assert _names(calls) == [ACTIVITY_PERSIST_CLOSURE_REQUEST, ACTIVITY_PERSIST_CLOSURE_DECISION]
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

    decision_calls = [c for c in calls if c.name == ACTIVITY_PERSIST_CLOSURE_DECISION]
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
    assert _names(calls) == [ACTIVITY_PERSIST_CLOSURE_REQUEST]
