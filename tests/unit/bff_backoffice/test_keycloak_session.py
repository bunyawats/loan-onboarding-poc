"""bff_backoffice/keycloak_session.py's tests mock session_store.py and
keycloak_auth.py at the function-call boundary -- no real Redis or
Keycloak needed. Every function under test takes a plain session_id
(not a FastAPI Request), per this module's own documented adaptation
from the reference project -- see its docstring."""

import time

import pytest
import respx
from httpx import Response

from loan_onboarding.bff_backoffice import keycloak_auth, keycloak_session, session_store

ISSUER = "http://localhost:8080/realms/loanrealm"


@pytest.fixture(autouse=True)
def keycloak_env(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER", "http://localhost:8080/realms/loanrealm")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "loan-onboarding-backoffice")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "test-secret")


class FakeStore:
    def __init__(self):
        self.data: dict[str, dict] = {}
        self.deleted: list[str] = []

    async def get(self, session_id):
        return self.data.get(session_id)

    async def set(self, session_id, data):
        self.data[session_id] = data

    async def delete(self, session_id):
        self.deleted.append(session_id)
        self.data.pop(session_id, None)


@pytest.fixture
def fake_store(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(session_store, "get", store.get)
    monkeypatch.setattr(session_store, "set", store.set)
    monkeypatch.setattr(session_store, "delete", store.delete)
    return store


def _valid_user(**overrides):
    defaults = dict(
        username="underwriter1",
        role="underwriter",
        access_token="access-abc",
        access_expires_at=time.time() + 300,
        refresh_token="refresh-abc",
        refresh_expires_at=time.time() + 1800,
    )
    defaults.update(overrides)
    return defaults


# ----------------------------------------------------------- get_session_user ----

async def test_get_session_user_no_session_id_raises_require_login():
    with pytest.raises(keycloak_session.RequireLoginRedirect):
        await keycloak_session.get_session_user(None)


async def test_get_session_user_unknown_session_raises_require_login(fake_store):
    with pytest.raises(keycloak_session.RequireLoginRedirect):
        await keycloak_session.get_session_user("nonexistent")


async def test_get_session_user_returns_valid_unexpired_session(fake_store):
    fake_store.data["sess-1"] = _valid_user()

    user = await keycloak_session.get_session_user("sess-1")

    assert user["username"] == "underwriter1"


async def test_get_session_user_refreshes_expired_access_token(fake_store, monkeypatch):
    fake_store.data["sess-1"] = _valid_user(access_expires_at=time.time() - 10)

    async def fake_refresh(refresh_token):
        assert refresh_token == "refresh-abc"
        return {"access_token": "new-access", "expires_in": 300}

    monkeypatch.setattr(keycloak_auth, "refresh_access_token", fake_refresh)

    user = await keycloak_session.get_session_user("sess-1")

    assert user["access_token"] == "new-access"
    assert fake_store.data["sess-1"]["access_token"] == "new-access"


async def test_get_session_user_deletes_session_when_refresh_fails(fake_store, monkeypatch):
    fake_store.data["sess-1"] = _valid_user(access_expires_at=time.time() - 10)

    async def fake_refresh(refresh_token):
        raise keycloak_auth.RefreshFailed("expired")

    monkeypatch.setattr(keycloak_auth, "refresh_access_token", fake_refresh)

    with pytest.raises(keycloak_session.RequireLoginRedirect):
        await keycloak_session.get_session_user("sess-1")

    assert "sess-1" in fake_store.deleted
    assert "sess-1" not in fake_store.data


# ------------------------------------------------------- require_session_role ----

async def test_require_session_role_passes_for_matching_role(fake_store):
    fake_store.data["sess-1"] = _valid_user(role="underwriter")
    checker = keycloak_session.require_session_role("underwriter")

    user = await checker("sess-1")

    assert user["role"] == "underwriter"


async def test_require_session_role_raises_role_denied_for_mismatched_role(fake_store):
    fake_store.data["sess-1"] = _valid_user(role="manager")
    checker = keycloak_session.require_session_role("underwriter")

    with pytest.raises(keycloak_session.RoleDenied):
        await checker("sess-1")


async def test_require_session_role_raises_require_login_for_no_session():
    checker = keycloak_session.require_session_role("underwriter")

    with pytest.raises(keycloak_session.RequireLoginRedirect):
        await checker(None)


# -------------------------------------------- check_permission / require_permission ----

async def test_check_permission_passes_when_granted(fake_store, monkeypatch):
    user = _valid_user()

    async def fake_get_permissions(access_token):
        return {"UnderwriterApprove", "UnderwriterReject"}

    monkeypatch.setattr(keycloak_auth, "get_permissions", fake_get_permissions)

    await keycloak_session.check_permission(user, "UnderwriterApprove")  # does not raise


async def test_check_permission_raises_permission_denied_when_not_granted(monkeypatch):
    user = _valid_user()

    async def fake_get_permissions(access_token):
        return {"UnderwriterReject"}

    monkeypatch.setattr(keycloak_auth, "get_permissions", fake_get_permissions)

    with pytest.raises(keycloak_session.PermissionDenied):
        await keycloak_session.check_permission(user, "UnderwriterApprove")


async def test_check_permission_raises_require_login_on_invalid_token(monkeypatch):
    user = _valid_user()

    async def fake_get_permissions(access_token):
        raise keycloak_auth.TokenInvalid("expired")

    monkeypatch.setattr(keycloak_auth, "get_permissions", fake_get_permissions)

    with pytest.raises(keycloak_session.RequireLoginRedirect):
        await keycloak_session.check_permission(user, "UnderwriterApprove")


async def test_check_permission_raises_runtime_error_on_infra_failure(monkeypatch):
    user = _valid_user()

    async def fake_get_permissions(access_token):
        raise keycloak_auth.PermissionCheckError("Keycloak unreachable")

    monkeypatch.setattr(keycloak_auth, "get_permissions", fake_get_permissions)

    with pytest.raises(RuntimeError):
        await keycloak_session.check_permission(user, "UnderwriterApprove")


async def test_require_permission_end_to_end(fake_store, monkeypatch):
    fake_store.data["sess-1"] = _valid_user()

    async def fake_get_permissions(access_token):
        return {"UnderwriterApprove"}

    monkeypatch.setattr(keycloak_auth, "get_permissions", fake_get_permissions)
    checker = keycloak_session.require_permission("UnderwriterApprove")

    user = await checker("sess-1")

    assert user["username"] == "underwriter1"


# ------------------------------------------------------------- complete_login ----

async def test_complete_login_state_mismatch_raises_value_error():
    with pytest.raises(ValueError, match="tampered"):
        await keycloak_session.complete_login(
            code="abc", state="state-a", expected_state="state-b", redirect_uri="http://x/ui/callback"
        )


async def test_complete_login_no_expected_state_raises_value_error():
    with pytest.raises(ValueError):
        await keycloak_session.complete_login(
            code="abc", state="state-a", expected_state=None, redirect_uri="http://x/ui/callback"
        )


async def test_complete_login_token_exchange_rejected_raises_value_error():
    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(400, json={"error": "invalid_grant"})
        )
        with pytest.raises(ValueError, match="rejected the login"):
            await keycloak_session.complete_login(
                code="bad-code", state="s", expected_state="s", redirect_uri="http://x/ui/callback"
            )


async def test_complete_login_underwriter_role_resolves_and_persists_session(fake_store, monkeypatch):
    def fake_decode_token(token):
        return {"preferred_username": "underwriter1", "realm_access": {"roles": ["Underwriter"]}}

    monkeypatch.setattr(keycloak_auth, "decode_token", fake_decode_token)

    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "access-tok",
                    "refresh_token": "refresh-tok",
                    "expires_in": 300,
                    "refresh_expires_in": 1800,
                },
            )
        )
        session_id, role = await keycloak_session.complete_login(
            code="good-code", state="s", expected_state="s", redirect_uri="http://x/ui/callback"
        )

    assert role == "underwriter"
    stored = fake_store.data[session_id]
    assert stored["username"] == "underwriter1"
    assert stored["access_token"] == "access-tok"
    assert stored["refresh_token"] == "refresh-tok"


