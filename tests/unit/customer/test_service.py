import asyncio
import uuid

import pytest

from loan_onboarding.customer import db, service
from loan_onboarding.customer.models import CustomerNotFound


async def test_find_by_identifier_returns_none_for_no_match():
    result = await service.find_by_identifier("nobody@example.com")
    assert result is None


async def test_get_or_create_creates_on_first_call():
    customer = await service.get_or_create("alice@example.com")
    assert customer.applicant_identifier == "alice@example.com"
    assert customer.customer_id is not None


async def test_get_or_create_is_idempotent_no_duplicate_row():
    first = await service.get_or_create("bob@example.com")
    second = await service.get_or_create("bob@example.com")
    assert first.customer_id == second.customer_id

    pool = await db._get_pool()
    count = await pool.fetchval(
        "SELECT count(*) FROM customers WHERE applicant_identifier = $1",
        "bob@example.com",
    )
    assert count == 1


async def test_get_or_create_concurrent_calls_create_exactly_one_row():
    """Proves the ON CONFLICT DO NOTHING path in customer/db.py, not
    just the sequential case above -- a naive find-then-insert would
    let two truly concurrent calls both pass the find, then race on
    the insert (one succeeding, one raising a unique-violation the
    service layer never asked for)."""
    results = await asyncio.gather(
        *(service.get_or_create("eve@example.com") for _ in range(10))
    )
    customer_ids = {c.customer_id for c in results}
    assert len(customer_ids) == 1

    pool = await db._get_pool()
    count = await pool.fetchval(
        "SELECT count(*) FROM customers WHERE applicant_identifier = $1",
        "eve@example.com",
    )
    assert count == 1


async def test_find_by_identifier_finds_existing_customer():
    created = await service.get_or_create("carol@example.com")
    found = await service.find_by_identifier("carol@example.com")
    assert found is not None
    assert found.customer_id == created.customer_id


async def test_get_returns_customer_by_id():
    created = await service.get_or_create("dave@example.com")
    fetched = await service.get(created.customer_id)
    assert fetched.customer_id == created.customer_id
    assert fetched.applicant_identifier == "dave@example.com"


async def test_get_raises_customer_not_found_for_unknown_id():
    with pytest.raises(CustomerNotFound):
        await service.get(uuid.uuid4())
