"""account/db.py's own tests hit a real Postgres (same deliberate
exception test_service.py/test_activities.py already document -- see
CLAUDE.md's Testing section), exercising account_closure_requests
directly rather than through account.service/activities.py -- Phase 22,
"Account closure request history (1:M)"."""

import itertools

import asyncpg
import pytest

from loan_onboarding.account import db

_customer_id_counter = itertools.count()
_application_id_counter = itertools.count()


def _fake_customer_id() -> str:
    return f"CUS-{next(_customer_id_counter):09d}"


def _fake_application_id() -> str:
    return f"APP-{next(_application_id_counter):09d}"


async def _seed_active_account(product_type: str = "personal_loan") -> asyncpg.Record:
    return await db.create(_fake_customer_id(), product_type, _fake_application_id())


async def test_create_closure_request_inserts_pending_row():
    account = await _seed_active_account()
    workflow_id = f"account-closure-{account['account_id']}"

    request = await db.create_closure_request(account["account_id"], workflow_id, "run-1")

    assert request["account_id"] == account["account_id"]
    assert request["workflow_id"] == workflow_id
    assert request["workflow_run_id"] == "run-1"
    assert request["status"] == "PENDING"
    assert request["requested_at"] is not None


async def test_create_closure_request_second_pending_for_same_account_conflicts():
    """Proves ux_closure_requests_account_pending actually fires -- not
    that the function would reject a duplicate, the real partial unique
    index does (same "prove the constraint, not just the code" DoD
    Phase 3's own accounts tests already established for
    ux_accounts_customer_active_product_type)."""
    account = await _seed_active_account()
    await db.create_closure_request(account["account_id"], "account-closure-x", "run-1")

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await db.create_closure_request(account["account_id"], "account-closure-x", "run-2")


async def test_create_closure_request_is_idempotent_for_the_same_run_id():
    account = await _seed_active_account()
    workflow_id = f"account-closure-{account['account_id']}"

    first = await db.create_closure_request(account["account_id"], workflow_id, "run-1")
    second = await db.create_closure_request(account["account_id"], workflow_id, "run-1")

    assert second["closure_request_id"] == first["closure_request_id"]
    assert second["requested_at"] == first["requested_at"]


async def test_two_cycle_request_reject_request_approve_preserves_full_history():
    """The actual point of Phase 22: a rejected request's history
    survives a second request against the same account, and
    accounts.status cycles correctly across both."""
    account = await _seed_active_account()
    account_id = account["account_id"]
    workflow_id = f"account-closure-{account_id}"

    assert account["status"] == "ACTIVE"

    first = await db.create_closure_request(account_id, workflow_id, "run-1")
    await db.set_status(account_id, "CLOSURE_REQUESTED")
    after_first_request = await db.get(account_id)
    assert after_first_request["status"] == "CLOSURE_REQUESTED"

    await db.update_closure_request_decision(
        first["closure_request_id"],
        status="REJECTED",
        decision_comment="balance not yet zero",
        decided_by="underwriter1",
        decided_at=first["requested_at"],
    )
    await db.set_status(account_id, "ACTIVE")
    after_reject = await db.get(account_id)
    assert after_reject["status"] == "ACTIVE"

    second = await db.create_closure_request(account_id, workflow_id, "run-2")
    assert second["closure_request_id"] != first["closure_request_id"]
    await db.set_status(account_id, "CLOSURE_REQUESTED")
    after_second_request = await db.get(account_id)
    assert after_second_request["status"] == "CLOSURE_REQUESTED"

    await db.update_closure_request_decision(
        second["closure_request_id"],
        status="APPROVED",
        decision_comment="balance confirmed zero",
        decided_by="manager1",
        decided_at=second["requested_at"],
    )
    await db.set_status(account_id, "CLOSED")
    after_approve = await db.get(account_id)
    assert after_approve["status"] == "CLOSED"

    history = await db.list_closure_requests_for_account(account_id)
    assert len(history) == 2
    # Newest first.
    assert history[0]["closure_request_id"] == second["closure_request_id"]
    assert history[0]["status"] == "APPROVED"
    assert history[0]["decision_comment"] == "balance confirmed zero"
    assert history[0]["decided_by"] == "manager1"
    assert history[1]["closure_request_id"] == first["closure_request_id"]
    assert history[1]["status"] == "REJECTED"
    assert history[1]["decision_comment"] == "balance not yet zero"
    assert history[1]["decided_by"] == "underwriter1"


async def test_get_pending_closure_request_returns_none_when_no_request_in_flight():
    account = await _seed_active_account()
    assert await db.get_pending_closure_request(account["account_id"]) is None


async def test_get_closure_request_by_id():
    account = await _seed_active_account()
    created = await db.create_closure_request(account["account_id"], "account-closure-x", "run-1")

    found = await db.get_closure_request(created["closure_request_id"])

    assert found is not None
    assert found["account_id"] == account["account_id"]


async def test_list_by_status_pending_joins_account_fields():
    account = await _seed_active_account("auto_loan")
    await db.create_closure_request(account["account_id"], "account-closure-x", "run-1")
    await db.set_status(account["account_id"], "CLOSURE_REQUESTED")

    results = await db.list_by_status("PENDING")

    assert len(results) == 1
    assert results[0]["account_id"] == account["account_id"]
    assert results[0]["customer_id"] == account["customer_id"]
    assert results[0]["product_type"] == "auto_loan"
