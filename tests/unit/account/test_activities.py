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


async def test_persist_closure_request_flips_status_and_creates_a_request_row():
    account = await _seed_active_account()
    workflow_id = f"account-closure-{account.account_id}"

    closure_request_id = await activities.persist_closure_request(
        PersistClosureRequestInput(account_id=account.account_id, workflow_id=workflow_id, workflow_run_id="run-1")
    )

    updated = await service.get(account.account_id)
    assert updated.status == "CLOSURE_REQUESTED"

    pending = await service.get_pending_closure_request(account.account_id)
    assert pending is not None
    assert pending.closure_request_id == closure_request_id
    assert pending.workflow_id == workflow_id
    assert pending.workflow_run_id == "run-1"
    assert pending.requested_at is not None


async def test_persist_closure_request_is_idempotent_on_retry():
    """A Temporal retry re-runs this same activity, with the same
    workflow_run_id (Temporal assigns run_id once, at workflow start,
    unaffected by activity retries within that execution) -- the second
    call must not fail, must not create a second row, and must not slide
    requested_at forward."""
    account = await _seed_active_account()
    workflow_id = f"account-closure-{account.account_id}"
    first_id = await activities.persist_closure_request(
        PersistClosureRequestInput(account_id=account.account_id, workflow_id=workflow_id, workflow_run_id="run-1")
    )
    first = await service.get_pending_closure_request(account.account_id)

    second_id = await activities.persist_closure_request(
        PersistClosureRequestInput(account_id=account.account_id, workflow_id=workflow_id, workflow_run_id="run-1")
    )
    second = await service.get_pending_closure_request(account.account_id)

    assert second_id == first_id
    assert second.requested_at == first.requested_at
    assert len(await service.list_closure_requests_for_account(account.account_id)) == 1


async def _request_closure(account_id: str, workflow_id: str) -> str:
    return await activities.persist_closure_request(
        PersistClosureRequestInput(account_id=account_id, workflow_id=workflow_id, workflow_run_id="run-1")
    )


async def test_persist_closure_decision_approve_closes_and_sends_email(_mock_notifications_service):
    account = await _seed_active_account("auto_loan")
    closure_request_id = await _request_closure(account.account_id, f"account-closure-{account.account_id}")

    inp = PersistClosureDecisionInput(
        account_id=account.account_id,
        closure_request_id=closure_request_id,
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

    history = await service.list_closure_requests_for_account(account.account_id)
    assert len(history) == 1
    assert history[0].status == "APPROVED"
    assert history[0].decided_by == "u1"
    assert history[0].decision_comment == "balance confirmed zero"
    assert history[0].decided_at is not None

    assert len(_mock_notifications_service) == 1
    sent = _mock_notifications_service[0]
    assert sent["applicant_identifier"] == "alice@example.com"
    assert sent["account_id"] == account.account_id
    assert sent["product_type"] == "auto_loan"
    assert sent["decision"] == DECISION_APPROVE


async def test_persist_closure_decision_reject_reverts_to_active_and_sends_email(_mock_notifications_service):
    account = await _seed_active_account("mortgage")
    closure_request_id = await _request_closure(account.account_id, f"account-closure-{account.account_id}")

    inp = PersistClosureDecisionInput(
        account_id=account.account_id,
        closure_request_id=closure_request_id,
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

    history = await service.list_closure_requests_for_account(account.account_id)
    assert history[0].status == "REJECTED"
    assert history[0].decided_by == "m1"
    assert history[0].decision_comment == "balance not yet zero"

    assert len(_mock_notifications_service) == 1
    assert _mock_notifications_service[0]["decision"] == DECISION_REJECT


async def test_persist_closure_decision_is_idempotent_on_retry(_mock_notifications_service):
    """A Temporal retry of an already-decided execution must not
    re-send the closure-decision email a second time -- the closure
    request row's own status (no longer PENDING) is the marker now,
    not the account's."""
    account = await _seed_active_account()
    closure_request_id = await _request_closure(account.account_id, f"account-closure-{account.account_id}")

    inp = PersistClosureDecisionInput(
        account_id=account.account_id,
        closure_request_id=closure_request_id,
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


async def test_persist_closure_decision_then_second_request_and_decision_preserves_both_histories(
    _mock_notifications_service,
):
    """The actual point of Phase 22: a rejected request doesn't erase
    itself when the account is requested for closure again."""
    account = await _seed_active_account()
    first_closure_request_id = await _request_closure(account.account_id, f"account-closure-{account.account_id}")
    await activities.persist_closure_decision(
        PersistClosureDecisionInput(
            account_id=account.account_id,
            closure_request_id=first_closure_request_id,
            applicant_identifier="alice@example.com",
            decision=DECISION_REJECT,
            actor_name="u1",
            comment="balance not yet zero",
            resulting_status="ACTIVE",
        )
    )

    second_closure_request_id = await _request_closure(account.account_id, f"account-closure-{account.account_id}")
    assert second_closure_request_id != first_closure_request_id
    await activities.persist_closure_decision(
        PersistClosureDecisionInput(
            account_id=account.account_id,
            closure_request_id=second_closure_request_id,
            applicant_identifier="alice@example.com",
            decision=DECISION_APPROVE,
            actor_name="m1",
            comment="balance confirmed zero",
            resulting_status="CLOSED",
        )
    )

    history = await service.list_closure_requests_for_account(account.account_id)
    assert len(history) == 2
    by_id = {r.closure_request_id: r for r in history}
    assert by_id[first_closure_request_id].status == "REJECTED"
    assert by_id[first_closure_request_id].decision_comment == "balance not yet zero"
    assert by_id[second_closure_request_id].status == "APPROVED"
    assert by_id[second_closure_request_id].decision_comment == "balance confirmed zero"
    assert len(_mock_notifications_service) == 2
