"""Redis-backed bulk-selection store for `bff_backoffice`'s staff
screens (P10-3), reusing the same `backoffice-redis` instance as
`session_store.py` -- but a separate key space/shape, per the
`list-pagination-bulk-actions` skill's explicit guidance: keep the
selection store separate from the auth-session store even though both
share the same Redis, since they're written at very different rates
(auth: once at login, occasionally on refresh; selection: on every
checkbox click) and neither should risk clobbering the other.

Keyed by session id, not username -- two browser tabs under the same
login share one selection; two separate logins by the same person do
not (same skill, Part 2's explicit key-choice guidance). A plain Redis
SET is a natural fit for "which application ids are selected" -- no
JSON encode/decode needed, `SADD`/`SREM`/`SMEMBERS` map directly onto
add/remove/read."""

from __future__ import annotations

import os

import redis.asyncio as redis

_KEY_PREFIX = "ui-selection:"
_TTL_SECONDS = 30 * 60  # same lifetime as the session itself (session_store.py)

_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(os.environ["BACKOFFICE_REDIS_URL"])
    return _redis


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


async def get(session_id: str) -> set[str]:
    r = _get_redis()
    members = await r.smembers(_key(session_id))
    return {m.decode() if isinstance(m, bytes) else m for m in members}


async def update(session_id: str, ids: list[str], checked: bool) -> set[str]:
    """Add (`checked=True`) or remove (`checked=False`) `ids` from this
    session's selection, returning the resulting set."""
    if ids:
        r = _get_redis()
        key = _key(session_id)
        if checked:
            await r.sadd(key, *ids)
        else:
            await r.srem(key, *ids)
        await r.expire(key, _TTL_SECONDS)
    return await get(session_id)


async def clear(session_id: str) -> None:
    r = _get_redis()
    await r.delete(_key(session_id))
