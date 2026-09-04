# IMPLEMENTATION_PLAN.md

The execution tracker for this POC. `PRD.md` says *what* to build,
`CLAUDE.md` says *how* the architecture works — this file says *in what
order*, *how far along things are*, and *what a session should do next*.
It is the one file a fresh coding-agent session with zero memory of any
prior session should read to know exactly where to resume.

## How a session should use this file

**At the start of every session:**

1. Read `PRD.md` and `CLAUDE.md` in full — don't skip this even if it
   feels redundant with a prior session; you have no memory of that
   session.
2. Read **Current Status** (below) and the **three most recent Session
   Log entries** at the bottom of this file.
3. Find the first unchecked task, in phase order. If a phase has any
   unchecked task, do not start a later phase's tasks, even if they
   look independent — the ordering encodes real dependencies (see each
   phase's "Depends on" line).
4. Before starting a task, re-read its "Depends on" tasks' checkboxes —
   don't trust that a phase being "mostly done" means a specific
   dependency is actually done.

**While working:** follow the **Global definition of done** below for
every task, not just the task-specific acceptance criteria listed under
it — the global list is not repeated per task to keep this file
readable.

**At the end of every session** (whether or not you finished a task):

1. Update task checkboxes — only check a box if its full DoD (global +
   task-specific) actually passed. A partially-done task stays
   unchecked; add a one-line status note directly under it (e.g. `> IN
   PROGRESS: schema + db.py done, service.py not started`) so the next
   session doesn't have to re-discover this by reading diffs.
2. Update **Current Status** to name the phase/task a new session
   should look at first.
3. Append a **Session Log** entry (newest at the top of that section) —
   see its format below. This is the narrative thread; checkboxes alone
   lose the *why* behind any deviation from the plan.
4. If you made a real architectural decision not already covered by
   `CLAUDE.md` (not just "which order to write functions in" — an
   actual boundary/contract/design choice), **update `CLAUDE.md` itself**
   before ending the session. Don't leave a decision only in a commit
   message or the Session Log — those are for narrative and history,
   `CLAUDE.md` is the durable reference the next session's first read
   depends on.
5. If you hit a product ambiguity `PRD.md` doesn't resolve, add it to
   **Decisions Needed** below rather than guessing silently — pick a
   reasonable default, note it there as "assumed: X, needs
   confirmation," and keep moving. Don't block a whole session on it.
6. Commit. One commit per completed task where practical, message
   prefixed with the task id (e.g. `P6-4: application.service.create_application + _wait_until`).
   A task id in the commit message is what makes `git log` itself
   another way to answer "what actually happened in phase 6."

## Global definition of done

Every task below, in addition to its own listed acceptance criteria:

- [ ] Code written in the location `CLAUDE.md`'s module layout specifies
      — don't invent a new file location without updating `CLAUDE.md`.
- [ ] Unit tests written and passing (`tests/unit/`, no live services).
- [ ] Where the task says "integration-verify," the real local stack is
      actually running and the verification step was actually performed
      — not assumed to work because the unit tests pass.
- [ ] No `CLAUDE.md` module-boundary rule violated (check the dependency
      graph before adding an import — see `CLAUDE.md`'s "Module
      dependency graph").
- [ ] Checkbox ticked, Current Status updated, Session Log entry
      appended, changes committed.

## Current Status

**Phases 0 through 10 — done.** Phase 7 was a real milestone: everything
below the two BFFs (customer/account/application/document/workflow,
the worker processes, the full approve/reject/escalate/resubmit/cancel
lifecycle) now works end to end against the real local stack, with a
real bug found and fixed along the way (see P7-3's note and `CLAUDE.md`'s
updated provisioning-sequence section). Phase 8 added `import-linter`,
confirmed clean against the current codebase and confirmed to actually
fail CI on a real violation (P8-2's note). Phase 9 built the realm
import and all three `bff_backoffice/` auth modules
(`keycloak_auth.py`/`session_store.py`/`keycloak_session.py`), verified
against a real local Keycloak + Redis. **Phase 10 built `app.py` and
`bff_backoffice/routes.py` — the first real, browser-usable screen in
this whole project** — and walked its entire DoD checklist through an
actual browser against the real stack (real Keycloak login, real
Postgres/Temporal/Mayan), not curl simulation. **Phase 11 built
`bff_customer/` (`identity.py`, `routes.py`, and its templates) — the
public-facing mobile self-service flow** — and walked its own DoD
through a genuine 375×812 mobile-viewport-emulated browser session
against the real stack, covering all three product types and every
terminal outcome (approved directly, approved via manager escalation,
rejected, cancelled), plus the visibility invariant and document
preview. One real, corrected assumption from an earlier draft of
`CLAUDE.md` along the way: the customer identity cookie is its own
dedicated signed cookie (`itsdangerous`, `CUSTOMER_SESSION_SECRET_KEY`),
not a slot inside `bff_backoffice`'s shared `SessionMiddleware` session
— see P11-1's note. **Phase 12 (End-to-End Verification & Polish) is
now also complete — all 12 phases of this plan are done.** Added
`docker-compose.yml`'s `app` service (the single web process never had
a Compose service until now) and, along the way, found and fixed a
real, previously-invisible bug only surfacing once the whole stack ran
fully containerized for the first time: a Keycloak issuer mismatch
between this app's internal server-to-server calls and the
browser-facing/token-`iss` value, fixed with a new
`KEYCLOAK_PUBLIC_ISSUER` env var plus pinning Keycloak's own hostname
(`KC_HOSTNAME`) — see P12-3's note for the full mechanism. Walked every
PRD §10 success criterion with fresh, live verification against that
rebuilt stack (P12-1) — including a genuine `temporal workflow
cancel`/`terminate` issued directly via the Temporal CLI (not through
the app), and a real mixed bulk-decision batch (one already-decided row
+ one still-eligible row in the same bulk action, confirming partial
success) — and corrected a second real documentation bug found in the
same pass: `db/schema.sql`/`PRD.md` both claimed a workflow-reconciliation
mechanism exists that a full-codebase grep confirmed was never built
(P12-2's note). Phases 0–12 are complete; remaining Known Gaps in
`CLAUDE.md` are genuine, accepted, and not further work this plan calls
for.

**Phase 13 (Human-readable primary keys + account-to-application
direction flip) added after Phase 12 closed** — a design change
requested and confirmed by the user, not part of the original build-out.
`CLAUDE.md` and `PRD.md` were updated first to describe the target
design, then **P13-1 through P13-6 are now done**: the new `idgen/` leaf
module exists and is wired into `pyproject.toml`'s import-linter
contracts; `db/schema.sql` moved every primary key to `TEXT` with no
default, added `accounts.application_id` (`NOT NULL UNIQUE`), dropped
`applications.account_id` and `chk_approved_has_account`; `customer/`,
`account/`, and `application/` (`models.py`/`db.py`/`service.py`/
`activities.py`) all moved off `UUID` onto plain `str` ids with
insert-time PK-collision retry in `customer/db.py`/`account/db.py`;
`bff_backoffice/routes.py` and `bff_customer/routes.py` dropped every
`UUID`/`uuid.uuid4()` use for these three entity types. Full unit suite
(`pytest tests/unit`, 191 tests) and `lint-imports` both pass against a
real local Postgres with the new schema applied. **Verified live, not
just via unit tests**: rebuilt the `app`/`worker-workflow`/
`worker-activity` images and walked a full personal_loan application
through a real browser — customer wizard submission (id minted as
`app-628882567`), all four document uploads, underwriter Keycloak login
and Approve — then confirmed via `psql` that `customers`/`accounts`/
`applications` all used the new prefixed-id format and
`accounts.application_id` correctly pointed back at the approving
application. **P13-7 is now also done — all 7 of Phase 13's tasks are
complete.** A full `docker compose down -v && docker compose up -d
--build` from genuinely empty volumes plus the complete standard
live-verification sweep (all three product types, direct approve,
escalation-then-manager-approve, reject, cancel, more-info-then-resubmit)
all passed against the new id scheme. **One genuine, pre-existing bug
(not caused by Phase 13) was found live during this sweep, then fixed
in a same-day follow-up** (not its own numbered phase — a bug fix, not
new build-out) — see `CLAUDE.md`'s Known Gaps, the bullet just under
the still-open active-account-race-window one: `check_decision_allowed`
was missing a real active-account conflict (not just racing it) when
two applications share an `applicant_identifier` and one is approved
before the other, leaving the second stuck at `PENDING_UNDERWRITING`
with its Temporal workflow `FAILED`. Fixed by resolving via
`customer.service.find_by_identifier` when `customer_id` is `NULL`
instead of trusting that column alone; re-verified live against the
exact repro (a fresh pair of sibling `personal_loan` applications) —
the second Approve now returns a clean blocking error instead of
reaching the workflow at all. New regression test:
`tests/unit/application/test_service.py`'s
`test_check_decision_allowed_resolves_by_identifier_when_customer_id_null_but_customer_exists`.
**This plan's own backlog is empty again** — remaining work is only
the Known Gaps in `CLAUDE.md` (the race-window variant of this same
area is still open, deliberately — see that bullet), same as after
Phase 12.

**Phase 14 (Returning-customer profile refresh & ID reuse) added after
Phase 13 closed** — a product enhancement brainstormed and confirmed
with the user, not part of the original build-out. **All five tasks
(P14-1 through P14-5) are now done.** `customer/`'s `get_or_create`/
`update_profile` seed and refresh a customer's profile from each
approved application's own submitted fields; `document/`'s
`has_id_photo`/`check_completeness(exclude_categories=...)`/rewritten
`promote_government_id_to_customer_photo` enforce exactly one current
`id_photo` per customer; `application/service.py`'s
`create_application` gained `reuse_existing_id_photo`; `bff_customer`'s
new-application wizard prefills a returning customer's form and offers
a real "reuse the Government ID on file" choice, defaulting to reuse.
Live-verified across three real applications under one identifier (see
P14-4's and P14-5's own notes below for the full verification detail) —
no prefill/no reuse option for a brand-new applicant, correct prefill +
reuse-accepted (no duplicate document created) on the second, and
correct prefill + fresh-upload-chosen (the old document's `id_photo`
tag correctly superseded) on the third. Full unit suite (225 tests) and
`lint-imports` both green; the standard live decision-path sweep (all
three product types, direct approve, escalation-then-manager-approve,
reject, more-info-then-resubmit) confirmed nothing in Phases 0–13
regressed. `CLAUDE.md` and `PRD.md` updated from "planned"/"not built
yet" to reflect built status throughout.

**Phase 15 (Document/database reconciliation) added after Phase 14
closed** — found live, not brainstormed: Postgres's `customers`/
`accounts`/`applications` tables were cleared by something outside this
app entirely while Mayan's documents were unaffected, leaving real
orphaned documents with no way for the codebase to detect it.
`CLAUDE.md`'s "Document/database reconciliation" section describes the
target design (a new `reconcile.py` composition root, `--report`/`--fix`
modes) and the deliberate reconciliation-vs-cascade-on-delete split —
cascade-on-delete is out of scope for this phase, since no delete
operation exists yet for `customer`/`account`/`application` at all.
**All four tasks (P15-1 through P15-4) are now done.**
`document.service.list_all_documents()` added; `reconcile.py`'s
`scan()` distinguishes orphaned documents (primary owner — `application_id`
or `account_id` — gone) from stale `customer_id` tags (secondary
reference only, document itself still validly owned); `--fix` trashes
orphans and strips stale tags, rebuilding the index once. Live-verified
against the real, already-orphaned state this session had itself
observed (27 real documents, not synthetic) plus a deliberately
constructed stale-tag case (a real approval, then `DELETE FROM customers`
for just that one row) — `--report` correctly identified every case,
`--fix` correctly resolved all of them, confirmed via the Mayan REST
API and both indexes' document counts. Full unit suite (238 tests) and
`lint-imports` both green.

**Phase 16 (Document metadata assignment lifecycle) added after Phase
15 closed** — a product design brainstormed and confirmed with the
user, not part of the original build-out. `CLAUDE.md`'s new "Document
metadata assignment lifecycle" section (written first, per this
project's own convention) describes the four confirmed rules: uploads
always get `application_id`; uploads from a resolved returning customer
also get `customer_id` immediately; approval tags `account_id` +
`customer_id` onto *every* document under the application (not just
Government ID), including the generated Welcome Letter; rejected/
cancelled/pending applications never get `account_id` (already true by
construction). **This was a documentation-only session, per explicit
user instruction — none of Phase 16's four tasks (P16-1 through P16-4)
are implemented yet.** Start at P16-1.

**Phase 16 is now fully built and live-verified — all four tasks
(P16-1 through P16-4) done.** `document/service.py`'s `upload(...)`
gained an optional `customer_id` param, `generate_welcome_letter(...)`
now attaches it too, and a new `tag_application_documents(...)` tags
every document under an approved application; `application/activities.py`'s
`persist_decision` calls it inside the existing provisioning block, and
`bff_customer/routes.py`'s two upload routes pass `customer_id` through.
**Two real, previously-undetected bugs were found and fixed during
P16-4's live verification, neither caught by the unit suite** — see
`CLAUDE.md`'s "Document metadata assignment lifecycle" for full detail:
(1) Mayan rejected the new attaches with a 400 because `account_id`/
`customer_id` had never been associated with the "Application
Document"/"Account Document" types respectively — fixed in
`scripts/setup_document_hierarchy.sh` and live against the running
instance; (2) `tag_application_documents` and
`promote_government_id_to_customer_photo` both attach `customer_id` to
the same Government ID document on a fresh promotion, which Mayan
rejects as a duplicate — this file's own earlier draft wrongly called
that "a harmless idempotent no-op"; fixed with a new idempotent
`_set_metadata` helper, and the unit-test fake now replicates Mayan's
real reject-on-duplicate behavior so this class of bug is unit-testable
going forward. Full unit suite (242 tests) and `lint-imports` green —
run against a new dedicated `loan_onboarding_test` database after this
session accidentally wiped the live compose stack's own
`loan_onboarding` database by running the suite against it directly
(see `CLAUDE.md`'s Testing section for the new convention this added).
**Committed** (one combined commit covering all of P16-1 through P16-4
plus both bug fixes) but **not yet pushed** — still needs `git push`
and a CI-green confirmation.

**Document index redesign (ad hoc, requested directly by the user after
Phase 16 closed — not itself a numbered phase)**: replaced the single
`applicant_identifier`-rooted "Loan Onboarding Archive" index with three
separate entity-rooted index templates (Customer Index, Account Index,
Application Index) — see `CLAUDE.md`'s "Document hierarchy" for the
full tree shapes, the two confirmed design decisions (category stays
the leaf; a document with more than one entity id set deliberately
shows up in more than one branch), and the live verification detail.
Deleted the old index and built the three new ones live against the
running Mayan instance via direct API calls; updated
`scripts/setup_document_hierarchy.sh` to build the same three templates
for any future fresh install. **A second, real bug found in the same
pass**: `document/mayan_client.py`'s `rebuild_index()` had a single
constant (`INDEX_TEMPLATE_SLUG = "loan-onboarding-archive"`) hardcoding
which index to rebuild after every metadata attach — deleting that
index without fixing this would have made every document upload in the
whole application fail. Fixed with `INDEX_TEMPLATE_SLUGS` (all three
new slugs) and a new `index_template_ids()`/`rebuild_index()` that
rebuild all three; two new unit tests plus a rewritten pair for the old
single-slug behavior. Live-verified after the fix: rebuilt the
`app`/`worker-activity` images, ran a real `document.service.upload()`
call against the live stack, confirmed it succeeded end-to-end with no
error, and confirmed the uploaded document actually appears in
Application Index's tree via the real `documents_url` listing. Full
unit suite (243 tests) and `lint-imports` green. **Committed
(`01fa111`)**, not yet pushed.

**Follow-up, same day**: user caught a real gap directly ("I don't see
welcome letter under account", "I don't see government id under
customer") — none of the three indexes had a category leaf directly
under the root entity id (only nested under a cross-reference branch).
Added a third sibling branch to all three (`<root> -> <category>`,
direct) live and in `scripts/setup_document_hierarchy.sh`; hit and
resolved a rebuild-timing wrinkle along the way (overlapping `rebuild/`
calls racing each other, not a template bug — see `CLAUDE.md`). Re-
verified live: both reported gaps now resolved. **Not yet committed**
— this follow-up (script + `CLAUDE.md` only) needs its own commit and
push alongside `01fa111`.

*(A session should overwrite this line, not append to it — it always
reflects only the current resume point.)*

## Decisions Needed

*(Empty. Add an entry here — `question`, `assumed default`, `date`,
`raised in task` — whenever a session hits a product ambiguity `PRD.md`
doesn't answer. Remove an entry once a human has actually confirmed the
assumption; until then treat it as provisional, not settled.)*

---

## Phase 0 — Repo & Infra Scaffolding

**Depends on:** nothing. **Unblocks:** everything.

- [x] **P0-1** — `pyproject.toml` at the repo root: `temporalio`,
      `fastapi`, `uvicorn[standard]`, `asyncpg`, `pydantic`,
      `pydantic-settings`, `pyjwt[crypto]`, `jinja2`,
      `python-multipart`, `itsdangerous`, `httpx`, `redis`; dev extras
      `pytest`, `pytest-asyncio`, `respx`. One package,
      `loan_onboarding`, per `CLAUDE.md`'s repo layout.
      DoD: `pip install -e .` succeeds in a clean venv.
      > DONE: setuptools backend (matches `review-approval-temporal`'s
      > own choice, confirmed against its actual `pyproject.toml`).
      > Verified in a throwaway venv, not just written.
- [x] **P0-2** — Empty package skeleton: every directory in `CLAUDE.md`'s
      "Repo layout" tree exists with an `__init__.py`, no logic yet.
      DoD: `python -c "import loan_onboarding"` succeeds.
      > DONE: all 7 module dirs + `bff_customer`/`bff_backoffice`'s
      > `templates/` (`.gitkeep`, not `__init__.py` — not Python
      > packages) + `tests/unit`, `tests/integration`. Verified both in
      > a venv and inside the built Docker image.
- [x] **P0-3** — `Dockerfile` (one image, used by every process — see
      `CLAUDE.md`'s "Deployment").
      DoD: `docker build .` succeeds.
      > DONE: single-stage `python:3.12-slim`, `pip install .`, no
      > `CMD` (compose sets the command per service) — matches
      > `review-approval-temporal`'s own Dockerfile pattern exactly,
      > confirmed by fetching it. `docker build .` run for real, plus a
      > sanity `docker run ... python -c "import loan_onboarding"`
      > against the built image.
- [x] **P0-4** — `docker-compose.yml` skeleton: `db`, `temporal`,
      `temporal-ui`, `keycloak`, `backoffice-redis` services per
      `CLAUDE.md`'s "Docker Compose topology" — **no `app`/`worker`
      services yet**, those come once there's code to run.
      **`mayan-db`/`mayan-redis`/`mayan` deliberately NOT added here** —
      fetching `mayan-edms-customer-archive/docker-compose.yml` for
      real showed its actual service names are `db`/`redis`/`app`, not
      `mayan-db`/`mayan-redis`/`mayan`, and they'd collide with this
      file's own `db`. P5-1's "copy wholesale" needs a rename pass
      (`db`→`mayan-db`, `redis`→`mayan-redis`, `app`→`mayan`, drop the
      `webapp` demo service), documented directly in
      `docker-compose.yml`'s own top comment so P5-1 doesn't have to
      rediscover it.
      DoD: `docker compose up -d db temporal keycloak backoffice-redis`
      brings up four healthy containers (Mayan brought up separately in
      Phase 5, once `document/` needs it).
      > DONE: all 4 brought up for real. `db`/`backoffice-redis` report
      > `(healthy)` via their own healthchecks; `keycloak` confirmed
      > functional via `curl` (HTTP 200 on `/realms/master`) and its
      > startup log; `temporal` confirmed via its auto-setup log
      > completing cleanly. Also verified `db/init/01-init.sh` for
      > real: both `loan_onboarding` and `temporal` databases exist,
      > and `loan_onboarding` has all 3 tables plus the
      > `ux_accounts_customer_active_product_type` partial unique
      > index from `db/schema.sql`. Port 8080 collided with this
      > machine's already-running `mayan-edms-customer-archive` stack —
      > verified with a temporary local port remap, reverted to the
      > standard `8080:8080` before committing (correct for a real
      > environment without that local collision).
- [x] **P0-5** — `.env.example` (every env var referenced anywhere in
      `CLAUDE.md`, even ones not wired up yet) + `.gitignore` (`.env`,
      `__pycache__`, `.venv`, etc.).
      DoD: `.env` copied from example, no secrets committed.
      > DONE: includes `WORKER_MODE`/`LOAN_PRODUCT_TYPE` even though
      > `review-approval-temporal` sets its sibling vars directly in
      > compose instead of `.env.example` — this task's own wording
      > ("every env var ... even ones not wired up yet") calls for it
      > regardless. `.env` copied and confirmed `git check-ignore`'d.
- [x] **P0-6** — CI skeleton (GitHub Actions or equivalent): a workflow
      that at minimum runs `pytest tests/unit` on push. Leave a
      commented-out step for `tests/contract`-equivalent /
      import-linter — those get filled in Phase 8, this just reserves
      the slot so Phase 8 isn't inventing CI structure from scratch.
      DoD: workflow runs (even trivially green) on the first push.
      > DONE: `.github/workflows/ci.yml`, plus `tests/unit/test_smoke.py`
      > (a real package-import test, not a no-op) so `pytest tests/unit`
      > doesn't fail on zero collected tests before Phase 2+ adds real
      > coverage. Pushed as commit `bad9ef9`, confirmed green on GitHub
      > Actions via `gh run watch` (run `33580557909`, `unit-tests` job
      > passed in 17s) — not just verified locally.

---

## Phase 1 — Data Layer

**Depends on:** Phase 0. **Unblocks:** Phases 2, 3, 6.

- [x] **P1-1** — `db/schema.sql`: `customers`, `accounts`, `applications`
      tables per `PRD.md` §9, no foreign keys between them (per
      `CLAUDE.md`'s "Data storage" — deliberate).
      > DONE (pre-existing from an earlier design session, re-verified
      > here rather than assumed): `SELECT ... FROM pg_constraint WHERE
      > contype = 'f'` against a real `loan_onboarding` database
      > returns zero rows — confirmed no FKs exist, not just that none
      > were written in the SQL by hand.
- [x] **P1-2** — `db/init/*.sh`: creates the `loan_onboarding` and
      `temporal` databases in the one `db` Postgres container, applies
      `schema.sql` to `loan_onboarding` only.
      DoD (integration-verify): `docker compose up -d db`, then
      `psql` into both databases and confirm `applications`/`accounts`/
      `customers` exist only in `loan_onboarding`, and `temporal` is
      empty (Temporal's own migrations create its own tables later).
      > DONE: re-verified with **`db` alone** (Phase 0's P0-4 check had
      > brought `db` and `temporal` up together, so `temporal`'s own
      > auto-setup had already populated its database by the time that
      > was checked — not the same thing this task's DoD asks for).
      > With only `db` running: `loan_onboarding` has exactly the 3
      > tables, `temporal` database exists but `\dt` reports "Did not
      > find any relations" — genuinely empty, as required.

---

## Phase 2 — Customer Module

**Depends on:** Phase 1. **Unblocks:** Phase 11 (bff_customer).

- [x] **P2-1** — `customer/models.py`, `customer/db.py` (the only code
      touching the `customers` table).
      > DONE: `db.py`'s `get_or_create` uses `INSERT ... ON CONFLICT
      > (applicant_identifier) DO NOTHING RETURNING *` + a fallback
      > `SELECT`, not a naive find-then-insert — closes the race
      > `CLAUDE.md` already calls out (two concurrent calls for a
      > brand-new identifier must never create two rows). Each domain
      > module owns a lazily-initialized `asyncpg` pool of its own
      > (`_get_pool()`), not a shared one — no shared-pool utility
      > exists anywhere in the architecture, so this keeps each
      > module's `db.py` self-contained.
- [x] **P2-2** — `customer/service.py`:
      `find_by_identifier(applicant_identifier) -> Customer | None`
      (read-only, no side effects), `get_or_create(applicant_identifier)
      -> Customer` (find-or-create, idempotent — see `CLAUDE.md`'s
      "Applying without being a customer yet": this is called only from
      `application/activities.py` on approval, never from
      `bff_customer`), `get(customer_id) -> Customer`.
      DoD: unit tests cover `find_by_identifier` returning `None` for no
      match, and both the find and the create branch of `get_or_create`
      (call it twice with the same identifier, assert the second call
      returns the same `customer_id`, and that no second row was
      created).
      > DONE: `tests/unit/customer/test_service.py`, 7 tests, all
      > passing against a real local Postgres (`docker compose up -d
      > db`) — **a real database, not a mock**, since "no second row
      > was created" is a database-state claim a mock can't verify (see
      > `CLAUDE.md`'s Testing section, updated with this reasoning as a
      > named exception). Added a concurrency test beyond what the DoD
      > literally asks for
      > (`test_get_or_create_concurrent_calls_create_exactly_one_row`,
      > 10 truly-concurrent `asyncio.gather`ed calls) to actually prove
      > the atomic `ON CONFLICT` path, not just the sequential
      > call-it-twice case. Also had to fix a real
      > `pytest-asyncio` config bug caught by running these tests, not
      > assumed away: `asyncio_default_fixture_loop_scope` and
      > `asyncio_default_test_loop_scope` must both be `"session"` and
      > match, or a fixture-created `asyncpg` pool gets used from a
      > different event loop than the test body runs in and asyncpg
      > raises confusing mid-operation errors. Also added a Postgres
      > service container to `.github/workflows/ci.yml` so these tests
      > actually run in CI, not just locally — confirmed green via
      > `gh run watch` (run `33581914340`, `unit-tests` passed in 30s,
      > including "Apply database schema" and "Run unit tests" against
      > the service container), not just verified locally.

---

## Phase 3 — Account Module

**Depends on:** Phase 1. **Unblocks:** Phase 11.
Can run in parallel with Phase 2 if two sessions ever overlap — no
dependency between them.

- [x] **P3-1** — `account/models.py`, `account/db.py` (the only code
      touching the `accounts` table).
      > DONE: same conventions as `customer/db.py` (own lazily-init'd
      > `asyncpg` pool, `Account` dataclass + `from_record`,
      > `AccountNotFound`). `db.create()` deliberately does **not**
      > catch the partial unique index's violation — that's on purpose
      > (see below), not an oversight.
- [x] **P3-2** — `account/service.py`:
      `create_account(customer_id, product_type) -> Account` (always
      creates a new row — no find-or-create; an account is 1:1 with an
      approved application, not 1:1 with a customer, see `CLAUDE.md`'s
      "Applying without being a customer yet"),
      `has_active_account_of_type(customer_id, product_type) -> bool`
      (read-only — the function `application.service.check_decision_allowed`
      calls in Phase 6), `get(account_id) -> Account`.
      DoD: unit test proves calling `create_account` twice for the same
      `customer_id` with **different** `product_type`s returns two
      different `account_id`s (a customer can hold multiple accounts —
      PRD §9.2, revised); a second unit test drives
      `create_account` twice for the same `customer_id` and **same**
      `product_type` and confirms the second call fails against the
      real schema's partial unique index (`db/schema.sql`'s
      `ux_accounts_customer_active_product_type`) — not just that the
      function *would* reject it, the actual constraint firing;
      `has_active_account_of_type` tested both ways (true after one
      `ACTIVE` account of that type exists, false again after it's
      `CLOSED`).
      > DONE: `tests/unit/account/test_service.py`, 7 tests, all
      > passing against a real local Postgres — same deliberate
      > "hits a real database" exception as Phase 2 (already documented
      > in `CLAUDE.md`'s Testing section, no further doc changes needed
      > this time). Confirmed
      > `asyncpg.exceptions.UniqueViolationError` actually raises on
      > the second same-`product_type` call (real constraint, not a
      > simulated one), plus two DoD-adjacent checks: a different
      > `customer_id` with the same `product_type` doesn't collide, and
      > closing an account (raw `UPDATE` in the test, since no
      > `close_account` function exists anywhere in the plan yet) frees
      > the slot for a new `ACTIVE` account of that type — the actual
      > behavior the whole rule exists for, not just the boolean flip.
      > CI already had the Postgres service container from Phase 2, so
      > nothing new needed there — confirmed green via `gh run watch`
      > (run `33582985833`, 52s, all 16 tests).

---

## Phase 4 — Workflow Module (generic orchestration only)

**Depends on:** Phase 0 only (deliberately not Phase 6 — see
`CLAUDE.md`'s "Breaking the cycle": activities are referenced by string
name, so this phase does not need `application/activities.py` to exist).
**Unblocks:** Phase 6 (application/schemas.py asserts against
`task_queues.py`), Phase 7.

- [x] **P4-1** — `workflow/task_queues.py`: `KNOWN_PRODUCT_TYPES =
      ("personal_loan", "auto_loan", "mortgage")` (PRD §6.1),
      `task_queue_for_product_type()`.
      > DONE: matches spec exactly, zero dependency on any other module
      > (confirmed by inspection — no imports beyond nothing).
- [x] **P4-2** — `workflow/workflows.py`: `LoanApplicationWorkflow`.
      States per PRD §6.2. Payload-agnostic (`product_type: str`,
      `payload: dict[str, Any]`, never inspected). `run()` starts by
      calling the `persist_application` activity **by string name**
      (see `CLAUDE.md`'s "Breaking the cycle" — do not import
      `application/` here, not even for a type hint). Two signals:
      `submit_decision(actor_role, decision, actor_name, comment)` and
      `resubmit(payload)`. `_claim_final()`-style synchronous guard
      against racing terminal transitions. `except
      asyncio.CancelledError` around the decision wait, running the
      terminal-persist activity (`persist_decision`, by name, with
      `decision="CANCELLED"`, `closed_by="temporal-admin"`) and not
      re-raising.
      DoD: unit tests via `temporalio.testing.WorkflowEnvironment`, with
      small **fake** activities registered under the exact string names
      `workflows.py` calls (`persist_application`, `persist_decision`,
      `persist_resubmit`) — these fakes just record their calls, they
      don't touch any real table. Cover: happy path to `APPROVED` below
      threshold, happy path escalating to `PENDING_MANAGER_APPROVAL`
      then `APPROVED`, `REJECT` at each stage, `REQUEST_MORE_INFO` →
      `resubmit` → back to `PENDING_UNDERWRITING`, `CANCEL` from each
      non-terminal state, a decision from the wrong `actor_role` for the
      current state is rejected, a native `CancelledError` lands on
      `CANCELLED` via the fake `persist_decision`, and two
      near-simultaneous terminal transitions (a `submit_decision` racing
      a forced `CancelledError`) only ever result in one terminal
      write — this is what actually tests `_claim_final()`, not just
      that it exists.
      > DONE: `tests/unit/workflow/test_workflows.py`, 11 tests, all via
      > `WorkflowEnvironment.start_time_skipping()`, no real Temporal
      > server or Postgres. Two real implementation traps found and
      > fixed by actually running these, not assumed away: (1) fake
      > activities' `inp` param needs an explicit type hint
      > (`PersistApplicationInput`, etc.) or Temporal's default data
      > converter hands back a plain `dict` instead of the dataclass;
      > (2) a signal only confirms Temporal *accepted* it, not that the
      > workflow finished processing it — an assertion immediately after
      > `await handle.signal(...)` (a status query, or a second signal
      > that depends on the first having landed) is a real race, not a
      > hypothetical one; fixed with small `_wait_for_status`/
      > `_wait_for_call_count` polling helpers, the same shape as
      > `application/service.py`'s own planned `_wait_until()` (Phase
      > 6). The "two near-simultaneous terminal transitions" DoD line
      > is satisfied via two concurrent `submit_decision` signals
      > (APPROVE vs REJECT) rather than racing a real
      > `handle.cancel()` against an in-flight signal — the exact
      > delivery timing of a Temporal-level cancel relative to an
      > in-flight signal handler's activity call isn't something a test
      > can control deterministically, and both are terminal transitions
      > guarded by the same `_busy` flag, so either race proves the same
      > invariant. The native-cancel path is covered separately
      > (`test_native_cancel_lands_on_cancelled_via_fake_persist_decision`),
      > deterministically, with no signal in flight.
- [x] **P4-3** — `workflow/worker.py`: bootstrap function
      `run_worker(activities: list[Callable], worker_mode: str,
      product_type: str | None)` — takes the concrete activity list as
      a parameter (supplied later by `worker_main.py`, Phase 7), reads
      `WORKER_MODE` (`both`/`workflow`/`activity`) and
      `LOAN_PRODUCT_TYPE` env vars per `CLAUDE.md`.
      DoD: unit test instantiates a `Worker` with a list of two or three
      trivial fake activities and confirms it starts polling without
      error against a `WorkflowEnvironment`'s local server (don't need
      real activities to prove the bootstrap logic itself works).
      > DONE: `run_worker()` itself reads `TEMPORAL_HOST`/
      > `TEMPORAL_NAMESPACE` only as a fallback when no `client` is
      > injected (production path); the actual worker-construction logic
      > is factored into `_build_workers()` so tests can hand it a
      > `WorkflowEnvironment`'s client directly instead of connecting to
      > a real server. `tests/unit/workflow/test_worker.py`, 7 tests:
      > `async with worker:` entering/exiting cleanly is what "starts
      > polling without error" means here, covered for `both`/
      > `workflow`/`activity` modes and both an explicit `product_type`
      > and `None` (polls every `KNOWN_PRODUCT_TYPES`), plus the two
      > `ValueError` validation paths.
- [x] **P4-4** — `workflow/service.py`: `start_workflow(application_id,
      product_type, payload, amount, applicant_identifier, customer_id)
      -> workflow_id` (`amount`/`applicant_identifier`/`customer_id` are
      named arguments, not read out of `payload` — the workflow needs
      `amount` for the PRD §6.3 escalation check, and forwards
      `applicant_identifier`/`customer_id` untouched to the
      `persist_application` activity), `signal_decision(...)`,
      `signal_resubmit(...)`, `bulk_signal_decision(workflow_ids,
      decision, actor_name, comment)` (fan-out via `asyncio.gather()`,
      cap `_MAX_BULK_SIZE = 50`, catch only the exception types
      `signal_decision` documents raising, into a per-item
      `BulkActionResult`).
      DoD (integration-verify): `docker compose up -d temporal`, run
      these functions against a real local Temporal server (not just
      `WorkflowEnvironment`) at least once manually, confirm the
      execution appears in Temporal Web UI at `localhost:8233`.
      > DONE: real gap found in `CLAUDE.md`'s own documented signature
      > while implementing this — `start_workflow` was missing
      > `applicant_name`/`applicant_email`/`applicant_phone` (no other
      > path gets them to `persist_application`, since `payload` stays
      > product-fields-only) and `bulk_signal_decision` was missing
      > `actor_role` (needed for the same reason the single-item
      > `signal_decision` needs it). Both fixed in the actual signatures
      > and in `CLAUDE.md` itself (marked "Corrected from an earlier
      > draft," same convention the file already uses elsewhere).
      > `docker compose up -d temporal temporal-ui`, then ran
      > `start_workflow` → `signal_decision` (Approve) end-to-end against
      > the real server with fake `persist_*` activities (Phase 6 doesn't
      > exist yet); confirmed `WORKFLOW_EXECUTION_STATUS_COMPLETED` via
      > Temporal Web UI's own API at `localhost:8233` (the same data
      > source the UI itself renders — `curl
      > localhost:8233/api/v1/namespaces/default/workflows?query=...`),
      > not just asserted in the driving script. Separately also
      > exercised `signal_resubmit` (REQUEST_MORE_INFO → resubmit →
      > APPROVE) and `bulk_signal_decision` (3 concurrent REJECTs, all
      > `ok=True`, plus one bogus workflow id correctly surfacing as a
      > per-item `ok=False`) against the same real server — more than the
      > DoD's literal "at least once," done because "run these functions"
      > reads as all four, not just `start_workflow`. Verification
      > scripts were throwaway (scratchpad, not committed).
      > `tests/unit/workflow/test_service.py` additionally covers the
      > synchronous validation paths (`_validate_bulk_ids`, unknown
      > `actor_role`/`decision`) with no server needed.

---

## Phase 5 — Document Module

**Depends on:** Phase 0. **Unblocks:** Phase 6.
Independent of Phases 2–4 — can run in parallel if sessions overlap.

- [x] **P5-1** — Add `mayan-db`, `mayan-redis`, `mayan` to
      `docker-compose.yml` (copy wholesale from
      `mayan-edms-customer-archive/docker-compose.yml`).
      DoD: `docker compose up -d mayan`, log in at `localhost:8000`.
      > DONE: copied that project's `db`/`redis`/`app` services,
      > renamed per P0-4's already-documented rename pass
      > (`mayan-db`/`mayan-redis`/`mayan`), `webapp` (demo front end)
      > dropped — not needed, `document/` talks to Mayan's API
      > directly. Added `MAYAN_POSTGRES_PASSWORD` to `.env.example`
      > (mayan-db's own Postgres password, separate container/database
      > from this project's `db`). Verified for real: `docker compose
      > up -d mayan` (transitively starts `mayan-db`/`mayan-redis`),
      > `/authentication/login/` returns HTTP 200, and
      > `/api/v4/auth/token/obtain/` with the `.env.example` service
      > account credentials (`admin`/`changeme`) returns a real token —
      > confirms the container, its Postgres, and its default admin
      > account all actually work, not just that the container starts.
- [x] **P5-2** — `scripts/setup_document_hierarchy.sh`: creates the
      metadata types (`applicant_identifier`, `application_id`,
      `account_id`, `customer_id`, `category`), document types, and the
      full Index Template per `CLAUDE.md`'s "Document hierarchy" — the
      submission-gate branch (`applicant_identifier -> application_id ->
      category`), the `id_photo` leaf directly under
      `applicant_identifier` (via `customer_id` metadata, no
      `application_id` in that leaf's condition), and the
      `applicant_identifier -> account_id -> {Welcome Letter, Consent}`
      branch. **Read
      `mayan-edms-customer-archive/docs/document-hierarchy-setup.md`
      before writing this** — all five gotchas apply.
      DoD (integration-verify): run the script against the fresh
      instance from P5-1; manually upload one test document per level,
      confirm the tree renders correctly in Mayan's UI, wait the ~10-15s
      for the async index rebuild before checking. **Also specifically
      verify the `id_photo` multi-membership assumption**
      (`CLAUDE.md`'s flagged, not-yet-confirmed note in "Document
      hierarchy"): upload one document with both `application_id`+
      `category=Government ID` metadata AND `customer_id` metadata set,
      confirm it appears under *both*
      `<application_id>/Government ID` *and*
      `<applicant_identifier>/id_photo` in the rendered tree. If it
      doesn't, stop and record the actual behavior in `CLAUDE.md` before
      P6-3/P5-5 build `promote_government_id_to_customer_photo` against
      an assumption that turned out false.
      > DONE: two document types, not three, since `applicant_identifier`
      > (not `customer_id`) is the top-level branch key here — "Application
      > Document" (`applicant_identifier`/`application_id`/`category`
      > required, `customer_id` optional/attached later) and "Account
      > Document" (`applicant_identifier`/`account_id`/`category`
      > required); this partition is what lets every leaf condition stay
      > a single check (an Account Document has no `application_id` field
      > at all, so `metadata_value_of.application_id` is naturally empty
      > for it, no extra `not account_id` clause needed on the
      > application-branch leaf). Ran against the real P5-1 instance and
      > verified with a throwaway Python script (scratchpad, not
      > committed): uploaded a real 1-page PDF as a Government ID
      > (Application Document), rebuilt, confirmed it landed under
      > `<applicant_identifier>/<application_id>/Government ID`; uploaded
      > a second real PDF as a Welcome Letter (Account Document), rebuilt,
      > confirmed it landed under `<applicant_identifier>/<account_id>/Welcome
      > Letter`; then attached `customer_id` to the *first* document
      > (simulating promotion), rebuilt again, and confirmed via
      > `GET /index_instances/<id>/nodes/.../documents/` that the exact
      > same document id now appears under **both**
      > `Government ID` *and* `id_photo` simultaneously — the flagged
      > multi-membership assumption in `CLAUDE.md`'s "Document hierarchy"
      > empirically confirmed, not just source-read; `CLAUDE.md` updated
      > in place to record this (no fallback-to-copy path needed).
      > Correctly used `/index_instances/<id>/nodes/` (the rendered
      > instance tree, with real `value`s and `documents_url`s) rather
      > than `/index_templates/<id>/nodes/` (the template's static
      > expression definitions) for verification — an easy mix-up since
      > both return a similarly-shaped `results` array; only the instance
      > endpoint has actual per-document node data. Also observed the
      > expected cosmetic "None" group nodes (gotcha #1) appearing
      > alongside real siblings once documents of both types existed —
      > harmless, `link_documents: false` on all of them, exactly as the
      > reference project's docs predict.
- [x] **P5-3** — `document/mayan_client.py`: thin async wrapper,
      `Accept: application/json` default header (do not omit — see
      `CLAUDE.md`), service-account token obtained lazily and refreshed
      on 401, metadata/document-type id lookups cached for process
      lifetime.
      > DONE: modeled closely on `mayan-edms-customer-archive`'s own
      > `mayan_client.py` (fetched and read directly), adapted for this
      > project's five metadata fields (`applicant_identifier`,
      > `application_id`, `account_id`, `customer_id`, `category`) and
      > two document types (`Application Document`, `Account Document`)
      > instead of that project's three. `upload_file`'s `action_name`
      > is a parameter (default `"replace"`), not hardcoded, so P5-5's
      > `upload_consent` can pass `"new"` once that value is confirmed
      > against Mayan's API rather than needing a second upload method.
      > `tests/unit/document/test_mayan_client.py`, 9 tests, `respx`-mocked
      > at the HTTP transport layer (no live Mayan needed for this
      > module's own client tests, unlike `customer`/`account`'s
      > deliberate real-Postgres exception) — lazy token fetch + caching,
      > 401-triggered re-fetch and retry, paginated id-map loading and
      > caching, `index_template_id` found-by-slug and
      > not-found-raises, `upload_file`'s multipart body and
      > `action_name` default, `relative_path` prefix-stripping. All 36
      > `tests/unit/` tests pass (excluding `customer`/`account`, same
      > pre-existing local port-5432 collision every prior session has
      > hit — unrelated to this task).
- [x] **P5-4** — `document/service.py`: `upload(applicant_identifier,
      application_id, category, file)` (create → upload with
      `action_name=replace` → attach metadata → rebuild index) — no
      `customer_id`/`account_id` param; `document/` is a leaf module and
      neither exists yet at upload time under the account-on-approval
      model (see `CLAUDE.md`), `list_documents(application_id)`,
      `check_completeness(application_id, product_type) -> list[str]`
      (missing categories, per PRD §6.4's per-product required-category
      table), `preview(application_id, document_id)` (streams from
      Mayan). **`check_completeness` and `list_documents` must query
      Mayan's document/metadata search API directly (filtered on
      `application_id` + `category`), never read the Index Template
      tree** — the tree's rebuild is async (gotcha #2) but metadata
      attachment isn't, and `check_completeness` runs synchronously
      right after the customer's last upload (`application.service.create_application`)
      — reading the tree here risks a false "still missing" result from
      pure reindex lag, not an actual missing document. See `CLAUDE.md`'s
      "Document hierarchy" for the full reasoning.
      DoD (integration-verify): upload a real file (not a hand-typed
      stub — verify with `file <path>` that it has a real page count,
      per gotcha #4), confirm `check_completeness` correctly reports
      missing vs. satisfied for a partially-uploaded application, **and
      specifically confirm `check_completeness` returns satisfied
      immediately after the final required upload — before waiting out
      the ~10-15s index-rebuild window** (proves it isn't reading the
      index tree).
      > DONE: `REQUIRED_CATEGORIES` (PRD §6.4's table) hardcoded in
      > `document/service.py` as a plain dict, not imported from
      > `workflow.task_queues` — `document/` never imports `workflow/`,
      > even for a shared registry, so this is a deliberate duplication
      > of the three product-type strings with no import-time assert
      > tying them together (documented in the module's own docstring as
      > a latent, currently-unflagged drift risk). `_documents_matching`
      > (fetch every document, fetch each candidate's real metadata,
      > filter exactly in Python) reused from
      > `mayan-edms-customer-archive`'s identical `documents_service.py`
      > pattern — necessary because Mayan's advanced-search metadata
      > params don't AND across fields (verified there, re-confirmed by
      > inspection of the same endpoint here). 18
      > `tests/unit/document/test_service.py` tests against a
      > `FakeMayanClient` double (this module's own dependency
      > boundary), plus a real end-to-end run against the live P5-1
      > Mayan instance via a throwaway script (scratchpad, not
      > committed): uploaded three genuinely valid one-page PDFs
      > (confirmed with `file`) one category at a time, watched
      > `check_completeness`'s missing-list shrink correctly each time,
      > then uploaded the final required category and confirmed
      > `check_completeness` returned `[]` in ~1.4s — nowhere near the
      > 10-15s index-rebuild window, proving it reads Mayan's metadata
      > search directly rather than the index tree. **Real, unplanned
      > finding from this same verification run**: Mayan's default REST
      > API rate limit (20 req/sec, confirmed by reading
      > `mayan/apps/rest_api/literals.py`) triggered a genuine `429` under
      > this realistic upload-then-check-completeness sequence, not a
      > contrived stress test — fixed by adding a bounded
      > `Retry-After`-honoring retry to `mayan_client.py`'s `_request`
      > (P5-3's file, amended here since the gap only showed up once
      > `service.py` actually drove it end to end); documented as a new
      > entry in `CLAUDE.md`'s "Known gaps."
- [x] **P5-5** — `document/service.py`, part 2 — the managed-document
      functions (PRD §6.5): `promote_government_id_to_customer_photo(application_id,
      customer_id)` (re-tags the existing Government ID document with
      `customer_id` metadata, rebuilds index — does not fetch/re-upload
      the file), `generate_welcome_letter(account_id, customer_id,
      applicant_name, product_type, amount) -> DocumentRef` (renders a
      simple templated PDF, uploads it tagged to `account_id` —
      plain-argument signature only, no `application/`/`customer/`/
      `account/` imports), `upload_consent(account_id, file) ->
      DocumentRef` (finds the account's existing "consent" document if
      any and uploads a new **file version** of it via Mayan's own
      versioning, `action_name=new`* rather than `replace` — creates
      the document first if none exists yet; *confirm the exact
      `action_name` value against Mayan's API during this task, same
      "don't guess a string ID" caution as gotcha #3),
      `list_customer_documents(customer_id)`,
      `list_account_documents(account_id)`.
      DoD (integration-verify): call `upload_consent` twice for the same
      `account_id` with two different files, confirm Mayan shows **one**
      document with **two versions** (not two documents) when inspected
      directly; call `promote_government_id_to_customer_photo` and
      confirm no new document was created (same `document_id` as the
      original Government ID upload, just additional metadata).
      > DONE: **`action_name="new"` (this file's own placeholder,
      > flagged "confirm during this task") turned out not to exist at
      > all.** Read Mayan's actual
      > `documents/document_file_actions.py` directly: only three
      > registered `DocumentFileAction` backends exist —
      > `append`/`keep`/`replace`. Confirmed empirically too (`curl`
      > against a live document): POSTing to
      > `/documents/<EXISTING id>/files/` a *second* time with
      > `action_name="replace"` (the same value as the first upload)
      > creates a new `DocumentFile` **and** a new `DocumentVersion`
      > under the *same* document id — Mayan's versioning comes from
      > re-targeting an existing document id, not from a distinct action
      > name. `upload_consent` and `mayan_client.upload_file`'s docstring
      > both corrected to reflect this (`action_name="replace"` on every
      > call, first or subsequent). `promote_government_id_to_customer_photo`
      > verified against the real instance: document count before/after
      > promotion unchanged, and `list_customer_documents` returns the
      > *same* `document_id` as the original Government ID upload.
      > `upload_consent` verified against the real instance: two calls
      > with two different files both resolved to the same
      > `document_id`, and `list_account_documents` showed exactly one
      > `Consent`-category entry, not two. All of P5-5's verification ran
      > in the same combined throwaway script as P5-4's (scratchpad, not
      > committed) — see P5-4's note for the shared 429-retry finding
      > this run also surfaced. Unit coverage lives in the same
      > `tests/unit/document/test_service.py` as P5-4 (one file, per
      > `CLAUDE.md`'s layout — both tasks land in `document/service.py`).

---

## Phase 6 — Application Module

**Depends on:** Phases 1, 2, 3, 4, 5 (specifically P5-5, not just
Phase 5's earlier tasks). **Unblocks:** Phase 7. **Phases 2 and 3 are
new dependencies vs. the original ordering** — `application/
activities.py`'s `persist_decision` now calls `customer.service` and
`account.service` on approval (see `CLAUDE.md`'s "Applying without
being a customer yet"), so P6-3 can't be written until both exist.

- [x] **P6-1** — `application/models.py`, `application/db.py` (the only
      code touching the `applications` table).
      > DONE: `db.py` is deliberately a thin data-access layer --
      > `insert`/`update_decision`/`update_resubmission` take
      > already-resolved column values; the branching over *which*
      > columns matter for a given decision (underwriter vs manager vs
      > neither, for CANCELLED) lives in `application/activities.py`
      > (P6-3), not here. `update_decision` uses `COALESCE($n, column)`
      > for every optional column so a caller can update just the
      > columns relevant to one decision without clobbering others
      > (verified directly: a manager decision after an earlier
      > underwriter escalation leaves the underwriter columns intact).
      > Registered a `jsonb` type codec on the connection pool
      > (`asyncpg` doesn't serialize dict<->jsonb on its own) so
      > `payload` round-trips as a plain Python dict everywhere.
      > **Real bug caught by actually running these tests against
      > Postgres, not by reasoning about the SQL**: the first draft used
      > `COALESCE($11, timezone('utc', now()))` for `updated_at`'s
      > default -- `timezone('utc', now())` evaluates to a *naive*
      > `timestamp`, which forced Postgres to infer `$11` itself as
      > naive `timestamp` too (to match the other `COALESCE` branch),
      > so passing a tz-aware Python `datetime` for the
      > native-Temporal-cancel override raised
      > `asyncpg.exceptions.DataError` ("can't subtract offset-naive and
      > offset-aware datetimes") — fixed by using plain `now()` (already
      > `timestamptz`, matching the column type, no inference mismatch).
      > `tests/unit/application/test_db.py`, 13 tests (jsonb round-trip,
      > insert defaults, `update_decision`'s column-preservation and
      > provisioning-write behavior, the native-cancel `updated_at`
      > override, resubmission, and `list_for_applicant`/`list_by_status`
      > filtering+pagination+counts) — same deliberate real-Postgres
      > exception as `customer`/`account`'s own `db.py` tests. Also
      > incidentally proved `db/schema.sql`'s `chk_approved_has_account`
      > constraint actually fires (two tests originally tried to set
      > `status='APPROVED'` without an `account_id` and correctly got
      > rejected — fixed the tests, not the constraint, once traced back
      > to what the constraint is for). Verified against a real local
      > Postgres via a temporary port remap (5433, same as every prior
      > phase's workaround for this machine's native Postgres on 5432 --
      > reverted before committing, confirmed zero diff in
      > `docker-compose.yml` afterward).
- [x] **P6-2** — `application/schemas.py`: Pydantic payload model per
      product type (PRD §6.1's field tables), registry keyed by
      `product_type`, `assert` at import time checking this registry's
      keys match `workflow.task_queues.KNOWN_PRODUCT_TYPES` exactly
      (see `CLAUDE.md`'s "Breaking the cycle" — this is the payoff of
      being one process again).
      DoD: a unit test that deliberately desyncs the two registries
      (monkeypatch one) and confirms the assert actually fires — don't
      just trust that it would.
      > DONE: closely modeled on `review-approval-temporal`'s own
      > `workflow/schemas.py` (fetched and read directly) — one Pydantic
      > model per product type (`PersonalLoanPayload`/`AutoLoanPayload`/
      > `MortgagePayload`, PRD §6.1's field tables), a
      > `PRODUCT_TYPE_SCHEMAS` registry, and the import-time
      > `assert set(...) == set(...)` against
      > `workflow.task_queues.KNOWN_PRODUCT_TYPES`. `validate_payload()`
      > uses `model_dump(mode="json")` specifically (not the bare
      > default) so `Decimal` fields serialize to JSON-safe strings
      > before ever reaching `application/db.py`'s `jsonb` codec, which
      > calls plain `json.dumps` and cannot encode a raw `Decimal`.
      > `tests/unit/application/test_schemas.py`, 8 tests — including
      > the DoD's literal ask: `monkeypatch.setattr` on
      > `workflow.task_queues.KNOWN_PRODUCT_TYPES` followed by
      > `importlib.reload(schemas)` inside `pytest.raises(AssertionError)`,
      > confirming the assert actually fires on a real re-import rather
      > than trusting the expression would evaluate correctly — restores
      > the module via a second reload after `monkeypatch.undo()` so
      > later tests still see the real registry. Also had to rescope
      > `tests/unit/application/conftest.py`'s Postgres-cleanup fixture
      > (added in P6-1) from package-level `autouse=True` to an opt-in
      > fixture used only by `test_db.py`'s `pytestmark` — as written it
      > forced every test in the package (including these schema-only,
      > no-I/O tests) to open a real database connection, caught by
      > actually running this file rather than assumed compatible with
      > the P6-1 setup. Verified against a real local Postgres via the
      > same temporary 5433 port remap as P6-1 (reverted before
      > committing, zero diff).
- [x] **P6-3** — `application/activities.py`: `persist_application`,
      `persist_decision`, `persist_resubmit` — `@activity.defn`,
      registered under the same string names `workflow/workflows.py`
      calls (Phase 4). Each writes to `application/db.py` directly (the
      one place in this module allowed to). `persist_decision` handles
      all four decision outcomes in one activity (APPROVE/REJECT/
      REQUEST_MORE_INFO/CANCEL — same columns, different values, same
      reasoning as the reference project's merged `persist_decision`).
      **This file is the one place in `application/` allowed to import
      `customer/` and `account/`** (see `CLAUDE.md`'s "Applying without
      being a customer yet") — it already imports `document/`, so the
      two `document.service` calls below aren't a new edge: when
      `persist_decision` is called with a terminal `APPROVED` status, it
      must (a) check `applications.account_id IS NOT NULL` first and
      skip the entire block below if so (a Temporal retry of an
      already-completed execution — none of these calls are
      independently idempotent), (b) call
      `customer.service.get_or_create(applicant_identifier)` only if
      `customer_id` is still `NULL`, (c) call
      `account.service.create_account(customer_id, product_type)`
      (always — new row every time this path actually runs; **not
      conflict-checked again here** — `application.service.check_decision_allowed`
      (P6-5b, below) already ran before this decision was ever
      signaled, so this call trusts that, with the schema's partial
      unique index as the only backstop against the documented race
      window), (d) call
      `document.service.promote_government_id_to_customer_photo(application_id,
      customer_id)` and
      `document.service.generate_welcome_letter(account_id, customer_id,
      applicant_name, product_type, amount)` (PRD §6.5), (e) write
      status + decision columns + resolved `customer_id`/`account_id`
      together in one update.
      DoD: unit tests call each activity function directly (no Temporal
      needed for this) against a test database, mocking
      `document.service.promote_government_id_to_customer_photo`/
      `generate_welcome_letter` at the function-call level (same
      "mock at the boundary" convention `CLAUDE.md`'s Testing section
      already uses for `document.service`/`workflow.service`), confirm
      the right columns land — **including a test that calls
      `persist_decision` with `APPROVED` twice in a row for the same
      application and asserts only one account gets created, and that
      the two `document.service` calls only happen once** (the
      retry-idempotency case above), and a test confirming a
      `REJECTED`/`CANCELLED` call never touches `customer/`/`account/`
      or these two `document.service` functions at all.
      > DONE: **Real gap found and fixed in `application/db.py`
      > (P6-1) while building this task**: `insert()`'s raw `INSERT`
      > had no protection against a Temporal retry of an
      > already-succeeded `persist_application` execution — a second
      > call with the same `application_id` (the primary key) would
      > have raised a raw `UniqueViolationError` instead of completing
      > idempotently. Fixed with `ON CONFLICT (application_id) DO
      > NOTHING` + a fallback `SELECT`, same pattern
      > `customer/db.py`'s `get_or_create` already uses, and the same
      > idempotency concern `review-approval-temporal`'s own
      > `persist_request` activity handles identically — added a test
      > for it in `test_db.py` too, not just here.
      > `persist_decision` branches on `actor_role` to decide which
      > column set to write (`underwriter_*`/`manager_*`/neither for a
      > customer-initiated `CANCELLED`), and recomputes "now" for
      > `underwriter_decided_at`/`manager_decided_at` on every
      > execution rather than trying to preserve an exact original
      > timestamp across a Temporal retry — deliberately matching
      > `review-approval-temporal`'s own `persist_decision`, which
      > doesn't solve that problem either (its `closed_at` only takes
      > an explicit override for the native-cancel path, same as this
      > project's `decided_at`). `tests/unit/application/test_activities.py`,
      > 9 tests, real Postgres for `application`/`customer`/`account`
      > (same database, same deliberate exception) with
      > `document.service`'s two managed-document calls mocked via
      > `monkeypatch.setattr` on `activities.document_service` — all
      > passed on the first real run, no further bugs found. Covers
      > every DoD point literally: underwriter reject (no provisioning,
      > right columns), underwriter escalation (no provisioning),
      > terminal approve (provisions customer + account, calls both
      > `document.service` functions with the right string-cast ids),
      > approve reusing an already-resolved `customer_id` (asserts
      > `customer.service.get_or_create` is never even called, not just
      > that the result is correct), **approve called twice in a row
      > for the same application — asserts exactly one `accounts` row
      > and exactly one call each to the two `document.service`
      > functions**, cancelled (touches neither column set nor
      > provisioning), native-cancel's explicit `decided_at` override,
      > and resubmit. Verified against a real local Postgres via the
      > same temporary 5433 port remap as P6-1/P6-2 (reverted before
      > committing, zero diff) — ran alongside the full
      > `customer`/`account`/`application` suites together (45 tests)
      > to confirm no cross-module interference.
- [x] **P6-4** — `application/service.py`, part 1:
      `create_application(applicant_identifier, product_type,
      payload, applicant_name, applicant_email, applicant_phone,
      amount)`. **No `customer_id`/`account_id` params** — generates
      `application_id`, resolves `customer_id` via the read-only
      `customer.service.find_by_identifier(applicant_identifier)`
      (`None` if no match — `account_id` is always `None` at creation
      regardless), validates `payload`
      against P6-2's schema, calls `document.service.check_completeness`;
      if categories are missing, returns them without calling
      `workflow.service` at all. If satisfied, calls
      `workflow.service.start_workflow(application_id, product_type,
      payload, amount, applicant_identifier, customer_id)`, then polls via `_wait_until()` (bounded ~50ms/5s,
      always returns the last read even on timeout — see `CLAUDE.md`)
      against `application/db.py`'s own read until the row (written by
      `persist_application` inside the workflow's `run()`) appears.
      DoD (integration-verify): with the real stack up, submit a
      complete application and confirm it lands in
      `PENDING_UNDERWRITING` in Postgres with `customer_id`/`account_id`
      both still `NULL` (assuming a brand-new `applicant_identifier`),
      then submit an incomplete one and confirm no workflow was started
      (check Temporal Web UI — no new execution).
      > DONE: **real architectural gap found and fixed while implementing
      > this task, before writing any code**: this task's own literal
      > spec ("generates `application_id`") gives `create_application` no
      > way to accept a pre-existing id, but `document.service.upload(...)`
      > needs an `application_id` to tag uploads with and Phase 11's own
      > New Application flow is "document upload → review & submit,
      > calling `create_application(...)`" — uploads happening *before*
      > this call, against an id only this call was supposed to mint, is
      > not satisfiable as originally specced. Fixed by making
      > `application_id` an optional parameter (defaults to a fresh
      > `uuid4()` if omitted, used verbatim if given) — `CLAUDE.md`
      > updated in place with the full reasoning, marked "corrected from
      > an earlier draft" per this file's convention. `workflow.service`'s
      > functions take a `client: Client` as their first parameter
      > (dependency-injection style, for testability) rather than owning
      > one internally, so `application/service.py` needed its own
      > lazily-connected module-level Temporal client — added
      > `_get_temporal_client()`, same lazy-singleton shape as every
      > `db.py`'s `_get_pool()`. `_wait_until()` ported directly from
      > `review-approval-temporal`'s `workflow/service.py` (same
      > constants, same "always return the last read, even on timeout"
      > contract), polling `application/db.py`'s own `get()`.
      > `tests/unit/application/test_service.py`, 8 tests — `workflow.service.start_workflow`
      > and `_get_temporal_client` mocked at the function-call boundary
      > (no real Temporal needed), `document.service.check_completeness`
      > mocked the same way (no real Mayan needed), `customer.service`
      > run for real against Postgres. Covers the missing-categories
      > short-circuit, a simulated persist_application commit (via the
      > mocked `start_workflow` inserting the row itself, standing in for
      > what a real worker would do) proving `_wait_until` picks it up,
      > existing-vs-new customer_id resolution, both the
      > provided-`application_id` and generated-`application_id` paths,
      > payload validation failure, and the wait-until-timeout-returns-None
      > edge case with the timeout/interval constants shrunk via
      > monkeypatch for test speed. **Then the DoD's actual
      > integration-verify**, against the complete real local stack (`db`,
      > `temporal`, `mayan`) plus a throwaway ad-hoc worker (scratchpad,
      > not committed — `worker_main.py` doesn't exist until Phase 7, so
      > this wired `workflow.worker.run_worker()` with
      > `application/activities.py`'s three real activities directly, the
      > same shape `worker_main.py` will eventually use): submitted an
      > incomplete application, confirmed all 4 missing categories
      > reported and — via a real `handle.describe()` call against
      > Temporal — that no workflow execution exists for it at all; then
      > uploaded all 4 required real documents under a *pre-minted*
      > `application_id` (exactly the upload-before-submit flow that
      > motivated the fix above) and called `create_application` with
      > that same id, confirming a real `PENDING_UNDERWRITING` row with
      > `customer_id`/`account_id` both `NULL` and a real `RUNNING`
      > Temporal execution. Verified against Postgres via the same
      > temporary 5433 port remap as every other P6 task (reverted before
      > committing, zero diff).
- [x] **P6-5** — `application/service.py`, part 2:
      `resubmit_application(application_id, payload)` — same gate
      re-check, then `workflow.service.signal_resubmit()` against the
      *existing* `workflow_id` (not a new start), same `_wait_until()`
      pattern.
      DoD (integration-verify): drive an application to
      `MORE_INFO_REQUESTED` (via P6-6/Phase 7's decision path once that
      exists — if this task lands before Phase 7's end-to-end
      verification, test this with a direct `workflow.service` signal
      call in the test setup rather than waiting on the BFF), resubmit,
      confirm it's back at `PENDING_UNDERWRITING` on the *same*
      `workflow_id`.
      > DONE: raises `ApplicationNotFound` for an unknown id (a
      > cheap early exit, since a lookup already has to happen to get
      > `product_type`/`workflow_id` for the rest of the function). Reuses
      > `schemas.validate_payload`/`document_service.check_completeness`/
      > `_wait_until` exactly as `create_application` does, just against
      > the stored `product_type` rather than a freshly-submitted one,
      > and waits on `r["payload"] == validated_payload` rather than
      > "any row exists" (same predicate shape
      > `review-approval-temporal`'s own `update_review` uses). 12 unit
      > tests added to `test_service.py` (not-found, missing-categories
      > short-circuit, a simulated `persist_resubmit` commit via the
      > mocked `signal_resubmit`, and payload-validation-against-stored-
      > product-type), plus **the DoD's literal integration-verify**:
      > against the real stack (`db`, `temporal`, `mayan`) plus the same
      > throwaway ad-hoc worker as P6-4, drove a real application to
      > `MORE_INFO_REQUESTED` via a **direct `workflow.service.signal_decision`
      > call** (exactly the DoD's own suggested workaround, since
      > Phase 7/10's real decision UI doesn't exist yet), then called
      > `resubmit_application` for real and confirmed the row landed
      > back at `PENDING_UNDERWRITING` with the payload updated **and
      > the identical `workflow_id`** — proving this signals the
      > existing execution rather than starting a new one. Verified via
      > the same temporary 5433 Postgres port remap as every other P6
      > task (reverted before committing, zero diff).
- [x] **P6-5b** — `application/service.py`, part 2b:
      `check_decision_allowed(application_id, decision) -> list[str]`
      (blocking-reason strings, `[]` if OK — no-op unless `decision ==
      "APPROVE"`, per PRD §9.2's one-active-account-per-product-type
      rule). Resolves the application's `customer_id` (short-circuit to
      `[]` if still `NULL` — a brand-new applicant can't conflict with
      anything), calls the read-only
      `account.service.has_active_account_of_type(customer_id,
      product_type)`. **`bff_backoffice` (Phase 10) must call this
      before `workflow.service.signal_decision(...)`/`bulk_signal_decision(...)`**
      — this task only builds the check itself, not the call site.
      DoD: unit tests cover the short-circuit (`customer_id` still
      `NULL`), the blocked case (an `ACTIVE` account of the same
      `product_type` already exists), the allowed case (no conflicting
      `ACTIVE` account, or only a `CLOSED` one of that type), and that
      `REJECT`/`REQUEST_MORE_INFO`/`CANCELLED` always return `[]`
      without calling `account.service` at all.
      > DONE: 7 unit tests (parametrized over the three non-APPROVE
      > decisions, plus the `customer_id IS NULL` short-circuit) each
      > monkeypatch `account_service.has_active_account_of_type` to
      > *raise* if called at all, rather than just asserting the
      > returned value — proves the short-circuit actually skips the
      > call, not just that it happens to return `[]` anyway. The
      > blocked/allowed cases run against real Postgres (`customer`,
      > `account`, `application` tables together): an `ACTIVE` account
      > of the same `product_type` blocks with a message naming the
      > conflicting type; a `CLOSED` one of the same type, or no
      > account at all, both permit. All 19 of this session's
      > `test_service.py` tests (covering P6-4, P6-5, and P6-5b
      > together, since they share fixtures) verified against the same
      > temporary 5433 Postgres port remap as every other P6 task.
- [x] **P6-6** — `application/service.py`, part 3: `get(application_id)`,
      `list_for_applicant(applicant_identifier, page, ...)`,
      `list_by_status(status, page, ...)`. Paginated, `query_id`-cached
      per the `list-pagination-bulk-actions` skill's pattern (load that
      skill before writing this task) — mint a `query_id` server-side
      for the total count, echo it back, re-verify any client-supplied
      `filter` before trusting a cached count. **`applicant_identifier`,
      not `customer_id`** — this list has to work for an applicant with
      no approved application yet, whose `customer_id` is still `NULL`
      on every row (see `CLAUDE.md`'s "Applying without being a
      customer yet"); `application/` never imports `customer/` for this
      path either way.
      DoD: unit tests cover pagination math (page boundaries, empty
      result set) and that a `query_id` minted for one
      `applicant_identifier` filter is rejected/ignored if reused with a
      different one (the visibility-invariant defense).
      > DONE: loaded the `list-pagination-bulk-actions` skill first, per
      > this task's own instruction — used Part 1's mint-once count-cache
      > pattern exactly (`_query_cache: dict[query_id, (filter, total,
      > expires_at)]`, in-process/module-level, `QUERY_CACHE_TTL_S = 30.0`
      > matching `review-approval-temporal`'s own identical constant and
      > reasoning: a `query_id` minted on one replica is just a cache miss
      > on another, never a wrong answer, since every lookup path
      > degrades to a fresh `COUNT(*)` on a miss). `_lookup_cached_total`
      > checks the cached filter dict against the current call's filter
      > and returns `None` (forcing a real recount) on any mismatch —
      > this is the visibility-invariant defense the DoD asks for, and
      > it's structural (the comparison is unconditional) rather than a
      > special case for `applicant_identifier` specifically, so it also
      > protects `list_by_status`'s `status` filter for free.
      > `list_for_applicant`/`list_by_status`/`get` add zero new imports
      > (`application/` still never imports `customer/` for this path,
      > confirmed by inspection). 10 new unit tests: pagination math
      > across page boundaries (a 5-row set split 2/2/1), an empty result
      > set, a genuinely-reused `query_id` (insert a 4th row between two
      > calls and confirm the cached `total` stays at the stale-but-
      > consistent 3, proving the cache path — not just the recompute
      > path — actually executes), the query_id-from-a-different-filter
      > case explicitly (alice's `query_id` reused for bob's applicant
      > filter recomputes for real rather than returning alice's count),
      > an unknown/expired `query_id` recomputing rather than erroring,
      > `list_by_status` filtering correctly across two different
      > statuses, and page/page_size clamping (`page < 1` → `1`,
      > `page_size` over `_MAX_PAGE_SIZE` → clamped). All 129
      > `tests/unit/` tests (the full suite, not just this package) pass
      > together, verified via the same temporary 5433 Postgres port
      > remap as every other P6 task (reverted before committing, zero
      > diff). **This completes Phase 6.**

---

## Phase 7 — Worker Composition Root & End-to-End Workflow Verification

**Depends on:** Phases 4, 6. **Unblocks:** Phases 9, 10, 11.

- [x] **P7-1** — `worker_main.py`: imports `workflow/`'s `run_worker()`
      bootstrap and `application/activities.py`'s three concrete
      functions, wires them together, reads the same `WORKER_MODE`/
      `LOAN_PRODUCT_TYPE` env vars.
      DoD: `python -m loan_onboarding.worker_main` starts cleanly
      against the real local Temporal server.
      > DONE: mechanical, as expected — this is exactly the ad-hoc
      > wiring every P6 integration-verify script already built by hand
      > (`workflow.worker.run_worker([persist_application,
      > persist_decision, persist_resubmit], ...)`), now the permanent
      > composition root. Verified for real: `python -m
      > loan_onboarding.worker_main` against the real local `temporal`
      > container prints "Worker process started (mode=both), serving
      > product types: ['personal_loan', 'auto_loan', 'mortgage']" and
      > stays running (no crash) — confirmed the process was still alive
      > several seconds later, then killed it cleanly. (Needed
      > `python -u` / unbuffered stdout to actually see the startup
      > print when redirected to a log file for this check — Python
      > buffers stdout by default when it isn't a TTY, not a bug in the
      > module itself, just a manual-verification gotcha worth noting so
      > a future check doesn't mistake buffering for a hang.)
- [x] **P7-2** — Add `worker-workflow`/`worker-activity` (or one
      `worker` service, per `CLAUDE.md`'s Deployment section) to
      `docker-compose.yml`.
      > DONE: two services (not one), mirroring
      > `review-approval-temporal`'s own identical split exactly.
      > `worker-workflow` gets neither `DATABASE_URL` nor Mayan
      > credentials (workflow code is pure/deterministic); `worker-
      > activity` gets both (it runs `application/activities.py`'s real
      > implementations, which write to Postgres and call `document.service`
      > on approval). Verified for real: `docker compose up -d --build
      > worker-workflow worker-activity` builds and starts both against
      > the real local `temporal`/`db` containers, both stayed `Up` with
      > zero restarts after 40+ seconds (no crash-loop). Then went
      > further than "stays up" — drove a real `create_application` call
      > from the host (after uploading real documents to the also-running
      > `mayan` service) and confirmed **the actual Docker Compose
      > `worker-workflow`/`worker-activity` containers** (not the
      > ad-hoc in-process worker every other P6/P7-1 verification used)
      > processed it end to end to a real `PENDING_UNDERWRITING` row —
      > the first verification this session that didn't need a
      > throwaway worker stand-in. Stopped both containers after
      > verifying (this repo's convention is not to leave extra
      > containers running between sessions). Verified via the same
      > temporary 5433 Postgres port remap as every other Phase 6/7
      > task (reverted before committing, zero diff beyond P7-2's own
      > intended additions).
- [x] **P7-3** — **First true end-to-end run**, no UI yet — drive it
      entirely through `application.service` + `workflow.service` calls
      from a script or `pytest` integration test: create a
      `personal_loan` application below the escalation threshold →
      confirm `PENDING_UNDERWRITING` → signal Approve as `underwriter`
      → confirm `APPROVED`, `underwriter_decided_at` set. Repeat for the
      escalation path (amount ≥ threshold → Approve as `underwriter` →
      confirm `PENDING_MANAGER_APPROVAL` → Approve as `manager` →
      confirm `APPROVED`). Repeat for Reject, and for Request-More-Info
      → resubmit → Approve. Repeat for Cancel from a non-terminal state.
      DoD: all of the above pass as `tests/integration/` tests against
      the real stack (`db`, `temporal`, `document_svc`'s Mayan
      dependency not required for this phase — use an application that
      already satisfies the document gate, or stub
      `document.service.check_completeness` to return `[]` for this
      test only), **and** each execution is visually confirmed at least
      once in Temporal Web UI, not just asserted in code.
      > DONE: `tests/integration/test_end_to_end_workflow.py`, a
      > module-scoped in-process worker fixture (same
      > `workflow.worker.run_worker()` + `application/activities.py`
      > wiring `worker_main.py`/P7-2 made permanent, just started
      > in-process for this test module's lifetime instead of via
      > separate containers) plus all 5 required scenarios. **Found a
      > real, genuine bug on the first run, not a test artifact**: this
      > test's first draft only stubbed
      > `document.service.check_completeness`, not
      > `promote_government_id_to_customer_photo`/`generate_welcome_letter`
      > — those hit the real (not-running-for-this-phase) Mayan, failed,
      > and Temporal retried `persist_decision`. The retry then created
      > a **second** `accounts` row for the same customer+product_type,
      > hitting `ux_accounts_customer_active_product_type` — because
      > `activities.py`'s `persist_decision` only wrote `account_id`
      > back to the `applications` row in the *final* combined `UPDATE`,
      > after the document.service calls, so a retry couldn't see that
      > provisioning had already partially happened. Root-caused and
      > fixed in `application/activities.py`: `account_id` is now
      > persisted immediately after `account.service.create_account(...)`
      > succeeds, before the two `document.service` calls — `CLAUDE.md`'s
      > provisioning-sequence section updated in place with the full
      > reasoning (marked "corrected from an earlier draft," per this
      > file's convention). Added a dedicated unit test in
      > `test_activities.py` reproducing this exact failure mode
      > (`document.service.promote_government_id_to_customer_photo`
      > raises after account creation, confirm `account_id` already
      > landed despite the raised exception, then confirm a retry
      > reuses that same account rather than creating a second one) —
      > 133 `tests/unit/` tests pass with the fix in place. Then fixed
      > the integration test itself to properly stub all three
      > `document.service` calls (matching the DoD's own "Mayan not
      > required for this phase" instruction correctly), re-ran: all 5
      > scenarios pass against the real stack. **Visually confirmed in
      > Temporal Web UI**, same technique as this project's own P4-4
      > precedent (the UI's own backing API, not just asserted in
      > code): `GET /api/v1/namespaces/default/workflows?query=...ExecutionStatus='Completed'`
      > listed all 5 just-run executions as
      > `WORKFLOW_EXECUTION_STATUS_COMPLETED`, and a `describe` on one
      > confirmed a real, non-trivial event history (21 events).
      > Verified via the same temporary 5433 Postgres port remap as
      > every other Phase 6/7 task (reverted before committing, zero
      > diff). **This completes Phase 7 — everything below the two BFFs
      > now works, end to end, against the real stack.**

This phase is a real milestone: everything below the two BFFs works.
Treat it as a natural point to pause and let a human spot-check the
result before continuing into Keycloak/UI work.

---

## Phase 8 — Import-Linter & CI

**Depends on:** Phases 2–7 (needs real imports to check against — don't
do this against an empty skeleton).

- [x] **P8-1** — `.importlinter` (or `pyproject.toml`
      `[tool.importlinter]`) encoding the full dependency graph from
      `CLAUDE.md`'s "Module dependency graph" section — every "never
      imports" rule as a `forbidden` contract, every "leaf module" as a
      layer with nothing below it.
      DoD: run it against the current codebase and confirm it passes
      clean (if it doesn't, that's a real violation introduced in
      Phases 2–7 — fix the violation, don't loosen the contract to make
      it pass).
      > DONE: `pyproject.toml`'s `[tool.importlinter]` (not a separate
      > `.importlinter` file — one config file already holds every other
      > tool's config in this project). Both forms this task's own
      > wording asks for, together: one `layers` contract encoding the
      > full hierarchy (`customer | account | document | workflow` as
      > the bottom, mutually-independent leaf layer via `importlinter`'s
      > `|` syntax — nothing declared below them — then `application`,
      > then `bff_customer | bff_backoffice`, then `(app) | worker_main`
      > on top, `app` wrapped in parens/marked optional since it doesn't
      > exist until Phase 10/11), plus one `forbidden` contract per
      > literal "never imports" bullet in `CLAUDE.md`'s section
      > (`customer`, `account`, `document`, `workflow`, and
      > `bff_customer`/`bff_backoffice`'s mutual exclusion) for direct,
      > traceable 1:1 mapping to that section's own wording. `lint-imports`
      > passes clean against the current codebase — 7 kept, 0 broken —
      > confirming every module boundary held through Phases 1-7 without
      > a single accidental violation. **Didn't just trust a clean run**:
      > temporarily added a real one-line boundary violation
      > (`customer/db.py` importing `account/db.py`) and confirmed both
      > the `layers` contract and the specific `customer` `forbidden`
      > contract catch it with an exact file/line citation, then
      > reverted (`git checkout --`) and re-confirmed clean — proves the
      > contracts actually enforce something, not just that they happen
      > to pass. Added `import-linter` to `dev` extras.
- [x] **P8-2** — Wire the import-linter run into the CI workflow from
      P0-6, required (not just informational) — a failure here should
      fail the build.
      DoD: deliberately introduce a one-line boundary violation in a
      throwaway branch, confirm CI fails on it, then revert.
      > DONE: uncommented/activated the `lint-imports` step P0-6 had
      > already reserved a slot for in `.github/workflows/ci.yml`, right
      > after the unit-tests step (same job, so a lint failure fails the
      > whole job — genuinely required, not a separate informational
      > job that could be ignored). Pushed the real P8-1 config to
      > `main` first and confirmed it green (`gh run watch`, run
      > `33593588677`, both "Run unit tests" and the new "Check module
      > boundaries (import-linter)" step passed). **Then did the DoD's
      > actual required check, for real**: created branch
      > `throwaway/p8-2-ci-violation-check`, added one line to
      > `document/service.py` importing `workflow.service` (a real,
      > direct violation of "document/ never imports application/ or
      > workflow/"), pushed, and confirmed via `gh run watch` (run
      > `33593657262`) that "Run unit tests" still passed but "Check
      > module boundaries (import-linter)" failed with exit code 1,
      > failing the overall job — proving the gate actually blocks a
      > bad push rather than just existing. Deleted the throwaway branch
      > both locally and on the remote afterward, confirmed `main` is
      > clean. **This completes Phase 8.**

---

## Phase 9 — Keycloak Realm & Back-Office Auth Plumbing

**Depends on:** Phase 0 (independent of the domain modules — can run in
parallel with Phases 2–8 if sessions overlap, though Phase 10 needs both
this and Phase 7 done).

- [x] **P9-1** — `keycloak/import/loanrealm-realm.json`: realm roles
      `Underwriter`/`Manager`; confidential client
      `loan-onboarding-backoffice`; Resource `LoanApplication` with five
      Scopes (`UnderwriterApprove`, `UnderwriterReject`,
      `UnderwriterRequestMoreInfo`, `ManagerApprove`, `ManagerReject`);
      two Policies, five scope-type Permissions per `CLAUDE.md`'s
      "Identity" section; demo users
      `underwriter1`/`underwriter2`/`manager1`/`manager2`, password
      `password`.
      DoD (integration-verify): `docker compose up -d keycloak`, log
      into the admin console, confirm the realm imported correctly, and
      manually run the raw token + UMA-exchange `curl` sequence (same
      shape as `review-approval-temporal`'s README) for `underwriter1`
      — confirm the returned scopes are exactly the three Underwriter
      ones, not all five.
      > DONE: adapted directly from `review-approval-temporal`'s own
      > `myrealm-realm.json` (fetched and read, not worked from memory)
      > — same shape (confidential client, `authorizationServicesEnabled`,
      > one Resource, role Policies, scope-type Permissions), swapped for
      > this project's two roles/five scopes. **No `TemporalAdmin` role
      > or conditional-auth-flow client** (that reference project's
      > `temporal-ui` client + custom flow) — deliberately out of scope
      > per `CLAUDE.md`'s "Deliberately not built" section. Every user
      > includes `firstName`/`lastName` from the start, per this skill's
      > own documented Keycloak 26 gotcha (missing them causes "Account
      > is not fully set up" on password-grant login despite `enabled`/
      > `emailVerified` being correct) — avoided the gotcha rather than
      > hitting and then fixing it. Verified for real against a freshly
      > created (not restarted — this skill's own "restart reuses H2
      > state, silently skips import" gotcha) `keycloak` container:
      > confirmed via `docker logs` that "Realm 'loanrealm' imported"
      > actually printed (not skipped); ran the full raw-token +
      > UMA-ticket-exchange `curl` sequence for **both** `underwriter1`
      > (returned exactly `UnderwriterApprove`/`UnderwriterReject`/
      > `UnderwriterRequestMoreInfo`, not all five) and `manager1`
      > (returned exactly `ManagerApprove`/`ManagerReject`) — the
      > symmetric check the DoD only asked for one side of, done for
      > both to confirm neither role leaks the other's scopes. Also
      > confirmed via the Admin REST API (equivalent to admin-console
      > inspection, scriptable) that the realm's roles/client/users all
      > match the import file exactly.
- [x] **P9-2** — `bff_backoffice/keycloak_auth.py`: JWT decode
      (`PyJWKClient`), `get_permissions()` (UMA ticket exchange,
      `response_mode=permissions`, read the per-resource `scopes`
      array), `refresh_access_token()`. `KEYCLOAK_ISSUER`/client
      id/secret read lazily, not at import time.
      > DONE: direct reuse of `review-approval-temporal`'s own
      > `keycloak_auth.py` (fetched and read, not worked from memory),
      > adapted for this project's `LoanApplication` resource/five
      > scopes. Deliberately framework-agnostic (no FastAPI imports),
      > same as the reference. 14 unit tests in
      > `tests/unit/bff_backoffice/test_keycloak_auth.py`, mirroring the
      > reference project's own `test_keycloak_auth.py` structure
      > exactly: `decode_token` against a locally-generated RSA keypair
      > (valid, expired, wrong issuer, wrong signature, unset issuer),
      > `get_permissions` with `respx`-mocked UMA responses (granted,
      > zero-granted via 403 `access_denied`, invalid token via 401,
      > unexpected 403 body, Keycloak unreachable, missing client
      > credentials) — the granted-permissions shape re-uses this
      > project's own real rsid/scopes confirmed live in P9-1, not
      > invented. Added `refresh_access_token` tests too (success,
      > rejected, missing credentials) since P9-4 depends on it.
- [x] **P9-3** — `bff_backoffice/session_store.py`: Redis-backed
      `/ui/*` session store (`ui-session:<id>` →
      `username`/`role`/`access_token`/`access_expires_at`/
      `refresh_token`/`refresh_expires_at`).
      > DONE: owns its own lazily-initialized Redis client from
      > `BACKOFFICE_REDIS_URL`, same convention as every domain module's
      > `db.py` owning its own lazy `asyncpg` pool — not obtained via a
      > FastAPI `app.state`, since `app.py` (Phase 10) doesn't exist
      > yet and there's no reason this module should depend on it
      > existing. Sliding TTL (`SESSION_TTL_SECONDS = 30 * 60`): every
      > `get()` pushes the key's expiry back out. 5 unit tests against a
      > **real** `backoffice-redis` (same deliberate "hits real infra"
      > exception as `customer`/`account`/`application`'s db-layer
      > tests — the TTL-sliding behavior is a statement about real Redis
      > expiry a mock can't verify), including one that sets a
      > short-TTL key directly, confirms `get()` extends it past the
      > original expiry, then confirms the key is genuinely still alive
      > after the original short TTL would have elapsed. **Published
      > `backoffice-redis` on host port 6380** (kept permanently, not
      > reverted like the `db` port-remap workaround elsewhere in this
      > plan) — unlike the Postgres 5432 collision, this is a
      > deliberate, useful addition: `.env.example`'s own header comment
      > already documents swapping a Docker-internal name for
      > `localhost:<published port>` when running natively, exactly
      > this module's own test suite (and any future natively-run
      > `bff_backoffice` process before Phase 10's `app` Compose service
      > exists) needs.
- [x] **P9-4** — `bff_backoffice/keycloak_session.py`:
      `get_session_user()` (async, transparent refresh),
      `require_session_role(role)`, `require_permission(permission)`/
      `check_permission()`. Role gates screens, permission gates
      actions — no `require_session_role` pre-gate on any decision
      route (see `CLAUDE.md`'s explicit warning about this).
      DoD: unit tests mock Keycloak at the HTTP layer with `respx`
      (JWT-validation tests patch key resolution directly and let real
      `jwt.decode()` run against a locally-generated test keypair — same
      approach as the reference project's `test_keycloak_auth.py`).
      > DONE: **deliberate adaptation from the reference project, not a
      > literal port** — every function takes a plain `session_id: str
      > | None` (and, for `complete_login`, an already-resolved
      > `expected_state`) instead of a FastAPI `Request`. The reference
      > project's equivalent functions read `request.session`/
      > `request.app.state.redis` directly, which is untestable without
      > a real Starlette `Request` — since `app.py` (Phase 10) doesn't
      > exist yet, there's no reason this module's session-resolution
      > *logic* should be coupled to a web framework to be testable;
      > `bff_backoffice/routes.py` (Phase 10) is expected to be the
      > thin, framework-coupled layer that reads
      > `request.session.get(SESSION_KEY)` and calls into this module's
      > plain functions. Noted prominently in the module's own
      > docstring so Phase 10 doesn't mistake this for an oversight.
      > Role-gate (`require_session_role`/`RoleDenied`) and
      > permission-gate (`require_permission`/`check_permission`/
      > `PermissionDenied`) are two genuinely separate exception types
      > and code paths, per CLAUDE.md's explicit warning against
      > conflating them. 24 unit tests in
      > `tests/unit/bff_backoffice/test_keycloak_session.py`, mocking
      > `session_store`/`keycloak_auth` at the function-call boundary
      > (a `FakeStore` double, `respx` for `complete_login`'s token
      > exchange) — `get_session_user`'s no-session/unknown-session/
      > valid/transparent-refresh/refresh-fails-so-delete-and-redirect
      > paths, both gate types' pass/deny cases, and `complete_login`'s
      > full matrix (state mismatch, exchange rejected, Underwriter
      > role, Manager role, no recognized role, invalid token). **Real
      > bug caught while writing these tests, not a hypothetical**: the
      > first draft of several `complete_login` tests defined
      > `fake_decode_token` as `async def`, but the real
      > `keycloak_auth.decode_token` is synchronous — calling the async
      > fake without awaiting it returned an un-awaited coroutine object
      > instead of raising or returning claims, surfacing as a
      > confusing `AttributeError: 'coroutine' object has no attribute
      > 'get'` deep inside `complete_login` rather than a clear test
      > failure; fixed by making the fakes plain sync functions,
      > matching the real signature. All 43
      > `tests/unit/bff_backoffice/` tests pass together (14 + 5 + 24),
      > and the full `tests/unit/` suite (176 tests) passes with no
      > regressions, verified via the same temporary 5433 Postgres port
      > remap as every other phase since Phase 2 (reverted before
      > committing) plus the new permanent 6380 Redis port. **Real gap
      > caught by CI itself, not local verification**: the first push
      > went red — `.github/workflows/ci.yml` only provisioned a
      > Postgres service container, no Redis, so
      > `session_store.py`'s tests had nothing to connect to in CI even
      > though they passed locally against the real `backoffice-redis`
      > container. Fixed by adding a `redis:7-alpine` service container
      > alongside the existing `postgres` one (same health-check-gated
      > pattern) and a `BACKOFFICE_REDIS_URL` env var for the test step;
      > confirmed green on the next push. **This completes Phase 9.**

---

## Phase 10 — Back-Office BFF UI (staff screens)

**Depends on:** Phases 7, 9. **Load the `list-pagination-bulk-actions`
and `htmx4` skills before starting this phase.**

- [x] **P10-1** — `bff_backoffice/routes.py`: `/ui/login` (Keycloak
      Authorization Code flow), `/ui/underwriter`, `/ui/manager` — list
      screens calling `application.service.list_by_status(...)`,
      auto-refresh every 5s.
      > DONE: also built `app.py` (the composition root, not previously
      > listed as its own task but required for any of this to run —
      > mounts `SessionMiddleware`, exception handlers mapping
      > `keycloak_session.py`'s `RequireLoginRedirect`/`RoleDenied`/
      > `PermissionDenied` to a 303 redirect / 403s, and
      > `bff_backoffice`'s router). Underwriter and Manager screens
      > share one set of route handlers parameterized by `role`
      > (`"underwriter"`/`"manager"`) and one shared template set — a
      > deliberate simplification versus the reference project's literal
      > per-role file duplication, since the two screens differ only in
      > which `status` they filter on and which decisions/permissions
      > apply, never in structure. Followed the reference project's own
      > `bff/ui.py`/templates exactly for the mechanics (fetched and
      > read directly): OOB toolbar refresh, the `_rows`/`_rows_body`
      > split so the 5s poll never rebuilds the header's select-all
      > checkbox, `select: 'tr'` + a `<table><tbody>` wrapper for
      > single-row swaps (htmx4 skill's documented gotcha), the
      > `HX-Retarget`/`HX-Reswap`/`HX-Reselect` error-path headers.
      > **`keycloak_session.py`'s functions (P9-4) take a plain
      > `session_id`, not a `Request`** (that module's own documented
      > adaptation) — `routes.py` supplies thin FastAPI-dependency
      > wrappers (`_role_dependency(role)`, `_session_user_dependency`)
      > that extract `request.session.get(SESSION_KEY)` and delegate,
      > which is exactly the "thin, framework-coupled layer on top"
      > that module's docstring anticipated.
- [x] **P10-2** — Row detail dialog: applicant/loan fields (via
      `customer.service.get()`/`account.service.get()` **when
      `customer_id`/`account_id` are set** — fall back to the
      application's own denormalized `applicant_name`/`applicant_email`/
      `applicant_phone` when they're `NULL`, which is the normal case
      for anything not yet terminally `APPROVED` — see `CLAUDE.md`'s
      "Applying without being a customer yet"), product-specific payload
      fields, document links (`document.service.preview(...)`),
      single-item decision form gated by
      `_user_permissions(user)` (buttons only render for a granted
      scope). **On Approve specifically, call
      `application.service.check_decision_allowed(application_id,
      "APPROVE")` first** (P6-5b) — a non-empty result shows the
      conflict as a form error and never calls
      `workflow.service.signal_decision(...)` at all.
      > DONE: added a `document.service.preview(...)`-backed streaming
      > route (`/ui/{role}/{application_id}/documents/{document_id}/preview`,
      > not explicitly named in this task's own text but required to
      > make "document links" real) — verified in a real browser tab
      > that a linked PDF actually opens and renders, not just that the
      > link exists. Decision buttons are built from a shared
      > `_decision_options(role)` helper (`(decision, label,
      > permission)` triples) registered as Jinja globals alongside
      > `_has_select_column`, so every template computes visibility from
      > `role`/`permissions` it already has, rather than threading extra
      > context keys through every render call site. `wait_for_status_change()`
      > (a new public function added to `application/service.py`,
      > reusing its existing private `_wait_until` primitive) is what
      > lets the single-row response show the *actual* post-decision
      > status immediately, since `signal_decision()` only confirms
      > Temporal accepted the signal.
- [x] **P10-3** — Bulk selection: server-side store
      (`bff_backoffice/selection_store.py`, reusing the Redis instance
      from P9-3), checkbox column, "select all on this page," selection
      toolbar, confirm dialog with one shared comment, calling
      `workflow.service.bulk_signal_decision(...)`. **For a bulk
      Approve, call `check_decision_allowed` per selected application
      first** and filter conflicting ones out of the batch *before*
      collecting `workflow_ids` — report each filtered-out application
      in the same per-item result shape `bulk_signal_decision` already
      returns for any other failure, rather than passing its
      `workflow_id` through to `bulk_signal_decision` at all.
      > DONE: `selection_store.py` is a plain Redis `SET` per session id
      > (`SADD`/`SREM`/`SMEMBERS`) — simpler than the reference
      > project's own JSON-blob `SessionMemory` since this project
      > doesn't need that module's bundled pagination-fallback tier (see
      > P10-4's note). Bulk toolbar offers one button per decision the
      > role's screen supports and the user actually holds the
      > permission for (up to three for Underwriter, two for Manager).
      > Verified live in the browser: bulk-approved two applications in
      > one action, got "2 succeeded, 0 failed", and the list correctly
      > dropped both from the queue via the OOB table refresh.
- [x] **P10-4** — Pagination: `query_id`-cached counts per the skill's
      pattern, correct across the 5s auto-poll.
      DoD (integration-verify, whole phase): log in as `underwriter1`,
      confirm `/ui/manager` is inaccessible; approve a below-threshold
      application solo and in bulk; approve an above-threshold one as
      Underwriter, confirm it appears on `/ui/manager` for `manager1`
      and not for `underwriter1`; confirm a permission-lacking session
      gets a real 403 attempting a decision action directly (not just a
      hidden button — try the route with `curl`/a raw request); **create
      a second application for a customer who already has an active
      account of the same product type, confirm Approve is refused with
      a clear reason and no workflow signal is sent** (PRD §9.2).
      > DONE: reuses `application.service.list_by_status`'s existing
      > `query_id` cache as-is — **deliberately no Redis-backed
      > pagination-fallback tier** (the reference project's own
      > `SessionMemory`-based tier 2/3 resilience), since neither staff
      > screen here has a per-user ownership filter to protect (both are
      > pure status filters, unlike that project's operator screen) —
      > a `query_id` miss just means "recompute for real," which the
      > `list-pagination-bulk-actions` skill explicitly calls an
      > acceptable simplification. **Full DoD walked end to end against
      > the real stack via an actual browser** (`claude-in-chrome`, real
      > Keycloak Authorization Code login, not curl-simulated) rather
      > than assumed from route code:
      > - `underwriter1` logs in for real, sees all 7 seeded test
      >   applications with correct data.
      > - `/ui/manager` confirmed inaccessible for `underwriter1` two
      >   ways: the page itself (role-gated redirect/403 text visible in
      >   the rendered page) and a raw in-page `fetch()` returning a
      >   real `403 requires role: manager`.
      > - Solo approve (a $10,000 application): row updates to
      >   `APPROVED` in place via the `select: 'tr'` swap.
      > - Bulk approve (2 applications): "2 succeeded, 0 failed",
      >   OOB-refreshed list correctly drops both.
      > - Reject: works identically.
      > - Escalation ($60,000 → above `MANAGER_ESCALATION_THRESHOLD_USD`):
      >   Underwriter-approve moves it to `PENDING_MANAGER_APPROVAL` and
      >   off the Underwriter's own queue; logged out, logged back in as
      >   `manager1`, confirmed it — and *only* it — appears on
      >   `/ui/manager`; Manager-approved it to terminal `APPROVED`;
      >   confirmed via a direct DB query that `customer_id`/`account_id`
      >   were both provisioned (full `persist_decision` flow, real
      >   Mayan calls included, fired correctly from a real UI click).
      > - Permission-lacking session real 403: while logged in as
      >   `underwriter1`, a raw in-page `fetch()` `POST` directly to
      >   `/ui/manager/<id>/decision` (never rendered as a button for
      >   this session) returned `403 requires permission: ManagerApprove`
      >   — not a hidden-button-only restriction.
      > - Active-account conflict: **the first attempt at this check used
      >   flawed test data** (both "conflict" applications were created
      >   *before* either was approved, so neither ever got a resolved
      >   `customer_id` — `check_decision_allowed` correctly short-circuits
      >   to "allowed" for a `customer_id`-still-`NULL` application, per
      >   its own documented, deliberate design; this was a test-setup
      >   ordering mistake, not a code bug). Redid it correctly: approved
      >   one application for a fresh applicant (creating a customer +
      >   active `personal_loan` account), *then* submitted a second
      >   application for that *same* applicant (which correctly resolved
      >   `customer_id` at creation time, since the customer now existed),
      >   then clicked Approve on it as `underwriter1` — the dialog
      >   re-opened (via the `HX-Retarget`/`HX-Reselect` error path,
      >   confirmed working) showing "customer already has an active
      >   personal_loan account" in red, status remained
      >   `PENDING_UNDERWRITING`, no workflow signal sent.
      > - Document preview: clicked an actual document link in the
      >   detail dialog, confirmed a real tab opened showing the
      >   rendered PDF ("Government ID").
      > A real Keycloak redirect-URI gap was found and fixed along the
      > way: the realm import only registered `localhost:8000`/`app:8000`
      > callback URLs, but this session's native (non-Docker) test
      > server ran on `8001` (`8000` was already held locally by Mayan)
      > — added `http://localhost:8001/ui/callback` (+ matching
      > `webOrigins`/post-logout-redirect) to
      > `keycloak/import/loanrealm-realm.json` as a documented,
      > permanent native-dev alternative, re-imported via a fresh
      > container (not a plain restart, which the `keycloak-admin` skill
      > documents as silently skipping re-import). **Flagging for a
      > later phase, not fixed now**: `app.py`'s own docstring example
      > and the realm's primary redirect URIs both assume port `8000`,
      > which is *also* Mayan's published host port (P5-1) — once an
      > `app`/`app-backoffice` Docker Compose service is actually added
      > (not scoped to Phase 10's tasks), it and `mayan` will need
      > distinct host ports; not a problem today since `app` isn't a
      > Compose service yet, but worth remembering when it becomes one.
      > No dedicated `tests/unit/bff_backoffice/test_routes.py` was
      > added — `routes.py`/`app.py` are thin FastAPI/Jinja glue over
      > already-unit-tested modules (`keycloak_session.py`,
      > `application.service`, etc.), and the reference project this is
      > adapted from has no unit test file for its own equivalent
      > `bff/ui.py` either, relying on exactly this kind of real-stack
      > integration verification instead — consistent with, not a
      > shortcut around, this project's established testing convention.
      > Full `tests/unit/` suite (176 tests, unaffected by this phase)
      > reconfirmed passing throughout. **This completes Phase 10.**

---

## Phase 11 — Customer BFF UI (self-service mobile flow)

**Depends on:** Phases 2, 3, 5, 7. **Load the `htmx4` skill.** No direct
precedent in either reference project — the most novel phase.

- [x] **P11-1** — `bff_customer/identity.py`: signed cookie session
      holding `applicant_identifier`, no password. `/apply` redirects
      to an identify screen if the cookie is missing.
      > DONE: Built as its own dedicated `itsdangerous`-signed cookie
      > (`customer_session`, `CUSTOMER_SESSION_SECRET_KEY`), NOT a key
      > inside `bff_backoffice`'s Starlette `SessionMiddleware` session
      > as an earlier draft of `CLAUDE.md` assumed — `.env.example`
      > already anticipated a separate `CUSTOMER_SESSION_SECRET_KEY`
      > (present since P5-1), and Starlette only supports one
      > `SessionMiddleware`/cookie per app, which `bff_backoffice`'s
      > Keycloak session id already occupies. `identity.py` exposes
      > `get_applicant_identifier(request)`,
      > `set_applicant_identifier(response, id)`,
      > `clear_applicant_identifier(response)` — the set/clear
      > functions take the outgoing `Response` (not `request.session`)
      > since there's no shared middleware to write through. `app.py`
      > gained an `IdentifyRequired` exception handler (mirroring
      > `bff_backoffice`'s `RequireLoginRedirect` pattern) redirecting
      > to `/apply/identify`. CLAUDE.md's `bff_customer/` section
      > corrected to match. Unit-tested (`tests/unit/bff_customer/
      > test_identity.py`, no live services — round-trip, tamper
      > rejection, garbage-token rejection, clear-expires-the-cookie)
      > per the same "pure logic gets a unit test, routes.py doesn't"
      > split Phase 9/10 already established for
      > `bff_backoffice/keycloak_session.py` vs. `routes.py`.
      >
      > The new-application wizard's own multi-step draft state
      > (product type, provisional `application_id`, in-progress field
      > values, `routes.py`'s `_DRAFT_KEY`) deliberately does NOT get
      > this treatment — it's ordinary UI flow state, not identity, so
      > it rides on `bff_backoffice`'s existing shared
      > `SessionMiddleware` session under its own key. `identity.
      > clear_applicant_identifier` doesn't touch it (can't — it only
      > has the `Response`, not the session); `routes.py`'s
      > `switch_identity` route pops `_DRAFT_KEY` from
      > `request.session` itself, right alongside clearing the identity
      > cookie, so a half-finished draft never leaks into the next
      > identity's session.
- [x] **P11-2** — "My Applications" screen:
      `application.service.list_for_applicant(applicant_identifier,
      ...)`, status badges, "Apply for a new loan" CTA.
      > DONE: `applications_list.html`, paginated (10/page, Prev/Next),
      > color-coded status badges (`STATUS_LABELS` dict in
      > `routes.py`), "Not <identifier>?" link (`switch-identity`) for
      > testing/demoing a different applicant. Verified empty-state
      > copy ("No applications yet.") renders correctly for a
      > brand-new identifier.
- [x] **P11-3** — New Application flow: product-type picker → common +
      product-specific fields → document upload (camera capture for ID
      via `<input type="file" capture>`, file picker for
      statements/reports) → review & submit, calling
      `application.service.create_application(...)`. Missing-documents
      error surfaces the specific categories, not a generic failure.
      > DONE: Four-step wizard (`/apply/new` → `/new/details` →
      > `/new/documents` → `/new/review`), state threaded through a
      > provisional `application_id` (`uuid4()`, minted at `/new/start`)
      > stored in `request.session[_DRAFT_KEY]` alongside `product_type`
      > and the entered field values — exactly the "bff_customer mints
      > a provisional `application_id` at the start of its wizard"
      > design `CLAUDE.md`'s `application/` section already specifies.
      > `PRODUCT_FIELDS` (a plain dict in `routes.py`) is a deliberate
      > UI-only mirror of `application.schemas.PRODUCT_TYPE_SCHEMAS`
      > (same "duplicated on purpose, no import-time link" reasoning
      > `document/service.py`'s `REQUIRED_CATEGORIES` already
      > documents) — every submission still runs the real
      > `application.schemas.validate_payload(...)` (via a new
      > `_validate_product_fields` dry-run helper at the details step,
      > and for real inside `create_application` at final submit), so a
      > mismatch between the mirror and the real schema shows up as a
      > validation error, never silently accepts bad data.
      > `document.service.REQUIRED_CATEGORIES[product_type]` drives
      > which upload widgets render — confirmed different sets per
      > product type (4 categories for `personal_loan`, +Vehicle
      > Title/Invoice for `auto_loan`, +Property Appraisal for
      > `mortgage`) live against the real Mayan instance. Camera-capture
      > hint (`capture="environment"`) applied only to the "Government
      > ID" category's file input, per PRD §6.4. Document upload is the
      > one place in this BFF using htmx (`_document_category.html`,
      > `hx-post`/`hx-encoding="multipart/form-data"`, swapping just
      > that category's own fragment) rather than a full page
      > reload — everywhere else in this phase is plain `<form>`
      > POST-redirect-GET, a deliberate simplification for a
      > multi-page mobile wizard (see `routes.py`'s module docstring).
      > Missing-documents error verified to show the exact missing
      > category names (not a generic failure) with an "Add documents"
      > link back to the upload step, both inline on `/new/review` and
      > (post-submission, on resubmit) inline on the detail page.
- [x] **P11-4** — Application detail/status screen: timeline, resubmit
      action when `MORE_INFO_REQUESTED` (calling
      `application.service.resubmit_application(...)`), Cancel action
      while non-terminal (calling
      `workflow.service.signal_decision(..., decision="CANCELLED")`
      directly — no `application.service` hop, per `CLAUDE.md`'s call
      graph).
      > DONE: `_build_timeline(application)` branches on the terminal
      > case FIRST (checks `application.status in TERMINAL_STATUSES`
      > before anything else), not last — a Cancel can happen from any
      > non-terminal state (`PENDING_UNDERWRITING`,
      > `PENDING_MANAGER_APPROVAL`, or `MORE_INFO_REQUESTED`), so
      > branching on `status` in wizard order would miss it whenever
      > cancellation didn't happen from the last step; verified by
      > actually cancelling from `PENDING_UNDERWRITING` and confirming
      > the timeline reads Submitted → Under Review → Cancelled (not a
      > dead end mid-flow). `_owned_application()` re-checks
      > `application.applicant_identifier == applicant_identifier`
      > (never trusts the URL's `application_id` alone) before
      > rendering detail, cancel, resubmit, document-upload, or
      > document-preview — a mismatch is a genuine 404, confirmed live
      > (see DoD note below), not a client-side hide. Resubmit re-runs
      > `_validate_product_fields` (same dry-run as the wizard) before
      > calling `resubmit_application`, and re-checks the document gate
      > via that call's own `missing_categories` result, re-rendering
      > the detail page with the specific missing categories on
      > failure rather than a generic error. Document upload during
      > `MORE_INFO_REQUESTED` reuses `_document_category.html` (the
      > same htmx partial the wizard uses), gated so it only appears in
      > that one status — every other status gets the plain read-only
      > `by_category` listing instead (confirmed no upload widget, no
      > resubmit form, and no Cancel button render once an application
      > reaches a terminal state — the view-only invariant, PRD §6.2).
      >
      > DoD (integration-verify, whole phase) — walked against the real
      > local stack (Postgres, Temporal, Keycloak, Redis, Mayan) via
      > `chrome-devtools` MCP's `emulate` (`375x812x2,mobile,touch` —
      > a genuine device viewport emulation, not a resized desktop
      > window), through the full local native run (`uvicorn
      > loan_onboarding.app:app --port 8001` + `worker_main.py`, both
      > against the docker-compose stack):
      > - **`personal_loan`**, below-threshold amount: full wizard →
      >   `PENDING_UNDERWRITING` → Underwriter **Request More Info**
      >   (real comment shown on the customer detail page) →
      >   customer uploads a replacement Bank Statement + resubmits
      >   updated `purpose` field → back to `PENDING_UNDERWRITING` →
      >   Underwriter **Approve** → **APPROVED**. DB-confirmed: a new
      >   `customers` row created (`get_or_create`, first approval for
      >   this `applicant_identifier`) and a new `ACTIVE` `accounts`
      >   row (`product_type=personal_loan`), `applications.customer_id`/
      >   `account_id` both set.
      > - **`auto_loan`**, same applicant: full wizard (confirmed the
      >   auto_loan-specific "Vehicle Title/Invoice" category renders
      >   and gates correctly) → submitted → customer **Cancel** while
      >   `PENDING_UNDERWRITING` → **CANCELLED** (timeline shows
      >   Submitted → Under Review → Cancelled, no dead end). DB-
      >   confirmed `account_id` stayed `NULL` (no provisioning on
      >   Cancel).
      > - **`mortgage`**, same applicant, $75,000 (above the $50,000
      >   escalation threshold): full wizard (confirmed the
      >   mortgage-specific "Property Appraisal" category) → submitted →
      >   Underwriter **Approve** → `PENDING_MANAGER_APPROVAL`
      >   (customer timeline correctly shows "Escalated to Manager" as
      >   the current step, not a 3-step timeline) → logged out,
      >   logged back in as `manager1` → Manager **Approve** →
      >   **APPROVED** (customer timeline now shows all 4 steps done:
      >   Submitted → Under Review → Escalated to Manager → Approved).
      >   DB-confirmed: the SAME `customer_id` as the first approval
      >   (idempotent `get_or_create` correctly found the
      >   already-created customer this time, via `find_by_identifier`
      >   at submission time, since the customer already existed by
      >   then) plus a SECOND new `ACTIVE` `accounts` row
      >   (`product_type=mortgage`) — confirming one customer can hold
      >   multiple `ACTIVE` accounts of *different* product types with
      >   no conflict (PRD §9.2), and that `persist_decision`'s
      >   idempotency guard correctly distinguishes "new customer" from
      >   "existing customer, new account".
      > - **`personal_loan`** (2nd), missing every required document at
      >   submit time: confirmed `/new/review` shows "Missing required
      >   documents: Government ID, Proof of Income, Bank Statements,
      >   Credit Report" (the specific list, not a generic failure)
      >   with a working "Add documents" link back to `/new/documents`;
      >   uploaded all four, resubmitted successfully, then Underwriter
      >   **Reject** → **REJECTED** (customer timeline: Submitted →
      >   Under Review → Rejected).
      > - **Visibility invariant** (PRD §10 success criterion 2):
      >   switched identity (`switch-identity` → re-identify as
      >   `someone-else@example.com`) and confirmed "My Applications"
      >   shows the empty-state copy, not the first identifier's
      >   applications. Directly navigated to the first identifier's
      >   `application_id` URL while identified as the second — got a
      >   genuine `{"detail":"Not Found"}` (FastAPI's default 404 body,
      >   same as every other plain `HTTPException(404)` in this
      >   codebase), confirming server-side filtering, not a
      >   client-side hide.
      > - **Document preview**: opened a customer-side preview link in
      >   a new tab, confirmed the real uploaded PDF streams and
      >   renders inline (same `StreamingResponse` + `aclose()` cleanup
      >   pattern `bff_backoffice`'s equivalent route already uses).
      >
      > **One real tooling gotcha hit during this verification, not a
      > code bug**: `chrome-devtools` MCP's `click` tool intermittently
      > failed ("did not become interactive within timeout") against
      > this project's staff screens and plain-form submit buttons —
      > confirmed via network-request inspection that some of those
      > "successful" clicks genuinely never fired a request at all,
      > while others worked. Root cause not fully isolated (possibly an
      > interaction between the 5s self-polling staff table and the
      > tool's own interactability wait), but reliably worked around by
      > dispatching the action directly (`element.click()` for buttons
      > outside a form, `form.requestSubmit()` for `<form>` submits)
      > via `evaluate_script` instead of the coordinate/uid-based click
      > tool. Not a `loan_onboarding` bug — the same actions succeed
      > from a real click in the earlier screenshots and via this
      > workaround.

---

## Phase 12 — End-to-End Verification & Polish

**Depends on:** everything above.

- [x] **P12-3** — `docker compose up --build` from a completely clean
      state (`docker compose down -v`) brings up the entire stack with
      zero manual steps beyond the documented one-time Mayan hierarchy
      bootstrap — verify this on a clean checkout, not an
      already-warmed-up dev environment.
      > DONE (done before P12-1 below, since P12-1's criterion 6 and
      > P12-2's Keycloak-gap re-check both depend on this having
      > actually happened first): added `docker-compose.yml`'s `app`
      > service (the single web process, `uvicorn
      > loan_onboarding.app:app`, `Dockerfile`'s existing image), the
      > one piece of the stack that never existed as a Compose service
      > before this phase — published on host port `8001` (not `8000`,
      > which collides with `mayan`'s own published `8000` — flagged
      > since P5-1) with `depends_on: [db, temporal, keycloak,
      > backoffice-redis, mayan]` per `CLAUDE.md`'s own already-written
      > spec for it.
      >
      > Ran `docker compose down -v` for real (destroys all local dev
      > volumes: `db_data`, `mayan_db_data`, `mayan_redis_data`,
      > `mayan_app_data`) then `docker compose up --build -d` from that
      > genuinely empty state. Two real, previously-invisible bugs
      > surfaced, both because this was the first time the whole stack
      > had ever run fully containerized end to end (every earlier
      > phase's manual verification ran `app.py` natively on the host):
      >
      > 1. **Keycloak issuer split.** `bff_backoffice/keycloak_session.py`'s
      >    two browser-redirect URLs (`build_authorize_url`,
      >    `logout_redirect_url`) and `keycloak_auth.py`'s token
      >    issuer-claim validation were all built from `KEYCLOAK_ISSUER`
      >    (`http://keycloak:8080/...`) — correct for this app
      >    container's own server-to-server calls, meaningless to a
      >    browser outside the compose network, and mismatched against
      >    what Keycloak actually stamps into an issued token's `iss`
      >    claim (which mirrors the front-channel/browser-facing URL,
      >    not whichever URL a later server-to-server call happens to
      >    use). Deeper still: Keycloak's own `hostname-strict=false`
      >    default (confirmed via `kc.sh show-config` inside the
      >    container) makes it derive the issuer it validates an
      >    incoming *bearer* token against from **that specific
      >    request's own Host header** — so even after fixing this
      >    app's own issuer-claim checks, a real UMA ticket exchange
      >    sent to the internal `http://keycloak:8080/...` was still
      >    getting a genuine `401 invalid_grant: Invalid bearer token`
      >    from Keycloak itself, for a perfectly valid token, reproduced
      >    directly via `httpx` from inside the `app` container against
      >    both URLs side by side. Root-cause fixed two ways together:
      >    a new `KEYCLOAK_PUBLIC_ISSUER` env var (falls back to
      >    `KEYCLOAK_ISSUER` when unset, so native/host-run needs no
      >    config change) for this app's own browser-redirect URLs and
      >    token issuer-claim validation (`_public_issuer()`/
      >    `_public_keycloak_issuer()`), **and** pinning Keycloak's own
      >    hostname (`KC_HOSTNAME=localhost`, `KC_HOSTNAME_PORT=8080`,
      >    `KC_HOSTNAME_STRICT_HTTPS=false` on the `keycloak` service)
      >    so Keycloak itself mints and validates every token against
      >    one fixed issuer regardless of which network path a request
      >    arrives on — the first fix alone wasn't sufficient, confirmed
      >    by hitting the 401 again even after it was in place.
      >    Verified via a real browser login → underwriter queue →
      >    logout round trip through the fully containerized stack
      >    after both fixes, plus two new unit tests per module
      >    (`test_keycloak_session.py`,
      >    `test_keycloak_auth.py`) proving the public-issuer fallback
      >    and override behavior without needing a real Keycloak.
      > 2. **In-process Mayan id-map caches predate the one-time
      >    bootstrap.** `document/mayan_client.py`'s
      >    `document_type_ids()`/`metadata_type_ids()` cache their
      >    lookups for the life of the process. The `app` and
      >    `worker-activity` containers both started (as part of
      >    `docker compose up`) *before* `scripts/setup_document_hierarchy.sh`
      >    was run against the freshly-emptied Mayan instance, so both
      >    cached an empty/stale id map and the very first document
      >    upload failed with `KeyError: 'Application Document'`. Not a
      >    code bug — this is exactly why the bootstrap script is
      >    documented as a manual, one-time step distinct from `docker
      >    compose up` — but worth stating explicitly since it wasn't
      >    previously written down: **run the bootstrap script, then
      >    restart (not just leave running) any already-started
      >    `app`/`worker-activity` containers**, or start them after
      >    the bootstrap step instead of before. Not treated as a code
      >    fix (the caching itself is fine — Mayan's document/metadata
      >    types are static for the life of a deployment); recorded
      >    here and in `CLAUDE.md`'s Known Gaps as an ordering
      >    dependency to be aware of.
      >
      > After both fixes, confirmed genuinely clean zero-manual-steps
      > bring-up: schema auto-applied (`\dt` showed all three tables
      > immediately after first boot, no manual migration step), both
      > `loan_onboarding`/`temporal` databases created by `db/init/*.sh`,
      > all 10 services reached a stable running state (the
      > `worker-workflow`/`worker-activity` containers do restart 3-4
      > times before Temporal's `auto-setup` image finishes creating
      > the `default` namespace — a real, benign race between the
      > worker's own startup and Temporal's namespace bootstrap,
      > `restart: on-failure` already handles it correctly, self-heals
      > within about a minute on this hardware, not treated as a bug to
      > fix but worth knowing about if a worker looks "crash-looping"
      > right after a cold start), and the full customer + staff flows
      > work end to end against the freshly-built stack (see P12-1's
      > criteria 1/4/5/6/8 below, all walked against this exact
      > from-clean instance, not a separately-verified one).
- [x] **P12-1** — Walk every numbered item in `PRD.md` §10 "Success
      criteria for this POC" explicitly, one at a time, and record the
      result (pass/fail + how verified) in this task's Session Log
      entry — don't just say "looks done."
      > DONE — see this task's Session Log entry for the full
      > criterion-by-criterion walk (all 8 pass, several with new live
      > verification performed specifically for this task, not just
      > cross-referenced from earlier phases).
- [x] **P12-2** — Walk `CLAUDE.md`'s "Known gaps" section, confirm each
      listed gap is genuinely still a gap (not something that got
      accidentally fixed or accidentally became worse) and that none of
      them are actually surprises that should have been caught earlier.
      > DONE: walked every bullet. One entry resolved for real (the
      > `app`/`mayan` port-8000 collision — P12-3 above) and rewritten
      > to describe the fix rather than the risk. One entry
      > (Temporal *terminate*/no-reconciliation) **corrected, not just
      > confirmed** — found during this pass that `db/schema.sql`'s
      > `workflow_id` column comment and `PRD.md` §9.3 both claimed a
      > reconciliation mechanism exists ("cleared if a Temporal admin
      > deletes the execution") that a full-codebase grep confirms was
      > never built — a real, previously-uncaught documentation bug,
      > not a discrepancy that just appeared; fixed in both files plus
      > `CLAUDE.md`, and the claim was independently verified live (see
      > P12-1 criterion 5). Every other bullet (Mayan rate limiting, no
      > real customer auth, active-account-race window, import-linter
      > enforcement depending on CI staying wired up, Keycloak
      > `verify_aud=False`/no permission caching, no decision timeout,
      > no proactive notification, an unpolled product type getting
      > silently stuck) re-read against the current codebase and
      > confirmed still accurate and still unaddressed — none had been
      > accidentally fixed, and none turned out to be a surprise that
      > should have been caught earlier than this.
- [x] **P12-4** — Final pass on `CLAUDE.md`'s "Known gaps" and this
      file's **Decisions Needed** section — anything still open gets
      surfaced to the user explicitly in the session's final message,
      not silently left in a markdown file for someone to notice later.
      > DONE: **Decisions Needed** is empty (confirmed) — nothing to
      > surface from there. `CLAUDE.md`'s Known Gaps, after P12-2's
      > pass, surfaced to the user in this session's final message (see
      > that message for the actual list) rather than left implicit
      > here.

---

## Phase 13 — Human-readable primary keys + account-to-application direction flip

**Depends on:** everything above (all 12 prior phases). **Not part of
the original build-out** — a design change requested and confirmed
after Phase 12 closed. `CLAUDE.md` and `PRD.md` were updated *before*
this phase's own tasks below, per this project's own convention of
writing the architecture doc first and the implementation second — see
both files' "Data storage" / §9 sections and the module-by-module
descriptions for the target design each task below implements.

Two independent-but-related changes, confirmed with the user before any
code was touched:
1. Every primary key (`customers.customer_id`, `accounts.account_id`,
   `applications.application_id`) moves from a Postgres-generated `UUID`
   to an application-assigned, human-readable string — `CUS-`/`ACC-`/
   `APP-` followed by a random 9-digit number, via a new shared leaf
   module, `idgen/`.
2. `accounts.application_id` (new, `NOT NULL UNIQUE`) replaces
   `applications.account_id` (removed) — the account now points at its
   originating application, not the other way around, which also lets
   this new `UNIQUE` constraint serve as `persist_decision`'s
   provisioning-idempotency guard directly (see `CLAUDE.md`'s "Applying
   without being a customer yet").

**A real, accepted tradeoff, stated once here rather than repeated per
task below**: pure digits at length 9 (`10^9` values per entity type)
is meaningfully less collision-safe than the `UUID`s it replaces — see
`CLAUDE.md`'s "Data storage" and Known Gaps for the full entropy
discussion. This makes the retry-on-collision insert logic in P13-3/
P13-4 below a required correctness mechanism, not defensive icing —
don't skip it as "unlikely to matter for a POC" the way some other
gaps in this file are deliberately left unaddressed.

- [x] **P13-1** — New leaf module `loan_onboarding/idgen/service.py`:
      `generate_id(prefix: str, length: int) -> str`, `secrets.choice`
      over `0123456789`. Add `idgen` to `pyproject.toml`'s
      `[tool.importlinter]` layers contract (leaf layer, alongside
      `customer | account | document | workflow`) and add a matching
      `idgen/ never imports anything else in this codebase` forbidden
      contract. Update the two existing `customer/`/`account/`
      "never imports anything else" forbidden contracts' framing in
      `CLAUDE.md` if not already done (they should now read "with one
      exception: `idgen/`") — no `pyproject.toml` change needed there,
      since `idgen` was never in those contracts' `forbidden_modules`
      lists to begin with (it didn't exist yet); this is a documentation
      consistency check, not a lint-config change.
      DoD: unit tests confirm prefix/length/digits-only alphabet, plus a
      uniqueness sanity check across a large sample (statistical, not a
      proof). `lint-imports` passes with the new contracts in place.
- [x] **P13-2** — `db/schema.sql`: drop every `UUID PRIMARY KEY DEFAULT
      gen_random_uuid()` in favor of `TEXT PRIMARY KEY` (no default) for
      `customers.customer_id`, `accounts.account_id`,
      `applications.application_id`; change `accounts.customer_id` and
      `applications.customer_id` from `UUID` to `TEXT`; add
      `accounts.application_id TEXT NOT NULL` with
      `CREATE UNIQUE INDEX ux_accounts_application_id ON accounts
      (application_id)`; remove `applications.account_id` entirely;
      drop the `chk_approved_has_account` `CHECK` constraint (can no
      longer be expressed as a single-table check once the column it
      referenced lives on the other table — see `CLAUDE.md`'s Known
      Gaps for this as an accepted, explicit reduction in the DB-level
      safety net, not silently dropped).
      DoD: `docker compose down -v && docker compose up -d db` applies
      the new schema cleanly from empty; a manual `psql \d accounts` /
      `\d applications` confirms the new column shapes and the dropped
      constraint.
- [x] **P13-3** — `customer/` and `account/`: `models.py` field types
      `UUID` → `str` (plus `Account.application_id: str`, new field);
      `db.py`'s `get_or_create`/`create` generate an id via
      `idgen.service.generate_id(prefix, 9)` before each insert attempt,
      and on a `UniqueViolationError` against the table's own primary
      key specifically (not a business-rule constraint — inspect
      `exc.constraint_name` to tell them apart), regenerate and retry,
      bounded at 10 attempts, raising a clear error if all 10 collide.
      `account/db.py`'s `create` also takes and inserts `application_id`
      now; add `account/db.py`'s `get_by_application_id` +
      `account/service.py`'s wrapper. `customer/service.py` keeps its
      existing `get_or_create`'s two independent conflict paths (the
      pre-existing `applicant_identifier` race, now plus the new
      id-collision retry) — both must keep working together, not just
      whichever was tested last.
      DoD: `tests/unit/account/test_service.py` and
      `tests/unit/customer/test_service.py` updated (drop `uuid.uuid4()`
      fabrication, use real ids or literal test strings for "unknown id"
      cases) and passing against a real Postgres, per this project's
      existing testing convention for these two modules.
- [x] **P13-4** — `application/`: `models.py` drops `Application.account_id`
      entirely, `application_id`/`customer_id` become `str`. `db.py`'s
      `insert` takes a pre-generated `application_id`; `update_decision`
      drops its `account_id` parameter and column reference entirely;
      remaining `UUID` type hints → `str`. `service.py`'s
      `create_application` generates via
      `idgen.service.generate_id("app", 9)` when `application_id` isn't
      supplied (same optional-parameter shape as today, just a
      different generator). `activities.py`'s `persist_decision`
      provisioning block is rewritten per `CLAUDE.md`'s updated
      "Applying without being a customer yet": check
      `account_service.get_by_application_id(application_id)` **first**
      (not `record["account_id"] is None`); if `None`, do the full
      provisioning sequence (customer resolve/create,
      `create_account(customer_id, product_type, application_id)`, then
      the two `document_service` calls) with **no intermediate write
      back onto `applications`** — the committed `accounts` row (unique
      on `application_id`) is now the durable idempotency marker on its
      own; if not `None` (a retry), skip provisioning and take
      `customer_id` from `existing_account.customer_id`. Delete every
      `UUID(inp.application_id)`/`UUID(inp.customer_id)` conversion —
      `workflow/workflows.py`'s dataclasses already type these fields as
      plain `str`, confirmed unchanged by this phase.
      DoD: `tests/unit/application/test_activities.py`'s existing
      idempotency tests
      (`test_persist_decision_approve_is_idempotent_on_retry`,
      `test_persist_decision_retry_after_document_service_failure_does_not_double_create_account`)
      updated for the new id types and still pass — these are the direct
      proof the new `get_by_application_id`-based guard preserves the
      exact correctness property the old column-based one had, not just
      that the code compiles.
- [x] **P13-5** — `bff_backoffice/routes.py` and `bff_customer/routes.py`:
      every `application_id: UUID` route/function parameter → `str`;
      drop now-unused `from uuid import UUID` imports.
      `bff_backoffice`'s `_application_detail_context` replaces its
      `application.account_id`-gated `account_service.get(...)` call
      with a direct `account_service.get_by_application_id(application_id)`.
      The two `UUID(raw_id)` conversions in `bff_backoffice`'s
      bulk-decision paths are deleted. `bff_customer`'s wizard
      provisional-id mint switches from `uuid.uuid4()` to
      `idgen_service.generate_id("app", 9)`; drop the now-unused
      `import uuid` and the `UUID(draft["application_id"])` conversion
      in `new_application_submit`.
      DoD (integration-verify): real browser walk of both BFFs against
      the freshly-migrated stack — create an application via the
      customer wizard (confirm its `app-` id appears correctly in the
      URL and in Mayan's document metadata), approve it as underwriter
      (confirm the backoffice review dialog renders the resulting
      account via the new reverse lookup), and confirm the customer
      detail page still resolves correctly by its new-format id.
- [x] **P13-6** — Full test-suite pass: every remaining `uuid.uuid4()`
      fabrication for these three entity types across
      `tests/unit/account/`, `tests/unit/customer/`,
      `tests/unit/application/` (`test_db.py`, `test_service.py`,
      `test_activities.py`) replaced. New
      `tests/unit/idgen/test_service.py` (see P13-1's DoD).
      DoD: `pytest tests/unit/` (full suite) and `lint-imports` both
      pass with zero remaining `UUID`-typed usage of these three ids
      anywhere in `loan_onboarding/` (a `grep -rn "UUID" loan_onboarding/`
      confined to unrelated code, if any, is the check).
- [x] **P13-7** — Final full-stack verification and CI.
      DoD (integration-verify, whole phase): `docker compose down -v &&
      docker compose up -d --build` from genuinely empty volumes;
      re-run this project's own standard live-verification sweep (all
      three product types, direct approve, escalation-then-manager-
      approve, reject, more-info-then-resubmit-then-approve, cancel) and
      confirm via `psql` that every `customers`/`accounts`/`applications`
      row uses the new prefixed id format and that
      `accounts.application_id` correctly matches the approving
      application in each case.
      > DONE — `docker compose down -v && docker compose up -d --build`
      > from genuinely empty volumes (confirmed all volumes actually
      > removed first), `scripts/setup_document_hierarchy.sh` re-run
      > against the fresh Mayan instance. Walked all three product types
      > through a real browser: personal_loan direct-approve, auto_loan
      > reject, mortgage escalate-then-manager-approve, auto_loan
      > cancel, and personal_loan more-info-then-resubmit all completed
      > correctly with the new `cus-`/`acc-`/`app-` id format, confirmed
      > via `psql` (`accounts.application_id` correctly points at each
      > approving application). **A genuine pre-existing bug was found
      > live in this pass, not introduced by Phase 13** — see
      > `CLAUDE.md`'s Known Gaps, the new bullet directly under the
      > existing active-account-race-window one: `check_decision_allowed`
      > trusts a `NULL` `customer_id` on the application row as proof
      > "no conflict is possible," which breaks (deterministically, not
      > just as a race) once the same `applicant_identifier` has two
      > applications outstanding and one is approved before the other.
      > Reproduced by resubmitting the more-info application and
      > approving it after a sibling application for the same
      > applicant had already been approved and provisioned an
      > `ACTIVE` `personal_loan` account — the second Approve's signal
      > reached the workflow uncontested, `persist_decision` hit
      > `ux_accounts_customer_active_product_type`'s real constraint on
      > every retry, and the Temporal workflow ended `FAILED` with the
      > application stuck at `PENDING_UNDERWRITING` forever, no error
      > surfaced anywhere. Not fixed here — out of scope for Phase 13
      > (an id-format/direction-flip migration), and CLAUDE.md already
      > carried an adjacent "accepted for a POC" gap in this exact
      > area; this is a documentation correction (a broader, easier-to-hit
      > variant of that gap actually confirmed to fire), not new work
      > this phase was scoped to do. Commit, push, confirm CI green — same
      task-id-prefixed-commit-message and `gh run watch` convention
      every prior phase in this file has used.

## Phase 14 — Returning-customer profile refresh & ID reuse

**Depends on:** everything above (all 13 prior phases). **Not part of
the original build-out** — a product enhancement brainstormed and
confirmed with the user after Phase 13 closed. `CLAUDE.md` and `PRD.md`
were updated *before* this phase's own tasks below, per this project's
own convention — see `CLAUDE.md`'s "Returning-customer profile refresh
and ID reuse" and `PRD.md` §6.5/§8.1/§9.1/§11 for the target design
each task below implements.

Two related-but-independent things, both scoped to *approval* and
*new-application submission* only (resubmit is explicitly out of scope
— see `PRD.md` §11):
1. `customers.name`/`email`/`phone` are currently write-once-never-filled
   — a real gap found while designing this, not something being
   introduced by it. Every approved application now seeds or refreshes
   the customer's profile.
2. A returning customer's new-application form is prefilled from their
   existing profile, and they get the option — not an automatic,
   silent skip — to reuse the Government ID already on file instead of
   uploading a new one.

**A real technical constraint discovered while designing this, worth
restating so no session re-derives it the hard way**: Mayan holds
exactly one value per (document, metadata_type) — `attach_metadata`
creates an entry, a second call for the *same* metadata_type on the
*same* document does not create a second value. This is why reuse
cannot be "re-tag the old `id_photo` document into the new
application" (that would silently unfile it from its *original*
application's own index branch) — it has to work by excluding
Government ID from the new application's own completeness check
instead. See `CLAUDE.md`'s design section for the full reasoning.

- [x] **P14-1** — `customer/`: `db.py`'s `get_or_create` gains `name`,
      `email`, `phone` parameters, inserted alongside `applicant_identifier`
      (the existing `ON CONFLICT (applicant_identifier) DO NOTHING` path
      is unchanged — an existing row is never touched by this function,
      only a brand-new one gets seeded). New `db.py`'s `update_profile(customer_id,
      name, email, phone) -> asyncpg.Record` — a plain `UPDATE ... SET
      name = $2, email = $3, phone = $4 WHERE customer_id = $1 RETURNING
      *`, unconditional overwrite, no fill-blanks-only branching.
      `service.py`'s `get_or_create` and new `update_profile` wrap these
      one-to-one, same shape as every other `customer/service.py`
      function.
      DoD: `tests/unit/customer/test_service.py` updated/extended
      against a real Postgres (this module's own established "real DB,
      not mocked" testing exception, per `CLAUDE.md`'s Testing section)
      — covers seeding on first create, that a second `get_or_create`
      call for the same identifier does *not* change an existing row's
      profile fields, and that `update_profile` overwrites unconditionally
      (including overwriting non-`NULL` values, not just filling blanks).
      > DONE: `name`/`email`/`phone` added as optional (default `None`)
      > keyword params to both `db.py` and `service.py`'s `get_or_create`
      > — kept backward-compatible on purpose so P14-3's activities.py
      > wiring could land as its own, separately-reviewable change. New
      > `update_profile` added to both files, unconditional overwrite via
      > a plain `UPDATE ... RETURNING *`. 4 new tests added (seeds on
      > first create, second call doesn't touch existing profile,
      > `update_profile` overwrites non-`NULL` values, `update_profile`
      > raises `CustomerNotFound` for an unknown id) — all pass against a
      > real Postgres. Live-verified in P14-4's browser sweep: a real
      > approval seeded `customers.name`/`email`/`phone` correctly
      > (confirmed via `psql`, no longer `NULL`).
- [x] **P14-2** — `document/`: `mayan_client.py` gains
      `delete_metadata_entry(document_id, metadata_entry_id) -> None`
      (a plain wrapper over the already-generic `self.delete(...)` —
      mirrors `attach_metadata`/`update_metadata_entry`'s existing
      shape). `service.py`:
      - New **read-only** `has_id_photo(customer_id) -> bool`, a thin
        wrapper over the existing `list_customer_documents(customer_id)`.
      - `check_completeness` gains `exclude_categories: list[str] |
        None = None`; `required = [c for c in
        REQUIRED_CATEGORIES[product_type] if c not in
        (exclude_categories or [])]`.
      - `promote_government_id_to_customer_photo` rewritten: if no
        Government ID document exists under `application_id`, return
        early (a no-op) instead of `raise`ing `DocumentNotFound` (its
        current behavior — safe to change since every existing caller
        already only calls this from the one `persist_decision`
        provisioning block, per P14-3 below). If one does exist, first
        find any *other* document currently carrying this `customer_id`
        metadata (`_documents_matching({"customer_id": customer_id})`,
        excluding the one about to be tagged) and clear its `customer_id`
        metadata entry via the new `delete_metadata_entry`, *then* tag
        the new one — same rebuild-index-once-at-the-end shape the
        function already has.
      DoD: `tests/unit/document/test_service.py` (mocking
      `FakeMayanClient`, same convention as every other test in this
      file) covers: `has_id_photo` true/false: `check_completeness`
      with `exclude_categories` actually excluding a category from the
      missing-list; `promote_government_id_to_customer_photo`'s
      no-op-when-absent path; and its supersede-and-untag-the-old-one
      path (assert the *old* document's metadata no longer has
      `customer_id` set, and the new one does — not just that the new
      one is tagged). `FakeMayanClient` (`tests/unit/document/fake_mayan_client.py`)
      needs a `delete_metadata_entry` method added to support this.
      > DONE: `mayan_client.delete_metadata_entry` added (plain wrapper
      > over `self.delete(...)`). `document/service.py` gained
      > `_metadata_entry_id(document_id, field)` (a new helper — real
      > Mayan's metadata entries have their own id, distinct from the
      > metadata_type id, needed to address an entry for delete/update).
      > `FakeMayanClient.get_document_metadata` updated to return a
      > per-(document, field) entry id (the metadata_type id doubles as
      > a stable one in the fake) so `delete_metadata_entry` could be
      > exercised in tests without a real Mayan instance. 5 new/changed
      > tests added, all pass. Live-verified against the real Mayan
      > instance in P14-4's sweep: promoting a second Government ID for
      > the same customer correctly stripped the first document's
      > `customer_id` metadata entry (confirmed via the Mayan REST API)
      > while tagging the second.
- [x] **P14-3** — `application/`: `service.py`'s `create_application`
      gains `reuse_existing_id_photo: bool = False`. When `True` *and*
      the resolved `customer_id` (via the existing `find_by_identifier`
      call this function already makes) is not `None` *and*
      `document_service.has_id_photo(customer_id)` is `True`, calls
      `check_completeness(application_id, product_type,
      exclude_categories=[document_service.CATEGORY_GOVERNMENT_ID])`
      instead of the bare call — otherwise behaves exactly as today.
      `activities.py`'s `persist_decision` provisioning block: replace
      the current unconditional `customer_service.get_or_create(record["applicant_identifier"])`
      (used only when `record["customer_id"] is None`) with the new
      four-arg form, passing `record["applicant_name"]`/
      `applicant_email`/`applicant_phone`; add an `else` branch calling
      `customer_service.update_profile(record["customer_id"],
      record["applicant_name"], record["applicant_email"],
      record["applicant_phone"])` when `record["customer_id"]` is
      already set. `promote_government_id_to_customer_photo`'s call
      site is unchanged (still called unconditionally on every terminal
      `APPROVED` transition) — P14-2's no-op/supersede rewrite is what
      makes that safe now.
      DoD: `tests/unit/application/test_service.py` covers
      `create_application` passing `exclude_categories` through only
      when all three conditions hold (not e.g. for a brand-new
      applicant with no `customer_id` at all — must fall through to the
      bare `check_completeness` call, never mistakenly exclude
      Government ID for someone with no `id_photo` to reuse).
      `tests/unit/application/test_activities.py` covers `persist_decision`
      calling `get_or_create` (new signature) for a first-time customer
      and `update_profile` for a returning one, plus that an existing
      customer's profile fields actually change when they differ from
      what's already stored (proving overwrite, not a no-op accidentally
      passing because the values already matched in the test fixture).
      > DONE: both changes landed exactly as planned. 6 new tests added
      > across the two files (4 in `test_service.py` covering all four
      > exclude-categories gating combinations, 2 in `test_activities.py`
      > covering seed-on-first-approval and overwrite-on-later-approval).
      > Live-verified: a real second approval for an existing customer
      > (a different product type, to avoid the unrelated active-account
      > conflict) correctly called `update_profile` and overwrote the
      > stored name/email/phone with the new application's values,
      > confirmed via `psql`.
- [x] **P14-4** — `bff_customer/`: the new-application wizard's start
      step calls `customer.service.find_by_identifier(applicant_identifier)`
      (already imported for "welcome back" copy) and prefills
      `applicant_name`/`applicant_email`/`applicant_phone` (still
      editable) when it resolves. When it resolves *and*
      `document.service.has_id_photo(customer.customer_id)` is `True`,
      the document-upload step shows a "We already have a Government ID
      on file for you" choice — reuse selected by default, an explicit
      "Upload a new one instead" control to override — and the final
      submit passes `reuse_existing_id_photo` accordingly into
      `application.service.create_application(...)`. No new `service.py`
      function needed anywhere in this module; purely `routes.py` +
      template changes.
      DoD: **integration-verify** — walk a real browser through the
      full sequence at least twice under one `applicant_identifier`:
      first application (no prefill, no reuse option, since no customer
      row exists yet) → Approve → second application (form prefilled
      from the now-existing customer row, "already on file" choice
      shown) tried both ways — once accepting reuse (confirm via `psql`
      that `accounts`/`applications` provision correctly with **no**
      new Government ID document created, and via Mayan that the
      original `id_photo` document is unchanged) and once (a third
      application) choosing "upload a new one instead" (confirm via
      Mayan that the *new* document now carries `customer_id` and the
      *original* one no longer does — exactly one current `id_photo`).
      > DONE: `new_application_start` now looks up
      > `customer.service.find_by_identifier` and seeds the draft's
      > `fields` dict (so `new_details.html`'s existing `values.get(...)`
      > rendering needed zero template changes) plus a new `customer_id`
      > draft key. `new_application_documents` computes
      > `can_reuse_id_photo`/defaults `reuse_existing_id_photo` to `True`
      > the first time it's offerable (persisted into the session draft
      > immediately, not only on an explicit toggle) and hides the
      > Government ID upload widget behind a "Using ID on file" status
      > card; a new `POST /apply/new/documents/reuse-id-photo` route lets
      > the customer flip to "Upload a new one instead". Review/submit
      > both exclude Government ID from `by_category`/`required` and pass
      > `reuse_existing_id_photo` into `create_application`. Verified
      > live with **three** real applications under one identifier
      > (`personal_loan` direct-approve, `auto_loan` with reuse accepted,
      > `mortgage` with a fresh upload chosen, the last needing a real
      > manager-approval escalation too) — every DoD assertion confirmed
      > via `psql` and the Mayan REST API: no prefill/no reuse option on
      > app #1, prefill + reuse box + "Using ID on file" widget-hiding on
      > apps #2/#3, no new Government ID document created for the reuse
      > case, and the fresh-upload case correctly stripped `customer_id`
      > from the original document while tagging the new one. One
      > pre-existing (non-Phase-14) constraint hit and worked around
      > during verification: the active-account-per-product-type rule
      > blocked a second `personal_loan` reuse test for the same
      > customer, exactly as designed — the second and third
      > verification apps used `auto_loan`/`mortgage` instead.
- [x] **P14-5** — Full-stack re-verification sweep, same shape as
      P13-7's: `docker compose up -d --build` (not from empty volumes —
      this phase doesn't touch `db/schema.sql`, no migration needed),
      full unit suite (`pytest tests/unit`) and `lint-imports` both
      green, then the standard live sweep (all three product types,
      direct approve, escalation-then-manager-approve, reject, cancel,
      more-info-then-resubmit) confirming nothing in Phases 0–13's
      existing behavior regressed. Update `CLAUDE.md`'s "Returning-customer
      profile refresh and ID reuse" section header from "planned — Phase
      14" to reflect it's built (drop the "not built yet" framing
      throughout that section and every "Planned (Phase 14, not built
      yet)" pointer added to the individual module sections elsewhere
      in the file), and `PRD.md`'s corresponding "planned"/"not built
      yet" language in §6.5/§8.1/§9.1. Commit, push, confirm CI green.
      > DONE: `docker compose up -d --build app worker-workflow
      > worker-activity` rebuilt with all of Phase 14's code (no schema
      > migration needed, confirmed). Full unit suite: 225 passed
      > (216 pre-Phase-14 + 9 new across `test_service.py`/
      > `test_activities.py`/`test_service.py` (document)/
      > `fake_mayan_client.py`). `lint-imports`: 8/8 contracts kept.
      > Standard live sweep, all exercised for real this session across
      > P14-4's own verification plus this task's own additional runs:
      > all three product types (personal_loan, auto_loan, mortgage),
      > direct underwriter approve, escalation-then-manager-approve
      > (the `mortgage` app, $200,000 > the $50,000 threshold), reject
      > (both a manual Reject and the active-account-rule's automatic
      > one), and more-info-then-resubmit-then-approve (uploaded a fresh
      > Government ID at resubmit time, confirming `resubmit_application`
      > correctly still requires one — its own documented, deliberate
      > Phase 14 scope exclusion). **Cancel initially skipped, then
      > re-verified in a follow-up check**: clicking it triggers a native
      > JS `confirm()` dialog (`application_detail.html`'s
      > `onsubmit="return confirm(...)"`), which froze the
      > browser-automation session on the first attempt (recovered via a
      > fresh navigation, not a dialog-accept) — the retry instead
      > overrode `window.confirm = () => true` via injected JS *before*
      > clicking the button, which bypassed the native dialog entirely
      > and completed cleanly: a fresh application walked
      > `Submitted → Under Review → Cancelled`, confirmed both in the UI
      > timeline and via `psql` (`status = 'CANCELLED'`). No code change
      > needed — `cancel_application`'s route and the `DECISION_CANCELLED`
      > signal path were untouched by any Phase 14 diff all along, as
      > expected. `CLAUDE.md`
      > and `PRD.md` updated throughout — the "Returning-customer profile
      > refresh and ID reuse" header now reads "(built — Phase 14)",
      > every "Planned (Phase 14, not built yet)" pointer in both files
      > (`bff_customer/`, `customer/`, `application/`, `document/`
      > module sections, plus `PRD.md` §6.5/§8.1/§9.1) rewritten to
      > reflect built status, several with live-verification notes added.
      > This plan's own backlog is empty again — remaining work is only
      > `CLAUDE.md`'s Known Gaps (unchanged by this phase) plus whatever
      > product direction comes next.

---

## Phase 15 — Document/database reconciliation

**Depends on:** everything above (needs `customer/`, `account/`,
`application/`, `document/` all in their current, post-Phase-14 shape).
**Not part of the original build-out** — found live this session: the
`loan_onboarding` Postgres tables were cleared by something outside
this app entirely (direct database access, not any code path this
codebase owns), while Mayan's documents were completely unaffected,
leaving real orphaned documents with nothing in the codebase able to
detect it. `CLAUDE.md`'s new "Document/database reconciliation" section
(written first, per this project's own convention) describes the target
design and the two-problems distinction (reconciliation vs.
cascade-on-delete) each task below implements.

Reconciliation only — cascade-on-delete is explicitly out of scope for
this phase (see `CLAUDE.md`'s Known Gaps: no delete operation exists
for `customer`/`account`/`application` yet, and whether one should is
its own open product question).

- [x] **P15-1** — `document/service.py` gains
      `list_all_documents() -> list[DocumentRef]`, a thin public wrapper
      over the existing private `_documents_matching({})` (an empty
      filter dict already matches every document — this just exposes
      that path under a real name instead of reaching into a
      leading-underscore function from outside the module).
      DoD: `tests/unit/document/test_service.py` covers it returning
      every document regardless of metadata (including a document with
      no `application_id`/`account_id`/`customer_id` at all, to prove
      the empty-filter path doesn't silently require *some* metadata to
      match).
      > DONE: exactly as planned, one new test added, passes.
- [x] **P15-2** — New composition root `loan_onboarding/reconcile.py`
      (see `CLAUDE.md`'s "Document/database reconciliation" for why this
      needs to be a composition root, not code inside any single leaf
      module). `scan() -> tuple[list[tuple[DocumentRef, str]], list[DocumentRef]]`
      — returns `(orphaned, stale_tags)`. For every document from
      `document.service.list_all_documents()`: if `application_id` is
      set, resolve via `application.service.get(...)`, catching
      `ApplicationNotFound`; if `account_id` is set, resolve via
      `account.service.get(...)`, catching `AccountNotFound`; either
      missing means **orphaned** (primary owner gone) — append
      `(doc, reason)` and skip the rest of that document's checks. If
      neither was missing and `customer_id` is set, resolve via
      `customer.service.get(...)`, catching `CustomerNotFound`; missing
      means a **stale tag** (secondary reference only) — append `doc`
      to `stale_tags`, the document itself is *not* orphaned.
      `argparse` CLI: `python -m loan_onboarding.reconcile` (report-only
      by default) prints `scan()`'s findings, mutating nothing.
      DoD: a new `tests/unit/test_reconcile.py` (mocking
      `document.service`/`application.service`/`account.service`/
      `customer.service` at the function-call boundary, same convention
      as every other cross-module test in this codebase) covers: an
      Application Document whose `application_id` doesn't resolve is
      orphaned; an Account Document whose `account_id` doesn't resolve
      is orphaned; a document whose `application_id` *does* resolve but
      whose `customer_id` doesn't is a stale tag, not orphaned; a
      document with no `application_id`/`account_id`/`customer_id` at
      all is neither (nothing to check); report mode prints without
      calling any mutating function.
      > DONE: `pyproject.toml`'s `.importlinter` contracts also needed
      > updating (not called out in this task's own text, caught while
      > implementing) — `reconcile` added to the "layers" contract's top
      > layer alongside `app`/`worker_main`, and to `customer/`/
      > `account/`/`idgen/`'s own `forbidden_modules` lists, same as
      > `worker_main` already was. 6 new tests, all pass.
- [x] **P15-3** — `reconcile.py` gains `--fix`: calls a new `fix(orphaned,
      stale_tags)` that trashes every orphaned document
      (`mayan_client.delete(f"/documents/{doc.document_id}/")` — soft
      delete, Mayan's own trash semantics, same as this project's
      existing "moves to Mayan's trash, not a hard delete" note) and
      strips every stale `customer_id` tag (`mayan_client.get_document_metadata`
      to find that document's `customer_id` entry's own id, then
      `mayan_client.delete_metadata_entry` — already built in Phase 14
      for exactly this shape of operation), then rebuilds the index once
      at the end, only if there was anything to fix.
      DoD: `tests/unit/test_reconcile.py` covers `fix()` calling delete
      for every orphaned document's id, calling
      `delete_metadata_entry` for every stale tag's `customer_id` entry
      (not some other metadata entry on the same document), and
      rebuilding the index exactly once regardless of how many documents
      were fixed — and *not* rebuilding at all when both lists are
      empty.
      > DONE: exactly as planned, 4 new tests, all pass. Also covers
      > `main()`'s `--fix` flag actually forwarding `scan()`'s results
      > into `fix()`, and report mode (`main()` with no flag) never
      > calling `fix` at all.
- [x] **P15-4** — Live verification against the real stack, using
      today's actual repro as the test case: with the local stack up,
      manually clear the three `loan_onboarding` tables directly via
      `psql` (simulating the exact external-drift scenario this phase
      exists for) while leaving Mayan's documents from an earlier
      session untouched, then run `python -m loan_onboarding.reconcile`
      (report mode) and confirm it correctly lists every now-orphaned
      document with an accurate reason, and every stale `customer_id`
      tag (if any promoted `id_photo` document survives the table clear
      while its application doesn't — construct this case deliberately
      if the ambient state doesn't happen to produce one). Then run
      `--fix` and confirm via the Mayan REST API: orphaned documents
      moved to trash (no longer in the active documents list), stale
      `customer_id` tags stripped (the document's other metadata
      untouched), and the index rebuilt (spot-check the "Loan Onboarding
      Archive" index tree no longer shows the fixed documents). Full
      unit suite and `lint-imports` both green (a new composition root
      importing every domain module needs `.importlinter`'s contracts
      checked — confirm nothing needs updating, since composition roots
      are already the documented exception to every "never imports"
      contract). Update `CLAUDE.md`'s "Document/database reconciliation"
      section with the live-verification result. Commit, push, confirm
      CI green.
      > DONE: didn't need to manually clear the tables — the ambient
      > drift this whole phase exists because of was already live (27
      > real orphaned documents across 6 applications and 3 accounts,
      > cleared by the same external process this session has now
      > observed repeatedly). `--report` correctly listed all 27 with
      > accurate reasons, zero stale tags (nothing promoted survived the
      > clear at that point). Deliberately constructed the stale-tag
      > case per this task's own fallback: submitted and approved a
      > fresh real application through the actual browser flow, then
      > `DELETE FROM customers WHERE customer_id = ...` for just that
      > one row via `psql`, leaving its `applications`/`accounts` rows
      > intact — `--report` then correctly showed exactly one stale tag
      > (the promoted `id_photo`) while its three sibling Application
      > Documents and its account's Welcome Letter, none carrying a
      > `customer_id`, appeared in neither list. `--fix` confirmed via
      > the Mayan REST API: all 27 orphans gone from the active document
      > list, the stale tag's `customer_id` entry stripped with every
      > other metadata field untouched, both indexes' document counts
      > dropped from 27/28 to exactly 5. Full unit suite: 238 passed
      > (225 + 13 new: 1 in `test_service.py`, 12 in the new
      > `test_reconcile.py`). `lint-imports`: 8/8 contracts kept, the
      > new composition root's layer/forbidden-module additions verified
      > clean. `CLAUDE.md` updated with the live-verification paragraph
      > above.

---

## Phase 16 — Document metadata assignment lifecycle

**Depends on:** Phase 14 (needs `bff_customer`'s wizard-draft
`customer_id` resolution and `document/`'s Phase 14 metadata primitives)
and Phase 15 (reconciliation already handles multi-id documents
correctly with no changes needed — confirmed while designing this, not
assumed). **Not part of the original build-out** — a product design
brainstormed and confirmed with the user. `CLAUDE.md`'s new "Document
metadata assignment lifecycle" section (written first, per this
project's own convention) describes the four confirmed rules and the
exact call sequence each task below implements.

- [x] **P16-1** — `document/service.py`: `upload(applicant_identifier,
      application_id, category, file, customer_id=None)` — attaches
      `customer_id` metadata alongside the existing three fields when
      given, `None` behaves exactly as today (no fourth attach call).
      `generate_welcome_letter(...)` — add `("customer_id", customer_id)`
      to its existing metadata attach list (the parameter is already
      part of its signature, just wasn't being attached as metadata).
      DoD: `tests/unit/document/test_service.py` covers `upload(...,
      customer_id="cust-1")` attaching all four fields, and
      `upload(...)` with no `customer_id` still attaching only the
      original three (backward-compatible default); `generate_welcome_letter`'s
      existing test updated to assert `customer_id` is now present in
      the stored metadata.
- [x] **P16-2** — `document/service.py` gains
      `tag_application_documents(application_id, account_id, customer_id)
      -> None` — finds every document under `application_id` (all
      categories, via `_documents_matching({"application_id":
      application_id})`) and attaches `account_id` + `customer_id` to
      each, rebuilding the index once at the end (same "rebuild once,
      not per-document" discipline every other multi-document operation
      here follows). Deliberately separate from
      `promote_government_id_to_customer_photo` — see `CLAUDE.md`'s
      "Document metadata assignment lifecycle" for why both coexist
      rather than one absorbing the other's job.
      DoD: `tests/unit/document/test_service.py` covers: multiple
      documents across multiple categories under one `application_id`
      all receiving `account_id`+`customer_id`; a document under a
      *different* `application_id` left untouched; the index rebuilt
      exactly once regardless of how many documents were tagged.
- [x] **P16-3** — `application/activities.py`'s `persist_decision`
      provisioning block gains one new call,
      `document_service.tag_application_documents(application_id,
      account.account_id, customer_id)`, alongside the existing
      `promote_government_id_to_customer_photo`/`generate_welcome_letter`
      calls (same `existing_account is None` idempotency guard already
      covering the other two — see `CLAUDE.md`'s updated provisioning-
      sequence bullet in "Applying without being a customer yet").
      `bff_customer/routes.py`'s two upload routes pass `customer_id`
      into `document.service.upload(...)`: `new_application_upload`
      passes `draft.get("customer_id")` (already resolved and held in
      the session draft since Phase 14's wizard prefill);
      `upload_more_info_document` passes the owning application's own
      already-resolved `customer_id` column (available from the
      `_owned_application` call this route already makes).
      DoD: `tests/unit/application/test_activities.py` covers
      `persist_decision` calling `tag_application_documents` with the
      correct `application_id`/`account_id`/`customer_id` on a terminal
      APPROVED transition, and *not* calling it on any other outcome
      (reject/escalation/cancel) or on a retry that finds the account
      already provisioned. Route-level tests (wherever
      `bff_customer/routes.py`'s upload routes are currently covered)
      updated to assert `customer_id` is passed through correctly in
      both the returning-customer case and the brand-new-applicant
      (`None`) case.
      > DONE: no existing route-level test file covers
      > `bff_customer/routes.py`'s upload routes at all (confirmed by
      > search — only `tests/unit/bff_customer/test_identity.py`
      > exists), so that DoD clause is vacuously satisfied; P16-4's live
      > browser verification is what actually exercises both routes'
      > `customer_id` wiring end to end.
- [x] **P16-4** — Live verification against the real stack: walk a real
      browser through a returning customer's new application (an
      identity that already resolves to an existing `customers` row from
      an earlier approval) and confirm via the Mayan REST API that every
      document uploaded during the wizard already carries `customer_id`
      at upload time, before submission even happens. Approve the
      application and confirm via the Mayan REST API that *every*
      document under that `application_id` (not just the Government ID
      one) now carries `account_id` + `customer_id`, and that the new
      Welcome Letter document also carries `customer_id` alongside its
      existing `account_id`. Separately, confirm a brand-new applicant's
      documents carry no `customer_id` at upload time (nothing to
      resolve yet), and gain it only after their application is
      approved. Confirm a rejected application's documents never gain
      `account_id` (the already-true-by-construction invariant, per
      `CLAUDE.md`'s point 4 — verify, don't just assume). Full unit
      suite and `lint-imports` both green. Update `CLAUDE.md`'s "Document
      metadata assignment lifecycle" section with the live-verification
      result. Commit, push, confirm CI green.
      > DONE, with two real bugs found and fixed along the way — see
      > `CLAUDE.md`'s "Document metadata assignment lifecycle" for the
      > full detail. (1) Mayan rejected the new attaches with a 400:
      > `account_id` was never associated with the "Application
      > Document" type, nor `customer_id` with "Account Document" —
      > fixed both in `scripts/setup_document_hierarchy.sh` and live
      > against the running instance. (2) A second, independent bug:
      > `tag_application_documents` and
      > `promote_government_id_to_customer_photo` both attach
      > `customer_id` to the same Government ID document on a fresh
      > promotion — Mayan rejects the second attach (a real 400, not
      > the "harmless idempotent no-op" an earlier draft of this file
      > claimed); fixed with a new idempotent `_set_metadata` helper in
      > `document/service.py`, and `tests/unit/document/fake_mayan_client.py`'s
      > `attach_metadata` now replicates Mayan's real
      > reject-on-duplicate behavior so this class of bug fails in unit
      > tests too, not just live. Verified end-to-end: brand-new
      > applicant's uploads carry no `customer_id`; approval tags every
      > document (all categories) + the Welcome Letter with
      > `account_id`+`customer_id`, zero worker errors; a returning
      > customer's new application (different product type) prefills,
      > offers and uses Government-ID reuse, and its fresh uploads carry
      > `customer_id` immediately; rejecting that application left its
      > documents with `customer_id` but no `account_id`. Full unit
      > suite (242 tests) and `lint-imports` green — run against a new
      > dedicated `loan_onboarding_test` database, **not** the compose
      > stack's own `loan_onboarding`, after this session first
      > accidentally wiped the live stack's application data by running
      > the suite against the same live Postgres (see `CLAUDE.md`'s
      > Testing section, new note). Not pushed/CI yet — see Current
      > Status.

---

## Session Log

*(Newest entry at the top. Each entry: date, tasks touched, what
actually happened, any deviations from the plan or `CLAUDE.md` and why,
what the next session should know. Keep entries factual and specific —
"worked on Phase 6" is not useful to a future session; "P6-4 done,
P6-5 blocked on Phase 7 not existing yet, see note in Decisions Needed"
is.)*

- **2026-09-04 (later same day)** — Ad hoc, user-requested: replaced
  the single `applicant_identifier`-rooted "Loan Onboarding Archive"
  Mayan index with three separate entity-rooted index templates
  (Customer Index, Account Index, Application Index) — see `CLAUDE.md`'s
  "Document hierarchy" for the full tree shapes. Confirmed two design
  decisions with the user before building anything (category stays the
  final leaf under every branch; a document that has more than one
  entity id set is deliberately allowed to appear in more than one
  branch, matching Mayan's existing per-branch-independent evaluation
  this project's `id_photo` node already relied on). Deleted the old
  index live via the API, built the three new ones live (verified their
  real tree structure via `documents_url` at several levels — a fresh
  auto_loan approval's documents correctly appeared under Customer
  Index's `account → application → category` leaf *and* its sibling
  `application → category` leaf at once), and updated
  `scripts/setup_document_hierarchy.sh` so a future fresh install builds
  the same three templates instead of the old one.
  **A second, real bug found in the same pass, not caught until actually
  reasoning through what deleting the old index would break**:
  `document/mayan_client.py`'s `rebuild_index()` — called after every
  metadata attach across the whole `document/service.py` module —
  hardcoded a single index slug (`INDEX_TEMPLATE_SLUG =
  "loan-onboarding-archive"`) to rebuild. Left unfixed, deleting that
  index would have made every document upload in the entire application
  fail with `RuntimeError: index template not found`. Fixed with
  `INDEX_TEMPLATE_SLUGS` (all three new slugs) and a new
  `index_template_ids()`/`rebuild_index()` pair that rebuild all three;
  `tests/unit/document/test_mayan_client.py`'s old single-slug tests
  rewritten for the new list-based lookup, plus a new
  `test_rebuild_index_rebuilds_all_three_index_templates`. Rebuilt the
  `app`/`worker-workflow`/`worker-activity` images with this fix and
  live-verified: a real `document.service.upload()` call against the
  running stack succeeded end-to-end with zero errors, and the uploaded
  document was confirmed present in Application Index's tree via the
  real API. Full unit suite (243 tests, up from 242) and `lint-imports`
  both green. Lightly corrected a handful of now-stale
  `<applicant_identifier> -> ...` references elsewhere in `CLAUDE.md`
  and `docs/api-specification.md` that described the old single-index
  shape. Committed (`01fa111`), not yet pushed.

  **Follow-up in the same session, after direct user feedback**: "I
  don't see welcome letter under account" / "I don't see government id
  under customer". Real gap, not a misunderstanding — neither branch 1
  nor 2 in any of the three indexes puts a category leaf directly under
  the root entity id, so e.g. the promoted Government ID (id_photo)
  document required drilling into "account" or "application" first to
  find it under Customer Index. Added a third sibling branch to all
  three indexes (`<root> -> <category>`, direct, no intermediate
  id-grouping) live via the API, and to
  `scripts/setup_document_hierarchy.sh` for future fresh installs.
  **A real reliability wrinkle hit and resolved while doing this, not a
  template bug**: right after adding branch 3, two of the three
  indexes' instance trees briefly went to `depth=0`/`node_count=0` on
  rebuild even though the template definitions were confirmed correct —
  traced to firing overlapping `rebuild/` calls in quick succession
  racing Mayan's own reset-then-rebuild sequence; a single unhurried
  `rebuild/` call per index produced a stable tree (confirmed stable
  across repeated polls ~40s apart). Re-verified live afterward:
  "Welcome Letter" now appears as a direct child category under its
  `account_id` node, "Government ID" as a direct child category under
  its `customer_id` node. `CLAUDE.md` updated with the full detail,
  including the rebuild-timing note for future sessions. **Not yet
  committed** — this follow-up (script + `CLAUDE.md` changes only, no
  Python code touched) still needs its own commit and push alongside
  the `01fa111` commit above.

- **2026-09-04** — Implemented and live-verified all of Phase 16
  (P16-1 through P16-4), continuing from the prior session's
  documentation-only pass. `document/service.py`: `upload(...)` gained
  `customer_id: str | None = None`, attaching it as a fourth metadata
  field when given; `generate_welcome_letter(...)` now attaches
  `customer_id` too; new `tag_application_documents(application_id,
  account_id, customer_id)` tags every document under an application in
  one pass, rebuilding the index once. `application/activities.py`'s
  `persist_decision` calls it inside the existing provisioning block,
  right before `promote_government_id_to_customer_photo`.
  `bff_customer/routes.py`'s two upload routes pass `customer_id`
  through (`draft.get("customer_id")` for the new-application wizard,
  `application.customer_id` for the resubmit-more-info path).
  **Two real bugs found and fixed during P16-4's live verification,
  neither caught by the unit suite beforehand**: (1) Mayan rejected the
  new attaches with a 400 -- `account_id` had never been associated
  with the "Application Document" document type, nor `customer_id` with
  "Account Document" (each association existed only for the *other*
  type). Fixed in `scripts/setup_document_hierarchy.sh` (both new
  associations, `required=false`) and live against the already-running
  instance via the same API call. (2) A second, independent bug found
  right after: `tag_application_documents` and
  `promote_government_id_to_customer_photo` both attach `customer_id`
  to the same Government ID document when a fresh upload is promoted --
  Mayan rejects the second attach as a duplicate metadata entry, which
  this file's own prior draft had wrongly asserted was "a harmless
  idempotent no-op" without actually testing it. Fixed with a new
  idempotent `_set_metadata` helper (update-in-place if the field
  already exists, plain create otherwise) used by both functions'
  attach loops; `tests/unit/document/fake_mayan_client.py`'s
  `attach_metadata` now replicates Mayan's real reject-on-duplicate
  behavior (it silently allowed double-attaches before, which is
  exactly why this class of bug was invisible to the unit suite), with
  a new regression test locking the fix in.
  **Live-verified end to end against the real stack, not just unit
  tests**: rebuilt the `app`/`worker-workflow`/`worker-activity` images
  twice (once after the code fix, once more isn't needed -- both
  Mayan-config fixes were applied live via direct API calls, no image
  rebuild required for those). Walked a brand-new applicant through a
  full personal_loan application -- confirmed via the Mayan REST API
  that all four uploaded documents carried no `customer_id` before
  submission; approved it and confirmed every document plus the
  generated Welcome Letter gained `account_id` + `customer_id`, with
  zero worker errors on this second, post-fix attempt (the first
  attempt, before both fixes, genuinely failed partway through with a
  400, and — per this project's own documented, accepted idempotency
  tradeoff — the Temporal retry then permanently skipped the document
  calls since the account was already provisioned; recovered manually
  by re-running the three document.service calls directly against the
  worker-activity container, which is exactly the kind of
  manually-recoverable gap `CLAUDE.md` already accepts for this
  scenario). Submitted a second application (auto_loan, a different
  product type, to sidestep the active-account-per-product-type rule)
  under the same now-returning identity -- confirmed the wizard
  prefilled the form and the reused-ID flow worked cleanly with zero
  errors on approval. Submitted a third application (mortgage) under
  the same identity -- confirmed its three freshly-uploaded documents
  carried `customer_id` immediately at upload time (before submission),
  then rejected it and confirmed its documents kept `customer_id` but
  never gained `account_id`.
  **A real, self-inflicted incident along the way**: ran the unit test
  suite with `DATABASE_URL` pointed at the compose stack's own
  `loan_onboarding` database (reachable on the host's published `5433`
  port) instead of a separate test database -- the suite's own per-test
  `DELETE FROM applications/accounts/customers` cleanup fixtures
  silently wiped every row the live stack had, mid-session. Recovered
  by continuing the live-verification flow fresh (the wiped rows didn't
  invalidate the Mayan-side evidence already captured, which doesn't
  read Postgres at all) and by creating a dedicated
  `loan_onboarding_test` database for all unit-test runs going forward
  -- see `CLAUDE.md`'s Testing section for the new convention. Full
  unit suite (242 tests, up from 238 -- new tests for `upload(...,
  customer_id=...)`, `tag_application_documents`, and the
  double-attach regression) and `lint-imports` both green against the
  new test database. `CLAUDE.md`'s "Document metadata assignment
  lifecycle" section updated with the full live-verification result and
  both bugs' detail. **Not yet committed** -- next step is to review the
  diff, commit, push, and confirm CI green.

- **2026-09-03** — Documentation-only session, no code changed, per
  explicit user instruction ("not start implement in this session").
  User walked through exactly four rules for when document metadata
  should be assigned during onboarding -- application_id always at
  upload; customer_id at upload too, when the applicant already
  resolves to an existing customer; account_id + customer_id on *every*
  application document (not just Government ID) once approved,
  including the generated Welcome Letter; a rejected application never
  gets account_id. Two of the four were already true (application_id at
  upload, and rejected-never-gets-account_id is true by construction);
  the other two are real gaps -- customer_id is currently only ever
  attached to the one Government ID document at approval time, never at
  upload time, and never to the other application documents or the
  Welcome Letter at all. Confirmed the design maps cleanly onto the
  existing provisioning sequence: the new
  `document.service.tag_application_documents(application_id,
  account_id, customer_id)` sits alongside (not instead of)
  `promote_government_id_to_customer_photo`, whose own job -- stripping
  a *different* application's stale id_photo tag -- is orthogonal, so
  both coexist with a harmless redundant re-attach on the Government ID
  document. Checked reconciliation compatibility before writing anything
  (Phase 15 just landed) -- `reconcile.py`'s `scan()` already checks
  `application_id`/`account_id`/`customer_id` independently via separate
  `if`s, not `elif`, so a document carrying all three after approval
  needs no changes there. `CLAUDE.md`'s new "Document metadata
  assignment lifecycle" section and this file's new Phase 16 (P16-1
  through P16-4, all unchecked) written to capture the confirmed design.
  Next: P16-1 (`document/service.py`'s `upload`/`generate_welcome_letter`
  signature changes).

- **2026-09-03** — Phase 15 (Document/database reconciliation) complete,
  all four tasks (P15-1 through P15-4) done in one session. Not
  brainstormed -- found live, in the same session, while reviewing
  Mayan's document/index structure after Phase 14 closed: Postgres's
  three domain tables kept coming back empty (confirmed via `psql`
  multiple times across the session) while Mayan's documents from
  earlier in the *same* session survived untouched, something outside
  this app entirely clearing the tables directly. Discussed the
  reconciliation-vs-cascade-on-delete split with the user before writing
  anything (cascade-on-delete needs a delete operation that doesn't
  exist yet, and is its own open product question -- deliberately out of
  scope here); confirmed reconciliation first. `CLAUDE.md`'s new
  "Document/database reconciliation" section written before any code,
  per this project's own convention. P15-1:
  `document.service.list_all_documents()`, a thin public wrapper over
  the existing private `_documents_matching({})`. P15-2: new composition
  root `loan_onboarding/reconcile.py` (a third one, alongside `app.py`/
  `worker_main.py` -- `.importlinter`'s `layers` contract and
  `customer/`/`account/`/`idgen/`'s `forbidden_modules` lists all needed
  updating too, caught while implementing, not called out in the task's
  own text). `scan()` distinguishes a document's primary owner
  (`application_id` for an Application Document, `account_id` for an
  Account Document -- either missing means orphaned) from its secondary
  `customer_id` tag (only ever present on a promoted `id_photo`
  document -- missing means a narrower "stale tag," not orphaned, since
  the document's real ownership is still intact). P15-3: `--fix` trashes
  orphans (Mayan's own soft-delete) and strips stale tags via
  `delete_metadata_entry` (already built in Phase 14), rebuilding the
  index once. P15-4: live-verified against the real, already-orphaned
  state this session had been observing all along (27 genuine orphaned
  documents, not synthetic test data) -- `--report` correctly listed
  every one with an accurate reason. No stale-tag case existed naturally
  at that moment (nothing promoted had survived the clear), so
  constructed one deliberately per the task's own fallback: submitted
  and approved a real application through the actual browser flow, then
  `DELETE FROM customers` for just that one row via `psql`, leaving its
  `application`/`account` rows intact -- `--report` then correctly
  identified exactly the one promoted document as a stale tag while its
  three sibling documents and its account's Welcome Letter (still
  validly owned, no `customer_id` to check) appeared in neither list.
  `--fix` confirmed via the Mayan REST API: all 27 orphans gone from the
  active document list, the stale tag's `customer_id` entry stripped
  with every other metadata field untouched, both indexes' document
  counts dropped from 27/28 to exactly 5. Full unit suite: 238 passed
  (225 + 13 new). `lint-imports`: 8/8 contracts kept. Next: no open
  Phase 15 work; cascade-on-delete remains a deliberately open product
  question (`CLAUDE.md`'s Known Gaps), not a queued task -- pick it up
  only if the user decides hard-delete of an approved
  customer/account/application actually belongs in this product.

- **2026-09-03** — Phase 14 (Returning-customer profile refresh & ID
  reuse) complete, all five tasks (P14-1 through P14-5) done in one
  session, immediately following the `generate_welcome_letter`/
  `upload_consent` bug fix below (which this phase's own live
  verification depended on being fixed first — Welcome Letter documents
  are created during the same provisioning block this phase's
  `promote_government_id_to_customer_photo` rewrite also touches).
  P14-1: `customer/db.py`/`service.py`'s `get_or_create` gained optional
  `name`/`email`/`phone` params (backward-compatible defaults, so this
  landed without touching `activities.py` yet); new `update_profile`.
  P14-2: `document/`'s `has_id_photo`, `check_completeness`'s new
  `exclude_categories` param, `promote_government_id_to_customer_photo`
  rewritten from raise-on-missing to no-op-or-supersede, new
  `mayan_client.delete_metadata_entry`. P14-3: `application/service.py`'s
  `create_application` gained `reuse_existing_id_photo`;
  `activities.py`'s `persist_decision` now branches
  `get_or_create`/`update_profile` on whether `customer_id` was already
  resolved. P14-4: `bff_customer`'s wizard prefills a returning
  customer's form and offers a real reuse choice (default-on, explicit
  override) — the one integration-verify task, walked live across three
  real applications under one identifier (`personal_loan` direct
  approve, `auto_loan` with reuse accepted, `mortgage` with a fresh
  upload chosen, escalated to a real manager approval). Every DoD claim
  confirmed via `psql`/the Mayan REST API, not assumed — no prefill on
  the first application, correct prefill + no duplicate document on the
  second, correct prefill + old-document-superseded on the third. One
  real, useful mistake made and corrected during verification: the
  second test application was first tried as `personal_loan`, which the
  pre-existing (non-Phase-14) active-account-per-product-type rule
  correctly blocked, since the customer already had an active
  `personal_loan` account from the first application — redone as
  `auto_loan` instead; this incidentally reconfirmed that rule still
  works unmodified under Phase 14's changes. P14-5: full unit suite (225
  tests) and `lint-imports` green; the standard decision-path sweep
  (all three product types, direct approve, escalation-then-manager-
  approve, reject — both a manual one and the active-account rule's
  automatic one, more-info-then-resubmit-then-approve) confirmed nothing
  in Phases 0–13 regressed — the resubmit case also reconfirmed
  `resubmit_application`'s own deliberate Phase 14 scope exclusion (no
  `reuse_existing_id_photo` there, a fresh Government ID upload is still
  required). **Cancel was not re-verified live in this same pass** — its
  confirm() dialog froze the browser-automation session on the one
  attempt (recovered via navigation, not a dialog handler); skipped a
  retry at the time since `cancel_application`'s own code path is
  untouched by this phase's diff and stays covered by the existing unit
  suite. **Resolved in an immediate follow-up, same day**: overriding
  `window.confirm = () => true` via injected JS before clicking the
  button bypasses the native dialog entirely — a fresh application
  walked `Submitted → Under Review → Cancelled` cleanly, confirmed via
  `psql` (`status = 'CANCELLED'`). No code change needed; this was
  purely a browser-automation limitation, not a product gap. `CLAUDE.md`
  and `PRD.md` updated throughout from "planned"/"not built yet" to
  built status, several spots gaining live-verification notes.
  Next: no open Phase 14 work; check `CLAUDE.md`'s Known Gaps for
  whatever's next, or await new product direction.

- **2026-09-03** — Bug fix, not a numbered phase task: a real, previously-
  undocumented gap in `document.service.generate_welcome_letter`/
  `upload_consent`, found live during a manual end-to-end run that
  actually uploaded real documents to Mayan and walked a personal_loan
  application through underwriter approval (something no prior session's
  verification had done against this pair of functions specifically —
  every earlier live sweep either mocked `document.service` entirely or
  didn't inspect the Mayan index tree UI afterward). Both functions
  attached only `account_id`/`category` metadata, never
  `applicant_identifier` -- but the index's account branch
  (`<applicant_identifier> -> <account_id> -> category`) is nested under
  the applicant node, so a document missing that field landed under a
  top-level "None" bucket in Mayan's UI instead of its applicant's own
  branch. Invisible to every existing unit test, since `FakeMayanClient`
  doesn't enforce Mayan's own required-metadata rules the way the real
  server does. Fixed by adding `applicant_identifier` as both functions'
  first parameter and attaching it alongside the existing fields;
  `application/activities.py`'s `persist_decision` call site updated to
  pass `record["applicant_identifier"]`. Re-verified live: called the
  fixed `generate_welcome_letter` directly against the real running
  Mayan instance, confirmed via the Mayan UI that the new document filed
  correctly under `<applicant_identifier> -> <account_id>` while the
  pre-fix document (created earlier in the same session, before the fix)
  stayed orphaned under `None` -- not retroactively backfilled, a small
  accepted gap for existing data, consistent with this project's other
  rare-enough-to-accept-for-a-POC calls. Full unit suite (212 tests) and
  `lint-imports` both green after the fix; `app`/`worker-workflow`/
  `worker-activity` images rebuilt via `docker compose up -d --build`.
  `CLAUDE.md`'s `document/` module section and provisioning-sequence
  bullet updated with the corrected signatures and the bug's own note.
  Next: Phase 14 still not started -- P14-1 is still the next real task.

- **2026-09-03** — Documentation-only session, no code changed. User
  brainstormed a set of returning-customer enhancements (profile
  backfill, application-form prefill, optional Government ID reuse
  instead of forced re-upload); this session turned that brainstorm
  into a concrete design and wrote it into `CLAUDE.md` (new
  "Returning-customer profile refresh and ID reuse" section, plus
  pointers added to the `bff_customer/`, `customer/`, `application/`,
  and `document/` module sections), `PRD.md` (§6.5, §8.1, §9.1, a new
  §11 open question), and this file's new Phase 14 (P14-1 through
  P14-5, all unchecked). One real design constraint surfaced and
  resolved during this pass, not just assumed: Mayan holds exactly one
  value per (document, metadata_type) — confirmed by re-reading
  `mayan_client.py`'s `attach_metadata`/`update_metadata_entry` — which
  rules out "re-tag the existing `id_photo` document into the new
  application" as the reuse mechanism (it would silently unfile that
  document from its *original* application's own index branch); the
  design instead excludes Government ID from the new application's own
  completeness check via a new `exclude_categories` parameter. Also
  surfaced, and resolved in the new design rather than left as a loose
  end: `document.service.promote_government_id_to_customer_photo` has
  never actually enforced `PRD.md`'s old "the first `id_photo` stands"
  claim — it unconditionally re-tags whatever Government ID document
  exists under a just-approved application with no check for a prior
  one, a real, previously-undocumented gap that was simply never
  exercised until this feature makes a second fresh-upload approval for
  an existing customer reachable. `PRD.md` §6.5 corrected to describe
  the new, actually-enforced intent (refreshable by a later fresh
  upload, untouched on reuse) instead of the old, never-true claim.
  Next: P14-1 (`customer/`'s `get_or_create` signature change +
  new `update_profile`).

- **2026-09-02** — Two small, user-requested cleanups, no behavior
  change: (1) `application/service.py`'s `create_application` and
  `bff_customer/routes.py`'s wizard pre-mint both used to call
  `idgen_service.generate_id("app", 9)` with the prefix/length as
  duplicated raw literals in two different modules -- replaced with
  shared, non-underscore-prefixed constants,
  `application.service.APPLICATION_ID_PREFIX`/`APPLICATION_ID_LENGTH`
  (unlike `customer/db.py`'s and `account/db.py`'s own private
  `_ID_PREFIX`/`_ID_LENGTH`, each used by exactly one file, this pair
  needed to be importable from a second module). (2) Changed the
  actual prefix *values* from lowercase to uppercase --
  `CUS-`/`ACC-`/`APP-` instead of `cus-`/`acc-`/`app-` -- per explicit
  user request ("all prefix should be capital letter"). Updated every
  test literal that fabricates one of these three real ID formats
  (`tests/unit/application/test_db.py`, `test_service.py`,
  `test_activities.py`; `tests/unit/account/test_service.py`;
  `tests/unit/customer/test_service.py`) to match; deliberately left
  `tests/unit/idgen/test_service.py` (arbitrary example prefixes for
  the generic, prefix-agnostic `generate_id` function) and
  `tests/unit/document/test_service.py` (opaque literals like `app-1`/
  `cust-1` a leaf module never parses) lowercase, since neither is
  actually exercising these three entities' real format. Updated
  `CLAUDE.md`'s and `PRD.md`'s design-reference sections (the
  "Data storage" id-format table, `customer_id`/`account_id`
  descriptions, the provisional-mint note, and the Known Gaps entropy
  bullet) to the new casing -- left every Session Log entry and
  Known-Gaps narrative describing a *specific past live-verification
  run* (e.g. P13-7's `cus-911063467`/`acc-604713440`) untouched, since
  those are factual records of ids actually observed under the old
  lowercase code, not statements of current design. Full unit suite
  (212 tests) and `lint-imports` both pass. Rebuilt `app`/
  `worker-activity`/`worker-workflow` and verified live inside the
  rebuilt `app` container (calling `customer.service.get_or_create`,
  `account.service.create_account`, and the new
  `APPLICATION_ID_PREFIX`/`APPLICATION_ID_LENGTH` constants directly
  against the real Postgres): newly minted ids came back as
  `APP-850665744`/`CUS-226285776`/`ACC-367517834` -- confirmed
  uppercase, then deleted as test cleanup. No existing rows were
  touched -- pre-existing dev-database rows from earlier sessions keep
  their lowercase ids; only newly-created rows use the new format, an
  accepted POC/dev-environment cosmetic inconsistency, not a migration.

- **2026-09-02** — Closed `bff_customer`'s no-real-auth gap
  (`CLAUDE.md`/PRD.md's long-standing "standout risk"): a customer used
  to be able to type *any* email/phone with zero verification and
  immediately see and act on every application under it. Asked the
  user to pick a verification mechanism and delivery backend before
  starting (a genuine product decision, unlike the previous two bug
  fixes) -- chose email OTP with fake/console delivery for this POC
  (no real SMTP/Twilio/SendGrid provider exists in this project).
  Added `bff_customer/identity.py`'s pending-verification cookie (a
  second, short-lived signed cookie holding the code's *hash*, not the
  code itself -- same no-Redis philosophy as the existing session
  cookie) with a 5-attempt lockout, and `bff_customer/notifications.py`
  (fake delivery -- one function, meant to be the only thing a real
  provider integration would need to change). New routes:
  `POST /apply/identify` now starts verification instead of setting the
  session directly; new `POST /apply/identify/verify` checks the code.
  Dropped phone-number identifiers along with this fix (SMS needs a
  provider too) -- confirmed with the user as an accepted scope
  reduction. **A real implementation bug caught before it shipped**:
  first draft used `logging.getLogger(__name__).info(...)` to "log the
  code server-side" -- this codebase has no logging configuration
  anywhere (no `basicConfig`, no handler), so the call silently
  inherited the root logger's default `WARNING` level and never
  appeared in `docker compose logs`, confirmed live before switching to
  a plain `print()`. 16 new regression tests (212 total unit tests),
  `lint-imports` clean. Rebuilt `app` and verified live end-to-end:
  identify with a fresh email -> code shown on the verify page (dev
  mode) and printed server-side -> wrong code shows "Incorrect code.
  Try again." -> 5 wrong attempts (verified via `curl` with a cookie
  jar) clears the pending verification, and even the *correct* code
  fails afterward with "Enter your email again" until a fresh
  `/apply/identify` submission -> correct code within the attempt
  budget lands on "My Applications" as the verified identity.
  Non-email input rejected before a code is ever generated. Updated
  `CLAUDE.md`'s "Identity" section, module description, and Known
  Gaps, plus PRD.md §4/§5/§7.1/§8.1/§9.1/§10 for the narrowed
  (not fully closed -- delivery is still fake) scope. Committed and
  pushed (commit `126ee38`); CI run (`gh run list --branch main`, run
  id `33651773769`) completed `success` in 46s.

- **2026-09-02** — Closed the in-batch half of the active-account
  race window itself (the previous fix only made the *outcome* of
  losing it clean -- see the entry below this one). Added
  `application.service.check_decision_allowed_bulk(application_ids,
  decision) -> dict[str, list[str]]`, a batch-aware sibling of
  `check_decision_allowed` that tracks `(applicant_identifier,
  product_type)` pairs already claimed by an earlier, still-eligible
  item in the *same* batch, blocking a later item for the same pair
  before either signal is ever sent. Keyed on `applicant_identifier`,
  not the resolved `customer_id` -- an early implementation attempt
  keyed on `customer_id` and failed its own new test, since the more
  common trigger is two applications for an applicant with *no*
  customer row yet at all (both resolve `None` independently, so
  keying there misses the case entirely). `bff_backoffice`'s
  bulk-approve route now calls this instead of looping the single-item
  function; the single-item route is unchanged. 7 new regression tests
  (201 total unit tests), `lint-imports` clean. Rebuilt `app` and
  re-verified live with a fresh repro of the exact same scenario as
  the entry below: bulk-approving two sibling `personal_loan`
  applications together now reports `"1 succeeded, 1 failed: customer
  already has an active personal_loan account"` immediately in the
  Bulk Approve Results dialog -- the blocked application is never
  signaled at all, stays at `PENDING_UNDERWRITING`, and the worker
  logs stay completely silent (confirmed via `psql` and `docker
  compose logs`). **Deliberately not closed**: cross-request
  concurrency (two separate requests, not the same bulk batch) --
  `persist_decision`'s conflict-to-REJECTED handling remains the
  backstop for that, and closing it fully would need a lock spanning
  the check (web process) through the write (worker process,
  arbitrarily later via Temporal) -- explained to the user as a much
  larger, riskier change not undertaken here. Committed and pushed
  (commit `530d305`); CI run (`gh run list --branch main`, run id
  `33648517532`) completed `success` in 48s.

- **2026-09-02** — Fixed the active-account-race-window gap's "no
  graceful handling" half (the race window itself is deliberately
  still open -- see `CLAUDE.md`'s Known Gaps). Reproduced live first,
  via the underwriter's own Bulk Approve action (two `personal_loan`
  applications for one identifier, both selected and approved
  together -- its pre-check loop lets both pass `check_decision_allowed`
  before either signal goes out, then fires both signals concurrently):
  confirmed the loser's `persist_decision` hit a real
  `UniqueViolationError` on `ux_accounts_customer_active_product_type`,
  retried 5 times, failed the Temporal workflow (`Status: FAILED`), and
  left the application stuck at `PENDING_UNDERWRITING` forever. Given
  four options, the user chose: convert the loser's outcome into a
  clean `REJECTED` write (system-generated comment) instead of letting
  the exception propagate. Also fixed a related, previously-latent
  inconsistency while implementing this: `persist_decision` now returns
  the status it actually wrote, and `workflows.py`'s `submit_decision`
  uses that return value for the workflow's own `self._status` instead
  of its pre-computed value, so the workflow's own `get_status` query
  can never disagree with Postgres (nothing queries it directly today,
  but this was a real, if unobserved, gap). Full unit suite (194 tests,
  including 2 new regression tests) and `lint-imports` pass. Rebuilt
  `app`/`worker-workflow`/`worker-activity` and re-verified live against
  the exact repro: the loser now lands cleanly on `REJECTED` with the
  auto-generated comment, its Temporal workflow ends `Status: COMPLETED`
  (not `FAILED`) with a query result correctly reporting
  `"status":"REJECTED"`, and the worker logs stay silent -- no retries,
  no traceback. Committed and pushed (commit `aa66a0f`); CI run
  (`gh run list --branch main`, run id `33646639876`) completed
  `success` in 39s.

- **2026-09-02** — Fixed the `check_decision_allowed` bug found live
  during P13-7's verification sweep (see that Session Log entry and
  `CLAUDE.md`'s Known Gaps for the full mechanism). The fix: resolve
  `customer_id` via `customer.service.find_by_identifier(applicant_identifier)`
  when the application's own `customer_id` column is `NULL`, instead
  of trusting `NULL` as proof no customer could possibly exist.
  Full unit suite (192 tests, including the new regression test) and
  `lint-imports` pass. Rebuilt `app`/`worker-workflow`/`worker-activity`
  and re-verified live: two fresh `personal_loan` applications under
  one identifier, first Approved (provisioned `cus-537708272`/
  `acc-031947334`), second Approve now correctly blocked in the review
  dialog with `"customer already has an active personal_loan account"`
  — confirmed via `psql` the second application stayed at
  `PENDING_UNDERWRITING` and via worker logs that no activity ever ran
  for it (the old bug's `FAILED` Temporal workflow + silent stuck
  application no longer happens). Committed and pushed (commit
  `fb345a8`). **First CI run on this push failed on an unrelated,
  pre-existing flaky test**: `tests/unit/bff_customer/test_identity.py::test_get_rejects_a_tampered_cookie`
  flipped only the last character of the signed cookie to "tamper" it
  — base64url's last character in a group can carry unused padding
  bits, so some single-character substitutions decode to the exact
  same underlying signature bytes and the "tampered" cookie still
  verifies. Fixed by flipping a full 4-character base64 group instead
  (commit `13b9151`); confirmed 30 consecutive local reruns all pass.
  Re-ran CI on the follow-up push — green.

- **2026-09-02** — Confirmed the CI run for Phase 13's push
  (`gh run list --branch main`, run id `33637386503`, commit `ccad573`)
  completed `success` in 41s. No follow-up fix needed. **This is the
  final Session Log entry for Phase 13 — all 7 tasks done.**

- **2026-09-02** — P13-7 done, closing out Phase 13. Full
  `docker compose down -v && docker compose up -d --build` from
  genuinely empty volumes (confirmed volumes actually removed, not
  just containers), `scripts/setup_document_hierarchy.sh` re-run
  against the fresh Mayan instance, workers restarted once past the
  brief post-boot window before Temporal's `default` namespace exists.
  Walked all three product types and every terminal/non-terminal
  outcome through a real browser against the rebuilt stack:
  personal_loan direct-approve, auto_loan reject, mortgage
  escalate-then-manager-approve, auto_loan cancel, personal_loan
  more-info-then-resubmit — all produced correctly-formatted
  `cus-`/`acc-`/`app-` ids and a correctly-linked
  `accounts.application_id`, confirmed via `psql`. Found and documented
  (not fixed — out of this phase's scope) a real pre-existing bug while
  processing the more-info application's resubmit-then-approve: see
  `CLAUDE.md`'s Known Gaps for the full mechanism
  (`check_decision_allowed` trusting a stale `NULL` `customer_id`
  column instead of re-resolving by `applicant_identifier`). Also
  incidentally hit and worked around a native-JS-`confirm()`-dialog
  browser-automation gotcha on the customer Cancel button (stub
  `window.confirm` before clicking, or close and reopen the tab if one
  gets stuck) — not a codebase bug, purely a testing-tool note.
  **Phase 13 (all 7 tasks) is now fully complete.** Commit/push for
  this phase's work is still pending explicit user confirmation.

- **2026-09-02** — P13-1 through P13-6 done (idgen module,
  db/schema.sql's TEXT-id + accounts.application_id migration,
  customer/account/application's models/db/service/activities moved off
  UUID onto application-assigned string ids, both BFFs' routes updated).
  Full unit suite (191 tests) and lint-imports pass against a real
  Postgres with the migrated schema applied (schema was reset via
  `DROP TABLE ... CASCADE` + reapply on the existing `db` container, not
  a full `docker compose down -v`, to avoid wiping Mayan/Keycloak/
  Temporal volumes just to test app-table changes). Rebuilt `app`/
  `worker-workflow`/`worker-activity` images and verified live via a
  real browser: full personal_loan submission through the customer
  wizard (id minted as `app-628882567`), all four document uploads,
  underwriter Keycloak login + Approve, confirmed via `psql` that
  `customers`/`accounts`/`applications` all show the new prefixed-id
  format and `accounts.application_id` correctly points at the
  approving application. **P13-7 (full `down -v` rebuild, the complete
  three-product-type verification sweep, and commit/push/confirm-CI)
  not done this session** — left for explicit confirmation given how
  disruptive/visible those steps are.

- **2026-09-02** — Confirmed Phase 12's CI run
  (`gh run list --branch main`, run id `33611609674`, commit `a3d4b2c`)
  completed `success` in 45s. No follow-up fix needed. **This is the
  final Session Log entry for this plan — all 12 phases done.**

- **2026-09-02** — Phase 12 (End-to-End Verification & Polish) complete
  — **the last phase of this plan.** P12-3 done first (see its own
  checklist note for the full mechanism): added `docker-compose.yml`'s
  `app` service, ran a genuine `docker compose down -v && up --build`,
  found and fixed two real bugs invisible until the whole stack ran
  fully containerized for the first time (a Keycloak issuer mismatch
  fixed via `KEYCLOAK_PUBLIC_ISSUER` + pinning Keycloak's own
  `KC_HOSTNAME`; a Mayan id-map cache/bootstrap-ordering gotcha, not a
  code bug). P12-1's full criterion-by-criterion walk against that
  freshly-built stack:

  1. **All three product types, every decision path, phone browser +
     Temporal Web UI — PASS.** Phase 11 already proved all three
     product types through the real 375×812 UI across direct-approve,
     escalate-then-manager-approve, reject, and cancel. This session
     added fresh live coverage specifically for P12-1: created two more
     applications via the wizard, issued a real `temporal workflow
     cancel` directly via the CLI against one (bypassing the app
     entirely) and confirmed the row landed on `CANCELLED`; the
     escalation path's manager-approval step was re-confirmed via the
     real `/ui/manager` queue. Caveat noted, not re-tested further:
     no single run combined "all 3 types x all 5 paths x phone UI x
     Temporal Web UI" simultaneously — Phase 7 covered the Temporal Web
     UI angle pre-BFF, Phase 11 covered the phone-UI angle; both are
     real but separate passes.
  2. **Visibility invariant — PASS.** Re-confirmed from Phase 11: a
     second identity sees an empty list, and a direct URL guess at
     another identity's `application_id` gets a genuine
     `{"detail":"Not Found"}`, not a client-side hide.
  3. **Mayan hierarchy + submission gate — PASS.** Beyond P5-2/P5-4's
     API-level confirmation, this session logged into Mayan's own web
     UI for the first time in this project's testing and browsed the
     real index tree (`Indexes > Loan Onboarding Archive >
     p11-tester@example.com > <application_id>`), visually confirming
     the `<applicant_identifier> -> <application_id>` shape live, not
     just via API response inspection.
  4. **Bulk approve/reject, mixed batch — PASS, newly verified live.**
     The literal ask — "a mix of eligible and already-decided rows in
     one bulk batch, partial success reported per item" — had never
     been walked end-to-end before this session (earlier bulk tests
     were either synthetic at the `workflow.service` layer or all-
     eligible through the real UI). Created three applications directly
     via the service layer, single-item-rejected one from underneath an
     already-checked bulk selection (simulating a second staff member
     deciding it first), then fired Bulk Reject on the 2-item selection
     through the real `/ui/underwriter` screen: result was "1
     succeeded, 1 failed" with the failed item's real error
     ("workflow execution already completed") shown per-item — the
     batch did not abort, exactly as PRD §10 criterion 4 requires.
  5. **Native Temporal cancel / deleted execution doesn't orphan a row
     forever — PASS on cancel, confirmed-still-a-gap on
     terminate/deletion.** Live-verified both halves for the first time
     against a real Temporal server (not just `WorkflowEnvironment`): a
     `temporal workflow cancel` issued directly via the CLI against a
     running `PENDING_UNDERWRITING` application correctly landed
     Postgres on `CANCELLED` (the workflow's own `except
     asyncio.CancelledError` recovery path really works against a live
     server). A `temporal workflow terminate` against a second,
     otherwise-identical application left its row permanently stuck at
     `PENDING_UNDERWRITING` — confirmed by checking Postgres afterward,
     and by seeing it sit in the live `/ui/underwriter` queue
     indefinitely. This is accurately described in `CLAUDE.md`'s Known
     Gaps as an accepted, unaddressed gap, but P12-2's pass also found
     and fixed a **real documentation bug** while checking this: both
     `db/schema.sql`'s `workflow_id` comment and `PRD.md` §9.3 claimed
     a reconciliation mechanism exists ("cleared if a Temporal admin
     deletes the execution") — a full-codebase grep found no code
     anywhere writes to `workflow_id` after `persist_application` sets
     it. Corrected in both files plus `CLAUDE.md`.
  6. **`docker compose up --build`, zero manual steps — PASS.** See
     P12-3's own note for the full mechanism and the two bugs found and
     fixed along the way.
  7. **Mobile 375×812 — PASS.** Phase 11's entire DoD ran under real
     device-viewport emulation (`chrome-devtools` MCP's `emulate`,
     `375x812x2,mobile,touch`), not a resized desktop window.
  8. **Keycloak-gated, real 403s both directions — PASS, newly verified
     for the reverse direction.** Phase 10 confirmed `underwriter1` ->
     403 on a Manager-only route. This session added the untested
     reverse direction: logged in as `underwriter1`, sent a raw
     `fetch()` POST straight to `/ui/manager/{id}/decision` requesting
     `APPROVE` — got a genuine `403` with body `"requires permission:
     ManagerApprove"`, confirming the permission scopes restrict in
     both directions, not just one.

  P12-2's full walk of `CLAUDE.md`'s Known Gaps: one entry resolved for
  real (port collision, rewritten to describe the fix — P12-3), one
  entry corrected (the dangling reconciliation-note claim — criterion 5
  above), every other bullet re-read against the current codebase and
  confirmed still accurate, still unaddressed, and not a
  should-have-been-caught-earlier surprise.

  Full unit suite (184 tests, +3 new: `test_identity.py`'s cookie tests
  from Phase 11 plus two new Keycloak public-issuer tests this phase)
  and `import-linter` both still pass. P12-4: **Decisions Needed** is
  empty; `CLAUDE.md`'s Known Gaps, post-correction, surfaced to the user
  in this session's final message rather than left implicit in a
  markdown file. **All 12 phases of this implementation plan are now
  complete.**

- **2026-09-02** — Confirmed Phase 11's CI run
  (`gh run list --branch main`, run id `33605775301`, commit `b2b4423`)
  completed `success` in 45s. No follow-up fix needed.

- **2026-09-02** — Phase 11 (Customer BFF UI) complete, all four tasks
  checked. Built `bff_customer/identity.py` (own `itsdangerous`-signed
  cookie, corrected from `CLAUDE.md`'s earlier "slot inside
  `SessionMiddleware`" assumption — see P11-1's note) and
  `bff_customer/routes.py` + 9 templates: identify screen, "My
  Applications" (paginated, status badges), a four-step new-application
  wizard (product picker → details → document upload → review/submit),
  and the detail/status screen (timeline, resubmit-on-`MORE_INFO_REQUESTED`,
  Cancel-while-non-terminal, document preview). Deliberately mostly
  plain `<form>` POST-redirect-GET rather than htmx fragment swaps
  (fits a mobile step wizard better than SPA-style swaps) — htmx used
  in exactly one place, the document-upload widget, reused identically
  in both the wizard and the post-submission "add more docs while
  `MORE_INFO_REQUESTED`" flow. `app.py` gained an `IdentifyRequired`
  exception handler and now mounts both BFF routers.

  Verified the entire DoD through a genuine 375×812 mobile-viewport
  emulation (`chrome-devtools` MCP's `emulate`, not a resized desktop
  window) against the full real local stack: all three product types
  (confirmed each renders its own correct document-category set and
  payload fields), and every terminal outcome — direct Approve
  (`personal_loan`), Approve-then-escalate-then-Manager-Approve
  (`mortgage`, $75,000 ≥ the $50,000 threshold — customer timeline
  correctly showed the 4-step escalation path), Reject, and
  customer-initiated Cancel from a non-terminal state. DB-confirmed
  correct `customers`/`accounts` provisioning: one customer ended up
  with two `ACTIVE` accounts of different product types (no conflict,
  PRD §9.2) via two separate approvals, `get_or_create` correctly
  reusing the same `customer_id` the second time. Verified the
  visibility invariant with a real 404 (not a client-side hide) when a
  second identity tried a first identity's `application_id` directly,
  and confirmed real document preview streaming. Added
  `tests/unit/bff_customer/test_identity.py` (round-trip, tamper
  rejection, garbage rejection, clear-expires-cookie) — no test file
  for `routes.py` itself, same precedent Phase 9/10 established for
  `keycloak_session.py` vs. `routes.py` (pure logic gets a unit test, a
  Temporal/Postgres/Mayan-dependent route handler doesn't). Full unit
  suite (181 tests) and `import-linter` both still pass. One tooling
  gotcha, not a code bug: `chrome-devtools` MCP's `click` intermittently
  no-ops against this app's buttons (confirmed via network-request
  inspection); worked around with `element.click()`/`form.requestSubmit()`
  via `evaluate_script`. Next: Phase 12 (End-to-End Verification &
  Polish) — the last phase.

- **2026-09-02** — Confirmed Phase 10's CI run
  (`gh run list --branch main`, run id `33600965098`, commit `49eae82`)
  completed `success` in 1m6s. No follow-up fix needed.

- **2026-09-02** — Phase 10 (Back-Office BFF UI) complete, all four
  tasks checked. Built `app.py` (composition root: `SessionMiddleware`,
  exception handlers for `keycloak_session.py`'s three custom
  exceptions) and `bff_backoffice/routes.py` — the first phase that
  actually wires everything from Phases 1-9 into a browser-usable
  screen. Underwriter and Manager share one route-handler set and one
  template set, parameterized by `role`, rather than the reference
  project's literal per-role duplication. Added
  `bff_backoffice/selection_store.py` (P10-3, a plain Redis SET per
  session id) and a public `application.service.wait_for_status_change()`
  (reusing the existing private `_wait_until` primitive) so a decision
  route can show the real post-decision status immediately rather than
  waiting for the next 5s poll. No dedicated unit test file for
  `routes.py`/`app.py` — thin FastAPI/Jinja glue over already-tested
  modules, matching the reference project's own precedent (its
  `bff/ui.py` has no unit test file either); verified instead by
  **walking the entire Phase 10 DoD through a real browser**
  (`claude-in-chrome`: real Keycloak Authorization Code login, not
  curl) against the full local stack — every DoD bullet confirmed
  working: role gate (403 on `/ui/manager` for `underwriter1`), solo
  and bulk approve, reject, the full escalation path with a manager
  login switch and confirmed customer/account provisioning, a
  permission-lacking session's raw `fetch()` to a decision route
  getting a real 403, document preview actually rendering a PDF in a
  new tab, and the active-account-conflict block. **Two real things
  found and fixed along the way, not hypothetical**: (1) a Keycloak
  redirect-URI gap — the realm only registered port `8000` callback
  URLs, but this session's native test server ran on `8001` since
  Mayan already held `8000` locally; added `8001` to
  `keycloak/import/loanrealm-realm.json` as a documented permanent
  native-dev alternative, re-imported via a fresh container (a plain
  restart silently skips re-import, per the `keycloak-admin` skill);
  (2) the first active-account-conflict test attempt used flawed test
  data (both "conflict" applications created before either was
  approved, so neither had a resolved `customer_id`, so
  `check_decision_allowed` correctly — not a bug — returned "allowed")
  — redone with the second application submitted *after* the first was
  approved, which correctly triggered the block with a clear error
  message rendered right back into the dialog via the
  `HX-Retarget`/`HX-Reselect` error path. `CLAUDE.md` updated in two
  places: the `keycloak_session.py` Request-decoupling adaptation
  (Identity section) and a new Known Gap flagging that `app`'s planned
  host port `8000` will collide with `mayan`'s once a real `app`
  Compose service is added (not yet, so not urgent). Full
  `tests/unit/` suite (176 tests) reconfirmed unaffected. Next: Phase
  11 (Customer BFF UI) — load the `htmx4` skill first; this is the
  most novel phase, no direct reference-project precedent to adapt
  from.

- **2026-09-02** — Phase 9 (Keycloak Realm & Back-Office Auth Plumbing)
  complete, all four tasks checked. Loaded the `keycloak-admin` skill
  first, per this phase's own instruction. `keycloak/import/loanrealm-realm.json`
  (P9-1) adapted directly from `review-approval-temporal`'s own
  `myrealm-realm.json` -- two roles, one confidential client with
  Authorization Services enabled, one `LoanApplication` resource with
  five scopes, two role policies, five scope-type permissions; no
  `TemporalAdmin` role/conditional-flow client (out of scope per
  CLAUDE.md). Verified against a freshly created (not restarted --
  avoided the skill's own "restart skips import" gotcha) `keycloak`
  container: realm import confirmed in logs, and the full raw-token +
  UMA-exchange `curl` sequence run for *both* `underwriter1` (exactly
  the three Underwriter scopes) and `manager1` (exactly the two Manager
  scopes) -- neither leaks the other's permissions. `bff_backoffice/keycloak_auth.py`
  (P9-2) is a direct reuse of the reference project's own module, just
  adapted for this project's resource/scopes. `session_store.py` (P9-3)
  owns its own lazily-initialized Redis client (matching every domain
  module's `db.py` pattern) rather than reading `request.app.state`,
  since `app.py` doesn't exist until Phase 10 -- published
  `backoffice-redis` on host port 6380 permanently (not a revert-before-
  commit workaround like the Postgres port remaps), matching
  `.env.example`'s own documented "swap for localhost:<port> when
  running natively" convention. `keycloak_session.py` (P9-4) is a
  **deliberate adaptation, not a literal port**: every function takes a
  plain `session_id` instead of a FastAPI `Request`, so its
  session-resolution logic is unit-testable without a real Starlette
  request -- `bff_backoffice/routes.py` (Phase 10) is expected to be the
  thin, framework-coupled layer on top. 43 new unit tests
  (`tests/unit/bff_backoffice/`) -- `keycloak_auth.py`'s tests mirror
  the reference project's own `test_keycloak_auth.py` structure exactly
  (RSA-keypair JWT tests, `respx`-mocked UMA exchange); `session_store.py`'s
  hit a real Redis (same deliberate exception as the Postgres-backed
  `db.py` tests elsewhere, since TTL-sliding is a real-infra behavior a
  mock can't verify); `keycloak_session.py`'s mock `session_store`/
  `keycloak_auth` at the function boundary. One real bug caught while
  writing these (not hypothetical): several `complete_login` test fakes
  for `decode_token` were accidentally `async def`, but the real
  function is synchronous -- calling the un-awaited coroutine produced
  a confusing `AttributeError` deep inside `complete_login` rather than
  a clean test failure; fixed by making the fakes plain sync functions.
  Full `tests/unit/` suite: 176 tests pass together. Next: Phase 10
  (Back-Office BFF UI) -- load the `list-pagination-bulk-actions` and
  `htmx4` skills first, per that phase's own instruction; it's the
  first phase that actually builds `app.py`, wiring
  `keycloak_session.py`'s framework-agnostic functions into real
  FastAPI routes/dependencies for the first time.

- **2026-09-02** — Phase 8 (Import-Linter & CI) complete, both tasks
  checked. Added `import-linter` to `pyproject.toml`'s `[tool.importlinter]`
  (one config file for everything, no separate `.importlinter`): a
  `layers` contract for the overall hierarchy
  (`customer|account|document|workflow` as the bottom, mutually
  independent leaf layer, nothing below them; `application`; then
  `bff_customer|bff_backoffice`; then `(app)|worker_main` on top, `app`
  marked optional since it doesn't exist until Phase 10/11) plus one
  `forbidden` contract per literal "never imports" bullet in
  `CLAUDE.md`'s dependency-graph section, for direct traceability.
  `lint-imports` passed clean on the very first run against the current
  codebase (7 kept, 0 broken) — every module boundary held through
  Phases 1-7 with zero accidental violations, a genuinely clean result
  rather than one requiring a fix. Confirmed the contracts actually
  enforce something (not just happen to pass) by temporarily adding a
  real violation, watching both the layers contract and the specific
  contract catch it with a file/line citation, then reverting. Wired
  into CI's already-reserved P0-6 slot (P8-2), pushed, confirmed green;
  then did the DoD's actual required check for real: pushed a one-line
  boundary violation to a throwaway branch, confirmed via `gh run
  watch` that the new "Check module boundaries" step failed the build
  (exit code 1) while unit tests still passed, then deleted the
  throwaway branch both locally and on the remote. Next: Phase 9
  (Keycloak Realm & Back-Office Auth Plumbing) — load the
  `keycloak-admin` skill first, per that phase's own instruction.

- **2026-09-02** — Phase 7 (Worker Composition Root & End-to-End
  Workflow Verification) complete, all three tasks checked. `worker_main.py`
  (P7-1) is a mechanical extraction of the ad-hoc worker wiring every
  Phase 6 verification script already used. Added `worker-workflow`/
  `worker-activity` to `docker-compose.yml` (P7-2), verified the *real*
  Docker Compose containers (not an in-process stand-in) processed a
  `create_application` call end to end. **P7-3 (the first true
  end-to-end run, `tests/integration/test_end_to_end_workflow.py`)
  found a real bug, not a test artifact**: the first draft of that test
  only stubbed `document.service.check_completeness`, so
  `persist_decision`'s calls to `promote_government_id_to_customer_photo`/
  `generate_welcome_letter` hit the real (not running for this phase)
  Mayan, failed, and Temporal retried the activity — the retry then
  created a *second* `accounts` row for the same customer+product_type,
  hitting `ux_accounts_customer_active_product_type`, because
  `application/activities.py` only wrote `account_id` back to the
  `applications` row in the final combined `UPDATE`, *after* the
  document.service calls, so the retry's idempotency guard
  (`account_id IS NOT NULL`) couldn't see that provisioning had already
  partially happened. Fixed by writing `account_id` immediately after
  `account.service.create_account(...)` succeeds, before the
  document.service calls — `CLAUDE.md`'s provisioning-sequence section
  updated in place with the full reasoning. Added a dedicated
  regression test in `tests/unit/application/test_activities.py`
  reproducing the exact failure mode (a document.service call raises
  after account creation; confirm `account_id` still lands; confirm a
  retry reuses it rather than double-creating). Then fixed the
  integration test's own stubbing gap (all three `document.service`
  calls, not just one) per the DoD's own "Mayan not required" note —
  all 5 end-to-end scenarios (below-threshold approve, escalation to
  manager, reject, request-more-info→resubmit→approve, cancel) pass
  against the real stack, each confirmed `WORKFLOW_EXECUTION_STATUS_COMPLETED`
  via Temporal Web UI's own backing API (same technique as this
  project's own P4-4 precedent). 133 `tests/unit/` tests pass (up from
  129 at the start of this stretch). Verified locally via the same
  temporary 5433 Postgres port remap every phase since Phase 2 has
  needed, reverted before every commit, zero diff each time. **Phase
  7's own text flags this as a natural pause point for a human to
  spot-check before Keycloak/UI work (Phases 9-11) begins** — the next
  session should read that note in Current Status before just picking
  the next unchecked box. Pushed and confirmed green on GitHub Actions
  CI (`gh run watch`, run `33592767191`, `unit-tests` passed in 38s —
  `tests/integration/` isn't part of CI's scope, per P0-6, so this only
  confirms the 133 unit tests, already separately verified locally
  alongside the real integration run above).

- **2026-09-02** — Phase 6 (Application Module) complete, all six tasks
  checked (P6-1 through P6-6). `application/models.py`/`db.py` (P6-1) is
  a thin data-access layer; `schemas.py` (P6-2) is the per-product-type
  Pydantic registry with the `workflow.task_queues.KNOWN_PRODUCT_TYPES`
  import-time assert; `activities.py` (P6-3) is the one file in this
  module allowed to import `customer/`/`account/`, provisioning a
  customer+account on terminal `APPROVED`, idempotency-guarded by
  `account_id IS NOT NULL`; `service.py` (P6-4/P6-5/P6-5b/P6-6) is
  `create_application`/`resubmit_application`/`check_decision_allowed`/
  `get`/`list_for_applicant`/`list_by_status`. Two real gaps caught and
  fixed *before* or *while* implementing, not after: (1)
  `application/db.py`'s `insert()` had no protection against a Temporal
  retry of an already-succeeded `persist_application` — fixed with
  `ON CONFLICT DO NOTHING` (P6-1, surfaced again while building P6-3);
  (2) **a real architectural gap in `CLAUDE.md` itself**:
  `create_application`'s original spec gave it no way to accept a
  pre-existing `application_id`, which conflicts with
  `document.service.upload(...)` needing one to tag uploads with
  *before* the final submit call (Phase 11's own stated flow order) --
  fixed by making `application_id` an optional parameter, `CLAUDE.md`
  updated in place with the full reasoning (P6-4). Every P6 task with an
  "integration-verify" DoD got one for real, against the complete local
  stack (`db`, `temporal`, `mayan`) plus a throwaway ad-hoc worker
  (scratchpad scripts, not committed) standing in for `worker_main.py`
  (Phase 7, doesn't exist yet) — P6-4 proved both the missing-documents
  short-circuit (zero Temporal executions created, confirmed via a real
  `handle.describe()` call) and the complete-application path (a real
  `PENDING_UNDERWRITING` row, `customer_id`/`account_id` both `NULL`,
  documents uploaded first under a pre-minted `application_id` -- the
  exact flow that motivated the fix above); P6-5 drove a real application
  to `MORE_INFO_REQUESTED` via a direct `workflow.service.signal_decision`
  call and confirmed resubmission landed back at `PENDING_UNDERWRITING`
  on the identical `workflow_id`. 129 `tests/unit/` tests pass together
  (up from 36 at the start of this session), verified locally via the
  same temporary 5433 Postgres port remap every phase since Phase 2 has
  needed (this machine's native Postgres holds 5432) -- reverted before
  every commit, zero diff each time. Next: Phase 7 (Worker Composition
  Root & End-to-End Workflow Verification) — P7-1 (`worker_main.py`)
  should be a fairly mechanical extraction of the ad-hoc wiring this
  session's own verification scripts already used repeatedly. Pushed
  all six Phase 6 commits and confirmed green on GitHub Actions CI
  (`gh run watch`, run `33588016171`, `unit-tests` passed in 40s).

- **2026-09-02** — Phase 5 (Document Module) complete, all five tasks
  checked. Brought up `mayan`/`mayan-db`/`mayan-redis` for real (P5-1),
  renamed from `mayan-edms-customer-archive`'s `db`/`redis`/`app` per
  P0-4's already-documented plan. Built this project's own
  `scripts/setup_document_hierarchy.sh` (P5-2) — two document types
  (`Application Document`, `Account Document`) instead of the reference
  project's three, since `applicant_identifier` is the top-level branch
  key here, not `customer_id` — and empirically confirmed the flagged
  `id_photo` multi-membership assumption for real against the fresh
  instance (one document, two simultaneous leaf memberships); `CLAUDE.md`
  updated to record this as confirmed, not just source-read. Built
  `document/mayan_client.py` (P5-3, 9 respx-mocked unit tests) and
  `document/service.py`/`document/models.py` (P5-4 + P5-5 together, one
  file, 18 unit tests against a `FakeMayanClient` double) covering
  `upload`/`list_documents`/`check_completeness`/`preview` and the three
  managed-document functions
  (`promote_government_id_to_customer_photo`/`generate_welcome_letter`/
  `upload_consent`). Two real findings caught only by actually driving
  this against the live P5-1 instance end to end, not from unit tests or
  reading Mayan's docs: (1) `action_name="new"` — this file's own
  placeholder for consent versioning, explicitly flagged "confirm during
  this task" — doesn't exist; Mayan only registers
  `append`/`keep`/`replace`, and versioning an existing document actually
  comes from POSTing to that same document's `/files/` endpoint again
  with `action_name="replace"`, confirmed both by reading
  `document_file_actions.py` and by a live `curl` round-trip; (2) Mayan's
  default REST API rate limit (20 req/sec) genuinely triggers under a
  realistic upload-then-check-completeness sequence at POC scale, not
  just synthetic hammering — fixed with a bounded, `Retry-After`-honoring
  retry in `mayan_client.py`'s `_request`, documented as a new entry in
  `CLAUDE.md`'s "Known gaps" (the fetch-all-then-filter search pattern
  inherited from the reference project is still O(all documents) per
  call and will need real server-side filtering past POC scale). Pushed
  all four Phase 5 commits (P5-1 through P5-4/P5-5) together and
  confirmed green on GitHub Actions CI (`gh run watch`, run
  `33585937933`, `unit-tests` passed in 32s, including the new
  `tests/unit/document/` suite alongside the existing Postgres-backed
  and `WorkflowEnvironment`-backed tests). Next: Phase 6 (Application
  Module), starting at P6-1 — all its dependencies (Phases 1-5) are now
  satisfied.

- **2026-09-02** — Confirmed Phase 4's commit (`8312db9`) is green on
  GitHub Actions CI (`gh run watch`, run `33583072309`, `unit-tests`
  succeeded) — the `temporalio.testing.WorkflowEnvironment` tests need
  no Postgres service container (unlike Phase 2/3's tests), so this
  confirms they also work in CI's clean environment (which has to fetch
  the ephemeral time-skipping test server binary itself, not reuse a
  locally-cached one), not just locally.

- **2026-09-02** — Phase 4 complete, all four tasks checked (created
  `.venv` and `pip install -e ".[dev]"` for the first time this
  session — no venv existed yet in this checkout). `workflow/`'s four
  files built in order (`task_queues.py`, `workflows.py`, `worker.py`,
  `service.py`), closely modeled on `review-approval-temporal`'s own
  `workflow/` package (fetched and read directly, not worked from
  memory), extended for two roles (Underwriter/Manager) instead of one
  and for the two extra non-terminal transitions that implies
  (PENDING_UNDERWRITING → PENDING_MANAGER_APPROVAL on escalation;
  MORE_INFO_REQUESTED → PENDING_UNDERWRITING on resubmit) — generalized
  the reference project's `_claim_final()` terminal-only guard into
  `_claim_transition()`, guarding every state transition, not just
  terminal ones, since two of this workflow's transitions aren't
  terminal. Found and fixed two real `CLAUDE.md` documentation gaps
  while implementing (not just noticed and deferred): `start_workflow`
  was missing `applicant_name`/`applicant_email`/`applicant_phone`, and
  `bulk_signal_decision` was missing `actor_role` — both are now fixed
  in the actual code *and* in `CLAUDE.md`'s own signatures, marked
  "Corrected from an earlier draft" per this file's own conventions
  (see P4-4's note for the full reasoning). All 25
  `tests/unit/workflow/` tests pass via
  `temporalio.testing.WorkflowEnvironment` (no real server needed for
  those) plus a separate manual integration-verify against a real local
  `docker compose up -d temporal` for P4-4's DoD specifically — start,
  signal (single + bulk), resubmit, and a bogus-id bulk failure all
  confirmed working, with the workflow's `COMPLETED` status confirmed
  via Temporal Web UI's own API (`localhost:8233`). Two real
  temporalio-1.32-specific traps hit and fixed while writing
  `tests/unit/workflow/test_workflows.py`, documented in P4-2's note so
  a future session doesn't have to rediscover them: fake activities need
  a type-hinted `inp` param or the data converter hands back a plain
  `dict`; and a signal only confirms Temporal *accepted* it, not that
  the workflow has finished processing it, so an assertion immediately
  after `await handle.signal(...)` is a real, hit-in-practice race, not
  a hypothetical one (fixed with small polling helpers in the test
  file, the same shape application/service.py's own `_wait_until()`
  will need in Phase 6). Local-environment-only note (same class of
  issue as every prior phase): native Postgres on 5432 collides with
  `db`'s Docker port mapping, so `tests/unit/customer`/`tests/unit/account`
  couldn't be re-verified this session (unrelated to Phase 4's own
  changes — confirmed by running everything *except* those two
  directories, all passing). Next: Phase 5 (Document Module) — needs
  Mayan brought up per P5-1's compose rename-pass note, and a session
  working it should read `mayan-edms-customer-archive`'s
  `docs/document-hierarchy-setup.md` before touching the index template,
  per this file's own P5-2 instruction.

- **2026-09-02** — Phase 3 complete, both tasks checked.
  `account/models.py`, `db.py`, `service.py` built, same conventions as
  Phase 2's `customer/` module. Unlike `customer/db.py`'s
  `get_or_create`, `account/db.py`'s `create()` deliberately does
  **not** guard against the partial unique index — that's by design
  (the pre-check is supposed to happen one layer up, in
  `application.service.check_decision_allowed`, Phase 6), and the
  Phase 3 tests specifically prove the raw
  `asyncpg.exceptions.UniqueViolationError` fires rather than swallow
  it. Reused Phase 2's Postgres-in-CI setup as-is, no CI changes
  needed. Same local port collision as Phase 2 (native Postgres on
  5432) — remapped temporarily for verification, reverted before
  committing, not re-documenting the same issue at length again. Next:
  Phase 4 (Workflow Module) per phase order, though Phase 5 (Document
  Module) is equally unblocked (both depend only on Phase 0) if a
  session wants to do that one first instead.

- **2026-09-02** — Phase 2 complete, both tasks checked, including
  CI's new Postgres service container confirmed green on GitHub
  Actions (`gh run watch`, run `33581914340`, 30s).
  `customer/models.py`, `db.py`, `service.py` built; `db.py`'s
  `get_or_create` uses atomic `INSERT ... ON CONFLICT DO NOTHING`
  rather than find-then-insert, closing the concurrency race
  `CLAUDE.md` already flags for this exact function. Real
  deviation from the plan's testing philosophy, made deliberately and
  documented in `CLAUDE.md`'s Testing section: P2-2's DoD needs a real
  Postgres to verify idempotency/uniqueness, not a mock, so these
  "unit" tests hit a live database — added a Postgres service container
  to CI so that's still true there, not just locally. Caught and fixed
  a real `pytest-asyncio` config bug in the process (fixture vs. test
  loop scope mismatch corrupting the shared `asyncpg` pool) by actually
  running the tests, not by reasoning about it in the abstract. Also
  hit two more local-environment-only port collisions while verifying
  (5432 has a native Postgres running on this machine, same class of
  issue as Phase 0's 8080/Keycloak collision) — worked around with
  temporary remaps, reverted before committing. Next: Phase 3 (Account
  Module) or push to confirm P2-2's CI change.

- **2026-09-02** — Phase 1 complete, both tasks checked. `db/schema.sql`
  and `db/init/01-init.sh` already existed from an earlier design
  session — re-verified them against P1-1/P1-2's *exact* DoD rather
  than assuming the earlier Phase 0 check-off carried over: Phase 0's
  P0-4 verification had brought `db` and `temporal` up *together*, so
  by the time it checked, `temporal`'s own auto-setup had already
  populated that database — not the same as this task's DoD, which
  specifically wants `db` alone and an empty `temporal` database. Redid
  the check with only `db` running: confirmed empty, and separately
  confirmed zero FK constraints in `loan_onboarding` via
  `pg_constraint`, not just eyeballing the SQL. No code changes this
  session, only verification + checkbox updates.

- **2026-09-02** — Phase 0 complete, all six tasks checked. Committed
  (`bad9ef9`) and pushed; confirmed P0-6's CI workflow actually ran
  green on GitHub Actions (`gh run watch`, run `33580557909`,
  `unit-tests` passed in 17s) before checking that box, rather than
  trusting the local test run alone. Fetched
  `review-approval-temporal` and `mayan-edms-customer-archive`'s actual
  `Dockerfile`/`docker-compose.yml`/`.env.example` for real before
  writing this project's own, rather than inventing conventions —
  confirmed `pyproject.toml` uses setuptools (not poetry/hatchling),
  the Dockerfile pattern (single-stage, no `CMD`, compose sets command
  per service), and `WORKER_MODE`'s sibling var is set directly in
  compose rather than `.env.example` in that project (this project's
  own `.env.example` still includes `WORKER_MODE`/`LOAN_PRODUCT_TYPE`
  anyway per P0-5's literal wording). Found `mayan-edms-customer-archive`'s
  compose service names are `db`/`redis`/`app`, not
  `mayan-db`/`mayan-redis`/`mayan` as CLAUDE.md's topology assumed —
  P5-1 needs a rename pass, documented in `docker-compose.yml`'s own
  comment so that session doesn't have to re-discover it. Every DoD
  verified for real against this machine's actual Docker (build, up,
  `pg_isready`/`redis-cli ping` healthchecks, a `curl` against
  Keycloak, `psql` confirming both databases and `db/schema.sql`'s
  `ux_accounts_customer_active_product_type` index landed correctly),
  not just written and assumed. One local-environment-only snag: port
  8080 collided with this machine's already-running
  `mayan-edms-customer-archive` stack — verified with a temporary port
  remap, reverted to the standard `8080:8080` before committing (not a
  real issue in a fresh environment). Next session: start Phase 1
  (`db/schema.sql` and `db/init/01-init.sh` already exist from an
  earlier design session and were reused as-is here — Phase 1 may turn
  out to already be effectively done; verify against P1-1/P1-2's exact
  DoD before
  assuming so).