async def test_complete_login_manager_role_resolves(fake_store, monkeypatch):
    def fake_decode_token(token):
        return {"preferred_username": "manager1", "realm_access": {"roles": ["Manager"]}}

    monkeypatch.setattr(keycloak_auth, "decode_token", fake_decode_token)

    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(200, json={"access_token": "access-tok", "expires_in": 300})
        )
        session_id, role = await keycloak_session.complete_login(
            code="good-code", state="s", expected_state="s", redirect_uri="http://x/ui/callback"
        )

    assert role == "manager"


async def test_complete_login_no_recognized_role_raises_value_error(fake_store, monkeypatch):
    def fake_decode_token(token):
        return {"preferred_username": "nobody", "realm_access": {"roles": ["SomeOtherRole"]}}

    monkeypatch.setattr(keycloak_auth, "decode_token", fake_decode_token)

    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(200, json={"access_token": "access-tok", "expires_in": 300})
        )
        with pytest.raises(ValueError, match="neither the Underwriter nor Manager role"):
            await keycloak_session.complete_login(
                code="good-code", state="s", expected_state="s", redirect_uri="http://x/ui/callback"
            )


async def test_complete_login_invalid_token_raises_value_error(monkeypatch):
    def fake_decode_token(token):
        raise ValueError("bad signature")

    monkeypatch.setattr(keycloak_auth, "decode_token", fake_decode_token)

    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(200, json={"access_token": "access-tok", "expires_in": 300})
        )
        with pytest.raises(ValueError, match="failed validation"):
            await keycloak_session.complete_login(
                code="good-code", state="s", expected_state="s", redirect_uri="http://x/ui/callback"
            )


