"""Customer-side identity (PRD §7.1, CLAUDE.md's "bff_customer/" module
section): a signed session cookie holding `applicant_identifier`, no
password, no Redis.

**Corrected from an earlier draft of this module and of CLAUDE.md**,
which described this surface as having no verification at all: typing
someone else's email into the identify form was, until this fix,
sufficient to view and act on every application filed under that
identifier -- no proof of ownership required. See CLAUDE.md's Known
Gaps for the incident this closes and the design this file now
implements: a one-time, 6-digit email verification code, generated
here and "sent" via `bff_customer.notifications` (a fake, POC-scoped
delivery -- this project has no real email/SMS provider configured;
see that module's own docstring), that the applicant must type back
correctly before the real session cookie below is ever set. Password-
based auth was deliberately not chosen -- see PRD §7.1's "no password
needed" framing, which this preserves; verifying *the identifier
itself*, not adding a credential, is what closes the actual gap
(anyone typing an email they don't own can no longer get in).

**Its own cookie, not a key inside `bff_backoffice`'s Starlette
`SessionMiddleware` session** -- `.env.example` already anticipates this
(`CUSTOMER_SESSION_SECRET_KEY`, present since P5-1, well before this
module existed) as a value distinct from `BACKOFFICE_SESSION_SECRET_KEY`,
and P11-1's own wording ("a signed cookie session holding
`applicant_identifier`") reads as one dedicated cookie for exactly this
one value, not a slot inside a shared multi-purpose session blob. Hand-
rolled with `itsdangerous` directly (the same library
`starlette.middleware.sessions.SessionMiddleware` uses internally) since
Starlette supports only one `SessionMiddleware`/cookie per app and that
one is already spoken for by `bff_backoffice`'s Keycloak session id. The
new pending-verification cookie below reuses the same library and the
same secret key, deliberately -- it's the identical "no Redis, no
server-side state" philosophy this module already committed to, just
applied to a second, short-lived, narrower-purpose cookie instead of
introducing a new store.

The new-application wizard's own multi-step draft state (`routes.py`'s
`_DRAFT_KEY`) is a separate, lower-stakes concern -- ordinary UI flow
state, not identity -- and continues to use the app's existing
`request.session` (the `SessionMiddleware` `app.py` mounts for
`bff_backoffice`) rather than a second custom cookie here.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.responses import Response

COOKIE_NAME = "customer_session"
# No password, so no natural "log out everywhere" moment to bound this
# by -- a full year is effectively "until the browser clears cookies",
# matching the no-real-auth-credential model PRD §7.1 describes for
# this surface (verifying the identifier, not adding a password, is
# what changed -- see this module's docstring).
_MAX_AGE_S = 60 * 60 * 24 * 365

# ----------------------------------------------------------------------
# Pending email verification -- a second, short-lived, narrower-purpose
# signed cookie. Holds a *hash* of the one-time code, never the code
# itself: the cookie is tamper-evident (itsdangerous signs it) but not
# encrypted, so anyone who can read it (the applicant's own browser,
# where this is a non-issue -- but also, e.g., a browser extension or
# XSS payload with cookie access) could read a plaintext code directly
# and skip ever needing the "sent" one. Hashing means reading the
# cookie only reveals the hash; passing verification still requires
# knowing the actual code, which only reaches the applicant through
# `notifications.send_verification_code`.
# ----------------------------------------------------------------------

_PENDING_COOKIE_NAME = "customer_pending_verification"
_PENDING_MAX_AGE_S = 60 * 10  # 10 minutes -- short enough that a code
# left un-entered goes stale quickly; long enough for a real inbox check.
_CODE_LENGTH = 6
_CODE_ALPHABET = "0123456789"
# 5 wrong guesses against a 6-digit code (10**6 possibilities) is a
# negligible brute-force success rate for a POC; the attempt counter
# lives *inside* the signed cookie itself (see record_failed_verification_attempt)
# rather than in a server-side store, so it can only ever be
# incremented by this module's own signature -- a caller can't reset it
# by, say, just not sending the cookie, since that starts an entirely
# new (zero-attempt) verification instead of resetting an existing one.
_MAX_VERIFICATION_ATTEMPTS = 5


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("CUSTOMER_SESSION_SECRET_KEY", "dev-only-insecure-secret")
    return URLSafeTimedSerializer(secret, salt="bff-customer-identify")


def _pending_serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("CUSTOMER_SESSION_SECRET_KEY", "dev-only-insecure-secret")
    return URLSafeTimedSerializer(secret, salt="bff-customer-pending-verification")


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def get_applicant_identifier(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if token is None:
        return None
    try:
        value = _serializer().loads(token, max_age=_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return None
    return value if isinstance(value, str) else None


def set_applicant_identifier(response: Response, applicant_identifier: str) -> None:
    token = _serializer().dumps(applicant_identifier)
    response.set_cookie(COOKIE_NAME, token, max_age=_MAX_AGE_S, httponly=True, samesite="lax")


def clear_applicant_identifier(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


def generate_verification_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


@dataclass(frozen=True, slots=True)
class PendingVerification:
    applicant_identifier: str
    code_hash: str
    attempts: int


def start_verification(response: Response, applicant_identifier: str, code: str) -> None:
    """Called once, right after a fresh code is generated and "sent" --
    never call this to re-arm an existing pending verification (that's
    what a fresh `/apply/identify` submission naturally does instead,
    overwriting whatever pending cookie was already there)."""
    payload = {"applicant_identifier": applicant_identifier, "code_hash": _hash_code(code), "attempts": 0}
    token = _pending_serializer().dumps(payload)
    response.set_cookie(_PENDING_COOKIE_NAME, token, max_age=_PENDING_MAX_AGE_S, httponly=True, samesite="lax")


def get_pending_verification(request: Request) -> PendingVerification | None:
    token = request.cookies.get(_PENDING_COOKIE_NAME)
    if token is None:
        return None
    try:
        value = _pending_serializer().loads(token, max_age=_PENDING_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(value, dict):
        return None
    try:
        return PendingVerification(
            applicant_identifier=value["applicant_identifier"],
            code_hash=value["code_hash"],
            attempts=value["attempts"],
        )
    except (KeyError, TypeError):
        return None


def verify_code(pending: PendingVerification, submitted_code: str) -> bool:
    """Timing-safe comparison of hashes, not of the raw codes -- neither
    side of this comparison is ever the plaintext code once it leaves
    `generate_verification_code`'s return value."""
    return secrets.compare_digest(pending.code_hash, _hash_code(submitted_code.strip()))


def record_failed_verification_attempt(response: Response, pending: PendingVerification) -> None:
    """Re-signs the pending cookie with `attempts` incremented, or clears
    it outright once `_MAX_VERIFICATION_ATTEMPTS` is reached -- forcing
    a brand-new `/apply/identify` submission (a fresh code) rather than
    letting the same pending verification be guessed against
    indefinitely."""
    attempts = pending.attempts + 1
    if attempts >= _MAX_VERIFICATION_ATTEMPTS:
        clear_pending_verification(response)
        return
    payload = {"applicant_identifier": pending.applicant_identifier, "code_hash": pending.code_hash, "attempts": attempts}
    token = _pending_serializer().dumps(payload)
    response.set_cookie(_PENDING_COOKIE_NAME, token, max_age=_PENDING_MAX_AGE_S, httponly=True, samesite="lax")


def clear_pending_verification(response: Response) -> None:
    response.delete_cookie(_PENDING_COOKIE_NAME)
