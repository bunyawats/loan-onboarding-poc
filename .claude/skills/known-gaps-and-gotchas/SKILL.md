---
name: known-gaps-and-gotchas
description: Accepted limitations and real operational gotchas hit while building loan-onboarding-poc -- no schema migration tooling (db/schema.sql changes don't apply to an existing volume), local-vs-dockerized worker races, the active-account-per-product-type race window, Temporal terminate-vs-cancel, stale image/config drift, two live-hit testing hazards (wrong DATABASE_URL wiping the live stack, asyncpg connection-pool exhaustion from one-off docker exec scripts), and Phase 21's risk-tier thresholds making PENDING_MANAGER_APPROVAL practically unreachable. Read before touching schema, workers, or running ad hoc scripts against the local stack. Triggers on "known gaps", "schema migration", "ALTER TABLE accounts", "worker race", "TooManyConnectionsError", "asyncpg pool exhaustion", "docker exec asyncio.run", "temporal workflow terminate", "loan_onboarding_test", "stuck workflow", "KeyError closure_workflow_id", "manager escalation", "PENDING_MANAGER_APPROVAL", "MANAGER_ESCALATION_THRESHOLD_USD".
---

## Known gaps to state explicitly once built

*(Every "Resolved" bullet below is trimmed to a current-state summary —
full repro/root-cause/reverification narrative for each lives in
`IMPLEMENTATION_PLAN.md`'s Session Log, 2026-09-04 docs-consolidation
entry, unless a more specific pointer is given.)*

- **`docker compose up -d` does not rebuild images, and Mayan's own
  index-template/metadata-type config can independently drift or
  reset.** Both hit live, in the same session — full repro in
  `IMPLEMENTATION_PLAN.md`'s "2026-09-04 (new session)" Session Log
  entry. **The operating rule this confirms**: this file's "already
  built and live-verified" describes a point in time, not a durable
  guarantee — re-verify both the running image and Mayan's live config
  directly before trusting a "clear test data and re-verify" pass to
  exercise current code.
- **This project has no schema migration tooling** — `db/schema.sql`
  changes only ever apply to a brand-new `db` volume
  (`db/init/01-init.sh`, first container start only), never to an
  already-running one. Bit for real after Phase 18 (a live `KeyError:
  'closure_workflow_id'` inside a Temporal activity, from an
  un-migrated `accounts` table — full repro and fix in
  `IMPLEMENTATION_PLAN.md`'s Session Log). **The operating rule this
  confirms**: a schema change landing in `db/schema.sql` is not
  "deployed" just because it's merged and the images are rebuilt — an
  existing `db` volume needs either a manual `ALTER TABLE` or a full
  `docker compose down -v` (destroying all data) before new code that
  assumes the new columns exist can run safely against it. No tooling
  in this project currently detects or prevents this mismatch.
- **A local `worker_main.py` process and the dockerized
  `worker-workflow`/`worker-activity` containers silently race each
  other for the same Temporal task queues if both are left running at
  once, pointed at different databases** — found live during Phase 19's
  verification, full repro in `IMPLEMENTATION_PLAN.md`'s Session Log.
  **The operating rule this confirms**: any local-worker verification
  session must stop *all three* of `app`/`worker-workflow`/
  `worker-activity`, not just `app` — the two worker containers hold no
  port to conflict with, so it's easy to forget they're still silently
  polling and racing.
- **Reconciliation (`reconcile.py`) only detects and fixes drift — it
  never prevents it, and nothing runs it automatically.** It has to be
  invoked by a human or a scheduled job, neither of which this project
  sets up. **Cascade-on-delete is deliberately not built** — there is no
  delete operation for `customer`/`account`/`application` anywhere in
  this codebase today, and whether a loan-onboarding system should ever
  hard-delete an approved entity (audit-trail implications) versus a
  status change is a real, unresolved product question, not a build gap
  — confirmed with the user as "reconciliation first," cascade
  deferred, not decided against.
- **`applications`'s old `chk_approved_has_account` DB-level check
  constraint is gone, not replaced.** Once the account pointer moved to
  `accounts.application_id` (see "Data storage"), "an APPROVED
  application has a matching account" can no longer be expressed as a
  single-table `CHECK` — enforcing it across two tables would need a
  trigger, which this POC deliberately doesn't add. The invariant is
  still true in practice (`persist_decision`'s logic guarantees it), but
  it moved from DB-enforced to code-enforced-only — a real, if narrow,
  reduction in the safety net.
- **The 9-digit-numeric primary key format (`CUS-`/`ACC-`/`APP-`) trades
  away collision-safety margin for a familiar, account-number-style
  look.** `10^9` values per entity type is real headroom for a POC but
  nowhere near a `UUID`'s — see "Data storage" for the entropy
  discussion and why the retry-on-collision insert logic in each
  module's `db.py` is load-bearing, not decorative. Revisit (longer id,
  or alphanumeric) if this ever needs to scale past POC data volumes.
- **Resolved (P12-3)**: `app`'s host port would have collided with
  `mayan`'s (both `8000`) — moved to `8001`. A second bug, invisible
  until the first fully containerized run, was found in the same pass:
  browser-redirect URLs and issuer-claim validation were built from the
  server-internal `KEYCLOAK_ISSUER`, mismatched against Keycloak's
  actual browser-facing `iss` claim — fixed with a new
  `KEYCLOAK_PUBLIC_ISSUER` env var.
- **Resolved (post-P12)**: `db`'s published host port `5432` collides
  with a native, host-installed Postgres on a dev machine — moved to
  `5433` on the host side only (in-Compose services reach `db:5432`
  internally, unaffected).
- **Mayan's default REST API rate limit (`REST_API_THROTTLING_RATE_USER`,
  20 req/sec) is real and gets hit at POC scale** (found in P5-4/P5-5
  running a realistic upload sequence against real Mayan).
  `mayan_client.py`'s `_request` retries on 429 honoring `Retry-After`,
  bounded at `_MAX_429_RETRIES = 5` — not a full fix.
  `document/service.py`'s `_documents_matching` (fetch every document,
  then every document's metadata, then filter in Python — Mayan's
  advanced-search endpoint doesn't AND multiple metadata fields
  together) is still O(all documents in the instance) per call and will
  throttle more as real data volume grows; fine for a POC, would need
  server-side filtering (or caching) before scaling past that.
