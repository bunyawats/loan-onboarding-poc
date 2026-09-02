import itertools

import asyncpg
import pytest

from loan_onboarding.account import db, service
from loan_onboarding.account.models import AccountNotFound

_customer_id_counter = itertools.count()
_application_id_counter = itertools.count()


def _fake_customer_id() -> str:
    return f"CUS-{next(_customer_id_counter):09d}"


def _fake_application_id() -> str:
    return f"APP-{next(_application_id_counter):09d}"


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
