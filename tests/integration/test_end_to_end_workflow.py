"""P7-3: the first true end-to-end run -- no UI yet, driven entirely
through application.service + workflow.service calls, against the real
local stack (db, temporal). Needs `docker compose up -d db temporal`
running first (see CLAUDE.md's Testing section on `@pytest.mark.integration`).

Mayan is NOT required for this phase -- document.service.check_completeness
is stubbed to return [] for every test here (CLAUDE.md's "Breaking the
cycle": document.service is mocked at the function-call boundary, same
convention as every P6 unit test), so no live Mayan instance is needed
just to prove the workflow/activity plumbing works end to end.

An in-process worker (module-scoped fixture below) stands in for
worker_main.py for the duration of this test module -- same
run_worker()-with-real-activities wiring P7-1/P7-2 made permanent,
just started here instead of via `docker compose up worker-workflow
worker-activity` so this test module is self-contained.
"""

import asyncio
import time
from decimal import Decimal
from uuid import uuid4

import pytest
from temporalio.client import Client

from loan_onboarding.account import service as account_service
from loan_onboarding.application import activities
from loan_onboarding.application import db as application_db
from loan_onboarding.application import service
from loan_onboarding.workflow import service as workflow_service
from loan_onboarding.workflow.worker import run_worker

pytestmark = pytest.mark.integration

_BELOW_THRESHOLD_AMOUNT = Decimal("10000")
_ESCALATION_AMOUNT = Decimal("60000")  # >= MANAGER_ESCALATION_THRESHOLD_USD (50,000)


@pytest.fixture(scope="module")
async def worker():
    task = asyncio.create_task(
        run_worker(
            [activities.persist_application, activities.persist_decision, activities.persist_resubmit],
            worker_mode="both",
            product_type=None,
        )
    )
    await asyncio.sleep(2)  # let it actually start polling before any test signals it
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.fixture(scope="module")
async def temporal_client():
    return await Client.connect("localhost:7233", namespace="default")


@pytest.fixture(autouse=True)
def _stub_document_service(monkeypatch):
    """Mayan is NOT required for this phase (P7-3's own DoD) -- stub
    every document.service call the create_application/persist_decision
    path can reach, not just check_completeness. Missing
    promote_government_id_to_customer_photo/generate_welcome_letter
    here was a real gap the first version of this test module had: it
    let a real (failing) Mayan call inside persist_decision's approve
    path surface as an activity retry, which in turn exposed a genuine
    bug in activities.py (see its own fix note) -- worth stubbing
    correctly here regardless, since this phase explicitly doesn't need
    Mayan at all."""

    async def fake_check_completeness(application_id, product_type):
        return []

    async def fake_promote(application_id, customer_id):
        pass

    async def fake_generate_welcome_letter(applicant_identifier, account_id, customer_id, applicant_name, product_type, amount):
        pass

    monkeypatch.setattr(service.document_service, "check_completeness", fake_check_completeness)
    monkeypatch.setattr(activities.document_service, "promote_government_id_to_customer_photo", fake_promote)
    monkeypatch.setattr(activities.document_service, "generate_welcome_letter", fake_generate_welcome_letter)


async def _create(applicant_identifier: str, amount: Decimal):
    return await service.create_application(
        applicant_identifier=applicant_identifier,
        product_type="personal_loan",
        payload={"purpose": "test", "employment_status": "employed", "monthly_income": "5000"},
        applicant_name="E2E Test Applicant",
        applicant_email=applicant_identifier,
        applicant_phone="555-0000",
        amount=amount,
    )


async def _poll_status(application_id, expected_status: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while True:
        record = await application_db.get(application_id)
        if record is not None and record["status"] == expected_status:
            return record
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"application {application_id} did not reach {expected_status!r} "
                f"within {timeout}s (last seen: {record['status'] if record else None!r})"
            )
        await asyncio.sleep(0.1)


async def test_below_threshold_approve_reaches_approved(worker, temporal_client):
    result = await _create(f"e2e-below-{uuid4()}@example.com", _BELOW_THRESHOLD_AMOUNT)
    assert result.application is not None
    assert result.application.status == "PENDING_UNDERWRITING"

    await workflow_service.signal_decision(
        temporal_client, result.application.workflow_id, "underwriter", "APPROVE", "underwriter1", "looks good"
    )

    record = await _poll_status(result.application_id, "APPROVED")
    assert record["underwriter_decided_at"] is not None
    assert record["underwriter_name"] == "underwriter1"


async def test_escalation_path_underwriter_then_manager_approve(worker, temporal_client):
    result = await _create(f"e2e-escalate-{uuid4()}@example.com", _ESCALATION_AMOUNT)
    assert result.application is not None

    await workflow_service.signal_decision(
        temporal_client, result.application.workflow_id, "underwriter", "APPROVE", "underwriter1", "escalating"
    )
    record = await _poll_status(result.application_id, "PENDING_MANAGER_APPROVAL")
    assert record["underwriter_name"] == "underwriter1"
    assert await account_service.get_by_application_id(result.application_id) is None

    await workflow_service.signal_decision(
        temporal_client, result.application.workflow_id, "manager", "APPROVE", "manager1", "approved"
    )
    record = await _poll_status(result.application_id, "APPROVED")
    assert record["manager_decided_at"] is not None
    assert record["manager_name"] == "manager1"


async def test_reject_reaches_rejected(worker, temporal_client):
    result = await _create(f"e2e-reject-{uuid4()}@example.com", _BELOW_THRESHOLD_AMOUNT)
    assert result.application is not None

    await workflow_service.signal_decision(
        temporal_client, result.application.workflow_id, "underwriter", "REJECT", "underwriter1", "not eligible"
    )

    record = await _poll_status(result.application_id, "REJECTED")
    assert record["underwriter_name"] == "underwriter1"
    assert await account_service.get_by_application_id(result.application_id) is None


async def test_request_more_info_then_resubmit_then_approve(worker, temporal_client):
    result = await _create(f"e2e-moreinfo-{uuid4()}@example.com", _BELOW_THRESHOLD_AMOUNT)
    assert result.application is not None
    workflow_id = result.application.workflow_id

    await workflow_service.signal_decision(
        temporal_client, workflow_id, "underwriter", "REQUEST_MORE_INFO", "underwriter1", "need bank statements"
    )
    await _poll_status(result.application_id, "MORE_INFO_REQUESTED")

    resubmit_result = await service.resubmit_application(
        result.application_id,
        {"purpose": "updated", "employment_status": "employed", "monthly_income": "5500"},
    )
    assert resubmit_result.application is not None
    assert resubmit_result.application.status == "PENDING_UNDERWRITING"
    assert resubmit_result.application.workflow_id == workflow_id  # same execution, not a new start

    await workflow_service.signal_decision(
        temporal_client, workflow_id, "underwriter", "APPROVE", "underwriter1", "now approved"
    )
    record = await _poll_status(result.application_id, "APPROVED")
    assert record["payload"]["purpose"] == "updated"


async def test_cancel_from_non_terminal_state(worker, temporal_client):
    result = await _create(f"e2e-cancel-{uuid4()}@example.com", _BELOW_THRESHOLD_AMOUNT)
    assert result.application is not None

    await workflow_service.signal_decision(
        temporal_client, result.application.workflow_id, "customer", "CANCELLED", "e2e-cancel-applicant", "changed my mind"
    )

    record = await _poll_status(result.application_id, "CANCELLED")
    assert await account_service.get_by_application_id(result.application_id) is None
    assert record["underwriter_name"] is None
