import asyncio

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
        await service.get("CUS-000000000")


async def test_get_or_create_customer_id_has_expected_format():
    customer = await service.get_or_create("frank@example.com")
    assert customer.customer_id.startswith("CUS-")
    assert len(customer.customer_id.removeprefix("CUS-")) == 9


async def test_get_or_create_seeds_profile_fields_on_first_create():
    customer = await service.get_or_create("grace@example.com", "Grace Hopper", "grace@example.com", "555-0101")
    assert customer.name == "Grace Hopper"
    assert customer.email == "grace@example.com"
    assert customer.phone == "555-0101"


async def test_get_or_create_second_call_does_not_change_existing_profile_fields():
    """A second get_or_create for an identifier that already resolved a
    customer is a plain lookup, not a re-seed -- ON CONFLICT DO NOTHING
    means whatever profile fields (or lack thereof) the first call
    wrote must survive untouched, even if this call passes different
    values."""
    first = await service.get_or_create("henry@example.com", "Henry First", "henry-old@example.com", "555-0102")
    second = await service.get_or_create("henry@example.com", "Henry Second", "henry-new@example.com", "555-9999")

    assert second.customer_id == first.customer_id
    assert second.name == "Henry First"
    assert second.email == "henry-old@example.com"
    assert second.phone == "555-0102"


async def test_update_profile_overwrites_unconditionally():
    """Not fill-blanks-only -- a later approved application's submitted
    details always win, even over an existing non-NULL value."""
    created = await service.get_or_create("ivy@example.com", "Ivy Original", "ivy-old@example.com", "555-0103")

    updated = await service.update_profile(created.customer_id, "Ivy Updated", "ivy-new@example.com", "555-8888")

    assert updated.customer_id == created.customer_id
    assert updated.name == "Ivy Updated"
    assert updated.email == "ivy-new@example.com"
    assert updated.phone == "555-8888"

    refetched = await service.get(created.customer_id)
    assert refetched.name == "Ivy Updated"
    assert refetched.email == "ivy-new@example.com"
    assert refetched.phone == "555-8888"


async def test_update_profile_raises_customer_not_found_for_unknown_id():
    with pytest.raises(CustomerNotFound):
        await service.update_profile("CUS-000000000", "Nobody", "nobody@example.com", "555-0000")