# --------------------------------------------------------------------- logout ----

async def test_logout_deletes_session_when_present(fake_store):
    fake_store.data["sess-1"] = _valid_user()

    await keycloak_session.logout("sess-1")

    assert "sess-1" in fake_store.deleted


async def test_logout_no_session_id_is_a_no_op(fake_store):
    await keycloak_session.logout(None)  # does not raise

    assert fake_store.deleted == []


# --------------------------------------------------------- URL-building helpers ----

def test_build_authorize_url_includes_redirect_and_state():
    url, state = keycloak_session.build_authorize_url("http://x/ui/callback")

    assert url.startswith(f"{ISSUER}/protocol/openid-connect/auth?")
    assert "redirect_uri=http" in url
    assert state in url


def test_logout_redirect_url_includes_client_and_redirect():
    url = keycloak_session.logout_redirect_url("http://x/ui/login")

    assert url.startswith(f"{ISSUER}/protocol/openid-connect/logout?")
    assert "loan-onboarding-backoffice" in url


def test_build_authorize_url_uses_public_issuer_when_set(monkeypatch):
    """The container-split scenario found in P12-3: `KEYCLOAK_ISSUER`
    is the internal, network-reachable address for this app's own
    server-to-server calls; the browser must never see it (it's not
    resolvable outside the compose network) -- these two browser
    redirects need `KEYCLOAK_PUBLIC_ISSUER` instead, whenever it's set."""
    monkeypatch.setenv("KEYCLOAK_ISSUER", "http://keycloak:8080/realms/loanrealm")
    monkeypatch.setenv("KEYCLOAK_PUBLIC_ISSUER", "http://localhost:8080/realms/loanrealm")

    authorize_url, _ = keycloak_session.build_authorize_url("http://x/ui/callback")
    logout_url = keycloak_session.logout_redirect_url("http://x/ui/login")

    assert authorize_url.startswith("http://localhost:8080/realms/loanrealm/protocol/openid-connect/auth?")
    assert logout_url.startswith("http://localhost:8080/realms/loanrealm/protocol/openid-connect/logout?")
    assert "keycloak:8080" not in authorize_url
    assert "keycloak:8080" not in logout_url
