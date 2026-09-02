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


# ----------------------------------------------------------------------
# Pending email verification -- closes the "type anyone's email in"
# gap (this module's own docstring has the full incident/design note).
# ----------------------------------------------------------------------

def _request_with_pending_cookie(value: str | None) -> SimpleNamespace:
    return SimpleNamespace(cookies={identity._PENDING_COOKIE_NAME: value} if value is not None else {})


def _pending_cookie_value_from(response: Response) -> str:
    header = response.headers["set-cookie"]
    return header.split(f"{identity._PENDING_COOKIE_NAME}=", 1)[1].split(";", 1)[0]


def test_generate_verification_code_is_six_digits():
    code = identity.generate_verification_code()
    assert len(code) == 6
    assert code.isdigit()


def test_generate_verification_code_is_statistically_unique():
    codes = {identity.generate_verification_code() for _ in range(200)}
    assert len(codes) > 190  # allow for the rare, expected collision


def test_start_verification_then_get_pending_round_trips():
    response = Response()
    identity.start_verification(response, "someone@example.com", "123456")

    pending = identity.get_pending_verification(_request_with_pending_cookie(_pending_cookie_value_from(response)))

    assert pending is not None
    assert pending.applicant_identifier == "someone@example.com"
    assert pending.attempts == 0
    # The raw code is never round-trippable from the cookie -- only its hash.
    assert pending.code_hash != "123456"


def test_get_pending_verification_returns_none_when_cookie_missing():
    assert identity.get_pending_verification(_request_with_pending_cookie(None)) is None


def test_get_pending_verification_rejects_garbage():
    assert identity.get_pending_verification(_request_with_pending_cookie("not-a-real-token")) is None


def test_verify_code_accepts_the_correct_code():
    response = Response()
    identity.start_verification(response, "someone@example.com", "123456")
    pending = identity.get_pending_verification(_request_with_pending_cookie(_pending_cookie_value_from(response)))

    assert identity.verify_code(pending, "123456") is True


def test_verify_code_rejects_a_wrong_code():
    response = Response()
    identity.start_verification(response, "someone@example.com", "123456")
    pending = identity.get_pending_verification(_request_with_pending_cookie(_pending_cookie_value_from(response)))

    assert identity.verify_code(pending, "654321") is False


def test_verify_code_strips_whitespace_from_the_submitted_code():
    response = Response()
    identity.start_verification(response, "someone@example.com", "123456")
    pending = identity.get_pending_verification(_request_with_pending_cookie(_pending_cookie_value_from(response)))

    assert identity.verify_code(pending, "  123456  ") is True


def test_record_failed_verification_attempt_increments_attempts():
    response = Response()
    identity.start_verification(response, "someone@example.com", "123456")
    pending = identity.get_pending_verification(_request_with_pending_cookie(_pending_cookie_value_from(response)))

    retry_response = Response()
    identity.record_failed_verification_attempt(retry_response, pending)
    updated = identity.get_pending_verification(
        _request_with_pending_cookie(_pending_cookie_value_from(retry_response))
    )

    assert updated is not None
    assert updated.attempts == 1
    assert updated.code_hash == pending.code_hash


def test_record_failed_verification_attempt_clears_cookie_after_max_attempts():
    """A caller can't reset the attempt counter by omitting the cookie --
    that just starts a brand-new (zero-attempt) verification via a fresh
    /apply/identify submission, never resets this one. The counter can
    only ever be incremented by this module's own signature."""
    response = Response()
    identity.start_verification(response, "someone@example.com", "123456")
    pending = identity.get_pending_verification(_request_with_pending_cookie(_pending_cookie_value_from(response)))

    for _ in range(identity._MAX_VERIFICATION_ATTEMPTS - 1):
        retry_response = Response()
        identity.record_failed_verification_attempt(retry_response, pending)
        pending = identity.get_pending_verification(
            _request_with_pending_cookie(_pending_cookie_value_from(retry_response))
        )
        assert pending is not None

    final_response = Response()
    identity.record_failed_verification_attempt(final_response, pending)

    header = final_response.headers["set-cookie"].lower()
    assert identity._PENDING_COOKIE_NAME in header
    assert "max-age=0" in header


def test_clear_pending_verification_expires_the_cookie():
    response = Response()
    identity.clear_pending_verification(response)

    header = response.headers["set-cookie"].lower()
    assert identity._PENDING_COOKIE_NAME in header
    assert "max-age=0" in header
