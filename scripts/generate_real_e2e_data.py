#!/usr/bin/env python3
"""Generates real, review-ready test data by driving the actual running
stack over HTTP -- never stubs `document.service`, never talks to
Postgres/Temporal/Mayan directly except to print a summary at the end.
Every application, Temporal workflow execution, and Mayan document this
script creates comes from the same real code path a real customer/
underwriter/manager would exercise (see CLAUDE.md's "Testing" section --
`tests/integration/test_end_to_end_workflow.py` is the *stubbed* sibling
of this script, deliberately kept that way for its own phase's DoD; this
script is for generating real, inspectable data instead).

Deliberately creates data ONLY -- never deletes anything, before or
after. Run it as many times as you like; each run uses fresh randomized
applicant identities (a Unix-timestamp suffix), so runs never collide
with each other or with whatever is already in the database. Its
companion, `clear_e2e_data.py`, does the opposite -- clears everything
in Postgres/Temporal/Mayan in one shot. Together they're meant to be a
repeatable generate -> review -> clear -> generate cycle, not a one-off.

--------------------------------------------------------------- Setup ----
Reproducible from a clean checkout on any machine with Docker installed
-- nothing about this script is tied to any one dev environment. From
the repo root:

    1. docker compose up -d
       Wait for every service to report healthy/running, Keycloak
       especially -- its first boot imports the realm and can take
       10-90s depending on the machine (`docker compose ps keycloak`;
       poll `curl http://localhost:8080/realms/loanrealm/.well-known/openid-configuration`
       until it returns HTTP 200 rather than assuming a fixed wait).
       This script's own first request will fail fast with a clear
       "is the stack up?" message if it isn't ready -- rerun it once
       Keycloak answers.
    2. python3 -m venv .venv && .venv/bin/pip install -e .
       (creates the project's own venv with this script's one real
       dependency, httpx, already listed in pyproject.toml -- nothing
       to install separately for this script specifically)
    3. .venv/bin/python3 scripts/generate_real_e2e_data.py

No Keycloak URL needs configuring separately -- the underwriter/manager
login flow reads the real authorize URL straight out of the running
app's own `/ui/login` response, so it always matches whatever
`KEYCLOAK_ISSUER` that app instance is actually configured with.

Configuration (env vars, all optional -- defaults match this project's
own `docker-compose.yml` as published to the host):
    APP_BASE_URL           default: http://localhost:8001
    UNDERWRITER_USERNAME   default: underwriter1
    UNDERWRITER_PASSWORD   default: password
    MANAGER_USERNAME       default: manager1
    MANAGER_PASSWORD       default: password
(The demo Keycloak users/passwords come from
`keycloak/import/loanrealm-realm.json`, imported automatically on the
`keycloak` container's first boot -- no manual Keycloak setup needed
either.)

Known environment-specific gotcha, not this script's fault: on a
memory-constrained Docker host, Mayan's own Celery/search-index
contention can slow down (never break) approval-heavy runs -- this
script already retries and polls around that (see `request_retry`/
`poll_final_status` below). If Keycloak itself gets OOM-killed under
memory pressure, `docker compose up -d keycloak` (and possibly
`docker compose restart keycloak` if its container comes back with a
broken network attachment -- confirmed to happen at least once on a
6GB-RAM Docker VM) resolves it; a much smaller Docker VM allocation may
need bumping (e.g. Colima: `colima stop && colima start --memory 8`).

Produces, across 3 applicant identities ("customers"):
  - 10 applications total, spanning every decision path currently
    REACHABLE through the real system as of the 2026-09-08
    PENDING_MANAGER_APPROVAL threshold fix (mock_risk_engine's
    HIGH_THRESHOLD moved from $50,000 to $100,000, see CLAUDE.md's Known
    Gaps / the known-gaps-and-gotchas skill): LOW-tier risk-engine
    auto-approve, HIGH-tier risk-engine auto-reject, and -- for
    MEDIUM-tier amounts, the band that reaches a human -- underwriter
    approve, underwriter reject, customer cancel,
    request-more-info -> resubmit -> approve, and (new) an
    escalation -- underwriter approve on a MEDIUM-tier, also
    escalation-eligible ($50,000-$99,999.99) amount -> manager approve.
    (Capped at 10, not more -- PRD's one-active-account-per-product-type
    rule means a customer that's approved all three product types has no
    type left to submit a further application under; see the scenario
    lists' own comments below. A dedicated third customer carries the
    escalation scenario alone, rather than folding it into Customer A or
    B's own lists, specifically so it doesn't have to reason about
    either customer's existing per-product-type account state.)
  - 6 accounts (one per terminal APPROVED application, auto-, human-, or
    manager-decided), each with a real generated Welcome Letter and a
    real uploaded Consent document.
  - 1 application deliberately left at PENDING_UNDERWRITING, untouched
    -- for you to decide yourself in the back-office UI.
  - 3 account closure scenarios (new -- see CLAUDE.md's "Account
    closure" / the account-closure skill), run against three of the
    accounts above after every application above is created: a customer
    closure request approved by an Underwriter (account ends CLOSED --
    also confirms the product type reappears in the customer's own
    product picker afterward, the actual payoff the whole feature exists
    for), one rejected by a Manager (account reverts to ACTIVE), and one
    the customer cancels themselves before staff ever decides it
    (account reverts to ACTIVE). Covers both staff roles' closure-review
    permission and all three ways a CLOSURE_REQUESTED account resolves.
All documents are real, valid, single-page PDFs (a tiny dependency-free
PDF writer below) -- never the malformed placeholder bytes that showed
up broken in Mayan during manual testing.
"""

