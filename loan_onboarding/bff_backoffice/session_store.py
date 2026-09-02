"""
Thin Redis wrapper for the `/ui/*` server-side session store (CLAUDE.md's
"Identity" -- back-office side). The browser cookie holds only an opaque
session id (see `keycloak_session.py`); everything else -- username,
role, and both Keycloak token pairs -- lives here, on the dedicated
`backoffice-redis` instance (never `mayan-redis` -- CLAUDE.md's "Data
storage").

Key shape: `ui-session:<session id>` -> JSON-encoded `{"username",
"role", "access_token", "access_expires_at", "refresh_token",
"refresh_expires_at"}`.

TTL is sliding, not a fixed cap from login: every successful `get()`
pushes the key's expiry back out to `SESSION_TTL_SECONDS`, so "session
lasts 30 minutes" tracks 30 minutes of *inactivity*, matching Keycloak's
own `ssoSessionIdleTimeout` default.

Owns its own lazily-initialized Redis client (`BACKOFFICE_REDIS_URL`),
same convention as every domain module's `db.py` owning its own
lazily-initialized `asyncpg` pool -- not something obtained from a
FastAPI `app.state`, since `app.py` (Phase 10) doesn't exist yet and
this module has no reason to depend on it existing.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any

import redis.asyncio as redis

SESSION_TTL_SECONDS = 30 * 60  # 30 minutes idle timeout, sliding

_KEY_PREFIX = "ui-session:"

_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(os.environ["BACKOFFICE_REDIS_URL"])
    return _redis


def new_session_id() -> str:
    # Same generation style as keycloak_session.py's OAuth CSRF state.
    return secrets.token_urlsafe(32)


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


async def get(session_id: str) -> dict[str, Any] | None:
    r = _get_redis()
    raw = await r.get(_key(session_id))
    if raw is None:
        return None
    await r.expire(_key(session_id), SESSION_TTL_SECONDS)
    return json.loads(raw)


async def set(session_id: str, data: dict[str, Any]) -> None:
    r = _get_redis()
    await r.set(_key(session_id), json.dumps(data), ex=SESSION_TTL_SECONDS)


async def delete(session_id: str) -> None:
    r = _get_redis()
    await r.delete(_key(session_id))
