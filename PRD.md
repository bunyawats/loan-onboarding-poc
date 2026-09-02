# PRD — Loan Onboarding POC

## 1. What this is

A proof-of-concept loan onboarding system: a **customer applies for a
loan themself**, from a mobile-first web app, uploading their own
supporting documents. An **Underwriter** reviews it and decides; for
larger loans, a **Manager** gives final sign-off. Documents live in
Mayan EDMS; the approval workflow runs on Temporal; PostgreSQL is the
queryable audit record; the UI is server-rendered FastAPI + HTMX.

This POC is deliberately built by combining the architecture of two
existing internal reference projects rather than designing from scratch:

- **[`review-approval-temporal`](https://github.com/bunyawats/review-approval-temporal)**
  — the multi-role Temporal + FastAPI BFF + Postgres approval-workflow
  shape (workflow/api/bff package split, `service.py` as the single
  business-logic choke point, payload-agnostic workflow with a
  per-type schema registry, bulk actions, paginated lists, the
  "requester sees only their own" visibility invariant — reused here for
  the customer's own application list), **and its Keycloak integration**
  (Authorization Code flow, Resources/Scopes/Policies/Permissions, UMA
  ticket exchange, Redis-backed session store) — reused here for
  back-office staff login (§7).
- **[`mayan-edms-customer-archive`](https://github.com/bunyawats/mayan-edms-customer-archive)**
  — the Mayan EDMS integration shape (metadata-driven document
  hierarchy via Index Templates, a thin async `MayanClient` with a
  shared service-account token, upload/search/delete through the FastAPI
  layer instead of the Mayan admin UI).

`CLAUDE.md` in this repo maps each architectural decision below to
specifically which reference project it's borrowed from, and what
changed to fit the loan domain.

### 1.1 Architecture at a glance

Like both reference projects, this is **one deployable** (one codebase,
one Docker image) — but organized as a **modular monolith**: seven
strictly bounded modules, each with its own data and a single
`service.py` entry point other modules call in-process, rather than
either an undifferentiated app or seven separately-deployed services:

| Module | Responsibility |
|---|---|
| Customer BFF | mobile-first self-service UI (public) |
| Back-Office BFF ("LOS") | Underwriter/Manager UI (Keycloak-gated) |
| Customer | customer profile |
| Account | the banking relationship an *approved* application produces (§9.2) |
| Application | the application entity, its state, and the submission document-completeness rule |
| Document | the Mayan EDMS integration |
| Workflow | the Temporal integration |

An earlier draft of this project built these seven as genuine
microservices (separate processes, network calls, one Postgres database
per module); that was more operational weight than this POC needs for
what a modular monolith already gets — see `CLAUDE.md` for the specific
trade-off and, more importantly, for the one real design problem
switching back had to solve (Application needs to call Workflow to
start a workflow, but Workflow's activities need to write back
Application data — resolved without a circular import). This document
stays focused on product behavior, not module topology, except where
the split changes what's user-visible (mainly §9's data model).

## 2. Problem statement

Loan onboarding today (in this POC's imagined starting point) requires a
customer to visit a branch or call in, and staff re-key everything by
hand — slow for the customer, and there's no single queryable record of
what's pending, what's been decided, or what documents exist for which
application. This POC demonstrates a structured alternative: customers
apply themselves from their phone, the approval flow runs as a durable
Temporal workflow, and each internal role gets a queue and actions
scoped to exactly what they're responsible for.

## 3. Goals

- Let a customer complete an entire loan application — details, document
  upload, submission, status tracking, responding to a request for more
  info — from a phone browser, no branch visit or staff involvement
  required for the happy path.
- Model a realistic 2–3 stage approval flow (customer submission →
  Underwriter → optional Manager escalation) as a durable Temporal
  workflow, so in-flight applications survive process restarts and are
  individually inspectable/operable via the Temporal Web UI.
- Give Underwriter and Manager staff a dedicated HTMX screen each,
  showing only the applications relevant to their queue, with
  single-item and bulk decision actions.
- Store loan documents in Mayan EDMS under a metadata-driven hierarchy
  scoped to applicant + application, reachable from the app's own UI on
  both the customer and staff sides (no separate login to the Mayan
  admin UI required for normal use).
- Keep Postgres as the queryable/audit record (list, filter, "what
  happened and when"), with Temporal as the source of truth for live
  workflow state — the same split both reference projects use.
- Prove out bulk decision actions (approve/reject N applications at
  once) and paginated, auto-refreshing list screens on the staff side,
  reusing the patterns already validated in `review-approval-temporal`
  (see the `list-pagination-bulk-actions` skill).

## 4. Non-goals (out of scope for this POC)

- **Real customer authentication.** No password, no OTP/magic-link
  verification. See §7 — this remains the biggest deliberate risk this
  POC accepts, and it's called out again there. (The back-office side,
  by contrast, now has real Keycloak authentication — see §7 and §5.)
- **A native mobile app.** "Mobile application" here means a
  mobile-first, responsive HTMX web app usable in a phone browser — not
  an iOS/Android binary. No separate JSON API module (`api/`) is built
  for this POC — the seven modules in `CLAUDE.md` are it. A native app
  later would be a new BFF-style caller of the same domain modules'
  `service.py` functions, the same way `bff_customer`/`bff_backoffice`
  are today — not a rework of business logic, but also not something
  currently scaffolded.
- **Staff-assisted / phone-in applications.** The only origination path
  in this POC is customer self-service. An internal "create on behalf
  of a customer" path is a plausible v2 addition, not built here.
- **Credit bureau / KYC integrations.** Any credit-check or
  identity-verification step is represented as a manual document upload
  (e.g. "Credit Report" as a PDF), not a live API integration.
- **Disbursement / servicing.** The workflow ends at `APPROVED` /
  `REJECTED` / `CANCELLED`. Funding, repayment schedules, and servicing
  are not modeled.
- **Push/SMS/email notifications.** Status changes are visible only when
  the customer opens the app; no proactive notification.
- **Multi-tenancy.** Single organization, single Mayan instance, single
  Postgres/Temporal namespace.
- **Production-grade security hardening.** This is a local
  Docker-Compose POC; see `CLAUDE.md`'s "Known gaps / hardening before
  production" section for what's deliberately deferred.

## 5. Roles

The customer side has no real authentication; the back-office side now
does, via Keycloak (see §7 for both).

| Role | Surface | Authentication | Can do |
|---|---|---|---|
| **Customer** | Mobile-first web app (public-facing) | None — self-entered email/phone, unverified | Identify themself, apply for a loan (product type + details), upload required documents from their phone, submit for review, track status of their own application(s), respond to "more info requested" by editing/uploading and resubmitting, cancel their own application while it's not yet terminal. |
| **Underwriter** | Back-office web app | Real Keycloak login | See all applications pending underwriter review, view the applicant's uploaded documents, decide **Approve** / **Reject** / **Request More Info**, act individually or in bulk. |
| **Manager** | Back-office web app | Real Keycloak login | See only applications escalated to them (loan amount at/above the escalation threshold, already Underwriter-approved), decide **Approve** / **Reject**, act individually or in bulk. |

The back-office app is a separate path from the customer-facing app (not
linked from it), gated by Keycloak login — unlike the earlier
role-switcher approach, a session on the back-office app genuinely
proves who's acting, not just which screen they clicked.

## 6. The approval workflow

### 6.1 Loan products

Each application has a **product type**, which determines its payload
shape (fields captured) — directly modeled on `review-approval-temporal`'s
`review_type` + per-type Pydantic schema registry:

| Product type | Product-specific fields |
|---|---|
| `personal_loan` | purpose, employment status, monthly income |
| `auto_loan` | vehicle make/model, VIN, down payment |
| `mortgage` | property address, appraised value, down payment |

Common fields on every application regardless of product: applicant
name, applicant email, applicant phone, requested amount.

### 6.2 States

```
PENDING_UNDERWRITING ──Approve (amount < threshold)──────────► APPROVED
       │  │                                                        ▲
       │  └──Approve (amount ≥ threshold)──► PENDING_MANAGER_APPROVAL
       │                                            │      │
       │                                       Approve   Reject
       │                                            │      │
       │                                            ▼      ▼
       │                                        APPROVED  REJECTED
       │
       ├──Reject────────────────────────────────────────► REJECTED
       │
       ├──Request More Info──► MORE_INFO_REQUESTED ──Resubmit (customer)──► PENDING_UNDERWRITING
       │
       └──Cancel (customer, any non-terminal state)──► CANCELLED
```

Terminal states: `APPROVED`, `REJECTED`, `CANCELLED`. Once terminal, an
application is view-only for every role — no exceptions (this is the
single most important invariant to preserve; both reference projects
call this out explicitly and enforce it in the shared service layer, not
just the UI).

**Reaching `APPROVED` — whichever arrow leads there — is also the
moment an account gets created** (§9.2): a customer record if one
didn't already exist for this applicant, and always a brand-new
account. `REJECTED` and `CANCELLED` create neither. See `CLAUDE.md`'s
"Applying without being a customer yet" for exactly where this happens
and why it has to be idempotent.

### 6.3 Escalation threshold

A single configurable amount (`MANAGER_ESCALATION_THRESHOLD_USD`, POC
default **$50,000**, one value for all product types) decides whether an
Underwriter's Approve is itself final or routes to a Manager. This is a
POC simplification — see `CLAUDE.md` for where a later per-product
threshold would plug in.

### 6.4 Document gate

An application cannot be submitted for underwriting until its required
document categories exist in Mayan EDMS for that application. Required
categories, common to every product: **Government ID**, **Proof of
Income**, **Bank Statements**, **Credit Report**. `mortgage` additionally
requires **Property Appraisal**; `auto_loan` additionally requires
**Vehicle Title/Invoice**.

This check happens once, at submission time (not continuously) — a
missing category surfaces as a clear "missing: Bank Statements" message
on the submit action, not a silent failure. On mobile, document capture
should support both "take a photo" (camera, for ID/paper documents) and
"choose a file" (for PDFs already on the phone).

A required category is satisfied by one or more uploaded documents —
a customer can upload several files under "Bank Statements" or "Proof
of Income" and the gate is satisfied the same as a single file would;
there's no cap on how many documents one category can hold.

### 6.5 Managed documents beyond the gate

Three more document associations exist outside the submission-gate
categories above — none of them customer-uploaded through the
application flow, all of them consequences of an application reaching
terminal `APPROVED` (§6.2, §9.2):

- **`id_photo`** (customer, exactly one) — when a customer record is
  created or matched at approval, the just-approved application's
  "Government ID" document is re-tagged as this customer's `id_photo`
  rather than asking them to upload it again. A returning customer
  applying for a second loan doesn't get a second `id_photo` — the
  first one stands.
- **`welcome_letter`** (account, exactly one) — generated automatically
  when the account is created at approval, no human involved. A simple
  templated document (applicant name, product type, amount, decision
  date) rather than a customer-facing form.
- **`consent`** (account, versioned) — one logical document per
  account whose content can be updated over time; each update is a new
  *version* of the same document (Mayan's own file-versioning), not a
  new document. **Which screen actually lets a customer or staff member
  trigger a consent upload isn't decided yet** — see §11's open
  questions.

## 7. Identity: customer side unauthenticated, back office real Keycloak

The two sides of this app have deliberately different identity models —
treated separately below.

### 7.1 Customer side (public-facing) — still no real auth

On first visit, the app asks for an email or phone number — no
password, no OTP, no verification — and stores it in a signed session
cookie for that browser. **Setting this cookie is a pure client-side
write — no database row is created at this point** (see §9.1 — a
`customers` row only ever gets created on approval). This value becomes
the applicant's `applicant_identifier`, used both for "my applications"
filtering in Postgres (the same visibility-invariant pattern
`review-approval-temporal` uses for its Operator role: filter by
`WHERE applicant_identifier = :session_value`, enforced in the shared
service layer, not just hidden in the UI) and as the top-level metadata
value in Mayan's document hierarchy. **Because this surface is
customer/public-facing, unauthenticated self-identification is a real
risk** — anyone who knows or guesses another customer's email/phone can
view their application status and documents. Treat any deployment of
this POC as non-public (e.g., behind a shared gateway password, VPN, or
an explicit "demo only" banner) until real customer authentication (a
passwordless email/SMS link, or a proper identity provider) replaces
this. This remains the single highest-priority item in `CLAUDE.md`'s
hardening section — adding Keycloak to the back office (below) does
**not** address it, since it's a separate surface with a separate
identity model.

### 7.2 Back-office side (internal, Underwriter/Manager) — real Keycloak

Underwriter and Manager both log in with real Keycloak credentials
(Authorization Code flow), directly reusing `review-approval-temporal`'s
validated integration:

- Two realm roles, **`Underwriter`** and **`Manager`**, gate which
  back-office screen a session can see (`/ui/underwriter` vs
  `/ui/manager`) — a plain role check, the same "role gates *screens*"
  principle the reference project uses.
- Fine-grained **permissions** (Keycloak Scopes on a Resource, checked
  via a UMA ticket exchange — not a role check) gate the actual
  **mutating decision actions**: `UnderwriterApprove`,
  `UnderwriterReject`, `UnderwriterRequestMoreInfo` (granted only via an
  `Underwriter Policy`), and `ManagerApprove`, `ManagerReject` (granted
  only via a `Manager Policy`). Splitting Approve/Reject into
  stage-specific scope names (rather than reusing one shared "Approve"
  scope the way the reference project could, since it only ever had one
  approving role) is the one deliberate divergence from that project's
  exact scope list — see `CLAUDE.md` for why a shared scope name would
  be a real privilege-boundary bug here.
- Decision buttons are only rendered for a session that actually holds
  the corresponding permission (mirrors the reference project's
  `_user_permissions(user)` pattern) — but, per that project's own
  lesson, the server-side check is the actual enforcement; hidden UI is
  a convenience, not a control.
- Sessions are stored server-side (Redis), not entirely in the browser
  cookie — the reference project measured a real Keycloak token set
  (access + refresh) at ~4.5KB signed, over the ~4KB real-browser cookie
  ceiling, which is why this pattern exists at all rather than a plain
  session cookie (which is all the customer side needs, having no
  tokens to hold).
- Demo users for the POC, provisioned via a Keycloak realm-import JSON
  (no admin-console clicking): `underwriter1`/`underwriter2`
  (`Underwriter` role), `manager1`/`manager2` (`Manager` role), password
  `password` for all — same convention as the reference project's own
  demo realm.

This is a straightforward reuse, not a redesign — see `CLAUDE.md` for
the concrete Resource/Scope/Policy layout and Docker Compose wiring.

## 8. UI requirements

### 8.1 Customer app (mobile-first, responsive)

- **Identify** — one-time (per browser) email/phone entry, no password.
- **My Applications** — the customer's own applications only, status
  badges, "Apply for a new loan" call to action.
- **New Application** — a mobile-friendly step flow: pick product type →
  common + product-specific fields → upload required documents (camera
  capture for ID, file picker for statements/reports) → review & submit.
  Blocked (with a specific, actionable message) until required documents
  are present.
- **Application detail / status** — current status, a simple timeline
  (submitted → under review → [escalated] → decision), the ability to
  add documents/edit fields and resubmit when in `MORE_INFO_REQUESTED`,
  and a Cancel action while non-terminal.

Design constraints: single-column layout, large touch targets, no
hover-only affordances, `<input type="file" capture>` for camera
capture, verified at a 375×812 mobile viewport during implementation
(not just a desktop window narrowed by eye).

### 8.2 Staff screens (`/ui/underwriter`, `/ui/manager`, desktop-oriented)

Gated by real Keycloak login (§7.2) — a session must have the
`Underwriter` or `Manager` role to see the corresponding screen at all.
Otherwise, direct reuse of `review-approval-temporal`'s Operator/Manager
screen design:

- A **login screen** (`/ui/login`, "Log in with Keycloak") in front of
  both screens, redirecting through Keycloak's Authorization Code flow.
- A **paginated list** (10 rows/page) of applications relevant to that
  role, auto-refreshing every 5 seconds so a decision made on one screen
  shows up on another without a manual reload.
- A **checkbox column** for bulk-eligible rows, a "select all on this
  page" header checkbox, and a selection toolbar (count + bulk action
  button(s)) that stays correct across the auto-refresh poll.
- **Row click → detail dialog**: applicant/loan details, product-specific
  fields, links to view each uploaded document (opens the Mayan-hosted
  file), and the decision form.
- **Bulk decision**: select multiple eligible rows → confirm dialog
  listing each selected application's product type and applicant → one
  shared comment applied to the whole batch → fire N concurrent
  decisions → per-item success/failure report. An Approve that would
  violate §9.2's one-active-account-per-product-type rule shows up in
  this same per-item report as a failure — the batch doesn't abort, and
  every other eligible item still goes through.
- **Approve can be refused with a reason**, same as the document gate:
  an application whose applicant already holds an active account of the
  same product type (§9.2) doesn't get signaled at all — the decision
  form shows the conflict instead of submitting.
- Decision buttons (single-item and bulk) only render for a logged-in
  session that actually holds the corresponding Keycloak permission
  (§7.2) — enforced server-side regardless of what the UI shows.

## 9. Data model

Split across the three domain modules (§1.1) rather than one table —
"Temporal owns live workflow state, the owning module's table is the
queryable record" still holds, per entity, the same principle
`review-approval-temporal`'s `review_requests` table follows. All three
tables live in one Postgres database (see `CLAUDE.md`'s "Data storage"),
but each is touched exclusively by its owning module's code — treat
them as if they were physically separate, since the point of the split
is the ownership discipline, not where the bytes happen to sit.

**Account-on-approval, not account-first**: an earlier draft of this
PRD had an account auto-opened the moment a customer started an
application — modeling an existing bank customer applying for another
product. The actual intent is closer to real loan origination: **most
applicants aren't customers yet, and an account is the *outcome* of an
approved loan, not a precondition of filing one.** See `CLAUDE.md`'s
"Applying without being a customer yet" for the full mechanics; the
tables below reflect it.

### 9.1 Customer (owned by the Customer module)

| Field | Notes |
|---|---|
| `customer_id` | UUID, primary key |
| `applicant_identifier` | the customer's self-entered email/phone (§7.1) — the natural key a returning applicant resolves to the same `customer_id` by |
| `name`, `email`, `phone` | profile fields, editable over time |
| `created_at` | |

A row here isn't created just because someone typed an identifier and
started an application — it's created (or matched, if one already
exists for that identifier) **only when an application under that
identifier is approved**. An applicant can submit and even complete
several applications with no `customers` row existing for them at all.

### 9.2 Account (owned by the Account module)

| Field | Notes |
|---|---|
| `account_id` | UUID, primary key |
| `customer_id` | reference to the owning customer (resolved via a `service.py` function call, not a database join or foreign key — see `CLAUDE.md`'s "Data storage") |
| `product_type` | `personal_loan` \| `auto_loan` \| `mortgage` — the product this account resulted from |
| `opened_at`, `status` | `status` is `ACTIVE` \| `CLOSED` |

**Not one account per customer.** A customer can hold multiple
accounts over time — one per approved application. An account is
created exactly once, at the moment an application reaches terminal
`APPROVED` (§6.2) — never before, and never for a `REJECTED` or
`CANCELLED` application.

**A customer's `ACTIVE` accounts may never repeat a `product_type`.**
A customer can hold a `CLOSED` `personal_loan` account and later open a
new, `ACTIVE` one — just never two `ACTIVE` `personal_loan` accounts at
once. An Underwriter or Manager attempting to Approve an application
that would violate this gets refused with a clear reason (e.g.
"applicant already has an active Personal Loan account") instead of
the approval silently failing — same "reject with a specific reason"
principle as §6.4's document gate. See `CLAUDE.md`'s "Applying without
being a customer yet" for exactly where this check runs and its one
accepted gap (a narrow race between two near-simultaneous approvals for
the same customer and product type).

### 9.3 Application (owned by the Application module)

| Field | Notes |
|---|---|
| `application_id` | UUID, primary key |
| `applicant_identifier` | the durable identity key (§7.1) — always present at submission, regardless of whether the applicant is a recognized customer yet. This is what "a customer only sees their own applications" (§10 criterion 2) is actually keyed on. |
| `customer_id` | **nullable** — set at submission if `applicant_identifier` matches an existing customer, otherwise `NULL` until (and unless) this application is later approved |
| `account_id` | **nullable** — `NULL` for the application's entire non-terminal lifetime; set exactly once, when the application reaches terminal `APPROVED` |
| `workflow_id` | Temporal workflow id, nullable until `persist_application` commits it. **Never cleared afterward** — a terminated workflow or a deleted execution leaves this pointing at a Temporal execution that no longer exists; no reconciliation job exists to catch it. See `CLAUDE.md`'s "Known gaps" (corrected in P12-1 from an earlier draft of this row, which incorrectly claimed such a mechanism existed). |
| `product_type` | `personal_loan` \| `auto_loan` \| `mortgage` |
| `payload` | JSONB, product-specific fields |
| `applicant_name`, `applicant_email`, `applicant_phone`, `amount` | captured **as submitted** — a deliberate snapshot, not a live read of the customer's current profile (see `CLAUDE.md`'s "Denormalized applicant fields" note) |
| `status` | `PENDING_UNDERWRITING` \| `MORE_INFO_REQUESTED` \| `PENDING_MANAGER_APPROVAL` \| `APPROVED` \| `REJECTED` \| `CANCELLED` |
| `underwriter_name`, `underwriter_comment`, `underwriter_decided_at` | set once the Underwriter acts — `underwriter_name` is the authenticated Keycloak username, not free text |
| `manager_name`, `manager_comment`, `manager_decided_at` | set only for escalated applications |
| `created_at`, `updated_at` | |

`applicant_identifier` and `application_id` are what's mirrored into
Mayan's document metadata — the hierarchy is two levels
(`applicant_identifier -> application_id`), not three, precisely
because `account_id` doesn't exist yet at document-upload time. See
`CLAUDE.md`'s document-hierarchy section.

## 10. Success criteria for this POC

1. All three product types can be created end to end **starting from the
   customer's own phone browser**: apply → (Approve fast-path, or
   Approve → escalate → Manager decides, or Reject, or Request More Info
   → customer resubmits → decide) → terminal state, verified both via
   the HTMX UI (customer and staff sides) and by watching the workflow
   execute in the Temporal Web UI.
2. A customer only ever sees their own applications; typing a different
   email/phone at the identify screen shows a different (empty, unless
   reused) application list — proving the visibility filter actually
   filters, not just hides via CSS.
3. Documents uploaded via the customer's phone are visible/organized in
   Mayan's own hierarchy view, and submission is blocked with a clear
   message until required categories are present.
4. Bulk approve/reject works for both Underwriter and Manager screens,
   including a mix of eligible and already-decided rows in one batch
   (partial success reported per item, not an all-or-nothing failure).
5. A native Temporal cancel or a deleted workflow execution doesn't
   orphan a Postgres row at a non-terminal status forever (reuse
   `review-approval-temporal`'s recovery mechanisms).
6. `docker compose up --build` brings up the entire stack — the one app
   image (web process(es) + Temporal worker process(es), all seven
   modules), Mayan + its own Postgres/Redis, the shared Postgres
   (`loan_onboarding` + `temporal` databases), Temporal + Web UI,
   Keycloak + its own Redis for back-office sessions — with no manual
   setup beyond the one-time, documented Mayan hierarchy bootstrap
   script.
7. The customer flow is usable end to end on an actual mobile viewport
   (375×812), not just a resized desktop browser.
8. The back-office app cannot be used without logging in via Keycloak;
   an `underwriter1` session gets a real `403` attempting a Manager-only
   decision (and vice versa), proving the permission scopes actually
   restrict actions rather than just hiding buttons.

## 11. Open questions (for the implementer / reviewer to resolve early)

- **Which surface triggers a `consent` upload (§6.5)?** Not designed
  yet — could be a post-approval customer screen, a back-office action,
  or both. `document.service.upload_consent(account_id, file)` doesn't
  care which caller uses it; this is purely a missing UI flow, not an
  API gap. Needs a decision before Phase 10/11 builds whichever screen
  it turns out to be.
- Should the escalation threshold be per-product instead of global?
  (Deferred to POC v2 — see `CLAUDE.md`.)
- Should `MORE_INFO_REQUESTED` → resubmit require *new* documents, or is
  editing the payload alone (no new upload) enough to resubmit? Current
  assumption: either is allowed; the document gate only re-runs if the
  customer actually adds/replaces a file.
- Is one `applicant_identifier` (email or phone, customer's choice)
  enough, or does the POC need to normalize/dedupe both so the same
  human can't accidentally fragment their history across two
  identifiers? Current assumption: no normalization, single free-text
  value — acceptable for a POC, worth revisiting before anything wider.
- Does a rejected/cancelled application's Mayan documents get retained
  indefinitely, or is there a retention/cleanup policy? Out of scope for
  the POC; Mayan's own trash/retention settings apply as-is.
- Should the back-office screens live behind a separate hostname
  entirely, on top of the Keycloak login they now require? Current
  assumption: same app, different path (`/ui/*` vs the customer app's
  own routes), defended by real login now rather than obscurity — a
  separate hostname would be a network-layer hardening step, not
  required for the POC to be meaningfully protected.
- Should Temporal Web UI itself also sit behind Keycloak (the reference
  project does this, gated by a `TemporalAdmin` role)? Not built here —
  this ask was scoped to the back-office *application*, not the
  Temporal operational tooling; worth revisiting if Temporal Web UI ends
  up exposed beyond the local dev machine.