from __future__ import annotations

import html
import os
import re
import sys
import time
from dataclasses import dataclass

import httpx

BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8001")
UNDERWRITER_USER = os.environ.get("UNDERWRITER_USERNAME", "underwriter1")
UNDERWRITER_PASSWORD = os.environ.get("UNDERWRITER_PASSWORD", "password")
MANAGER_USER = os.environ.get("MANAGER_USERNAME", "manager1")
MANAGER_PASSWORD = os.environ.get("MANAGER_PASSWORD", "password")


# --------------------------------------------------------------- PDF writer ----
# A minimal, dependency-free, *valid* single-page PDF (correct xref table
# and stream /Length -- unlike an earlier ad hoc hand-rolled attempt this
# project hit, which uploaded fine but showed 0 extractable pages in
# Mayan). No external tool (cupsfilter, reportlab, ...) required, so this
# script is portable to any dev machine.
def make_pdf(title: str, lines: list[str]) -> bytes:
    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    text_ops = ["BT", "/F1 14 Tf", "14 TL", "72 740 Td"]
    for line in lines:
        text_ops.append(f"({esc(line)}) Tj")
        text_ops.append("T*")
    text_ops.append("ET")
    content = "\n".join(text_ops).encode("latin-1")

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
    )
    objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray()
    out += b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)


DOCS: dict[str, bytes] = {
    "Government ID": make_pdf(
        "Government ID",
        ["STATE DRIVER LICENSE (SAMPLE / TEST DOCUMENT)", "Synthetic document -- loan-onboarding-poc e2e data."],
    ),
    "Proof of Income": make_pdf(
        "Proof of Income",
        ["PAY STUB (SAMPLE / TEST DOCUMENT)", "Synthetic document -- loan-onboarding-poc e2e data."],
    ),
    "Bank Statements": make_pdf(
        "Bank Statements",
        ["BANK STATEMENT (SAMPLE / TEST DOCUMENT)", "Synthetic document -- loan-onboarding-poc e2e data."],
    ),
    "Credit Report": make_pdf(
        "Credit Report",
        ["CREDIT REPORT SUMMARY (SAMPLE / TEST DOCUMENT)", "Synthetic document -- loan-onboarding-poc e2e data."],
    ),
    "Vehicle Title/Invoice": make_pdf(
        "Vehicle Title/Invoice",
        ["VEHICLE TITLE / INVOICE (SAMPLE / TEST DOCUMENT)", "Synthetic document -- loan-onboarding-poc e2e data."],
    ),
    "Property Appraisal": make_pdf(
        "Property Appraisal",
        ["PROPERTY APPRAISAL (SAMPLE / TEST DOCUMENT)", "Synthetic document -- loan-onboarding-poc e2e data."],
    ),
    "Consent": make_pdf(
        "Consent",
        ["CONSENT TO ACCOUNT TERMS (SAMPLE / TEST DOCUMENT)", "Synthetic document -- loan-onboarding-poc e2e data."],
    ),
}

REQUIRED_CATEGORIES = {
    "personal_loan": ["Government ID", "Proof of Income", "Bank Statements", "Credit Report"],
    "auto_loan": ["Government ID", "Proof of Income", "Bank Statements", "Credit Report", "Vehicle Title/Invoice"],
    "mortgage": ["Government ID", "Proof of Income", "Bank Statements", "Credit Report", "Property Appraisal"],
}

PRODUCT_FIELDS = {
    "personal_loan": {"purpose": "Debt consolidation", "employment_status": "employed", "monthly_income": "5000"},
    "auto_loan": {"vehicle_make_model": "Toyota Camry 2024", "vin": "1HGCM82633A004352", "down_payment": "2000"},
    "mortgage": {"property_address": "123 Test St, Testville", "appraised_value": "250000", "down_payment": "20000"},
}


@dataclass
class AppResult:
    label: str
    application_id: str
    email: str
    product_type: str
    amount: str
    target_status: str
    account_id: str | None = None


@dataclass
class Scenario:
    label: str
    product_type: str
    amount: str
    # "risk_auto_approve" | "risk_auto_reject" (no human decision -- the
    # risk engine alone resolves it) | "approve" | "reject" | "cancel" |
    # "more_info_approve" | "leave_pending" (all five of these need a
    # MEDIUM-tier amount, $15,000-$99,999.99, to actually reach a human --
    # see mock_risk_engine/main.py's LOW_THRESHOLD/HIGH_THRESHOLD) |
    # "escalate_approve" | "escalate_reject" (need a MEDIUM-tier amount
    # that's ALSO escalation-eligible, i.e. $50,000-$99,999.99 -- see
    # workflows.py's MANAGER_ESCALATION_THRESHOLD_USD. "escalate_approve"
    # is used by CUSTOMER_C_SCENARIOS below; "escalate_reject" is kept,
    # still unused, as an easy addition for a future session)
    decision_path: str


