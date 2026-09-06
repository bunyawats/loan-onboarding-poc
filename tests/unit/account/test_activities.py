"""account/activities.py's tests hit a real Postgres for the accounts
table (same deliberate exception as test_service.py) but mock
notifications.service.send_account_closure_decision at the function-call
level -- matching CLAUDE.md's Testing convention and the same pattern
application/activities.py's own tests already follow for
document.service."""

import itertools

import pytest

from loan_onboarding.account import activities, db, service
from loan_onboarding.workflow.workflows import (
    DECISION_APPROVE,
    DECISION_REJECT,
    PersistClosureDecisionInput,
    PersistClosureRequestInput,
)

_customer_id_counter = itertools.count()
_application_id_counter = itertools.count()


def _fake_customer_id() -> str:
    return f"CUS-{next(_customer_id_counter):09d}"


def _fake_application_id() -> str:
    return f"APP-{next(_application_id_counter):09d}"


@pytest.fixture(autouse=True)
def _mock_notifications_service(monkeypatch):
    calls = []

    def fake_send_account_closure_decision(applicant_identifier, account_id, product_type, decision, comment):
        calls.append(
            dict(
                applicant_identifier=applicant_identifier,
                account_id=account_id,
                product_type=product_type,
                decision=decision,
                comment=comment,
            )
        )

    monkeypatch.setattr(
        activities.notifications_service, "send_account_closure_decision", fake_send_account_closure_decision
    )
    return calls


async def _seed_active_account(product_type: str = "personal_loan"):
    return await service.create_account(_fake_customer_id(), product_type, _fake_application_id())


async def test_persist_closure_request_flips_status_and_records_workflow_id():
    account = await _seed_active_account()
    workflow_id = f"account-closure-{account.account_id}"

    await activities.persist_closure_request(
        PersistClosureRequestInput(account_id=account.account_id, workflow_id=workflow_id)
    )

    updated = await service.get(account.account_id)
    assert updated.status == "CLOSURE_REQUESTED"
    assert updated.closure_workflow_id == workflow_id
    assert updated.closure_requested_at is not None


async def test_persist_closure_request_is_idempotent_on_retry():
    """A Temporal retry re-runs this same activity -- the second call
    must not fail, and must not slide closure_requested_at forward."""
    account = await _seed_active_account()
    workflow_id = f"account-closure-{account.account_id}"
    await activities.persist_closure_request(
        PersistClosureRequestInput(account_id=account.account_id, workflow_id=workflow_id)
    )
    first = await service.get(account.account_id)

    await activities.persist_closure_request(
        PersistClosureRequestInput(account_id=account.account_id, workflow_id=workflow_id)
    )
    second = await service.get(account.account_id)

    assert second.status == "CLOSURE_REQUESTED"
    assert second.closure_requested_at == first.closure_requested_at


async def _request_closure(account_id: str, workflow_id: str) -> None:
    await activities.persist_closure_request(
        PersistClosureRequestInput(account_id=account_id, workflow_id=workflow_id)
    )


async def test_persist_closure_decision_approve_closes_and_sends_email(_mock_notifications_service):
    account = await _seed_active_account("auto_loan")
    await _request_closure(account.account_id, f"account-closure-{account.account_id}")

    inp = PersistClosureDecisionInput(
        account_id=account.account_id,
        applicant_identifier="alice@example.com",
        decision=DECISION_APPROVE,
        actor_name="u1",
        comment="balance confirmed zero",
        resulting_status="CLOSED",
    )
    result = await activities.persist_closure_decision(inp)

    assert result == "CLOSED"
    updated = await service.get(account.account_id)
    assert updated.status == "CLOSED"
    assert updated.closure_decided_by == "u1"
    assert updated.closure_decision_comment == "balance confirmed zero"
    assert updated.closure_decided_at is not None

    assert len(_mock_notifications_service) == 1
    sent = _mock_notifications_service[0]
    assert sent["applicant_identifier"] == "alice@example.com"
    assert sent["account_id"] == account.account_id
    assert sent["product_type"] == "auto_loan"
    assert sent["decision"] == DECISION_APPROVE


async def test_persist_closure_decision_reject_reverts_to_active_and_sends_email(_mock_notifications_service):
    account = await _seed_active_account("mortgage")
    await _request_closure(account.account_id, f"account-closure-{account.account_id}")

    inp = PersistClosureDecisionInput(
        account_id=account.account_id,
        applicant_identifier="bob@example.com",
        decision=DECISION_REJECT,
        actor_name="m1",
        comment="balance not yet zero",
        resulting_status="ACTIVE",
    )
    result = await activities.persist_closure_decision(inp)

    assert result == "ACTIVE"
    updated = await service.get(account.account_id)
    assert updated.status == "ACTIVE"
    assert updated.closure_decided_by == "m1"
    assert updated.closure_decision_comment == "balance not yet zero"

    assert len(_mock_notifications_service) == 1
    assert _mock_notifications_service[0]["decision"] == DECISION_REJECT


async def test_persist_closure_decision_is_idempotent_on_retry(_mock_notifications_service):
    """A Temporal retry of an already-decided execution must not
    re-send the closure-decision email a second time -- the account's
    own status (no longer CLOSURE_REQUESTED) is the marker."""
    account = await _seed_active_account()
    await _request_closure(account.account_id, f"account-closure-{account.account_id}")

    inp = PersistClosureDecisionInput(
        account_id=account.account_id,
        applicant_identifier="alice@example.com",
        decision=DECISION_APPROVE,
        actor_name="u1",
        comment="confirmed",
        resulting_status="CLOSED",
    )
    first_result = await activities.persist_closure_decision(inp)
    second_result = await activities.persist_closure_decision(inp)

    assert first_result == "CLOSED"
    assert second_result == "CLOSED"
    assert len(_mock_notifications_service) == 1  # not sent twice
