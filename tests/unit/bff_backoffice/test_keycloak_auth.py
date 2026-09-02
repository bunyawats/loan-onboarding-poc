"""
Unit tests for bff_backoffice/keycloak_auth.py -- no live Keycloak
needed. Mirrors review-approval-temporal's own test_keycloak_auth.py
(fetched and read directly), adapted for this project's Resource/Scopes.

JWT validation (decode_token) is tested against a locally-generated RSA
keypair, with _get_jwk_client patched to return it -- PyJWKClient fetches
its JWKS via urllib.request, not httpx, so respx (which only intercepts
httpx) can't mock that call directly. This still exercises the real
jwt.decode() validation logic (signature, issuer, expiry), only faking
"where the verification key comes from."

Permission checks (get_permissions) go through httpx, so respx mocks the
UMA ticket exchange call directly -- the three response shapes asserted
here (200 granted list, 403 access_denied, 401 invalid_grant) were
confirmed against this project's own real Keycloak instance in P9-1,
not assumed from docs.
"""

import time

import httpx
import jwt as pyjwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import Response

from loan_onboarding.bff_backoffice import keycloak_auth

ISSUER = "http://localhost:8080/realms/testrealm"


@pytest.fixture(autouse=True)
def keycloak_env(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER", ISSUER)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "loan-onboarding-backoffice")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "test-secret")
    keycloak_auth._jwk_client = None  # reset the module-level cache between tests


@pytest.fixture
def rsa_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _make_token(private_key, **claim_overrides):
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": "test-user-id",
        "preferred_username": "underwriter1",
        "iat": now,
        "exp": now + 300,
        **claim_overrides,
    }
    return pyjwt.encode(payload, private_key, algorithm="RS256")


def _patch_jwk_client(monkeypatch, public_key):
    """Make decode_token()'s key lookup return `public_key`, regardless
    of what actually signed the token under test -- lets tests control
    exactly which key the verifier thinks is legitimate."""

    class _FakeSigningKey:
        key = public_key

    class _FakeJWKClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    monkeypatch.setattr(keycloak_auth, "_get_jwk_client", lambda: _FakeJWKClient())


# --------------------------------------------------------------- decode_token ----

def test_decode_token_valid(monkeypatch, rsa_keys):
    private_key, public_key = rsa_keys
    _patch_jwk_client(monkeypatch, public_key)

    claims = keycloak_auth.decode_token(_make_token(private_key))

    assert claims["preferred_username"] == "underwriter1"


def test_decode_token_expired(monkeypatch, rsa_keys):
    private_key, public_key = rsa_keys
    _patch_jwk_client(monkeypatch, public_key)
    token = _make_token(private_key, exp=int(time.time()) - 60)

    with pytest.raises(pyjwt.ExpiredSignatureError):
        keycloak_auth.decode_token(token)


def test_decode_token_wrong_issuer(monkeypatch, rsa_keys):
    private_key, public_key = rsa_keys
    _patch_jwk_client(monkeypatch, public_key)
    token = _make_token(private_key, iss="http://not-us.example.com/realms/other")

    with pytest.raises(pyjwt.InvalidIssuerError):
        keycloak_auth.decode_token(token)


def test_decode_token_wrong_signature(monkeypatch, rsa_keys):
    # Token is signed by a DIFFERENT keypair than the one the (mocked)
    # JWKS endpoint claims is legitimate -- signature must not verify.
    signing_key, _ = rsa_keys
    claimed_public_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    _patch_jwk_client(monkeypatch, claimed_public_key)
    token = _make_token(signing_key)

    with pytest.raises(pyjwt.InvalidSignatureError):
        keycloak_auth.decode_token(token)


def test_decode_token_keycloak_issuer_unset(monkeypatch, rsa_keys):
    monkeypatch.delenv("KEYCLOAK_ISSUER", raising=False)

    with pytest.raises(RuntimeError):
        keycloak_auth.decode_token("irrelevant")


# ----------------------------------------------------------- get_permissions ----

async def test_get_permissions_granted():
    # Real shape confirmed empirically against this project's own P9-1
    # instance: one entry per resource (always exactly one here -- a
    # single "LoanApplication" resource carries all five scopes), with a
    # "scopes" list of every granted scope name.
    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "rsid": "86cbccdd-2a19-4a6d-abb2-840d6311ce14",
                        "rsname": "LoanApplication",
                        "scopes": ["UnderwriterApprove", "UnderwriterReject", "UnderwriterRequestMoreInfo"],
                    }
                ],
            )
        )
        perms = await keycloak_auth.get_permissions("some-token")

    assert perms == {"UnderwriterApprove", "UnderwriterReject", "UnderwriterRequestMoreInfo"}


async def test_get_permissions_zero_granted():
    # Confirmed empirically: Keycloak returns 403 access_denied for a
    # validly-authenticated token that simply has no matching Resources,
    # not a 200 with an empty list.
    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(403, json={"error": "access_denied", "error_description": "not_authorized"})
        )
        perms = await keycloak_auth.get_permissions("some-token")

    assert perms == set()


async def test_get_permissions_invalid_token():
    # Confirmed empirically: a garbage/expired bearer token gets 401
    # invalid_grant from the UMA endpoint -- distinct from the 403
    # access_denied "zero permissions" case above, so callers can tell
    # "not logged in" apart from "logged in, no permission."
    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(401, json={"error": "invalid_grant", "error_description": "Invalid bearer token"})
        )
        with pytest.raises(keycloak_auth.TokenInvalid):
            await keycloak_auth.get_permissions("bad-token")


async def test_get_permissions_unexpected_403_body():
    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(403, json={"error": "something_else"})
        )
        with pytest.raises(keycloak_auth.PermissionCheckError):
            await keycloak_auth.get_permissions("some-token")


async def test_get_permissions_keycloak_unreachable():
    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(keycloak_auth.PermissionCheckError):
            await keycloak_auth.get_permissions("some-token")


async def test_get_permissions_missing_client_credentials(monkeypatch):
    monkeypatch.delenv("KEYCLOAK_CLIENT_ID", raising=False)

    with pytest.raises(RuntimeError):
        await keycloak_auth.get_permissions("some-token")


# ------------------------------------------------------- refresh_access_token ----

async def test_refresh_access_token_success():
    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(200, json={"access_token": "new-token", "expires_in": 300})
        )
        result = await keycloak_auth.refresh_access_token("some-refresh-token")

    assert result["access_token"] == "new-token"


async def test_refresh_access_token_rejected():
    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=Response(400, json={"error": "invalid_grant"})
        )
        with pytest.raises(keycloak_auth.RefreshFailed):
            await keycloak_auth.refresh_access_token("expired-refresh-token")


async def test_refresh_access_token_missing_client_credentials(monkeypatch):
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)

    with pytest.raises(RuntimeError):
        await keycloak_auth.refresh_access_token("some-refresh-token")