- **Resolved, narrowed rather than fully closed.** `bff_customer/` used
  to accept a self-typed email/phone with zero verification (PRD §7.1)
  — this POC's standout risk. **Fixed** by requiring a 6-digit
  email-verification code before the session cookie is ever set — see
  "Identity" above and `bff_customer/identity.py`'s module docstring.
  **Still a real, accepted limitation**: no real email/SMS provider, so
  delivery is fake (`notifications/service.py` prints the code
  server-side, the verify page shows it directly, labeled dev-only) —
  this proves the mechanism, not a production-ready login. Phone-number
  identifiers were dropped along with this fix (SMS would need a
  provider this project has none of either), confirmed with the user as
  an accepted scope reduction.
- **Resolved (no-graceful-handling half only — the race window itself
  is deliberately still open).** The active-account-per-product-type
  rule is checked before a decision is signaled but not atomically with
  it — two near-simultaneous Approves for the same customer+product_type
  can both pass the check before either commits; the partial unique
  index always stopped the bad *write*, but the loser used to fail its
  whole Temporal workflow and get stuck forever with no error surfaced.
  **Fixed**: `persist_decision` now catches that specific constraint
  violation and converts the loser into a clean `REJECTED` (with a
  system-generated comment), chosen over two other options (fail fast,
  or a distributed lock) as the one that closes "stuck forever" without
  serializing the check-and-write. **The in-batch half of the window is
  now also closed** by `check_decision_allowed_bulk`, which tracks
  `(applicant_identifier, product_type)` pairs an earlier item in the
  same batch already claimed. **What's still deliberately accepted**:
  only same-request concurrency is closed — two decisions from
  *separate* HTTP requests close enough in time can still both pass
  their own checks; closing that fully would need a lock spanning from
  the web-process check to the worker-process write, a much larger,
  riskier change not undertaken here. `persist_decision`'s
  conflict-to-REJECTED handling remains the backstop for that
  cross-request case. See `application/service.py`'s
  `check_decision_allowed_bulk` and `application/activities.py`'s
  `persist_decision`, plus each one's test coverage in
  `tests/unit/application/`.
