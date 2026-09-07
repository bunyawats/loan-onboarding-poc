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

ROLE_UNDERWRITER = "underwriter"
ROLE_MANAGER = "manager"
ROLE_CUSTOMER = "customer"
VALID_ACTOR_ROLES = (ROLE_UNDERWRITER, ROLE_MANAGER, ROLE_CUSTOMER)

DECISION_APPROVE = "APPROVE"
DECISION_REJECT = "REJECT"
DECISION_REQUEST_MORE_INFO = "REQUEST_MORE_INFO"
DECISION_CANCELLED = "CANCELLED"
VALID_DECISIONS = (DECISION_APPROVE, DECISION_REJECT, DECISION_REQUEST_MORE_INFO, DECISION_CANCELLED)

STATUS_PENDING_RISK_ASSESSMENT = "PENDING_RISK_ASSESSMENT"
STATUS_PENDING_UNDERWRITING = "PENDING_UNDERWRITING"
STATUS_PENDING_MANAGER_APPROVAL = "PENDING_MANAGER_APPROVAL"
STATUS_MORE_INFO_REQUESTED = "MORE_INFO_REQUESTED"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_CANCELLED = "CANCELLED"

TERMINAL_STATUSES = frozenset({STATUS_APPROVED, STATUS_REJECTED, STATUS_CANCELLED})

# Phase 21, "Automated risk assessment via NATS" -- see CLAUDE.md / the
# risk-assessment-nats skill. RISK_ENGINE_ACTOR_NAME is the documented,
# deliberate exception to "underwriter_name/manager_name are always an
# authenticated Keycloak username, never client-submitted free text" --
# an automated decision has no Keycloak session behind it by definition.
RISK_TIER_LOW = "LOW"
RISK_TIER_MEDIUM = "MEDIUM"
RISK_TIER_HIGH = "HIGH"
VALID_RISK_TIERS = (RISK_TIER_LOW, RISK_TIER_MEDIUM, RISK_TIER_HIGH)
RISK_ENGINE_ACTOR_NAME = "risk-engine-auto"
RISK_ENGINE_AUTO_COMMENT = "Automated decision by risk assessment"

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
    # Written verbatim as the row's initial status -- STATUS_PENDING_RISK_
    # ASSESSMENT as of Phase 21, not a database DEFAULT (CLAUDE.md's "no
    # implicit database default" discipline, same reasoning primary keys
    # already follow -- see "Data storage"). run() below passes
    # self._status here explicitly (not this field's own default) so the
    # two can never drift apart.
    initial_status: str = STATUS_PENDING_RISK_ASSESSMENT


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
    # Phase 21: only ever set for a risk-driven auto-decision (LOW ->
    # this terminal APPROVE, HIGH -> this terminal REJECT) -- None for
    # every human decision, which leaves applications.risk_tier NULL.
    risk_tier: Optional[str] = None


@dataclass
class PersistResubmitInput:
    application_id: str
    payload: dict[str, Any]


@dataclass
class SubmitRiskAssessmentInput:
    application_id: str
    applicant_identifier: str
    product_type: str
    amount: float
    payload: dict[str, Any]


@dataclass
class PersistRiskAssessmentClearedInput:
    """The MEDIUM-tier outcome: no decision was made, so none of
    PersistDecisionInput's actor_role/decision/comment fields apply --
    this is a dedicated, minimal activity input rather than overloading
    PersistDecisionInput with a fourth, non-decision "decision" value
    (CLAUDE.md's "each activity has different column-update semantics,
    don't collapse into one generic activity")."""

    application_id: str


