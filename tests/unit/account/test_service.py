import itertools

import asyncpg
import pytest

from loan_onboarding.account import db, service
from loan_onboarding.account.models import AccountNotActive, AccountNotFound

_customer_id_counter = itertools.count()
_application_id_counter = itertools.count()


def _fake_customer_id() -> str:
    return f"CUS-{next(_customer_id_counter):09d}"


def _fake_application_id() -> str:
    return f"APP-{next(_application_id_counter):09d}"


@pytest.fixture(autouse=True)
def _fast_wait_until(monkeypatch):
    # Same shrink application/service.py's own tests already apply to
    # its _wait_until -- makes the timeout-path (never reached below,
    # but kept for consistency) fast without changing the actual
    # "poll, then give up" behavior.
    monkeypatch.setattr(service, "_CONFIRM_TIMEOUT_S", 0.2)
    monkeypatch.setattr(service, "_CONFIRM_INTERVAL_S", 0.02)


@pytest.fixture
def start_close_account_workflow_calls(monkeypatch):
    calls = []

    async def fake_get_client():
        return "fake-temporal-client"

    async def fake_start_close_account_workflow(client, account_id, applicant_identifier):
        calls.append(dict(client=client, account_id=account_id, applicant_identifier=applicant_identifier))
        return f"account-closure-{account_id}"

    monkeypatch.setattr(service, "_get_temporal_client", fake_get_client)
    monkeypatch.setattr(
        service.workflow_service, "start_close_account_workflow", fake_start_close_account_workflow
    )
    return calls


async def test_create_account_different_product_types_creates_two_accounts():
    customer_id = _fake_customer_id()
    personal_loan = await service.create_account(customer_id, "personal_loan", _fake_application_id())
    auto_loan = await service.create_account(customer_id, "auto_loan", _fake_application_id())

    assert personal_loan.account_id != auto_loan.account_id
    assert personal_loan.customer_id == customer_id
    assert auto_loan.customer_id == customer_id


async def test_create_account_same_product_type_second_call_hits_real_constraint():
    """Proves db/schema.sql's ux_accounts_customer_active_product_type
    actually fires -- not that the function would reject a duplicate,
    the real partial unique index does. create_account is deliberately
    NOT conflict-safe on its own (see db.py's docstring); the
    pre-approval check_decision_allowed gate is what's supposed to
    prevent this in the real call path (Phase 6)."""
    customer_id = _fake_customer_id()
    await service.create_account(customer_id, "personal_loan", _fake_application_id())

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await service.create_account(customer_id, "personal_loan", _fake_application_id())


async def test_create_account_same_product_type_different_customers_is_fine():
    await service.create_account(_fake_customer_id(), "personal_loan", _fake_application_id())
    # A different customer_id -- must not collide with the first row.
    await service.create_account(_fake_customer_id(), "personal_loan", _fake_application_id())


async def test_create_account_application_id_must_be_unique():
    """accounts.application_id is NOT NULL UNIQUE -- an account is 1:1
    with the application that produced it, and this constraint doubles
    as persist_decision's provisioning idempotency guard."""
    application_id = _fake_application_id()
    await service.create_account(_fake_customer_id(), "personal_loan", application_id)

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await service.create_account(_fake_customer_id(), "auto_loan", application_id)


async def test_has_active_account_of_type_true_after_active_account_exists():
    customer_id = _fake_customer_id()
    await service.create_account(customer_id, "mortgage", _fake_application_id())

    assert await service.has_active_account_of_type(customer_id, "mortgage") is True
    assert await service.has_active_account_of_type(customer_id, "auto_loan") is False


async def test_has_active_account_of_type_false_again_once_closed():
    customer_id = _fake_customer_id()
    account = await service.create_account(customer_id, "mortgage", _fake_application_id())
    assert await service.has_active_account_of_type(customer_id, "mortgage") is True

    pool = await db._get_pool()
    await pool.execute(
        "UPDATE accounts SET status = 'CLOSED' WHERE account_id = $1",
        account.account_id,
    )

    assert await service.has_active_account_of_type(customer_id, "mortgage") is False

    # And the rule this all exists for: closing it frees the slot up
    # for a new ACTIVE account of the same product_type.
    reopened = await service.create_account(customer_id, "mortgage", _fake_application_id())
    assert reopened.account_id != account.account_id


async def test_get_returns_account_by_id():
    created = await service.create_account(_fake_customer_id(), "auto_loan", _fake_application_id())
    fetched = await service.get(created.account_id)
    assert fetched.account_id == created.account_id
    assert fetched.product_type == "auto_loan"
    assert fetched.status == "ACTIVE"


async def test_get_raises_account_not_found_for_unknown_id():
    with pytest.raises(AccountNotFound):
        await service.get("ACC-000000000")


async def test_get_by_application_id_returns_none_for_unknown_id():
    assert await service.get_by_application_id("APP-999999999") is None


async def test_get_by_application_id_returns_the_account_it_produced():
    application_id = _fake_application_id()
    created = await service.create_account(_fake_customer_id(), "personal_loan", application_id)

    found = await service.get_by_application_id(application_id)
    assert found is not None
    assert found.account_id == created.account_id