- **The active-account-per-product-type rule doesn't count
  `CLOSURE_REQUESTED` as active, and the resulting reject-path collision
  is a real, unhandled crash — found while reviewing the ER diagram
  against `db/schema.sql` for Phase 18, not caught at build time.** Both
  `db/schema.sql`'s partial unique index
  (`ux_accounts_customer_active_product_type`, `WHERE status =
  'ACTIVE'`) and `account/db.py`'s `has_active_account_of_type` SQL
  (`... AND status = 'ACTIVE'`) treat an account as no longer "active"
  the moment its status moves to `CLOSURE_REQUESTED` — before the
  closure is actually decided. A customer with a pending closure request
  on their `personal_loan` account can therefore apply for, and be
  approved for, a *second* `personal_loan` account while the first
  request is still pending; nothing in
  `application.service.check_decision_allowed`/
  `get_available_product_types` blocks it, since both just call this
  same `has_active_account_of_type` read.
  **This is not a harmless double-up — it sets up a real, unhandled
  failure**: if the first closure request is later **rejected** (reverting
  that account's status back to `ACTIVE`) *after* the second account has
  already been approved and is `ACTIVE`, `account/activities.py`'s
  `persist_closure_decision` has no `try`/`except` around its
  `UPDATE accounts SET status = 'ACTIVE' ...` write —
  unlike `application/activities.py`'s `persist_decision`, which
  deliberately catches this exact constraint violation and converts the
  loser into a clean `REJECTED` (see the race-window bullet above),
  `persist_closure_decision` has no equivalent handling. The `UPDATE`
  hits the same partial unique index and raises an uncaught
  `UniqueViolationError`, failing the Temporal activity — the same
  "stuck forever with no error surfaced" shape this file already
  documents for other unhandled Temporal-activity failures, just via a
  different trigger (a reject, not an approve). Not fixed here — left as
  an open question for a future session (either give `persist_closure_decision`
  the same conflict-to-clean-outcome handling `persist_decision` has, or
  have `has_active_account_of_type` treat `CLOSURE_REQUESTED` as active
  in the first place, closing the double-up at its source instead of
  its consequence) rather than guessed at or half-fixed here.
- **Resolved, found live in Phase 13's P13-7 sweep.**
  `check_decision_allowed`'s short-circuit used to trust a `NULL`
  `applications.customer_id` as "no customer exists," which is wrong
  for a sibling application under the same identifier whose column was
  never backfilled after an *earlier* sibling's approval — this let a
  second Approve reach the workflow uncontested and then deterministically
  hit the same active-account unique-constraint violation. **Fixed** by
  having `check_decision_allowed` resolve via
  `customer.service.find_by_identifier(...)` when `customer_id` is
  `NULL`, instead of trusting the column alone. This is a separate,
  narrower fix from the race-window gap immediately above — unaffected
  by it.
- Module boundaries are enforced by import-linter config, not by a
  process/network boundary — a determined or careless change can still
  violate them if CI isn't actually wired to fail on a violation. Don't
  treat "we organized it into folders" as equivalent to "the boundary is
  enforced" until the lint step exists and is required.
- Same Keycloak-side gaps the reference project has and hasn't closed:
  `verify_aud=False` until a real audience is configured; no caching on
  permission checks (every mutating action is a live UMA exchange).
- **Resolved (2026-09-08).** Phase 21's risk-tier thresholds and PRD
  §6.3's pre-existing manager-escalation threshold used to overlap
  exactly, making the manager-approval path practically unreachable —
  found live while walking a HIGH-risk application through the manager
  queue on request. The mock Risk Engine's MEDIUM bucket was
  `$15,000 <= amount < $50,000` (`mock_risk_engine/main.py`'s
  `LOW_THRESHOLD`/`HIGH_THRESHOLD`), exactly matching
  `workflows.py`'s manager-escalation check (`amount >= 50_000`,
  `MANAGER_ESCALATION_THRESHOLD_USD`) — so no amount could ever be both
  MEDIUM (to reach human `PENDING_UNDERWRITING` at all) and
  escalation-eligible. **Fixed** by moving `mock_risk_engine/main.py`'s
  `HIGH_THRESHOLD` from $50,000 to $100,000 (`MANAGER_ESCALATION_THRESHOLD_USD`
  itself left unchanged, since it's the more established, PRD-§6.3-named
  default vs. the risk-tier thresholds' own still-"proposed, not
  confirmed" status) — this opens a real `$50,000–$99,999.99` band where
  an application is both MEDIUM (reaches Underwriter) and
  escalation-eligible (`>= $50,000`), so an Underwriter's Approve on an
  application in that band now genuinely reaches
  `PENDING_MANAGER_APPROVAL`. `mock_risk_engine/tests/test_main.py`'s
  boundary table extended to cover the new band. **Live-verified
  against the real running stack, same day**: rebuilt the
  `mock-risk-engine` container (`docker compose build mock-risk-engine
  && docker compose up -d mock-risk-engine` — confirmed live in the
  container via `docker exec ... python3 -c "import main; ..."` that
  `HIGH_THRESHOLD` actually loaded as `100000`, not just that the source
  file changed), then drove one real `$60,000` `personal_loan`
  application through the real stack end to end (a throwaway script,
  not committed, reusing `scripts/generate_real_e2e_data.py`'s own
  helpers): risk assessment resolved to `PENDING_UNDERWRITING` (not
  auto-rejected as `HIGH`, which is what the old $50,000 threshold would
  have done), a real Underwriter (`underwriter1`) Approve escalated it
  to `PENDING_MANAGER_APPROVAL`, a real Manager (`manager1`) Approve
  resolved it to `APPROVED` — confirmed via `psql` (`underwriter_name`/
  `manager_name` both set to the real staff usernames, not
  `"risk-engine-auto"`; `risk_tier` correctly `NULL`, since only an
  auto-LOW/auto-HIGH decision writes that column) and the real Mayan
  REST API (`welcome_letter_ACC-*.pdf` and `consent.pdf` both present,
  tagged to the newly-provisioned real account). All test data was
  cleared first (`scripts/clear_e2e_data.py --yes`), so this was the
  only application in the system during verification.
  **One real, minor gap found along the way, in `clear_e2e_data.py`
  itself, unrelated to this fix**: `rebuild_mayan_indexes`'s polling
  loop has no retry around Mayan's own transient `RemoteProtocolError`
  ("Server disconnected without sending a response") — the same real,
  memory-pressure-driven flakiness `generate_real_e2e_data.py`'s
  `request_retry` already works around elsewhere. Hit twice in a row
  this session; worked around with a manual retry loop, not the
  script's own code. Not fixed here (out of scope for this bug) — a
  future session should give `rebuild_mayan_indexes` the same
  retry-with-backoff treatment `request_retry` already gives this exact
  failure mode.
  `scripts/generate_real_e2e_data.py`'s own docstring/comments and its
  unused `escalate_approve`/`escalate_reject` scenario branches still
  describe the old unreachable-path behavior — deliberately still not
  updated, since wiring a real escalation scenario into that script's
  own committed scenario lists is a separate scope decision, not
  required to close or verify the threshold-overlap bug itself.
- No timeout on "wait for Underwriter/Manager decision."
- **A Temporal *terminate* (vs. *cancel*) still can't be recovered from
  inside the workflow, structurally — no event is ever delivered to
  catch — and no reconciliation job exists anywhere in this codebase to
  catch it from the outside either.** An earlier draft of
  `db/schema.sql`'s `workflow_id` column comment (and `PRD.md` §9.3's
  data-model table) claimed this column "gets cleared if a Temporal
  admin deletes the execution," describing a reconciliation mechanism
  as if it existed — corrected in P12-1 after grepping the codebase and
  finding no code anywhere writes to `workflow_id` after
  `persist_application` sets it; `PRD.md` §9.3 still carries this
  correction inline in its own `workflow_id` row. Verified for real in
  P12-1: a genuine `temporal workflow cancel` correctly lands the
  Postgres row on `CANCELLED`, but a `temporal workflow terminate`
  leaves it permanently stuck with no error raised anywhere — a human
  operator today has no query, alert, or job that would ever surface
  this.
