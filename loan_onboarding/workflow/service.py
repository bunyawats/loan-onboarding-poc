"""
The one file allowed to talk to Temporal in this module. Both BFFs and
application/service.py call through here, never through temporalio
directly -- see CLAUDE.md's module dependency graph.

No Postgres pool here, deliberately -- unlike review-approval-temporal's
workflow/service.py (which polls its own `pool` after start/signal calls
to confirm a write landed), this project's "wait for the write to land"
logic (`_wait_until`) lives in application/service.py, polling
application/db.py's own read. workflow/ owns no table of its own (see
CLAUDE.md's "workflow/" module section) and start_workflow/signal_* here
only ever confirm that Temporal *accepted* the call, never that any
downstream activity has actually committed anything.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from temporalio.client import Client
from temporalio.service import RPCError

from loan_onboarding.workflow.task_queues import task_queue_for_product_type
from loan_onboarding.workflow.workflows import (
    VALID_ACTOR_ROLES,
    VALID_DECISIONS,
    ApplicationWorkflowInput,
    LoanApplicationWorkflow,
)


def _workflow_id_for_application(application_id: str) -> str:
    return f"loan-application-{application_id}"


async def start_workflow(
    client: Client,
    application_id: str,
    product_type: str,
    payload: dict[str, Any],
    amount: float,
    applicant_identifier: str,
    applicant_name: str,
    applicant_email: str,
    applicant_phone: str,
    customer_id: Optional[str] = None,
) -> str:
    """`amount`/`applicant_identifier`/`applicant_name`/`applicant_email`/
    `applicant_phone`/`customer_id` are plain named arguments, never read
    out of `payload` -- see CLAUDE.md's "application/" module section:
    the workflow needs `amount` for its own escalation-threshold check,
    and forwards the rest, opaque, to the `persist_application` activity
    so it has them to write into the row. `payload` itself stays
    product-specific-fields-only.

    Only confirms Temporal *accepted* the start -- callers that need to
    know `persist_application` has actually committed (e.g.
    application.service.create_application, which immediately wants to
    show the created application) poll for that themselves via
    `_wait_until()`, not here.
    """
    wf_id = _workflow_id_for_application(application_id)
    await client.start_workflow(
        LoanApplicationWorkflow.run,
        ApplicationWorkflowInput(
            application_id=application_id,
            product_type=product_type,
            payload=payload,
            amount=amount,
            applicant_identifier=applicant_identifier,
            applicant_name=applicant_name,
            applicant_email=applicant_email,
            applicant_phone=applicant_phone,
            customer_id=customer_id,
        ),
        id=wf_id,
        task_queue=task_queue_for_product_type(product_type),
    )
    return wf_id


async def signal_decision(
    client: Client,
    workflow_id: str,
    actor_role: str,
    decision: str,
    actor_name: str,
    comment: str = "",
) -> None:
    """Called directly by bff_backoffice (Approve/Reject/RequestMoreInfo,
    actor_role in {"underwriter", "manager"}) and bff_customer (Cancel,
    actor_role="customer", decision="CANCELLED") -- see CLAUDE.md. Only
    confirms Temporal *accepted* the signal -- the workflow's own
    `_resolve_transition` is what actually validates actor_role/decision
    against the current status, and that validation runs inside the
    workflow's signal handler, asynchronously, after this call has
    already returned. A rejected transition never surfaces here as an
    exception; see application/service.py's `_wait_until()` pattern for
    how a caller actually confirms a signalled decision took effect.
    """
    if actor_role not in VALID_ACTOR_ROLES:
        raise ValueError(f"actor_role must be one of {VALID_ACTOR_ROLES}")
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {VALID_DECISIONS}")
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal(
        LoanApplicationWorkflow.submit_decision,
        args=[actor_role, decision, actor_name, comment],
    )


async def signal_resubmit(
    client: Client, workflow_id: str, payload: dict[str, Any]
) -> None:
    """Called only by application.service.resubmit_application, against
    the *existing* workflow_id (the same running execution, still
    waiting from MORE_INFO_REQUESTED) -- never a new workflow start.
    """
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal(LoanApplicationWorkflow.resubmit, args=[payload])


# Every application is its own Temporal workflow execution -- there's no
# "signal N workflow ids in one call" primitive, so a bulk decision is
# mechanically N concurrent calls to the single-item signal above,
# collected into a best-effort, per-item result list. Same shape as
# review-approval-temporal's bulk_submit_decision().
_MAX_BULK_SIZE = 50


@dataclass
class BulkActionResult:
    workflow_id: str
    ok: bool
    error: Optional[str] = None


def _validate_bulk_ids(workflow_ids: list[str]) -> list[str]:
    deduped = list(dict.fromkeys(workflow_ids))  # de-dup, preserve order
    if not deduped:
        raise ValueError("workflow_ids must not be empty")
    if len(deduped) > _MAX_BULK_SIZE:
        raise ValueError(f"a single bulk action supports at most {_MAX_BULK_SIZE} workflow ids")
    return deduped


async def bulk_signal_decision(
    client: Client,
    workflow_ids: list[str],
    actor_role: str,
    decision: str,
    actor_name: str,
    comment: str = "",
) -> list[BulkActionResult]:
    ids = _validate_bulk_ids(workflow_ids)
    if actor_role not in VALID_ACTOR_ROLES:
        raise ValueError(f"actor_role must be one of {VALID_ACTOR_ROLES}")
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {VALID_DECISIONS}")

    async def _one(wf_id: str) -> BulkActionResult:
        try:
            handle = client.get_workflow_handle(wf_id)
            await handle.signal(
                LoanApplicationWorkflow.submit_decision,
                args=[actor_role, decision, actor_name, comment],
            )
            return BulkActionResult(wf_id, True)
        except RPCError as e:
            return BulkActionResult(wf_id, False, str(e))

    return list(await asyncio.gather(*(_one(wid) for wid in ids)))
