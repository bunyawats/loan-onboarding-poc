"""
LoanApplicationWorkflow: one execution per loan application.

Payload-agnostic in the sense CLAUDE.md defines: `product_type: str` +
`payload: dict[str, Any]` never gets inspected here. `amount` is the one
piece of loan-domain-shaped data this workflow *does* look at directly
(PRD §6.3's escalation-threshold check), and the state machine itself
(PENDING_UNDERWRITING / PENDING_MANAGER_APPROVAL / MORE_INFO_REQUESTED /
APPROVED / REJECTED / CANCELLED) is a loan-specific business rule that
has to be colocated with Temporal workflow code -- see CLAUDE.md's
"workflow/" module section for why that's still "generic" in the sense
that actually matters (no import of application/, no reach into its
table or types).

Activities are called **by string name**
(`workflow.execute_activity("persist_application", ...)`), never by
importing a function reference from application/activities.py -- that's
what lets this module (and its tests) exist before that file does. The
input dataclasses below are the *shape* half of that contract:
application/activities.py's real `@activity.defn` functions (Phase 6)
must accept a same-shaped argument (same field names/types) registered
under the matching string name, wired together by worker_main.py. See
CLAUDE.md's "Breaking the application <-> workflow cycle".
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

# PRD §6.3 -- a single configurable amount, same for every product type.
# Lives here (not in application/) because this is the workflow's own
# Approve-transition branch point, not a value application/ needs to
# reason about anywhere else.
MANAGER_ESCALATION_THRESHOLD_USD = 50_000

VALID_ACTOR_ROLES = ("underwriter", "manager", "customer")
VALID_DECISIONS = ("APPROVE", "REJECT", "REQUEST_MORE_INFO", "CANCELLED")

TERMINAL_STATUSES = frozenset({"APPROVED", "REJECTED", "CANCELLED"})

DEFAULT_RETRY_POLICY = RetryPolicy(maximum_attempts=5)
DEFAULT_ACTIVITY_TIMEOUT = timedelta(seconds=30)


@dataclass
class ApplicationWorkflowInput:
    application_id: str
    product_type: str
    payload: dict[str, Any]
    amount: float
    applicant_identifier: str
    applicant_name: str
    applicant_email: str
    applicant_phone: str
    customer_id: Optional[str] = None


@dataclass
class ApplicationStatus:
    status: str
    closed_by: Optional[str] = None
    closed_comment: Optional[str] = None


@dataclass
class PersistApplicationInput:
    application_id: str
    workflow_id: str
    product_type: str
    payload: dict[str, Any]
    amount: float
    applicant_identifier: str
    applicant_name: str
    applicant_email: str
    applicant_phone: str
    customer_id: Optional[str] = None


@dataclass
class PersistDecisionInput:
    application_id: str
    actor_role: str  # underwriter | manager | customer
    decision: str  # APPROVE | REJECT | REQUEST_MORE_INFO | CANCELLED
    actor_name: str
    comment: str
    resulting_status: str
    # Only ever set for a native Temporal cancellation (see run()'s
    # except clause below) -- lets the persisted timestamp reflect the
    # moment Temporal delivered the cancel rather than whenever this
    # activity happens to actually run. Unset (None) for every normal
    # signal-driven decision, which has no separate "decided at" moment
    # to reconcile against.
    decided_at: Optional[datetime] = None


@dataclass
class PersistResubmitInput:
    application_id: str
    payload: dict[str, Any]


@workflow.defn
class LoanApplicationWorkflow:
    def __init__(self) -> None:
        self._application_id: str = ""
        self._payload: dict[str, Any] = {}
        self._amount: float = 0.0
        self._status = "PENDING_UNDERWRITING"
        self._closed_by: Optional[str] = None
        self._closed_comment: Optional[str] = None
        self._finalized = False
        # Guards every state transition (not just terminal ones, unlike
        # the single-role reference project this is descended from --
        # PENDING_UNDERWRITING -> PENDING_MANAGER_APPROVAL and
        # MORE_INFO_REQUESTED -> PENDING_UNDERWRITING are both real,
        # non-terminal transitions here). Set synchronously, with no
        # `await` between the check and the set, so only the first
        # signal to arrive while nothing else is in flight ever gets to
        # proceed -- everyone else (including a caller that arrives
        # while the winner is mid-`await workflow.execute_activity`)
        # bails out immediately instead of racing it.
        self._busy = False

    def _is_final(self) -> bool:
        return self._finalized

    def _claim_transition(self) -> bool:
        if self._finalized or self._busy:
            return False
        self._busy = True
        return True

    def _resolve_transition(self, actor_role: str, decision: str) -> tuple[str, bool]:
        """Returns (resulting_status, is_terminal), or raises ValueError
        if `decision` isn't valid for `actor_role` at the current status.
        """
        if decision == "CANCELLED":
            if self._status in TERMINAL_STATUSES:
                raise ValueError(f"application already {self._status}, cannot cancel")
            if actor_role != "customer":
                raise ValueError(
                    f"CANCELLED must be requested by actor_role='customer', got {actor_role!r}"
                )
            return "CANCELLED", True

        if self._status == "PENDING_UNDERWRITING":
            if actor_role != "underwriter":
                raise ValueError(
                    f"{decision!r} at PENDING_UNDERWRITING requires actor_role='underwriter', "
                    f"got {actor_role!r}"
                )
            if decision == "APPROVE":
                if self._amount >= MANAGER_ESCALATION_THRESHOLD_USD:
                    return "PENDING_MANAGER_APPROVAL", False
                return "APPROVED", True
            if decision == "REJECT":
                return "REJECTED", True
            if decision == "REQUEST_MORE_INFO":
                return "MORE_INFO_REQUESTED", False
            raise ValueError(f"invalid decision {decision!r} for underwriter")

        if self._status == "PENDING_MANAGER_APPROVAL":
            if actor_role != "manager":
                raise ValueError(
                    f"{decision!r} at PENDING_MANAGER_APPROVAL requires actor_role='manager', "
                    f"got {actor_role!r}"
                )
            if decision == "APPROVE":
                return "APPROVED", True
            if decision == "REJECT":
                return "REJECTED", True
            raise ValueError(f"invalid decision {decision!r} for manager")

        raise ValueError(
            f"no decision accepted while status={self._status!r} "
            f"(use resubmit() while MORE_INFO_REQUESTED)"
        )

    @workflow.run
    async def run(self, req: ApplicationWorkflowInput) -> ApplicationStatus:
        self._application_id = req.application_id
        self._payload = req.payload
        self._amount = req.amount

        await workflow.execute_activity(
            "persist_application",
            PersistApplicationInput(
                application_id=req.application_id,
                workflow_id=workflow.info().workflow_id,
                product_type=req.product_type,
                payload=req.payload,
                amount=req.amount,
                applicant_identifier=req.applicant_identifier,
                applicant_name=req.applicant_name,
                applicant_email=req.applicant_email,
                applicant_phone=req.applicant_phone,
                customer_id=req.customer_id,
            ),
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        # Durably waits here, across however many non-terminal
        # transitions (Underwriter escalates -> Manager decides;
        # Underwriter requests more info -> customer resubmits -> back
        # to Underwriter) happen before something terminal lands --
        # every signal handler below updates self._status itself, this
        # only cares about the final one.
        #
        # A native Temporal cancel (Web UI / CLI, not our own
        # submit_decision signal with decision="CANCELLED") is delivered
        # here as asyncio.CancelledError instead of through a signal
        # handler -- persist_decision would otherwise never run and the
        # row would stay stuck at whatever non-terminal status it was in
        # forever, even though Temporal itself considers the execution
        # finished. Recover by doing the same persistence a real
        # CANCELLED decision would have done, attributed to Temporal
        # itself, then let run() complete normally (not re-raised).
        try:
            await workflow.wait_condition(self._is_final)
        except asyncio.CancelledError:
            if self._claim_transition():
                await workflow.execute_activity(
                    "persist_decision",
                    PersistDecisionInput(
                        application_id=self._application_id,
                        actor_role="customer",
                        decision="CANCELLED",
                        actor_name="temporal-admin",
                        comment="forced by temporal system",
                        resulting_status="CANCELLED",
                        decided_at=workflow.now(),
                    ),
                    start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                    retry_policy=DEFAULT_RETRY_POLICY,
                )
                self._finalized = True
                self._status = "CANCELLED"
                self._closed_by = "temporal-admin"
                self._closed_comment = "forced by temporal system"

        return ApplicationStatus(
            status=self._status,
            closed_by=self._closed_by,
            closed_comment=self._closed_comment,
        )

    @workflow.signal
    async def submit_decision(
        self, actor_role: str, decision: str, actor_name: str, comment: str = ""
    ) -> None:
        if not self._claim_transition():
            return  # already decided, or another transition in flight -- ignore

        try:
            resulting_status, is_terminal = self._resolve_transition(actor_role, decision)
        except ValueError as e:
            self._busy = False  # this attempt never actually transitioned
            raise ApplicationError(str(e))

        # persist_decision returns the status it actually wrote -- not
        # necessarily `resulting_status` verbatim. An Approve can lose
        # the active-account-per-product-type race after this signal
        # already passed check_decision_allowed (CLAUDE.md's Known
        # Gaps); persist_decision converts that into a clean REJECTED
        # write rather than raising, and self._status has to agree with
        # whatever actually landed in Postgres, not the status this
        # workflow *intended* before the activity ran.
        actual_status = await workflow.execute_activity(
            "persist_decision",
            PersistDecisionInput(
                application_id=self._application_id,
                actor_role=actor_role,
                decision=decision,
                actor_name=actor_name,
                comment=comment,
                resulting_status=resulting_status,
            ),
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
            result_type=str,
        )
        self._status = actual_status
        if is_terminal:
            self._finalized = True
            self._closed_by = actor_name
            self._closed_comment = comment
        self._busy = False

    @workflow.signal
    async def resubmit(self, payload: dict[str, Any]) -> None:
        if self._finalized or self._status != "MORE_INFO_REQUESTED":
            return  # not awaiting a resubmission -- ignore
        if not self._claim_transition():
            return

        await workflow.execute_activity(
            "persist_resubmit",
            PersistResubmitInput(application_id=self._application_id, payload=payload),
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        self._payload = payload
        self._status = "PENDING_UNDERWRITING"
        self._busy = False

    @workflow.query
    def get_status(self) -> ApplicationStatus:
        return ApplicationStatus(
            status=self._status,
            closed_by=self._closed_by,
            closed_comment=self._closed_comment,
        )