- No proactive notification (email/SMS) on status change.
- A product type present in `application/schemas.py`'s registry but
  missing from `workflow/task_queues.py`'s `KNOWN_PRODUCT_TYPES` is
  caught immediately by the import-time assert (see "Breaking the
  cycle") — but a product type with **no worker actually polling its
  queue** still leaves applications stuck at `PENDING_UNDERWRITING`
  forever with no error anywhere; the assert can't catch that one, same
  unaddressed gap the reference project documents for its own
  `KNOWN_REVIEW_TYPES`.
- **If this ever needs to scale past one team/one deploy cadence**, the
  module boundaries here are deliberately drawn so any of the seven
  could be extracted into a real service later with the *interface*
  already correct (`service.py`'s function signatures become the new
  HTTP contract) — the work left at that point is standing up the
  process/network boundary and picking a wire format, not rediscovering
  where the seams should be.


## Testing hazards found live (not general testing policy -- see CLAUDE.md Testing section for that)

**When running these against a local `docker compose` stack you're also
using for live/manual verification, point `DATABASE_URL` at a separate
database (e.g. `loan_onboarding_test`), never the compose stack's own
`loan_onboarding`.** Found the hard way in P16-4: these tests' per-test
cleanup fixtures do a real `DELETE FROM applications`/`accounts`/
`customers` against whatever `DATABASE_URL` points at — running the
suite against the same live Postgres a `docker compose up -d app` is
using silently wipes every application/account/customer the live stack
had, mid-session, with no error. (The two databases live in the same
Postgres *container*, both reachable on the host's published `5433`
port — see "Data storage" — so pointing at the wrong one is an easy
mistake, not a hypothetical.) Create the test database once
(`CREATE DATABASE loan_onboarding_test;` then apply `db/schema.sql` to
it) and keep using it for every local unit-test run alongside a running
compose stack.

**A second, related real hazard found in a later session's clean-slate
E2E re-verification: ad hoc `docker exec <container> python3 -c
"asyncio.run(...)"` one-off scripts (used for manual document-service
recovery calls, or just to poke at a module directly) each create a
brand-new `asyncpg` pool via that module's own `_get_pool()` — and
`asyncpg.create_pool()` defaults to `min_size=10`, opening 10 real
connections per call.** Running several such one-off scripts across a
session (this one ran roughly a dozen over its course) can exhaust
Postgres's `max_connections` (100 by default) well before anything
looks obviously wrong — the symptom was every real request, browser-
driven or not, starting to fail with `asyncpg.exceptions.TooManyConnectionsError:
sorry, too many clients already`, including inside a running Temporal
activity (turning an in-progress approval into a genuinely stuck,
`FAILED` workflow — recovered by deleting that one workflow execution
via `temporal workflow delete` and its now-orphaned application row,
not by anything automatic). Each one-off process exiting *should*
release its connections via ordinary TCP teardown, but in practice the
connections lingered long enough to compound across many closely-spaced
invocations. Fixed by restarting `db` (safe — data lives on the
volume, not in the container) plus the app/worker containers whose own
pools were sitting on now-invalid connections after that restart.
**The operating rule this confirms**: prefer the running app/worker
containers' own long-lived pools (drive verification through the real
browser flow, or read state via `psql`/the Mayan REST API/`temporal`
CLI directly) over spinning up fresh one-off Python processes against
this codebase's own modules; if a one-off script is genuinely
necessary, keep it to one at a time and don't let more than a couple
accumulate across a session without restarting `db` in between.
