---
name: gmail-smtp-delivery
description: The Phase 20 design for real email delivery in loan-onboarding-poc via Gmail SMTP (stdlib smtplib + App Password), scoped to send_account_closure_decision/send_welcome_letter_email only (send_verification_code stays fake), opt-in via SMTP_* env vars, and the two real gotchas found live (Docker stdout buffering hiding print() output, PYTHONUNBUFFERED=1). Triggers on "SMTP", "Gmail App Password", "send_email", "notifications/service.py", "smtplib", "PYTHONUNBUFFERED", "real email delivery", "SMTP_USERNAME", "SMTP_PASSWORD", "welcome letter email", "closure decision email".
---

### Real email delivery via Gmail SMTP (built and live-verified — Phase 20)

Built and live-verified against the real stack (P20-1 through P20-3),
using the user's own Gmail account — full build history, the two real
gaps found and fixed along the way (Docker stdout buffering hiding
every `print()`-based delivery confirmation; a browser-automation-only
`confirm()`-dialog hang), and the live-verification sweep (all 3
real-send outcomes confirmed delivered: Welcome Letter,
closure-decision Approve, closure-decision Reject) all live in
`IMPLEMENTATION_PLAN.md`'s Phase 20 section and Session Log, not here.
Raised directly by the user: `notifications/service.py`'s own docstring
had said, since Phase 18, that fake `print()` delivery is "the one
thing that would need to change (same signatures, real bodies) if a
real provider is ever wired up" — this phase is that. The design below
wires it in as an **optional** real delivery path, confirmed with the
user as SMTP + a Gmail App Password (stdlib `smtplib`, zero new
dependencies), not the Gmail API/OAuth2 (heavier setup — a Google Cloud
project, an OAuth consent screen, token storage/refresh — out of
proportion to what this POC needs).

- **Scoped to exactly the two functions the user named — `send_account_closure_decision`
  and `send_welcome_letter_email` — not `send_verification_code`.**
  The OTP code stays fake-only, still shown directly in the verify-code
  page's own response (Phase 11's accepted, deliberate design — see
  "Identity" below): real delivery there isn't what was asked for, and
  changing it would remove the one way a tester without real inbox
  access can currently complete the identify flow at all.
- **Opt-in via env vars, never a required dependency.** Real sending
  fires only when both `SMTP_USERNAME` and `SMTP_PASSWORD` are set;
  otherwise both functions fall through to the exact same fake
  `print()` behavior this codebase already has, unchanged. This is
  load-bearing, not a nicety: the existing unit tests for both
  functions (`tests/unit/notifications/test_service.py`) assert on
  `capsys`-captured `print()` output and must keep passing with zero
  SMTP configuration in CI — real sending only ever gets exercised by a
  new, `smtplib`-mocked test plus this phase's own live-verification
  step, never by CI itself. New env vars (`.env.example`, and
  `docker-compose.yml`'s `worker-activity` service only — the one
  process that actually calls `persist_decision`/`persist_closure_decision`,
  same reasoning `MAYAN_*`'s placement there already follows;
  `worker-workflow` does no I/O and doesn't need them): `SMTP_HOST`
  (default `smtp.gmail.com`), `SMTP_PORT` (default `587`),
  `SMTP_USERNAME`, `SMTP_PASSWORD` (a Gmail **App Password**, not the
  account's real password — Google requires this for third-party SMTP
  auth once 2-Step Verification is on, which an App Password itself
  requires), `SMTP_FROM_ADDRESS` (defaults to `SMTP_USERNAME` if
  unset — Gmail's own SMTP relay requires the `From:` header to match
  the authenticated account, or a configured "Send As" alias, which
  this POC doesn't set up).
- **Recipient is `applicant_identifier` itself, no new parameter
  needed.** `bff_customer`'s identify flow has only ever accepted an
  email address since Phase 11's OTP fix (see "Identity" below) — the
  same value already threaded through both functions' existing
  signatures *is* the address to send to. This is what makes "simulate
  sending" work exactly as the user described: testing this POC by
  typing your own Gmail address as the applicant identifier sends the
  Welcome Letter/closure-decision email to that same inbox.
- **A real SMTP send failure must never fail the Temporal activity —
  caught and logged, not raised.** Both call sites
  (`account/activities.py`'s `persist_closure_decision`,
  `application/activities.py`'s `persist_decision`) sit inside
  idempotency-guarded provisioning blocks whose other three calls
  already accept "a retry skips this permanently, once the account/
  decision is already committed" as a smaller, more recoverable gap
  than letting a transient failure retry the whole activity (see
  "Applying without being a customer yet" and P19-3's own live
  confirmation of exactly this mechanism). A flaky Gmail connection is
  exactly the kind of transient failure that tradeoff already exists
  for — letting it propagate would risk turning a successful
  approval/closure-decision into the same class of stuck, `Failed`
  Temporal workflow this file's Known Gaps section already documents
  happening for a wholly unrelated reason (Postgres connection
  exhaustion) in an earlier session. `document.service`'s own calls in
  these same blocks are the deliberate counter-example, not a
  precedent to match: Mayan is this POC's actual document-of-record
  system, so a failure there *should* retry; a notification email
  failing to send is explicitly a best-effort, POC-fake concern by
  PRD §4's own framing, not critical infrastructure.
- **Implementation**: a new private `_send_email(to_address, subject,
  body)` helper inside `notifications/service.py` — checks the four
  env vars, sends via `smtplib.SMTP(host, port)` +
  `starttls()`/`login()`/`send_message()` (an `email.message.EmailMessage`,
  plain text) wrapped in a bare `try`/`except Exception`, or falls
  through to the module's existing `print(...)` path when unconfigured.
  `send_account_closure_decision`/`send_welcome_letter_email` call it
  instead of `print(...)` directly; `send_verification_code` is
  untouched. Zero new `pyproject.toml` dependencies — `smtplib`/`email`
  are Python stdlib.
- **Credentials never committed, never handled by the assistant on the
  user's behalf.** `.env.example` gets the two non-secret defaults
  (`SMTP_HOST`/`SMTP_PORT`) plus empty placeholders for
  `SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_FROM_ADDRESS` — the real App
  Password goes only into the user's own local, gitignored `.env`,
  added by the user directly (not pasted into a chat for an assistant
  to write down), same discipline this project already applies to
  every other real secret it has (`KEYCLOAK_CLIENT_SECRET`,
  `MAYAN_SERVICE_ACCOUNT_PASSWORD`, etc. all ship placeholder-only
  defaults in `.env.example`).

