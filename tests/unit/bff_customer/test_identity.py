"""bff_customer/identity.py's tests -- pure sign/verify logic (a hand-
rolled itsdangerous cookie, per identity.py's own module docstring on
why it isn't just a key in bff_backoffice's SessionMiddleware session),
no live services needed."""

from __future__ import annotations

from types import SimpleNamespace

from starlette.responses import Response

from loan_onboarding.bff_customer import identity


def _request_with_cookie(value: str | None) -> SimpleNamespace:
    return SimpleNamespace(cookies={identity.COOKIE_NAME: value} if value is not None else {})


def _cookie_value_from(response: Response) -> str:
    header = response.headers["set-cookie"]
    return header.split(f"{identity.COOKIE_NAME}=", 1)[1].split(";", 1)[0]


def test_get_returns_none_when_cookie_missing():
    assert identity.get_applicant_identifier(_request_with_cookie(None)) is None


def test_set_then_get_round_trips_the_identifier():
    response = Response()
    identity.set_applicant_identifier(response, "someone@example.com")

    result = identity.get_applicant_identifier(_request_with_cookie(_cookie_value_from(response)))

    assert result == "someone@example.com"


def test_get_rejects_a_tampered_cookie():
    """Flips the last 4 base64url characters (a full 3-byte group) of
    the signature, not just the last one -- flipping a single character
    right at the end is flaky: base64's last character in a group can
    carry unused padding bits, so some single-character substitutions
    decode to the exact same underlying bytes and the "tampered"
    signature still verifies. A full group is guaranteed to change at
    least one underlying byte."""
    response = Response()
    identity.set_applicant_identifier(response, "someone@example.com")
    token = _cookie_value_from(response)
    tampered = token[:-4] + ("aaaa" if token[-4:] != "aaaa" else "bbbb")

    assert identity.get_applicant_identifier(_request_with_cookie(tampered)) is None


def test_get_rejects_garbage():
    assert identity.get_applicant_identifier(_request_with_cookie("not-a-real-token")) is None


def test_clear_expires_the_cookie():
    response = Response()
    identity.clear_applicant_identifier(response)

    header = response.headers["set-cookie"].lower()
    assert identity.COOKIE_NAME in header
    assert "max-age=0" in header