@workflow.defn
class LoanApplicationWorkflow:
    def __init__(self) -> None:
        self._application_id: str = ""
        self._payload: dict[str, Any] = {}
        self._amount: float = 0.0
        # Phase 21: every application now passes through an automated
        # risk assessment first -- see "Automated risk assessment via
        # NATS" (CLAUDE.md / the risk-assessment-nats skill).
        # signal_risk_decision() is what moves this out of this initial
        # status, same role submit_decision() already plays for
        # STATUS_PENDING_UNDERWRITING below.
        self._status = STATUS_PENDING_RISK_ASSESSMENT
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
        if decision == DECISION_CANCELLED:
            if self._status in TERMINAL_STATUSES:
                raise ValueError(f"application already {self._status}, cannot cancel")
            if actor_role != ROLE_CUSTOMER:
                raise ValueError(
                    f"CANCELLED must be requested by actor_role='customer', got {actor_role!r}"
                )
            return STATUS_CANCELLED, True

        if self._status == STATUS_PENDING_UNDERWRITING:
            if actor_role != ROLE_UNDERWRITER:
                raise ValueError(
                    f"{decision!r} at PENDING_UNDERWRITING requires actor_role='underwriter', "
                    f"got {actor_role!r}"
                )
            if decision == DECISION_APPROVE:
                if self._amount >= MANAGER_ESCALATION_THRESHOLD_USD:
                    return STATUS_PENDING_MANAGER_APPROVAL, False
                return STATUS_APPROVED, True
            if decision == DECISION_REJECT:
                return STATUS_REJECTED, True
            if decision == DECISION_REQUEST_MORE_INFO:
                return STATUS_MORE_INFO_REQUESTED, False
            raise ValueError(f"invalid decision {decision!r} for underwriter")

        if self._status == STATUS_PENDING_MANAGER_APPROVAL:
            if actor_role != ROLE_MANAGER:
                raise ValueError(
                    f"{decision!r} at PENDING_MANAGER_APPROVAL requires actor_role='manager', "
                    f"got {actor_role!r}"
                )
            if decision == DECISION_APPROVE:
                return STATUS_APPROVED, True
            if decision == DECISION_REJECT:
                return STATUS_REJECTED, True
            raise ValueError(f"invalid decision {decision!r} for manager")

        raise ValueError(
            f"no decision accepted while status={self._status!r} "
            f"(use resubmit() while MORE_INFO_REQUESTED)"
        )

    def _resolve_risk_decision(self, risk_tier: str) -> tuple[str, bool]:
        """Returns (resulting_status, is_terminal), or raises ValueError
        for an unrecognized tier. Only ever called while
        self._status == STATUS_PENDING_RISK_ASSESSMENT (signal_risk_decision
        checks this before calling in)."""
        if risk_tier == RISK_TIER_LOW:
            return STATUS_APPROVED, True
        if risk_tier == RISK_TIER_HIGH:
            return STATUS_REJECTED, True
        if risk_tier == RISK_TIER_MEDIUM:
            # Falls straight through into today's unchanged human
            # PENDING_UNDERWRITING wait -- no tagging or badge, an
            # application resolved MEDIUM looks identical to any other
            # row in the underwriting queue (confirmed with the user,
            # CLAUDE.md's "Automated risk assessment via NATS").
            return STATUS_PENDING_UNDERWRITING, False
        raise ValueError(f"invalid risk_tier {risk_tier!r}, expected one of {VALID_RISK_TIERS!r}")

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
                initial_status=self._status,
            ),
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        # Phase 21: kick off the automated risk assessment -- fire-and-
        # forget from this workflow's own perspective (the eventual
        # decision arrives later, as the signal_risk_decision signal
        # below, sent by the NATS Adapter, not as this activity's return
        # value). Still awaited, not detached, so a submission failure
        # retries per DEFAULT_RETRY_POLICY like every other activity here
        # -- nothing else will ever move this application out of
        # PENDING_RISK_ASSESSMENT if the submission itself never lands.
        await workflow.execute_activity(
            "submit_risk_assessment",
            SubmitRiskAssessmentInput(
                application_id=req.application_id,
                applicant_identifier=req.applicant_identifier,
                product_type=req.product_type,
                amount=req.amount,
                payload=req.payload,
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
                        actor_role=ROLE_CUSTOMER,
                        decision=DECISION_CANCELLED,
                        actor_name="temporal-admin",
                        comment="forced by temporal system",
                        resulting_status=STATUS_CANCELLED,
                        decided_at=workflow.now(),
                    ),
                    start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                    retry_policy=DEFAULT_RETRY_POLICY,
                )
                self._finalized = True
                self._status = STATUS_CANCELLED
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
    async def signal_risk_decision(self, risk_tier: str) -> None:
        """Sent directly by the standalone NATS Adapter service (not a
        BFF route handler, unlike every other signal here) once the Risk
        Engine has decided a tier -- see CLAUDE.md's "Automated risk
        assessment via NATS" / the risk-assessment-nats skill. NATS is
        at-least-once, and so is the Adapter's own retry of a failed
        signal call, so a duplicate/late delivery is expected, not
        exceptional -- the `self._status != STATUS_PENDING_RISK_ASSESSMENT`
        check below (not just `_claim_transition()`'s busy/finalized
        guard, which alone would still be True again once a MEDIUM
        resolution's own `self._busy = False` runs) is what actually
        makes a second delivery arriving after a MEDIUM resolution a
        silent no-op instead of an incorrect second transition."""
        if self._finalized or self._status != STATUS_PENDING_RISK_ASSESSMENT:
            return  # already resolved, or a duplicate/late delivery -- ignore
        if not self._claim_transition():
            return

        try:
            resulting_status, is_terminal = self._resolve_risk_decision(risk_tier)
        except ValueError as e:
            self._busy = False  # this attempt never actually transitioned
            raise ApplicationError(str(e))

        if is_terminal:
            actual_status = await workflow.execute_activity(
                "persist_decision",
                PersistDecisionInput(
                    application_id=self._application_id,
                    actor_role=ROLE_UNDERWRITER,
                    decision=DECISION_APPROVE if resulting_status == STATUS_APPROVED else DECISION_REJECT,
                    actor_name=RISK_ENGINE_ACTOR_NAME,
                    comment=RISK_ENGINE_AUTO_COMMENT,
                    resulting_status=resulting_status,
                    risk_tier=risk_tier,
                ),
                start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
                result_type=str,
            )
            self._status = actual_status
            self._finalized = True
            self._closed_by = RISK_ENGINE_ACTOR_NAME
            self._closed_comment = RISK_ENGINE_AUTO_COMMENT
        else:
            await workflow.execute_activity(
                "persist_risk_assessment_cleared",
                PersistRiskAssessmentClearedInput(application_id=self._application_id),
                start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
            self._status = STATUS_PENDING_UNDERWRITING
        self._busy = False

    @workflow.signal
    async def resubmit(self, payload: dict[str, Any]) -> None:
        if self._finalized or self._status != STATUS_MORE_INFO_REQUESTED:
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
        self._status = STATUS_PENDING_UNDERWRITING
        self._busy = False

    @workflow.query
    def get_status(self) -> ApplicationStatus:
        return ApplicationStatus(
            status=self._status,
            closed_by=self._closed_by,
            closed_comment=self._closed_comment,
        )


# ----------------------------------------------------------------------
# CloseAccountWorkflow (Phase 18, "Account closure" -- see CLAUDE.md).
# One execution per closure *request*, not per account -- a rejected or
# customer-cancelled request always reverts the account to ACTIVE and
# this workflow's own run() then completes; a later request against the
# same account starts a brand-new execution (account/service.py's
# request_closure() is only reachable while the account is ACTIVE, so
# there's never a live CloseAccountWorkflow to collide with). Reuses
# this module's ROLE_*/DECISION_* constants -- the same actor-role and
# decision taxonomy LoanApplicationWorkflow uses -- since Account
# closure decisions are made by the same Underwriter/Manager roles, no
# new taxonomy needed. Same "activities called by string name" contract
# as LoanApplicationWorkflow -- see this module's own docstring and
# CLAUDE.md's "Breaking the application <-> workflow cycle" (the
# equivalent split for account/ is account/activities.py, not built
# until P18-4).
# ----------------------------------------------------------------------

STATUS_ACCOUNT_CLOSURE_REQUESTED = "CLOSURE_REQUESTED"
STATUS_ACCOUNT_ACTIVE = "ACTIVE"
STATUS_ACCOUNT_CLOSED = "CLOSED"


@dataclass
class CloseAccountWorkflowInput:
    account_id: str
    # Opaque pass-through, never inspected here -- accounts carries no
    # applicant_identifier column of its own (only customer_id, and
    # account/ isn't granted a customer/ import to resolve one -- see
    # CLAUDE.md's module dependency graph). The caller
    # (account.service.request_closure(), which bff_customer calls
    # already holding this value from its own session cookie) supplies
    # it once at start; this workflow carries it in its own durable
    # state across however many signals arrive, purely so
    # persist_closure_decision (account/activities.py) has it to pass to
    # notifications.service.send_account_closure_decision, the same
    # "forward an opaque identity string, never resolve it" role
    # ApplicationWorkflowInput's own applicant_* fields already play for
    # LoanApplicationWorkflow.
    applicant_identifier: str


@dataclass
class CloseAccountStatus:
    status: str  # CLOSURE_REQUESTED | ACTIVE (reverted) | CLOSED
    closed_by: Optional[str] = None
    closed_comment: Optional[str] = None


@dataclass
class PersistClosureRequestInput:
    account_id: str
    workflow_id: str


@dataclass
class PersistClosureDecisionInput:
    account_id: str
    applicant_identifier: str
    decision: str  # APPROVE | REJECT | CANCELLED
    actor_name: str
    comment: str
    resulting_status: str  # CLOSED | ACTIVE


@workflow.defn
class CloseAccountWorkflow:
    def __init__(self) -> None:
        self._account_id: str = ""
        self._applicant_identifier: str = ""
        self._status = STATUS_ACCOUNT_CLOSURE_REQUESTED
        self._closed_by: Optional[str] = None
        self._closed_comment: Optional[str] = None
        self._finalized = False
        # Same synchronous single-writer guard LoanApplicationWorkflow
        # uses -- only the first signal to arrive while nothing else is
        # in flight ever gets to proceed.
        self._busy = False

    def _is_final(self) -> bool:
        return self._finalized

    def _claim_transition(self) -> bool:
        if self._finalized or self._busy:
            return False
        self._busy = True
        return True

    def _resolve_decision(self, actor_role: str, decision: str) -> str:
        """Returns resulting_status, or raises ValueError. Unlike
        LoanApplicationWorkflow's multi-stage _resolve_transition, this
        has exactly one state to decide from (CLOSURE_REQUESTED) and no
        escalation tier -- either Underwriter or Manager may decide, per
        CLAUDE.md's "Account closure" scoping."""
        if actor_role not in (ROLE_UNDERWRITER, ROLE_MANAGER):
            raise ValueError(
                f"a closure decision requires actor_role in "
                f"{(ROLE_UNDERWRITER, ROLE_MANAGER)!r}, got {actor_role!r}"
            )
        if decision == DECISION_APPROVE:
            return STATUS_ACCOUNT_CLOSED
        if decision == DECISION_REJECT:
            return STATUS_ACCOUNT_ACTIVE
        raise ValueError(f"invalid closure decision {decision!r}")

    @workflow.run
    async def run(self, req: CloseAccountWorkflowInput) -> CloseAccountStatus:
        self._account_id = req.account_id
        self._applicant_identifier = req.applicant_identifier

        await workflow.execute_activity(
            "persist_closure_request",
            PersistClosureRequestInput(
                account_id=req.account_id,
                workflow_id=workflow.info().workflow_id,
            ),
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        await workflow.wait_condition(self._is_final)

        return CloseAccountStatus(
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
            resulting_status = self._resolve_decision(actor_role, decision)
        except ValueError as e:
            self._busy = False  # this attempt never actually transitioned
            raise ApplicationError(str(e))

        actual_status = await workflow.execute_activity(
            "persist_closure_decision",
            PersistClosureDecisionInput(
                account_id=self._account_id,
                applicant_identifier=self._applicant_identifier,
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
        self._finalized = True
        self._closed_by = actor_name
        self._closed_comment = comment
        self._busy = False

    @workflow.signal
    async def cancel(self) -> None:
        """The customer's own cancel -- only meaningful while still
        CLOSURE_REQUESTED (an already-decided request has nothing left
        to cancel). No actor_name/comment parameter, unlike
        submit_decision -- this is always the requesting customer,
        attributed generically ("customer") rather than needing a second
        signal parameter; self._applicant_identifier (captured once, at
        run() start) is what actually reaches the closure-decision email,
        not this signal's own arguments."""
        if self._finalized or self._status != STATUS_ACCOUNT_CLOSURE_REQUESTED:
            return  # nothing pending to cancel -- ignore
        if not self._claim_transition():
            return

        actual_status = await workflow.execute_activity(
            "persist_closure_decision",
            PersistClosureDecisionInput(
                account_id=self._account_id,
                applicant_identifier=self._applicant_identifier,
                decision=DECISION_CANCELLED,
                actor_name="customer",
                comment="closure request cancelled by customer",
                resulting_status=STATUS_ACCOUNT_ACTIVE,
            ),
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
            result_type=str,
        )
        self._status = actual_status
        self._finalized = True
        self._closed_by = "customer"
        self._closed_comment = "closure request cancelled by customer"
        self._busy = False

    @workflow.query
    def get_status(self) -> CloseAccountStatus:
        return CloseAccountStatus(
            status=self._status,
            closed_by=self._closed_by,
            closed_comment=self._closed_comment,
        )