# PRD's one-active-account-per-product-type rule (get_available_product_
# types' hard elimination) means each customer below may hold AT MOST
# ONE eventually-APPROVED application per product type -- there are only
# three product types, so a customer that approves all three has no
# product type left to submit a fourth application under, regardless of
# that next scenario's own intended outcome (confirmed live: an earlier
# draft of this list had "reject"/"leave_pending" scenarios scheduled
# *after* their own product type had already been approved for the same
# customer, and every one 400'd at /apply/new/start with "you already
# have an active <type> account"). A REJECTED/CANCELLED application
# never creates an account, so a product type used by one of those stays
# reusable later in the same customer's list -- that's what lets mortgage
# appear twice for Customer A (reject, then a real approve) and twice for
# Customer B (an auto-reject, then left pending) below.
CUSTOMER_A_SCENARIOS = [
    Scenario("A1", "personal_loan", "8000", "risk_auto_approve"),  # LOW tier
    Scenario("A2", "auto_loan", "20000", "approve"),
    Scenario("A3", "mortgage", "30000", "reject"),  # mortgage stays available -- REJECTED, not active
    Scenario("A4", "mortgage", "45000", "more_info_approve"),
]
CUSTOMER_B_SCENARIOS = [
    Scenario("B1", "personal_loan", "25000", "cancel"),  # personal_loan stays available -- CANCELLED, not active
    Scenario("B2", "mortgage", "150000", "risk_auto_reject"),  # HIGH tier (>= $100,000); mortgage stays available
    Scenario("B3", "auto_loan", "30000", "approve"),
    Scenario("B4", "personal_loan", "5000", "risk_auto_approve"),  # LOW tier
    Scenario("B5", "mortgage", "40000", "leave_pending"),
]
# A dedicated third customer for the escalation path (PENDING_MANAGER_
# APPROVAL) rather than folding it into Customer A or B's own lists --
# both of those already end each product type at ACTIVE or a deliberate
# non-terminal state, so a fresh identity keeps this scenario's own
# product-type bookkeeping trivial (one customer, one application,
# nothing else to reason about). $60,000 is inside the MEDIUM band
# ($15,000-$99,999.99) AND >= MANAGER_ESCALATION_THRESHOLD_USD
# ($50,000), the overlap band the 2026-09-08 threshold fix opened up.
CUSTOMER_C_SCENARIOS = [
    Scenario("C1", "personal_loan", "60000", "escalate_approve"),
]


@dataclass
class ClosureScenario:
    label: str
    source_label: str          # which Scenario above's resulting APPROVED account this acts on
    decision_path: str          # "close_approve" | "close_reject" | "close_cancel"
    role: str = "underwriter"    # staff role deciding -- "underwriter" | "manager"; ignored for close_cancel
    comment: str = "balance confirmed zero"  # the staff attestation text; ignored for close_cancel


# Runs after every application above is created (so each source
# application is already a real ACTIVE account) and sequentially, one
# closure at a time -- decide_closure() below finds the pending request
# by reading it back off the single-row /ui/{role}/closures queue, which
# only works cleanly with exactly one request in flight at once. Each
# entry below picks a distinct customer/product-type account, so none
# of these three closures ever race or interact with each other.
CLOSURE_SCENARIOS = [
    # A1 (personal_loan) closed for real -- also the one that verifies
    # the actual payoff (CLAUDE.md's "Account closure": once CLOSED,
    # the product type reappears in Customer A's own picker).
    ClosureScenario("CLOSE-A1", "A1", "close_approve", role="underwriter", comment="balance confirmed zero"),
    # A2 (auto_loan) -- a Manager rejects it (both closure-eligible
    # roles get exercised across these three scenarios, not just
    # Underwriter); account reverts to ACTIVE, stays eliminated from
    # the picker exactly as before.
    ClosureScenario(
        "CLOSE-A2", "A2", "close_reject", role="manager", comment="balance not yet settled -- staying open"
    ),
    # B3 (auto_loan, Customer B) -- the customer changes their mind
    # before any staff member ever sees it; account reverts to ACTIVE,
    # no staff role/comment involved at all.
    ClosureScenario("CLOSE-B3", "B3", "close_cancel"),
]


def log(msg: str) -> None:
    print(f"[e2e-data] {msg}", flush=True)


def extract(pattern: str, text: str, group: int = 1) -> str:
    m = re.search(pattern, text)
    if not m:
        raise RuntimeError(f"pattern not found: {pattern!r}")
    return m.group(group)


def expect(resp: httpx.Response, *codes: int) -> httpx.Response:
    """`Response.raise_for_status()` treats an un-followed 3xx as an
    error too (confirmed directly against httpx 0.28) -- but 303 is the
    *expected* success response for most of this app's POST-redirect-GET
    routes, since every client here runs with follow_redirects=False.
    Check against the actual expected code(s) instead."""
    if resp.status_code not in codes:
        raise RuntimeError(
            f"expected {codes} from {resp.request.method} {resp.request.url}, got {resp.status_code}:\n"
            f"{resp.text[:1000]}"
        )
    return resp


