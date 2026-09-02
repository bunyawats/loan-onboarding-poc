"""application/service.py's create_application tests mock
workflow.service.start_workflow and _get_temporal_client at the
function-call boundary (CLAUDE.md's Testing convention: mock
document.service/workflow.service calls for a module under test) --
no real Temporal server needed. document.service.check_completeness is
mocked the same way (no real Mayan needed). customer.service runs for
real against Postgres (same deliberate exception as every other
db-backed test in this package) since resolving an existing customer_id
is exactly the integration point worth exercising for real."""

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from loan_onboarding.application import db as application_db, service
from loan_onboarding.customer import db as customer_db


@pytest.fixture(autouse=True)
async def _clean_tables():
    app_pool = await application_db._get_pool()
    yield
    await app_pool.execute("DELETE FROM applications")
    cust_pool = await customer_db._get_pool()
    await cust_pool.execute("DELETE FROM customers")


@pytest.fixture(autouse=True)
def _fast_wait_until(monkeypatch):
    # _wait_until reads these as module globals at call time -- shrinking
    # them here makes the timeout-path tests fast without changing
    # _wait_until's actual "poll, then give up" behavior.
    monkeypatch.setattr(service, "_CONFIRM_TIMEOUT_S", 0.2)
    monkeypatch.setattr(service, "_CONFIRM_INTERVAL_S", 0.02)


@pytest.fixture
def start_workflow_calls(monkeypatch):
    calls = []

    async def fake_get_client():
        return "fake-temporal-client"

    async def fake_start_workflow(
        client,
        application_id,
        product_type,
        payload,
        amount,
        applicant_identifier,
        applicant_name,
        applicant_email,
        applicant_phone,
        customer_id,
    ):
        calls.append(
            dict(
                client=client,
                application_id=application_id,
                product_type=product_type,
                payload=payload,
                amount=amount,
                applicant_identifier=applicant_identifier,
                applicant_name=applicant_name,
                applicant_email=applicant_email,
                applicant_phone=applicant_phone,
                customer_id=customer_id,
            )
        )
        return f"loan-application-{application_id}"

    monkeypatch.setattr(service, "_get_temporal_client", fake_get_client)
    monkeypatch.setattr(service.workflow_service, "start_workflow", fake_start_workflow)
    return calls


def _mock_completeness(monkeypatch, missing=None):
    async def fake_check_completeness(application_id, product_type):
        return missing or []

    monkeypatch.setattr(service.document_service, "check_completeness", fake_check_completeness)


def _personal_loan_payload(**overrides):
    defaults = dict(purpose="home improvement", employment_status="employed", monthly_income="5000")
    defaults.update(overrides)
    return defaults


async def test_missing_documents_returns_missing_categories_without_starting_workflow(
    monkeypatch, start_workflow_calls
):
    _mock_completeness(monkeypatch, missing=["Government ID", "Credit Report"])

    result = await service.create_application(
        applicant_identifier="alice@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Alice",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert result.application is None
    assert result.missing_categories == ["Government ID", "Credit Report"]
    assert result.application_id is not None
    assert start_workflow_calls == []


async def test_complete_application_starts_workflow_and_waits_for_persisted_row(
    monkeypatch, start_workflow_calls
):
    _mock_completeness(monkeypatch, missing=[])

    # Simulate persist_application (the workflow's first activity)
    # committing almost immediately -- what a real worker would do --
    # so _wait_until's poll finds it within the (shrunk) timeout.
    async def fake_start_workflow_with_commit(
        client, application_id, product_type, payload, amount,
        applicant_identifier, applicant_name, applicant_email, applicant_phone, customer_id,
    ):
        await application_db.insert(
            application_id=uuid.UUID(application_id),
            applicant_identifier=applicant_identifier,
            customer_id=uuid.UUID(customer_id) if customer_id else None,
            workflow_id=f"loan-application-{application_id}",
            product_type=product_type,
            payload=payload,
            applicant_name=applicant_name,
            applicant_email=applicant_email,
            applicant_phone=applicant_phone,
            amount=Decimal(str(amount)),
        )
        start_workflow_calls.append(locals())
        return f"loan-application-{application_id}"

    monkeypatch.setattr(service.workflow_service, "start_workflow", fake_start_workflow_with_commit)

    result = await service.create_application(
        applicant_identifier="alice@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Alice",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert result.missing_categories == []
    assert result.application is not None
    assert result.application.status == "PENDING_UNDERWRITING"
    assert result.application.customer_id is None
    assert result.application.account_id is None
    assert len(start_workflow_calls) == 1


async def test_resolves_existing_customer_id_and_passes_it_to_start_workflow(monkeypatch, start_workflow_calls):
    _mock_completeness(monkeypatch, missing=[])
    existing = await customer_db.get_or_create("returning@example.com")

    await service.create_application(
        applicant_identifier="returning@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Returning Customer",
        applicant_email="returning@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert start_workflow_calls[0]["customer_id"] == str(existing["customer_id"])


async def test_new_applicant_passes_none_customer_id(monkeypatch, start_workflow_calls):
    _mock_completeness(monkeypatch, missing=[])

    await service.create_application(
        applicant_identifier="brand-new@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="New Applicant",
        applicant_email="brand-new@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert start_workflow_calls[0]["customer_id"] is None


async def test_uses_provided_application_id_when_given(monkeypatch, start_workflow_calls):
    _mock_completeness(monkeypatch, missing=[])
    provided_id = uuid.uuid4()

    result = await service.create_application(
        applicant_identifier="alice@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Alice",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
        application_id=provided_id,
    )

    assert result.application_id == provided_id
    assert start_workflow_calls[0]["application_id"] == str(provided_id)


async def test_generates_application_id_when_not_given(monkeypatch, start_workflow_calls):
    _mock_completeness(monkeypatch, missing=[])

    result = await service.create_application(
        applicant_identifier="alice@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Alice",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert isinstance(result.application_id, uuid.UUID)


async def test_invalid_payload_raises_and_never_starts_workflow(monkeypatch, start_workflow_calls):
    _mock_completeness(monkeypatch, missing=[])

    with pytest.raises(ValidationError):
        await service.create_application(
            applicant_identifier="alice@example.com",
            product_type="personal_loan",
            payload={"purpose": "vacation"},  # missing required fields
            applicant_name="Alice",
            applicant_email="alice@example.com",
            applicant_phone="555-0100",
            amount=Decimal("10000"),
        )

    assert start_workflow_calls == []


async def test_wait_until_timeout_returns_none_application_even_though_workflow_started(
    monkeypatch, start_workflow_calls
):
    """Documents the accepted behavior: start_workflow only confirms
    Temporal *accepted* the call; if persist_application never commits
    within the wait budget, create_application still returns cleanly
    (application=None) rather than hanging or raising."""
    _mock_completeness(monkeypatch, missing=[])

    result = await service.create_application(
        applicant_identifier="alice@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Alice",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert len(start_workflow_calls) == 1
    assert result.missing_categories == []
    assert result.application is None
