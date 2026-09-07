---
name: risk-assessment-nats
description: The planned (Phase 21, not yet built) design for loan-onboarding-poc's automated risk assessment -- a standalone NATS Adapter service as the sole owner of NATS connectivity, KrakenD fronting the Risk-Engine HTTP boundary, the mock Risk Engine (HTTP-only, no NATS), the new PENDING_RISK_ASSESSMENT workflow state, and the thin risk/ leaf module. Triggers on "NATS", "risk assessment", "risk/ module", "NATS Adapter", "KrakenD", "PENDING_RISK_ASSESSMENT", "signal_risk_decision", "submit_risk_assessment", "mock-risk-engine", "risk_tier", "Phase 21".
---

### Automated risk assessment via NATS (planned — Phase 21, not yet built)

This section describes the target design for `IMPLEMENTATION_PLAN.md`'s
Phase 21, written first per this project's own convention, before any
of it is implemented — nothing below is built yet; every "planned"
marker in this section and in "`risk/` — Risk assessment module" below
is literal, not a stale leftover. Raised directly by the user as a
future enhancement, distinct from Phase 18-20's account-closure/
notification work: simulate a genuinely **external, asynchronous**
system — a Risk Engine — consulted over a message broker (NATS) rather
than a synchronous HTTP call, and let it auto-decide the easy cases
(very low or very high risk) without a human ever touching them.

**Revised after a second design pass, once `docs/research-krakend.md`
existed to inform it**: the first draft of this section had the mock
Risk Engine speak NATS directly and left the real-engine/HTTP-gateway
question fully open ("no gateway product has been chosen"). Confirmed
directly with the user: KrakenD is now the chosen gateway, and — more
consequentially — **NATS connectivity moves out of the mock Risk Engine
and out of this codebase's own process entirely, into one new
standalone service, the NATS Adapter, which becomes the *only* thing
anywhere that depends on the NATS protocol.** This is a bigger, cleaner
revision than just picking a gateway product: the mock Risk Engine (and
any real one that later replaces it) now only ever speaks plain HTTP,
never NATS — the same "genuinely external system this codebase doesn't
own" framing already applied to Mayan and Keycloak, now taken further
so it never needs NATS awareness even in principle.

- **Where it sits in the state machine**: unchanged from the first
  draft. A new state, `PENDING_RISK_ASSESSMENT`, entered immediately
  after `persist_application` commits — *before* today's
  `PENDING_UNDERWRITING`. A new activity, `submit_risk_assessment`
  (owned by `application/activities.py`, called by the workflow's own
  `execute_activity(...)`-by-name, same mechanism `persist_application`/
  `persist_decision` already use — see "Breaking the cycle"), calls
  `risk.service.submit_risk_assessment(...)` with the application's
  risk criteria (amount, product type, payload). A new signal,
  `signal_risk_decision(risk_tier)`, is what moves the workflow out of
  this state — sent not by a BFF route handler (the source of every
  other signal today) but by the NATS Adapter (below), which computes
  the deterministic `loan-application-<application_id>` workflow id
  itself (the same scheme `workflow/service.py`'s own
  `_workflow_id_for_application` already uses) and signals Temporal
  directly.
- **Decision routing**: unchanged. `LOW` risk auto-transitions straight
  to `APPROVED` — reusing the *exact same* `persist_decision` activity
  and provisioning block a human Underwriter's Approve already triggers
  (customer/account creation, Welcome Letter email, document tagging —
  see "Applying without being a customer yet"), just with
  `underwriter_name` set to a fixed marker value
  (`"risk-engine-auto"`) instead of an authenticated Keycloak username.
  **This is a deliberate, called-out exception** to the rule stated
  elsewhere in this file that `underwriter_name`/`manager_name` are
  "always an authenticated Keycloak username, never client-submitted
  free text" — an automated decision has no Keycloak session behind it
  by definition, so the invariant has to bend here on purpose, not by
  accident. `HIGH` risk auto-transitions straight to `REJECTED`, same
  `persist_decision` REJECT path. **`MEDIUM` risk gets no new branch at
  all** — it falls straight through into today's existing
  `PENDING_UNDERWRITING`, waiting on a human `submit_decision` signal
  exactly as it does today. Confirmed with the user: no risk-tier
  column or badge is surfaced anywhere in `bff_backoffice`'s UI for this
  phase — a `MEDIUM` application looks identical to any other row in
  the underwriting queue.
