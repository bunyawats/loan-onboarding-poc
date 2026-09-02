"""Customer-side identity (PRD §7.1, CLAUDE.md's "bff_customer/" module
section): a plain signed session cookie holding `applicant_identifier`,
no password, no verification, no Redis.

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
one is already spoken for by `bff_backoffice`'s Keycloak session id.

The new-application wizard's own multi-step draft state (`routes.py`'s
`_DRAFT_KEY`) is a separate, lower-stakes concern -- ordinary UI flow
state, not identity -- and continues to use the app's existing
`request.session` (the `SessionMiddleware` `app.py` mounts for
`bff_backoffice`) rather than a second custom cookie here.
"""

from __future__ import annotations

import os

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.responses import Response

COOKIE_NAME = "customer_session"
# No password, so no natural "log out everywhere" moment to bound this
# by -- a full year is effectively "until the browser clears cookies",
# matching the no-real-auth model PRD §7.1 describes for this surface.
_MAX_AGE_S = 60 * 60 * 24 * 365


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("CUSTOMER_SESSION_SECRET_KEY", "dev-only-insecure-secret")
    return URLSafeTimedSerializer(secret, salt="bff-customer-identify")


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