async def test_request_closure_rejects_non_active_account(start_close_account_workflow_calls):
    """Same 'the UI hides it, the service still enforces it' discipline
    application.service's product-type picker already follows -- never
    even reaches workflow.service.start_close_account_workflow for a
    non-ACTIVE account."""
    account = await service.create_account(_fake_customer_id(), "personal_loan", _fake_application_id())
    pool = await db._get_pool()
    await pool.execute("UPDATE accounts SET status = 'CLOSED' WHERE account_id = $1", account.account_id)

    with pytest.raises(AccountNotActive):
        await service.request_closure(account.account_id, "alice@example.com")
    assert start_close_account_workflow_calls == []


async def test_request_closure_starts_workflow_and_waits_for_committed_status(monkeypatch, start_close_account_workflow_calls):
    account = await service.create_account(_fake_customer_id(), "personal_loan", _fake_application_id())

    # Simulate persist_closure_request (the workflow's first activity)
    # committing almost immediately -- what a real worker would do --
    # so request_closure's own _wait_until poll finds it within the
    # (shrunk) timeout, same pattern
    # application/test_service.py's fake_start_workflow_with_commit uses.
    async def fake_start_and_commit(client, account_id, applicant_identifier):
        await db.update_closure_request(account_id, f"account-closure-{account_id}")
        start_close_account_workflow_calls.append(
            dict(client=client, account_id=account_id, applicant_identifier=applicant_identifier)
        )
        return f"account-closure-{account_id}"

    monkeypatch.setattr(service.workflow_service, "start_close_account_workflow", fake_start_and_commit)

    workflow_id = await service.request_closure(account.account_id, "alice@example.com")

    assert workflow_id == f"account-closure-{account.account_id}"
    assert len(start_close_account_workflow_calls) == 1
    assert start_close_account_workflow_calls[0]["applicant_identifier"] == "alice@example.com"

    updated = await service.get(account.account_id)
    assert updated.status == "CLOSURE_REQUESTED"
    assert updated.closure_workflow_id == workflow_id
    assert updated.closure_requested_at is not None


async def test_request_closure_returns_last_read_record_even_on_wait_timeout(start_close_account_workflow_calls):
    """The fixture's default fake never actually writes to accounts --
    proves request_closure doesn't raise or hang when persist_closure_request
    never lands within the (shrunk) timeout, same 'always return
    whatever was last read, even on timeout' contract
    application/service.py's own _wait_until documents."""
    account = await service.create_account(_fake_customer_id(), "personal_loan", _fake_application_id())

    workflow_id = await service.request_closure(account.account_id, "alice@example.com")

    assert workflow_id == f"account-closure-{account.account_id}"
    # Status never actually flipped -- the fake didn't write it -- but
    # request_closure still returned rather than raising or hanging.
    still_active = await service.get(account.account_id)
    assert still_active.status == "ACTIVE"


async def test_list_pending_closure_requests_returns_only_closure_requested_accounts():
    active = await service.create_account(_fake_customer_id(), "personal_loan", _fake_application_id())
    pending = await service.create_account(_fake_customer_id(), "auto_loan", _fake_application_id())
    closed = await service.create_account(_fake_customer_id(), "mortgage", _fake_application_id())

    pool = await db._get_pool()
    await pool.execute(
        "UPDATE accounts SET status = 'CLOSURE_REQUESTED', closure_requested_at = now() WHERE account_id = $1",
        pending.account_id,
    )
    await pool.execute("UPDATE accounts SET status = 'CLOSED' WHERE account_id = $1", closed.account_id)

    results = await service.list_pending_closure_requests()

    assert [r.account_id for r in results] == [pending.account_id]
    assert active.account_id not in [r.account_id for r in results]
    assert closed.account_id not in [r.account_id for r in results]


async def test_list_pending_closure_requests_empty_when_none_pending():
    await service.create_account(_fake_customer_id(), "personal_loan", _fake_application_id())
    assert await service.list_pending_closure_requests() == []


async def test_list_pending_closure_requests_orders_oldest_request_first():
    pool = await db._get_pool()
    first = await service.create_account(_fake_customer_id(), "personal_loan", _fake_application_id())
    second = await service.create_account(_fake_customer_id(), "auto_loan", _fake_application_id())
    # Insert in reverse chronological order so a naive "insertion order"
    # assumption would fail this test if list_by_status ever dropped its
    # own ORDER BY closure_requested_at.
    await pool.execute(
        "UPDATE accounts SET status = 'CLOSURE_REQUESTED', closure_requested_at = now() WHERE account_id = $1",
        second.account_id,
    )
    await pool.execute(
        "UPDATE accounts SET status = 'CLOSURE_REQUESTED', closure_requested_at = now() - interval '1 hour' WHERE account_id = $1",
        first.account_id,
    )

    results = await service.list_pending_closure_requests()

    assert [r.account_id for r in results] == [first.account_id, second.account_id]


async def test_wait_for_status_change_returns_updated_account():
    account = await service.create_account(_fake_customer_id(), "personal_loan", _fake_application_id())
    pool = await db._get_pool()
    await pool.execute("UPDATE accounts SET status = 'CLOSED' WHERE account_id = $1", account.account_id)

    updated = await service.wait_for_status_change(account.account_id, "ACTIVE")

    assert updated.status == "CLOSED"


async def test_wait_for_status_change_returns_last_read_on_timeout():
    account = await service.create_account(_fake_customer_id(), "personal_loan", _fake_application_id())

    # Status never actually changes -- proves this returns the
    # unchanged record rather than hanging or raising, same accepted
    # timeout behavior application/service.py's own version documents.
    unchanged = await service.wait_for_status_change(account.account_id, "ACTIVE")

    assert unchanged.status == "ACTIVE"
