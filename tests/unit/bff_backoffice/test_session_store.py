"""bff_backoffice/session_store.py's tests hit a real Redis rather than
mocking -- the TTL-sliding behavior on get() is a statement about real
Redis expiry a mock can't meaningfully verify, same deliberate exception
as customer/account/application's own db.py tests hitting real Postgres
(CLAUDE.md's Testing section)."""

import asyncio

import pytest

from loan_onboarding.bff_backoffice import session_store


@pytest.fixture(autouse=True)
async def _clean_redis():
    yield
    r = session_store._get_redis()
    async for key in r.scan_iter(f"{session_store._KEY_PREFIX}*"):
        await r.delete(key)


def test_new_session_id_is_unique_and_url_safe():
    ids = {session_store.new_session_id() for _ in range(20)}
    assert len(ids) == 20
    for session_id in ids:
        assert len(session_id) > 20


async def test_get_returns_none_for_unknown_session():
    assert await session_store.get("does-not-exist") is None


async def test_set_then_get_round_trips_data():
    session_id = session_store.new_session_id()
    data = {"username": "underwriter1", "role": "underwriter", "access_token": "abc"}

    await session_store.set(session_id, data)
    result = await session_store.get(session_id)

    assert result == data


async def test_delete_removes_the_session():
    session_id = session_store.new_session_id()
    await session_store.set(session_id, {"role": "underwriter"})

    await session_store.delete(session_id)

    assert await session_store.get(session_id) is None


async def test_get_slides_the_ttl_forward():
    """A real Redis TTL assertion -- set a short-lived key directly
    (bypassing the module's own SESSION_TTL_SECONDS), confirm get()
    pushes its expiry back out to the full session TTL rather than
    leaving the short one in place."""
    session_id = session_store.new_session_id()
    r = session_store._get_redis()
    await r.set(session_store._key(session_id), '{"role": "underwriter"}', ex=2)

    ttl_before = await r.ttl(session_store._key(session_id))
    assert ttl_before <= 2

    await session_store.get(session_id)

    ttl_after = await r.ttl(session_store._key(session_id))
    assert ttl_after > 2

    # And it's genuinely still alive past the original short TTL.
    await asyncio.sleep(2.5)
    assert await session_store.get(session_id) is not None
