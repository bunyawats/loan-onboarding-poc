"""
Real Keycloak session auth for the `/ui/*` HTMX UI (Authorization Code
flow), direct adaptation of `review-approval-temporal`'s own
`bff/keycloak_session.py`.

Session shape: the browser cookie (signed via Starlette's
`SessionMiddleware`, `BACKOFFICE_SESSION_SECRET_KEY`) holds only an
opaque server-side session id, minted at login
(`session_store.new_session_id()`); everything else lives in Redis
under that id (`session_store.py`) -- deliberately never in the cookie
itself, since a real access+refresh token pair runs ~4.5KB signed, over
the ~4KB limit real browsers enforce per cookie (measured directly in
the reference project this is adapted from).

**Deliberate adaptation from the reference project, not a literal
port**: every function below takes a plain `session_id: str | None`
(or, for `complete_login`, an already-resolved `expected_state`)
instead of a FastAPI `Request` object. The reference project's
equivalent functions reached into `request.session`/`request.app.state.redis`
directly, which made them impossible to unit test without a real
Starlette `Request`. `app.py` (Phase 10) doesn't exist yet, and there's
no reason this module's session-resolution *logic* should depend on a
web framework to be testable -- `bff_backoffice/routes.py` (Phase 10)
is expected to be the thin, framework-coupled layer that reads
`request.session.get(SESSION_KEY)` and calls into this module's plain
functions, mirroring how `workflow/service.py` stays framework-agnostic
and lets its callers own the HTTP-shaped bits.

Two authorization mechanisms, for two different purposes -- don't
conflate them (CLAUDE.md's "Identity" is explicit about this, including
a past incident in the reference project from conflating them):

- **`require_session_role(role)`** gates *page/screen selection*
  ("underwriter" vs "manager"), not specific actions -- `Underwriter`/
  `Manager` are plain realm roles (see
  `keycloak/import/loanrealm-realm.json`), and there's no
  Resource/Permission for "which screen can I see", so a role check is
  the right tool here.
- **`require_permission(permission)`** / **`check_permission(user,
  permission)`** gate the five *mutating* actions
  (`UnderwriterApprove`/`UnderwriterReject`/`UnderwriterRequestMoreInfo`/
  `ManagerApprove`/`ManagerReject` -- Scopes on the single
  `LoanApplication` Resource) via a real UMA ticket exchange
  (`keycloak_auth.get_permissions()`) -- **no `require_session_role`
  pre-gate on any decision route, ever** (CLAUDE.md's explicit warning:
  the reference project shipped both once and produced two different
  403 reasons for the same denied action).

Access-token refresh happens lazily, inside `get_session_user()` (the
single choke point both mechanisms above call through): an expired
`access_token` triggers a `keycloak_auth.refresh_access_token()` call
using the stored `refresh_token`, transparent to the caller. Only when
the refresh token itself is rejected does the session actually end.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any, Callable, Coroutine
from urllib.parse import urlencode

import httpx

from loan_onboarding.bff_backoffice import keycloak_auth, session_store

SESSION_KEY = "user"

VALID_ROLES = ("underwriter", "manager")


class RequireLoginRedirect(Exception):
    """No valid session -- `bff_backoffice/routes.py` (Phase 10) maps
    this to a 303 redirect to `/ui/login`."""


class RoleDenied(Exception):
    """Session exists but its role doesn't match `require_session_role`'s
    required role -- `routes.py` maps this to a 403."""

    def __init__(self, role: str) -> None:
        super().__init__(f"requires role: {role}")
        self.role = role


class PermissionDenied(Exception):
    """Session's token doesn't carry the required Scope -- `routes.py`
    maps this to a 403."""

    def __init__(self, permission: str) -> None:
        super().__init__(f"requires permission: {permission}")
        self.permission = permission


def _issuer() -> str:
    issuer = os.environ.get("KEYCLOAK_ISSUER")
    if not issuer:
        raise RuntimeError("KEYCLOAK_ISSUER is not set")
    return issuer


def _public_issuer() -> str:
    """The issuer URL as the *browser* needs to reach it -- distinct
    from `_issuer()` (used for every server-to-server call: token
    exchange, JWKS fetch, UMA permission checks) once `app` and
    `keycloak` are separate containers on a Docker network. Found for
    real during P12-3's from-clean `docker compose up`: with
    `KEYCLOAK_ISSUER=http://keycloak:8080/realms/loanrealm` (correct
    for the `app` container's own server-to-server calls),
    `build_authorize_url`/`logout_redirect_url` were sending the
    browser to `http://keycloak:...` too -- a hostname that only
    resolves inside the compose network, never on the host running the
    browser. Never hit before because every earlier phase's testing ran
    `app.py` natively on the host, where `KEYCLOAK_ISSUER=http://localhost:8080/...`
    was correct for both purposes at once.

    Falls back to `_issuer()` when unset, so the native/host-run case
    (both issuer values identical) needs no config change -- only
    `docker-compose.yml`'s `app` service sets `KEYCLOAK_PUBLIC_ISSUER`
    explicitly."""
    return os.environ.get("KEYCLOAK_PUBLIC_ISSUER") or _issuer()


def _client_id() -> str:
    client_id = os.environ.get("KEYCLOAK_CLIENT_ID")
    if not client_id:
        raise RuntimeError("KEYCLOAK_CLIENT_ID is not set")
    return client_id


def _client_secret() -> str:
    secret = os.environ.get("KEYCLOAK_CLIENT_SECRET")
    if not secret:
        raise RuntimeError("KEYCLOAK_CLIENT_SECRET is not set")
    return secret


def build_authorize_url(redirect_uri: str) -> tuple[str, str]:
    """Generates a fresh CSRF state and returns `(authorize_url, state)`
    -- the caller (a route handler) stashes `state` in its own
    `request.session` and passes it back into `complete_login()` as
    `expected_state` on callback."""
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": _client_id(),
        "response_type": "code",
        "scope": "openid",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{_public_issuer()}/protocol/openid-connect/auth?{urlencode(params)}", state


async def complete_login(code: str, state: str, expected_state: str | None, redirect_uri: str) -> tuple[str, str]:
    """Exchange an authorization code for tokens, validate the returned
    access token, resolve the session's role, and persist the session to
    `session_store`. Returns `(session_id, role)` -- the caller only
    needs to set its own `request.session[SESSION_KEY] = session_id`.

    Raises ValueError on any failure (state mismatch, code exchange
    rejected, no Underwriter/Manager role) -- callers map that to a
    re-rendered login page with an error.
    """
    if not expected_state or expected_state != state:
        raise ValueError("Login session expired or was tampered with -- try again.")

    token_url = f"{_issuer()}/protocol/openid-connect/token"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": _client_id(),
                "client_secret": _client_secret(),
            },
        )
    if response.status_code != 200:
        raise ValueError(f"Keycloak rejected the login: {response.text}")
    tokens = response.json()

    try:
        claims = keycloak_auth.decode_token(tokens["access_token"])
    except Exception as e:
        raise ValueError(f"Keycloak issued a token that failed validation: {e}")

    roles = set(claims.get("realm_access", {}).get("roles", []))
    if "Underwriter" in roles:
        role = "underwriter"
    elif "Manager" in roles:
        role = "manager"
    else:
        raise ValueError("This account has neither the Underwriter nor Manager role -- contact an admin.")

    session_id = session_store.new_session_id()
    await session_store.set(
        session_id,
        {
            "username": claims.get("preferred_username", claims.get("sub")),
            "role": role,
            "access_token": tokens["access_token"],
            "access_expires_at": time.time() + tokens.get("expires_in", 300),
            "refresh_token": tokens.get("refresh_token"),
            "refresh_expires_at": time.time() + tokens.get("refresh_expires_in", 0),
        },
    )
    return session_id, role


def logout_redirect_url(redirect_uri: str) -> str:
    """URL to send the browser to for a real Keycloak single-logout --
    ends Keycloak's own session too, not just this app's cookie.
    `redirect_uri` must match a `post.logout.redirect.uris` entry
    registered on the client (`keycloak/import/loanrealm-realm.json`)."""
    params = {"client_id": _client_id(), "post_logout_redirect_uri": redirect_uri}
    return f"{_public_issuer()}/protocol/openid-connect/logout?{urlencode(params)}"


async def logout(session_id: str | None) -> None:
    if session_id:
        await session_store.delete(session_id)


async def get_session_user(session_id: str | None) -> dict[str, Any]:
    """Resolve the current session, transparently refreshing an expired
    access token. Raises `RequireLoginRedirect` if there's no valid
    session left after that attempt."""
    if not session_id:
        raise RequireLoginRedirect()

    user = await session_store.get(session_id)
    if not user:
        raise RequireLoginRedirect()

    if time.time() >= user.get("access_expires_at", 0):
        try:
            refreshed = await keycloak_auth.refresh_access_token(user["refresh_token"])
        except keycloak_auth.RefreshFailed:
            # Refresh token itself is no longer good -- a real end of
            # session, not a transient failure.
            await session_store.delete(session_id)
            raise RequireLoginRedirect()

        user["access_token"] = refreshed["access_token"]
        user["access_expires_at"] = time.time() + refreshed.get("expires_in", 300)
        if refreshed.get("refresh_token"):
            user["refresh_token"] = refreshed["refresh_token"]
        if "refresh_expires_in" in refreshed:
            user["refresh_expires_at"] = time.time() + refreshed["refresh_expires_in"]
        await session_store.set(session_id, user)

    return user


def require_session_role(role: str) -> Callable[[str | None], Coroutine[Any, Any, dict[str, Any]]]:
    """`require_session_role("underwriter")` -- gates page/screen
    selection, not actions. See this module's docstring for why this
    stays role-based rather than a permission check."""

    async def checker(session_id: str | None) -> dict[str, Any]:
        user = await get_session_user(session_id)
        if user["role"] != role:
            raise RoleDenied(role)
        return user

    return checker


async def check_permission(user: dict[str, Any], permission: str) -> None:
    """Raise `PermissionDenied` if `user` doesn't have `permission`, via
    a real UMA ticket exchange -- never cached (CLAUDE.md's "Identity":
    "no caching on permission checks")."""
    try:
        granted = await keycloak_auth.get_permissions(user["access_token"])
    except keycloak_auth.TokenInvalid:
        # Session's access token itself is no longer valid (rejected for
        # some reason other than plain expiry, which get_session_user()
        # already handles) -- send back through login.
        raise RequireLoginRedirect()
    except keycloak_auth.PermissionCheckError as e:
        raise RuntimeError(f"permission check failed: {e}") from e
    if permission not in granted:
        raise PermissionDenied(permission)


def require_permission(permission: str) -> Callable[[str | None], Coroutine[Any, Any, dict[str, Any]]]:
    """`require_permission("UnderwriterApprove")` -- gates the five
    mutating actions via a real UMA ticket exchange. See this module's
    docstring for how this differs from `require_session_role()`."""

    async def checker(session_id: str | None) -> dict[str, Any]:
        user = await get_session_user(session_id)
        await check_permission(user, permission)
        return user

    return checker
