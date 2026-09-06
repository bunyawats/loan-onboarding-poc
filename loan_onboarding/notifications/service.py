"""Notification delivery -- a shared leaf module (Phase 18, "Account
closure" -- see `CLAUDE.md`), same "zero dependency on anything else in
this codebase" shape as `idgen/`.

**Promoted out of `bff_customer/notifications.py`**, where this used to
live as the only place fake/dev-only email delivery existed, reachable
only from a BFF's synchronous HTTP request handler. `account/activities.py`
needs to send an account-closure-decision email from inside a Temporal
*activity* (the backend worker process, not a BFF) and cannot reach
into `bff_customer` -- wrong direction entirely, since BFFs are
consumers of domain modules, never the reverse. Promoting the existing
mechanism here lets both `bff_customer`'s OTP flow and
`account/activities.py`'s closure-decision email share one delivery
mechanism instead of duplicating it.

**Fake by default, real delivery optional (Phase 20, see `CLAUDE.md`'s
"Real email delivery via Gmail SMTP").** `send_verification_code` is
still always fake -- see that function's own docstring for why (the
verify-code page shows the OTP code directly in its own response;
switching that to real-only delivery would remove the only way a
tester without real inbox access can complete the identify flow at
all). `send_account_closure_decision`/`send_welcome_letter_email` both
go through the private `_send_email` helper below: it sends a real
email via SMTP when `SMTP_USERNAME`/`SMTP_PASSWORD` are both set (a
Gmail account + an App Password, in the common case -- see
`.env.example`), and falls through to the exact same fake `print()`
behavior this module always had, byte-for-byte, when they aren't. Zero
new `pyproject.toml` dependencies -- `smtplib`/`email` are Python
stdlib. A real send failure is caught and logged, never raised -- see
`_send_email`'s own docstring for why that's a deliberate design
choice, not an oversight."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

_DEFAULT_SMTP_HOST = "smtp.gmail.com"
_DEFAULT_SMTP_PORT = "587"


def send_verification_code(applicant_identifier: str, code: str) -> None:
    # print(), not the logging module -- this codebase has no logging
    # configuration anywhere (no basicConfig, no handler), so a plain
    # logger.info() call here would inherit the root logger's default
    # WARNING level and be silently dropped, never reaching
    # `docker compose logs`/the console despite looking like it should.
    # Confirmed live: it didn't show up until switched to this.
    print(f"Verification code for {applicant_identifier}: {code} (POC: no real email/SMS provider configured -- see this module's docstring)")


def _send_email(to_address: str, subject: str, body: str) -> None:
    """Sends a real email via SMTP when `SMTP_USERNAME`/`SMTP_PASSWORD`
    are both set (checked here, not by the caller, so every email-
    sending function above shares one real-vs-fake decision); otherwise
    prints `body` plus the same "(POC: ...)" trailer this module's
    functions have always printed, so the fake path stays byte-for-byte
    identical to before Phase 20 -- `body` deliberately carries none of
    that disclaimer itself, since it doubles as the real email's own
    content, where it would be actively wrong (the email genuinely was
    sent by a real provider in that branch).

    **A real send failure is caught here and logged, never raised.**
    Both callers sit inside Temporal activities whose other provisioning
    calls already accept "a retry skips this permanently, once the
    account/decision is already committed" as a smaller, more
    recoverable gap than letting a transient failure retry the whole
    activity (see `CLAUDE.md`'s "Applying without being a customer
    yet" and its Phase 19 live confirmation of exactly this mechanism).
    A flaky Gmail connection is exactly that kind of transient failure;
    letting it propagate would risk turning a successful approval or
    closure decision into a stuck, `Failed` Temporal workflow over
    nothing more than an undelivered notification -- a materially worse
    outcome than the notification simply not arriving."""
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    if not username or not password:
        print(f"{body} (POC: no real email/SMS provider configured -- see this module's docstring)")
        return

    host = os.environ.get("SMTP_HOST", _DEFAULT_SMTP_HOST)
    port = int(os.environ.get("SMTP_PORT", _DEFAULT_SMTP_PORT))
    from_address = os.environ.get("SMTP_FROM_ADDRESS") or username

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = to_address
    message.set_content(body)

    try:
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring
        print(f"Failed to send email to {to_address} ({subject!r}): {exc!r} -- see this module's docstring")


def send_account_closure_decision(
    applicant_identifier: str,
    account_id: str,
    product_type: str,
    decision: str,
    comment: str,
) -> None:
    """Called only from `account/activities.py`'s `persist_closure_decision`,
    on either outcome (approve -> `CLOSED`, reject -> reverts to
    `ACTIVE`) -- a narrow, one-decision-only exception to `PRD.md` §4's
    "no proactive notification" non-goal, confirmed with the user as
    scoped to this one decision, not a general notification feature."""
    body = (
        f"Account closure {decision} for {applicant_identifier} "
        f"(account {account_id}, {product_type}): {comment}"
    )
    _send_email(applicant_identifier, f"Account closure {decision}", body)


def send_welcome_letter_email(
    applicant_identifier: str,
    account_id: str,
    product_type: str,
    amount: str,
) -> None:
    """Called only from `application/activities.py`'s `persist_decision`,
    inside the same `existing_account is None` provisioning block that
    already calls `document.service.generate_welcome_letter(...)` -- the
    second of `PRD.md` §4's two narrow exceptions to the "no proactive
    notification" non-goal, confirmed with the user as scoped to this
    one moment (account creation), not a general notification feature.
    Rides along inside that same idempotency guard rather than a new
    mechanism of its own: a Temporal retry that finds the account
    already provisioned skips this call too, permanently, same accepted
    tradeoff already documented for the other three calls in that
    block."""
    body = (
        f"Welcome letter email for {applicant_identifier} "
        f"(account {account_id}, {product_type}, ${amount})"
    )
    _send_email(applicant_identifier, "Your new account is ready", body)