def request_retry(
    fn, *args, ok_codes: tuple[int, ...], retry_codes: tuple[int, ...] = (500, 502, 503, 504),
    max_attempts: int = 8, backoff: float = 1.0, **kwargs
) -> httpx.Response:
    """Mayan sits under real memory/Celery pressure on this project's
    dev box (its Whoosh search index hits `LockError` under concurrent
    task load -- confirmed live via `docker logs mayan`), which
    occasionally surfaces as a transient 500 or a dropped connection on
    whichever app route happens to be mid-request against Mayan at that
    moment. None of that reflects a real bug in the request itself --
    retry with backoff rather than treat it as fatal."""
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = fn(*args, **kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
            time.sleep(backoff * (attempt + 1))
            continue
        if resp.status_code in ok_codes:
            return resp
        if resp.status_code not in retry_codes:
            return resp  # a real failure -- let the caller's expect() report it properly
        last_exc = RuntimeError(f"HTTP {resp.status_code} from {resp.request.url}")
        time.sleep(backoff * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError("request_retry: exhausted attempts with no response")


STATUS_LABELS = {
    "PENDING_UNDERWRITING": "Under Review",
    "PENDING_MANAGER_APPROVAL": "Escalated for Manager Review",
    "MORE_INFO_REQUESTED": "More Info Requested",
    "APPROVED": "Approved",
    "REJECTED": "Rejected",
    "CANCELLED": "Cancelled",
}
REVERSE_STATUS_LABELS = {v: k for k, v in STATUS_LABELS.items()}
# `_build_timeline`'s own non-terminal PENDING_MANAGER_APPROVAL step uses
# a different, hardcoded string ("Escalated to Manager") than
# STATUS_LABELS' terminal-state mapping ("Escalated for Manager Review")
# -- confirmed live (a prior run's escalation step logged "UNKNOWN"
# because only the STATUS_LABELS string was mapped). Both map to the
# same internal status; add the alias rather than trust one string.
REVERSE_STATUS_LABELS["Escalated to Manager"] = "PENDING_MANAGER_APPROVAL"


def poll_final_status(client: httpx.Client, application_id: str, expected: set[str], timeout: float = 45.0) -> str:
    """Ground truth for "what did this application actually end up as,"
    read from the customer detail page's own timeline (whose "current"
    step -- the last one, rendered `font-semibold` -- is built from
    `STATUS_LABELS` for a terminal state, or a separate hardcoded string
    for each non-terminal one -- see `REVERSE_STATUS_LABELS`' alias
    just above). Deliberately does NOT trust the decision route's own
    immediately-returned badge:
    confirmed live that `application_service.wait_for_status_change`'s
    bounded ~5s wait can still return before a heavier APPROVE
    transition's full provisioning (customer/account/Mayan tagging/
    Welcome Letter) has actually committed, especially under the Mayan
    load described in `request_retry`'s docstring -- polling this page
    independently, after every step of a scenario is done, is what
    actually confirms the terminal state landed. Tolerates a transient
    500/disconnect from this GET itself (it renders documents.list_documents,
    which calls out to Mayan) the same way -- Mayan's own memory/Celery
    pressure can surface here too, not just on the mutating calls
    `request_retry` already covers; confirmed live during a real
    generate_real_e2e_data.py run that an un-retried 500 here crashes the
    whole script rather than just costing one poll iteration."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            resp = client.get(f"{BASE_URL}/apply/applications/{application_id}")
        except httpx.TransportError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
            continue
        if resp.status_code in (500, 502, 503, 504):
            if time.monotonic() >= deadline:
                expect(resp, 200)  # raise the real error, not a bare timeout
            time.sleep(0.5)
            continue
        expect(resp, 200)  # any other unexpected code is a real bug -- fail fast, don't retry through it
        # base.html's shared nav brand ("Loan Onboarding") also renders
        # with a bare `font-semibold` class -- confirmed live it's an
        # exact false-positive match for the same regex. Slice from the
        # page's own <h1> (start of {% block content %}) so only the
        # timeline's real "current" step span can match.
        body = resp.text.split("<h1", 1)[-1]
        m = re.findall(r'font-semibold">([^<]+)<', body)
        last_label = m[-1].strip() if m else None
        status = REVERSE_STATUS_LABELS.get(last_label)
        if status in expected:
            return status
        if time.monotonic() >= deadline:
            return status or "UNKNOWN"
        time.sleep(0.5)


def customer_identify_and_verify(client: httpx.Client, email: str) -> None:
    resp = expect(client.post(f"{BASE_URL}/apply/identify", data={"applicant_identifier": email}), 200)
    code = extract(r"<strong>(\d{6})</strong>", resp.text)
    expect(client.post(f"{BASE_URL}/apply/identify/verify", data={"code": code}), 303)


def submit_application(client: httpx.Client, label: str, email: str, product_type: str, amount: str) -> str:
    expect(client.post(f"{BASE_URL}/apply/new/start", data={"product_type": product_type}), 303)

    fields = {
        "applicant_name": f"E2E Data Applicant {label}",
        "applicant_email": email,
        "applicant_phone": "555-0100",
        "amount": amount,
        **PRODUCT_FIELDS[product_type],
    }
    expect(client.post(f"{BASE_URL}/apply/new/details", data=fields), 303)

    for category in REQUIRED_CATEGORIES[product_type]:
        request_retry(
            client.post,
            f"{BASE_URL}/apply/new/documents/upload",
            data={"category": category},
            files={
                "file": (
                    f"{category.lower().replace(' ', '_').replace('/', '_')}.pdf",
                    DOCS[category],
                    "application/pdf",
                )
            },
            ok_codes=(200,),
        )
        time.sleep(0.3)  # give Mayan's Celery pipeline (index rebuild, metadata attach) room to breathe

    resp = expect(request_retry(client.post, f"{BASE_URL}/apply/new/review", ok_codes=(303,)), 303)
    location = resp.headers.get("location", "")
    application_id = location.rsplit("/", 1)[-1]
    log(f"  [{label}] submitted {application_id} ({product_type}, ${amount})")
    return application_id


def upload_consent(client: httpx.Client, application_id: str) -> None:
    """The back-office decision route's own bounded wait
    (`application_service.wait_for_status_change`, ~5s) can still return
    before the terminal APPROVED write is visible to a *different*
    connection -- confirmed live: an immediate consent upload right
    after a decision() call occasionally 400s with "consent is only
    available for an approved application" even though the row is
    APPROVED a moment later. Retry briefly rather than assume the
    decision has fully caught up by the time its own response returns
    (on top of request_retry's own separate retry for transient Mayan
    5xx/disconnects)."""
    last_resp = None
    for attempt in range(10):
        resp = request_retry(
            client.post,
            f"{BASE_URL}/apply/applications/{application_id}/consent/upload",
            files={"file": ("consent.pdf", DOCS["Consent"], "application/pdf")},
            ok_codes=(303, 400),
        )
        if resp.status_code == 303:
            return
        last_resp = resp
        time.sleep(0.5)
    expect(last_resp, 303)


def cancel_application(client: httpx.Client, application_id: str) -> None:
    expect(client.post(f"{BASE_URL}/apply/applications/{application_id}/cancel"), 303)


def resubmit_application(client: httpx.Client, application_id: str, product_type: str) -> None:
    fields = dict(PRODUCT_FIELDS[product_type])
    # perturb one field so the resubmission is a genuine edit, not a no-op
    key = next(iter(fields))
    fields[key] = fields[key] + " (updated)"
    expect(client.post(f"{BASE_URL}/apply/applications/{application_id}/resubmit", data=fields), 303)


class BackofficeSession:
    """One authenticated Keycloak session for a given staff role, reused
    across every decision that role makes in this run -- logging in once
    per role rather than once per decision keeps this script's Keycloak
    load light (see CLAUDE.md's Known Gaps note on this project's own
    memory-constrained dev environment)."""

    def __init__(self, username: str, password: str):
        self.client = httpx.Client(follow_redirects=False, timeout=30.0)
        self._login(username, password)

    def _login(self, username: str, password: str) -> None:
        resp = self.client.get(f"{BASE_URL}/ui/login")
        resp.raise_for_status()
        auth_url = html.unescape(extract(r'href="([^"]*realms/loanrealm[^"]*)"', resp.text))

        kc_client = httpx.Client(follow_redirects=True, timeout=30.0)
        resp = kc_client.get(auth_url)
        resp.raise_for_status()
        form_action = html.unescape(extract(r'<form id="kc-form-login"[^>]*action="([^"]*)"', resp.text))

        # Keycloak marks AUTH_SESSION_ID/KC_RESTART `Secure` even over
        # plain http (this dev stack has no TLS in front of Keycloak) --
        # httpx's cookie jar correctly refuses to resend a `Secure`
        # cookie over http, so forward them explicitly instead of
        # relying on the jar (confirmed live: curl is lax about this and
        # sends them anyway, httpx is not).
        kc_cookies = dict(kc_client.cookies)
        resp = kc_client.post(
            form_action,
            data={"username": username, "password": password, "credentialId": ""},
            cookies=kc_cookies,
            follow_redirects=False,
        )
        if resp.status_code != 302:
            raise RuntimeError(f"Keycloak login failed for {username}: HTTP {resp.status_code}\n{resp.text[:500]}")
        callback_url = resp.headers["location"]

        resp = self.client.get(callback_url)
        if resp.status_code != 303:
            raise RuntimeError(f"backoffice callback failed for {username}: HTTP {resp.status_code}")
        log(f"  logged in to back office as {username!r}")

    def decision(self, role: str, application_id: str, decision: str, comment: str) -> None:
        """Fires the decision signal. Deliberately returns nothing --
        the response's own badge can be stale (see `poll_final_status`'s
        docstring); callers must confirm the real outcome themselves."""
        expect(
            request_retry(
                self.client.post,
                f"{BASE_URL}/ui/{role}/{application_id}/decision",
                data={"decision": decision, "comment": comment},
                ok_codes=(200,),
            ),
            200,
        )

    def decide_closure(self, role: str, decision: str, comment: str) -> str:
        """Account closure (CLAUDE.md's "Account closure" / the
        account-closure skill) is a plain POST-redirect-GET (303), not
        an htmx fragment swap like `decision()` above -- and its route
        is keyed by `account_id`, which never appears anywhere on the
        customer-facing page this script otherwise scrapes ids from. The
        only place to learn it is this same `/ui/{role}/closures` queue
        page the decision itself posts to, so read it back from there.
        Asserts exactly one pending row: `CLOSURE_SCENARIOS` runs one
        closure at a time, request-then-decide, before starting the
        next, so more or fewer than one pending request here is a real
        bug (a leftover from a previous run, or two closures racing),
        not something to silently pick around."""
        resp = expect(self.client.get(f"{BASE_URL}/ui/{role}/closures"), 200)
        matches = re.findall(rf"/ui/{role}/closures/([A-Za-z0-9\-]+)/decision", resp.text)
        if len(matches) != 1:
            raise RuntimeError(
                f"expected exactly one pending closure request in the {role!r} queue, "
                f"found {len(matches)}: {matches}"
            )
        account_id = matches[0]
        expect(
            request_retry(
                self.client.post,
                f"{BASE_URL}/ui/{role}/closures/{account_id}/decision",
                data={"decision": decision, "comment": comment},
                ok_codes=(303,),
            ),
            303,
        )
        return account_id


def wait_past_risk_assessment(client: httpx.Client, application_id: str, label: str) -> None:
    """Phase 21: every application starts at PENDING_RISK_ASSESSMENT, not
    PENDING_UNDERWRITING -- a decision signal fired before risk
    assessment resolves arrives while the workflow is in a status
    submit_decision doesn't accept for any actor_role, and is silently
    rejected (raises inside the signal handler, no state change, no
    error surfaced to this script's own HTTP call, since signaling is
    fire-and-forget). Confirmed live: firing 'approve' immediately after
    submit_application() left the application stuck, and the following
    upload_consent() call then 400'd forever. Every scenario below that
    needs a human decision (or a customer cancel) must wait for this
    first. A MEDIUM-tier amount (this script's own convention for any
    such scenario) always resolves to PENDING_UNDERWRITING -- APPROVED/
    REJECTED here would mean a scenario's amount drifted out of the
    MEDIUM band by mistake, so treat either as a hard error rather than
    silently continuing into a decision call that can no longer apply."""
    status = poll_final_status(
        client, application_id, {"PENDING_UNDERWRITING", "APPROVED", "REJECTED"}, timeout=60.0
    )
    if status != "PENDING_UNDERWRITING":
        raise RuntimeError(
            f"[{label}] {application_id}: expected risk assessment to resolve to PENDING_UNDERWRITING "
            f"(a MEDIUM-tier amount), but landed on {status!r} instead -- check this scenario's amount "
            f"is inside mock_risk_engine/main.py's MEDIUM band ($15,000-$99,999.99)"
        )


def poll_account_status(client: httpx.Client, application_id: str, expected: set[str], timeout: float = 45.0) -> str:
    """Account closure (CLAUDE.md's "Account closure" / the
    account-closure skill) changes `accounts.status`, not
    `applications.status` -- `poll_final_status` above can't see it at
    all. Reads it instead off the same customer application-detail
    page's own closure-section markup: the "Request account closure"
    form (action `.../closure/request`) only renders when ACTIVE, the
    "Cancel closure request" form (action `.../closure/cancel`) only
    when CLOSURE_REQUESTED, and the whole section is hidden once CLOSED
    -- see `application_detail.html`'s own comment on this. Every caller
    here only ever polls this for an application it already knows is
    APPROVED with a real account, so "neither marker present" is read as
    CLOSED, never mistaken for "no account exists yet." Same transient
    500/disconnect tolerance as `poll_final_status`, for the same reason
    (this page also renders `document.service` calls that reach Mayan,
    for the Consent section)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            resp = client.get(f"{BASE_URL}/apply/applications/{application_id}")
        except httpx.TransportError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
            continue
        if resp.status_code in (500, 502, 503, 504):
            if time.monotonic() >= deadline:
                expect(resp, 200)
            time.sleep(0.5)
            continue
        expect(resp, 200)
        if "/closure/request\"" in resp.text:
            status = "ACTIVE"
        elif "/closure/cancel\"" in resp.text:
            status = "CLOSURE_REQUESTED"
        else:
            status = "CLOSED"
        if status in expected:
            return status
        if time.monotonic() >= deadline:
            return status
        time.sleep(0.5)


def request_account_closure(client: httpx.Client, application_id: str) -> None:
    expect(client.post(f"{BASE_URL}/apply/applications/{application_id}/closure/request"), 303)


def cancel_account_closure(client: httpx.Client, application_id: str) -> None:
    expect(client.post(f"{BASE_URL}/apply/applications/{application_id}/closure/cancel"), 303)


def run_closure_scenario(
    scenario: ClosureScenario, source: AppResult, underwriter: BackofficeSession, manager: BackofficeSession
) -> None:
    """Acts on `source`'s already-provisioned, ACTIVE account -- not a
    new application, so there's no `document.service.upload(...)`/risk-
    assessment wait here at all, just the closure request/decision
    round trip. Re-identifies as `source.email` on a fresh client rather
    than reusing whatever client `run_scenario` used for that
    application -- `run_scenario` doesn't return its client, and
    re-running the (fast, no-OTP-provider) identify/verify flow is
    cheaper than threading one through."""
    client = httpx.Client(follow_redirects=False, timeout=30.0)
    customer_identify_and_verify(client, source.email)
    application_id = source.application_id

    request_account_closure(client, application_id)
    status = poll_account_status(client, application_id, {"CLOSURE_REQUESTED"})
    log(f"  [{scenario.label}] {application_id} closure requested -> {status}")

    if scenario.decision_path == "close_approve":
        staff = underwriter if scenario.role == "underwriter" else manager
        account_id = staff.decide_closure(scenario.role, "APPROVE", scenario.comment)
        status = poll_account_status(client, application_id, {"CLOSED"})
        log(f"  [{scenario.label}] {account_id} -> {status} (approved by {scenario.role})")

        # The actual payoff (CLAUDE.md's "Account closure"): once CLOSED,
        # has_active_account_of_type stops counting this account, so the
        # product picker should offer this product type again.
        resp = expect(client.get(f"{BASE_URL}/apply/new"), 200)
        marker = f'value="{source.product_type}"'
        if marker not in resp.text:
            raise RuntimeError(
                f"[{scenario.label}] expected {source.product_type!r} to reappear in the product picker "
                f"after closure, but it didn't -- has_active_account_of_type may not be excluding CLOSED"
            )
        log(f"  [{scenario.label}] confirmed {source.product_type!r} reappears in the product picker")

    elif scenario.decision_path == "close_reject":
        staff = underwriter if scenario.role == "underwriter" else manager
        account_id = staff.decide_closure(scenario.role, "REJECT", scenario.comment)
        status = poll_account_status(client, application_id, {"ACTIVE"})
        log(f"  [{scenario.label}] {account_id} -> {status} (rejected by {scenario.role}, reverted to ACTIVE)")

    elif scenario.decision_path == "close_cancel":
        cancel_account_closure(client, application_id)
        status = poll_account_status(client, application_id, {"ACTIVE"})
        log(f"  [{scenario.label}] {application_id} -> {status} (customer cancelled, reverted to ACTIVE)")

    else:
        raise RuntimeError(f"unknown closure decision_path {scenario.decision_path!r}")


def run_scenario(
    scenario: Scenario, email: str, underwriter: BackofficeSession, manager: BackofficeSession
) -> AppResult:
    client = httpx.Client(follow_redirects=False, timeout=30.0)
    customer_identify_and_verify(client, email)
    application_id = submit_application(client, scenario.label, email, scenario.product_type, scenario.amount)

    if scenario.decision_path not in ("risk_auto_approve", "risk_auto_reject"):
        wait_past_risk_assessment(client, application_id, scenario.label)

    if scenario.decision_path == "risk_auto_approve":
        # No decision() call at all -- the risk engine alone resolves
        # this within seconds of submission (mock_risk_engine's
        # simulated delay is ~1s), via signal_risk_decision, the same
        # persist_decision code path a human APPROVE uses (underwriter_
        # name="risk-engine-auto"). Poll timeout is generous for the
        # real NATS round trip (submit -> risk-adapter -> mock engine ->
        # risk-adapter -> Temporal signal), not just Mayan/DB lag.
        status = poll_final_status(client, application_id, {"APPROVED"}, timeout=60.0)
        upload_consent(client, application_id)
        log(f"  [{scenario.label}] {application_id} -> {status} (risk-engine auto-approve, consent uploaded)")

    elif scenario.decision_path == "risk_auto_reject":
        status = poll_final_status(client, application_id, {"REJECTED"}, timeout=60.0)
        log(f"  [{scenario.label}] {application_id} -> {status} (risk-engine auto-reject)")

    elif scenario.decision_path == "approve":
        underwriter.decision("underwriter", application_id, "APPROVE", f"{scenario.label}: approved")
        status = poll_final_status(client, application_id, {"APPROVED"})
        upload_consent(client, application_id)
        log(f"  [{scenario.label}] {application_id} -> {status} (consent uploaded)")

    elif scenario.decision_path == "reject":
        underwriter.decision("underwriter", application_id, "REJECT", f"{scenario.label}: rejected")
        status = poll_final_status(client, application_id, {"REJECTED"})
        log(f"  [{scenario.label}] {application_id} -> {status}")

    elif scenario.decision_path == "cancel":
        cancel_application(client, application_id)
        status = poll_final_status(client, application_id, {"CANCELLED"})
        log(f"  [{scenario.label}] {application_id} -> {status}")

    elif scenario.decision_path == "escalate_approve":
        underwriter.decision("underwriter", application_id, "APPROVE", f"{scenario.label}: escalating")
        escalated = poll_final_status(client, application_id, {"PENDING_MANAGER_APPROVAL"})
        log(f"  [{scenario.label}] {application_id} -> {escalated} (escalated)")
        manager.decision("manager", application_id, "APPROVE", f"{scenario.label}: manager approved")
        status = poll_final_status(client, application_id, {"APPROVED"})
        upload_consent(client, application_id)
        log(f"  [{scenario.label}] {application_id} -> {status} (consent uploaded)")

    elif scenario.decision_path == "escalate_reject":
        underwriter.decision("underwriter", application_id, "APPROVE", f"{scenario.label}: escalating")
        escalated = poll_final_status(client, application_id, {"PENDING_MANAGER_APPROVAL"})
        log(f"  [{scenario.label}] {application_id} -> {escalated} (escalated)")
        manager.decision("manager", application_id, "REJECT", f"{scenario.label}: manager rejected")
        status = poll_final_status(client, application_id, {"REJECTED"})
        log(f"  [{scenario.label}] {application_id} -> {status}")

    elif scenario.decision_path == "more_info_approve":
        underwriter.decision("underwriter", application_id, "REQUEST_MORE_INFO", f"{scenario.label}: need more info")
        more_info = poll_final_status(client, application_id, {"MORE_INFO_REQUESTED"})
        log(f"  [{scenario.label}] {application_id} -> {more_info}")
        resubmit_application(client, application_id, scenario.product_type)
        underwriter.decision("underwriter", application_id, "APPROVE", f"{scenario.label}: approved after resubmit")
        status = poll_final_status(client, application_id, {"APPROVED"})
        upload_consent(client, application_id)
        log(f"  [{scenario.label}] {application_id} -> {status} (resubmitted, consent uploaded)")

    elif scenario.decision_path == "leave_pending":
        status = "PENDING_UNDERWRITING"
        log(f"  [{scenario.label}] {application_id} left PENDING_UNDERWRITING for manual review")

    else:
        raise RuntimeError(f"unknown decision_path {scenario.decision_path!r}")

    return AppResult(scenario.label, application_id, email, scenario.product_type, scenario.amount, status)


def main() -> None:
    log(f"target app: {BASE_URL}")
    try:
        resp = httpx.get(f"{BASE_URL}/apply/identify", timeout=10.0)
        resp.raise_for_status()
    except httpx.TransportError as exc:
        raise RuntimeError(
            f"could not reach {BASE_URL} ({exc}). Is the stack up? Try `docker compose up -d` "
            f"and `docker compose ps` from the repo root, or set APP_BASE_URL if it's not on port 8001."
        ) from exc
    log("app reachable")

    log("logging in back-office sessions (reused across all decisions)...")
    underwriter = BackofficeSession(UNDERWRITER_USER, UNDERWRITER_PASSWORD)
    manager = BackofficeSession(MANAGER_USER, MANAGER_PASSWORD)

    run_id = str(int(time.time()))
    email_a = f"e2e-data-a-{run_id}@example.com"
    email_b = f"e2e-data-b-{run_id}@example.com"
    email_c = f"e2e-data-c-{run_id}@example.com"

    results: list[AppResult] = []
    log(f"=== Customer A ({email_a}) ===")
    for scenario in CUSTOMER_A_SCENARIOS:
        results.append(run_scenario(scenario, email_a, underwriter, manager))
    log(f"=== Customer B ({email_b}) ===")
    for scenario in CUSTOMER_B_SCENARIOS:
        results.append(run_scenario(scenario, email_b, underwriter, manager))
    log(f"=== Customer C ({email_c}) ===")
    for scenario in CUSTOMER_C_SCENARIOS:
        results.append(run_scenario(scenario, email_c, underwriter, manager))

    log("=== Account closure scenarios ===")
    results_by_label = {r.label: r for r in results}
    for closure_scenario in CLOSURE_SCENARIOS:
        run_closure_scenario(closure_scenario, results_by_label[closure_scenario.source_label], underwriter, manager)

    approved = [r for r in results if r.target_status == "APPROVED"]
    pending = [r for r in results if r.target_status == "PENDING_UNDERWRITING"]

    print("\n" + "=" * 72)
    print(f"Done. {len(results)} applications created across 3 customers ({email_a}, {email_b}, {email_c}).")
    print(f"  {len(approved)} approved (each with an account + Welcome Letter + Consent in Mayan)")
    print(f"  {len(pending)} left PENDING_UNDERWRITING for your manual review")
    print("Nothing was deleted -- this data (and its Temporal workflow executions and")
    print("Mayan documents) stays until you clear it yourself.")
    print("Includes one PENDING_MANAGER_APPROVAL escalation scenario (Customer C) -- this")
    print("path was unreachable prior to the 2026-09-08 threshold fix (see CLAUDE.md's")
    print("Known Gaps / the known-gaps-and-gotchas skill).")
    print(f"Also ran {len(CLOSURE_SCENARIOS)} account closure scenarios on top of accounts above:")
    print("  CLOSE-A1 -> CLOSED (Underwriter-approved; product type reappeared in the picker)")
    print("  CLOSE-A2 -> ACTIVE (Manager-rejected)")
    print("  CLOSE-B3 -> ACTIVE (customer cancelled their own request)")
    print("=" * 72)
    for r in results:
        print(f"  [{r.label}] {r.application_id}  {r.product_type:14s}  ${r.amount:>8s}  -> {r.target_status}")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(f"\nHTTP error: {exc.response.status_code} {exc.request.url}\n{exc.response.text[:1000]}", file=sys.stderr)
        sys.exit(1)
    except httpx.TransportError as exc:
        print(
            f"\nConnection error: {exc}\n"
            "Is the full stack up (`docker compose up -d`)? Keycloak and Mayan take longest to "
            "become ready -- see this script's own module docstring for what to poll for.",
            file=sys.stderr,
        )
        sys.exit(1)
    except RuntimeError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