- **The NATS Adapter — one new standalone service, sole owner of NATS
  connectivity in this whole system.** Not part of the `loan_onboarding`
  Python package (no import edge from anywhere in this codebase into
  it) — its own container, its own process, same "genuinely external,
  not app code" treatment `mock-risk-engine` already gets. It exposes
  two small HTTP endpoints of its own and runs two background NATS
  subscriber loops in the same process:
  1. `POST /assessments` — called by `risk.service.submit_risk_assessment(...)`
     (a plain `httpx` call, not a NATS publish — see `risk/`'s own
     module section below for why this changes `risk/`'s dependency
     footprint). Publishes the request body onto the
     `risk.assessment.submitted` NATS subject and returns `202` once
     Temporal-style "accepted, not yet processed" — the same
     "only confirms accepted, not applied" caveat
     `workflow.service.start_workflow` already carries for its own
     callers.
  2. A subscriber loop on `risk.assessment.submitted`: for each
     message, calls the Risk Engine's own `POST /assess` — **through
     KrakenD**, not directly (see below).
  3. `POST /decisions` — the webhook the Risk Engine calls, **through
     KrakenD**, once it has a tier. Publishes the request body onto the
     `risk.assessment.decided` NATS subject and returns `202`.
  4. A subscriber loop on `risk.assessment.decided`: for each message,
     computes `workflow_id = f"loan-application-{application_id}"` and
     sends the `signal_risk_decision` signal directly, via its own
     `temporalio.client.Client` connection — **not** by importing
     `workflow.service` (it can't; it's not part of this package) and
     **not** by calling back into the `app`/`worker-*` processes over
     HTTP either — a direct Temporal signal is the same kind of
     standard, client-authorized action `bff_backoffice`'s decision
     routes already perform, just issued from a different process. This
     does mean the Adapter independently duplicates a small amount of
     Temporal-connection-bootstrap logic `workflow/service.py` already
     has — accepted as the cost of keeping the Adapter a self-contained
     service rather than adding a new internal-only HTTP surface (and
     its own auth question) to the main web process.
