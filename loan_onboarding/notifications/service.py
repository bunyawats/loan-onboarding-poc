"""Fake/dev-only notification delivery -- a shared leaf module (Phase
18, "Account closure" -- see `CLAUDE.md`), same "zero dependency on
anything else in this codebase" shape as `idgen/`.

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

**Deliberately fake for this POC.** This project has no real email/SMS
provider configured anywhere -- no SMTP host, no Twilio/SendGrid/SES
credentials, nothing in `.env.example`. Every function below logs to
stdout instead of actually sending anything; `CLAUDE.md`'s Known Gaps
says so explicitly, and this is the one module that would need to
change (same signatures, real bodies) if a real provider is ever wired
up."""

from __future__ import annotations


def send_verification_code(applicant_identifier: str, code: str) -> None:
    # print(), not the logging module -- this codebase has no logging
    # configuration anywhere (no basicConfig, no handler), so a plain
    # logger.info() call here would inherit the root logger's default
    # WARNING level and be silently dropped, never reaching
    # `docker compose logs`/the console despite looking like it should.
    # Confirmed live: it didn't show up until switched to this.
    print(f"Verification code for {applicant_identifier}: {code} (POC: no real email/SMS provider configured -- see this module's docstring)")


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
    print(
        f"Account closure {decision} for {applicant_identifier} "
        f"(account {account_id}, {product_type}): {comment} "
        "(POC: no real email/SMS provider configured -- see this module's docstring)"
    )


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
    print(
        f"Welcome letter email for {applicant_identifier} "
        f"(account {account_id}, {product_type}, ${amount}) "
        "(POC: no real email/SMS provider configured -- see this module's docstring)"
    )
