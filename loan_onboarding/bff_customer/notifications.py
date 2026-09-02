"""Delivery of the customer identify flow's one-time email verification
code (`identity.py`'s docstring has the full "why" for the flow this
supports).

**Deliberately fake for this POC.** This project has no real email/SMS
provider configured anywhere -- no SMTP host, no Twilio/SendGrid/SES
credentials, nothing in `.env.example` -- and standing one up wasn't
this fix's scope (confirmed with the user: verify by email code, but
with fake/console delivery rather than wiring up a real provider).
This function logs the code server-side instead of emailing it;
`bff_customer/routes.py`'s verify-code page also surfaces the code
directly in its own response for the same reason -- there is no other
way for a tester (or this POC's own live-verification sweeps) to learn
the code, since no real inbox will ever receive it. Both of those are
consequences of *not* having real delivery, not attempts to hide that
fact -- `CLAUDE.md`'s Known Gaps says so explicitly, and this is the
one function that would need to change (same signature, real body) if
a real provider is ever wired up."""

from __future__ import annotations


def send_verification_code(applicant_identifier: str, code: str) -> None:
    # print(), not the logging module -- this codebase has no logging
    # configuration anywhere (no basicConfig, no handler), so a plain
    # logger.info() call here would inherit the root logger's default
    # WARNING level and be silently dropped, never reaching
    # `docker compose logs`/the console despite looking like it should.
    # Confirmed live: it didn't show up until switched to this.
    print(f"Verification code for {applicant_identifier}: {code} (POC: no real email/SMS provider configured -- see this module's docstring)")