- **KrakenD sits specifically at the Risk-Engine boundary, both
  directions — not between this codebase and the NATS Adapter.**
  Confirmed directly with the user. The Adapter's own call to the Risk
  Engine's `POST /assess` goes through KrakenD; the Risk Engine's own
  call to the Adapter's `POST /decisions` webhook goes through KrakenD
  too. `application/activities.py` → the Adapter's `POST /assessments`
  is a plain, direct internal HTTP call — no gateway hop, since that
  traffic never crosses out to a system this codebase doesn't own.
  **KrakenD's job here is deliberately the plain, well-supported one**:
  a conventional HTTP↔HTTP reverse-proxy/API-gateway (routing, and
  wherever needed, auth/rate-limiting/circuit-breaking) in front of the
  Risk Engine, not KrakenD's own NATS pub/sub backend feature — using
  that feature would have given KrakenD its own NATS dependency,
  contradicting "the NATS Adapter is the only thing that depends on
  NATS." This is also *simpler* than the shape `docs/research-krakend.md`'s
  own speculative sketch explored before this decision was made: that
  sketch worried about whether KrakenD could itself bridge a NATS
  subject to an outbound HTTP call (it can't, on its own) — moot now,
  since the NATS Adapter does that bridging itself and KrakenD never
  needs to touch NATS at all.
- **The Mock Risk Engine only ever speaks HTTP — `POST /assess` in,
  `POST /decisions` (via KrakenD) out — never NATS, not even as a
  mock.** Its own container, its own process — confirmed with the user
  directly over building it as Python code inside `loan_onboarding`,
  matching how Mayan and Keycloak are already treated as real external
  systems this codebase doesn't own. Its decision rule for this phase is
  a deliberately simple, deterministic bucketing on `amount` — **assumed
  default, not yet confirmed, see `IMPLEMENTATION_PLAN.md`'s Decisions
  Needed**: `< $15,000 → LOW`, `$15,000–$50,000 → MEDIUM`,
  `≥ $50,000 → HIGH`. Picked so the mock is trivially testable (a
  known amount always produces a known tier) rather than trying to
  simulate a real scoring model.
- **At-least-once delivery means the Adapter's own signal-sending needs
  a duplicate guard, and so does the workflow's signal handler.** NATS
  core pub/sub (no JetStream needed for this phase) doesn't promise
  exactly-once delivery, and neither does the Adapter's own subscriber
  loop retrying a failed Temporal signal call. The workflow's handler
  for this new signal needs the same "ignore a signal once a decision is
  already claimed" guard `_claim_final()`-style logic already gives the
  human-decision path — a duplicate/redelivered risk decision must not
  be able to double-apply.
- **No timeout on the risk-engine callback — a known gap carried
  forward on purpose, not solved differently here.** Same accepted gap
  this file's Known Gaps section already documents for "no timeout on
  wait for Underwriter/Manager decision" — an application that never
  gets a risk decision (Risk Engine down, message lost, KrakenD
  misrouted) sits at `PENDING_RISK_ASSESSMENT` forever, same shape as
  the existing gap, not a new category of problem.
- **New Docker Compose services (planned)**: `nats` (official
  `nats:latest` image — core pub/sub only, JetStream not needed for
  this phase since neither leg needs replay/durability beyond what
  Temporal's own activity retry already gives the publishing side),
  `mock-risk-engine` (HTTP-only, as above), `risk-adapter` (the NATS
  Adapter — holds the NATS connection, the two HTTP endpoints, and its
  own Temporal client), `krakend` (fronting `mock-risk-engine` ↔
  `risk-adapter` traffic both directions).


### `risk/` -- Risk assessment module (planned -- Phase 21, not yet built)

*(See "Automated risk assessment via NATS" above for the full design
this module implements — this section covers only its own code shape,
same split every other module section follows.)*

**Revised alongside "Automated risk assessment via NATS" above once
KrakenD + the standalone NATS Adapter were decided**: this module no
longer touches NATS at all, or the network in general beyond one plain
HTTP call — all NATS connectivity moved to the new, separately-deployed
NATS Adapter service (not part of this Python package). `risk/` is now
the thinnest module in the codebase, thinner even than `document/mayan_client.py`'s
"thin async client" shape, since there's no protocol-specific client to
wrap anymore — just one HTTP `POST`.

- **No `nats_client.py`.** This file was in the original draft of this
  section; removed once NATS connectivity moved to the standalone NATS
  Adapter service. `risk/` has no NATS dependency of any kind, not even
  a wrapped one.
- `service.submit_risk_assessment(application_id, applicant_identifier,
  product_type, amount, payload) -> None` — a plain `httpx.post(...)`
  to the NATS Adapter's `POST /assessments` endpoint
  (`RISK_ADAPTER_URL` env var, Docker-internal service name, same
  discipline this file already documents for `KEYCLOAK_ISSUER`), body
  mirroring the function's own arguments. Returns once the Adapter
  confirms it accepted the submission for NATS publish — same "accepted,
  not yet processed" caveat every other `service.py`-owned outbound call
  in this codebase already carries. Called only from
  `application/activities.py`'s new `submit_risk_assessment` activity,
  same "activities.py is where outbound calls to leaf integration
  modules happen" pattern `document/`/`workflow/`/`notifications/` are
  already called from there.
- **No subscribe-side code for the *decision* leg lives here, and never
  will** — that's now the NATS Adapter's own job end-to-end (subscribe
  to the decision subject, signal Temporal directly), not something any
  code inside the `loan_onboarding` package does. `risk_listener_main.py`,
  the fourth composition root the original draft of this section
  planned, is no longer needed — there's no in-package NATS
  subscription left for it to own.
- **Never imports `application/`, `workflow/`, `customer/`,
  `account/`, or `document/`.** A leaf, same shape as `document/` and
  `workflow/` themselves — `idgen/` is the one exception every other
  leaf already gets, if this module ends up needing to mint its own id
  (e.g. a `risk_assessment_id` correlating a submission with its
  eventual decision message) — not yet decided whether one is needed.
- **No Postgres table of its own for this phase.** The risk tier a
  decision resolves to gets written onto a new, nullable
  `applications.risk_tier` column — `application/`'s own table, written
  by the same `persist_decision` activity that already writes every
  other decision-outcome column — not by `risk/` itself.

