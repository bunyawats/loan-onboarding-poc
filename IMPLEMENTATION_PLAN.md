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

**Phase 0 — not started.** Start here: [P0-1](#phase-0--repo--infra-scaffolding).

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

- [ ] **P0-1** — `pyproject.toml` at the repo root: `temporalio`,
      `fastapi`, `uvicorn[standard]`, `asyncpg`, `pydantic`,
      `pydantic-settings`, `pyjwt[crypto]`, `jinja2`,
      `python-multipart`, `itsdangerous`, `httpx`, `redis`; dev extras
      `pytest`, `pytest-asyncio`, `respx`. One package,
      `loan_onboarding`, per `CLAUDE.md`'s repo layout.
      DoD: `pip install -e .` succeeds in a clean venv.
- [ ] **P0-2** — Empty package skeleton: every directory in `CLAUDE.md`'s
      "Repo layout" tree exists with an `__init__.py`, no logic yet.
      DoD: `python -c "import loan_onboarding"` succeeds.
- [ ] **P0-3** — `Dockerfile` (one image, used by every process — see
      `CLAUDE.md`'s "Deployment").
      DoD: `docker build .` succeeds.
- [ ] **P0-4** — `docker-compose.yml` skeleton: `mayan-db`, `mayan-redis`,
      `mayan`, `db`, `temporal`, `temporal-ui`, `keycloak`,
      `backoffice-redis` services per `CLAUDE.md`'s "Docker Compose
      topology" — **no `app`/`worker` services yet**, those come once
      there's code to run.
      DoD: `docker compose up -d db temporal keycloak backoffice-redis`
      brings up four healthy containers (Mayan brought up separately in
      Phase 5, once `document/` needs it).
- [ ] **P0-5** — `.env.example` (every env var referenced anywhere in
      `CLAUDE.md`, even ones not wired up yet) + `.gitignore` (`.env`,
      `__pycache__`, `.venv`, etc.).
      DoD: `.env` copied from example, no secrets committed.
- [ ] **P0-6** — CI skeleton (GitHub Actions or equivalent): a workflow
      that at minimum runs `pytest tests/unit` on push. Leave a
      commented-out step for `tests/contract`-equivalent /
      import-linter — those get filled in Phase 8, this just reserves
      the slot so Phase 8 isn't inventing CI structure from scratch.
      DoD: workflow runs (even trivially green) on the first push.

---

## Phase 1 — Data Layer

**Depends on:** Phase 0. **Unblocks:** Phases 2, 3, 6.

- [ ] **P1-1** — `db/schema.sql`: `customers`, `accounts`, `applications`
      tables per `PRD.md` §9, no foreign keys between them (per
      `CLAUDE.md`'s "Data storage" — deliberate).
- [ ] **P1-2** — `db/init/*.sh`: creates the `loan_onboarding` and
      `temporal` databases in the one `db` Postgres container, applies
      `schema.sql` to `loan_onboarding` only.
      DoD (integration-verify): `docker compose up -d db`, then
      `psql` into both databases and confirm `applications`/`accounts`/
      `customers` exist only in `loan_onboarding`, and `temporal` is
      empty (Temporal's own migrations create its own tables later).

---

## Phase 2 — Customer Module

**Depends on:** Phase 1. **Unblocks:** Phase 11 (bff_customer).

- [ ] **P2-1** — `customer/models.py`, `customer/db.py` (the only code
      touching the `customers` table).
- [ ] **P2-2** — `customer/service.py`:
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

---

## Phase 3 — Account Module

**Depends on:** Phase 1. **Unblocks:** Phase 11.
Can run in parallel with Phase 2 if two sessions ever overlap — no
dependency between them.

- [ ] **P3-1** — `account/models.py`, `account/db.py` (the only code
      touching the `accounts` table).
- [ ] **P3-2** — `account/service.py`:
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

---

## Phase 4 — Workflow Module (generic orchestration only)

**Depends on:** Phase 0 only (deliberately not Phase 6 — see
`CLAUDE.md`'s "Breaking the cycle": activities are referenced by string
name, so this phase does not need `application/activities.py` to exist).
**Unblocks:** Phase 6 (application/schemas.py asserts against
`task_queues.py`), Phase 7.

- [ ] **P4-1** — `workflow/task_queues.py`: `KNOWN_PRODUCT_TYPES =
      ("personal_loan", "auto_loan", "mortgage")` (PRD §6.1),
      `task_queue_for_product_type()`.
- [ ] **P4-2** — `workflow/workflows.py`: `LoanApplicationWorkflow`.
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
- [ ] **P4-3** — `workflow/worker.py`: bootstrap function
      `run_worker(activities: list[Callable], worker_mode: str,
      product_type: str | None)` — takes the concrete activity list as
      a parameter (supplied later by `worker_main.py`, Phase 7), reads
      `WORKER_MODE` (`both`/`workflow`/`activity`) and
      `LOAN_PRODUCT_TYPE` env vars per `CLAUDE.md`.
      DoD: unit test instantiates a `Worker` with a list of two or three
      trivial fake activities and confirms it starts polling without
      error against a `WorkflowEnvironment`'s local server (don't need
      real activities to prove the bootstrap logic itself works).
- [ ] **P4-4** — `workflow/service.py`: `start_workflow(application_id,
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

---

## Phase 5 — Document Module

**Depends on:** Phase 0. **Unblocks:** Phase 6.
Independent of Phases 2–4 — can run in parallel if sessions overlap.

- [ ] **P5-1** — Add `mayan-db`, `mayan-redis`, `mayan` to
      `docker-compose.yml` (copy wholesale from
      `mayan-edms-customer-archive/docker-compose.yml`).
      DoD: `docker compose up -d mayan`, log in at `localhost:8000`.
- [ ] **P5-2** — `scripts/setup_document_hierarchy.sh`: creates the
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
- [ ] **P5-3** — `document/mayan_client.py`: thin async wrapper,
      `Accept: application/json` default header (do not omit — see
      `CLAUDE.md`), service-account token obtained lazily and refreshed
      on 401, metadata/document-type id lookups cached for process
      lifetime.
- [ ] **P5-4** — `document/service.py`: `upload(applicant_identifier,
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
- [ ] **P5-5** — `document/service.py`, part 2 — the managed-document
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

---

## Phase 6 — Application Module

**Depends on:** Phases 1, 2, 3, 4, 5 (specifically P5-5, not just
Phase 5's earlier tasks). **Unblocks:** Phase 7. **Phases 2 and 3 are
new dependencies vs. the original ordering** — `application/
activities.py`'s `persist_decision` now calls `customer.service` and
`account.service` on approval (see `CLAUDE.md`'s "Applying without
being a customer yet"), so P6-3 can't be written until both exist.

- [ ] **P6-1** — `application/models.py`, `application/db.py` (the only
      code touching the `applications` table).
- [ ] **P6-2** — `application/schemas.py`: Pydantic payload model per
      product type (PRD §6.1's field tables), registry keyed by
      `product_type`, `assert` at import time checking this registry's
      keys match `workflow.task_queues.KNOWN_PRODUCT_TYPES` exactly
      (see `CLAUDE.md`'s "Breaking the cycle" — this is the payoff of
      being one process again).
      DoD: a unit test that deliberately desyncs the two registries
      (monkeypatch one) and confirms the assert actually fires — don't
      just trust that it would.
- [ ] **P6-3** — `application/activities.py`: `persist_application`,
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
- [ ] **P6-4** — `application/service.py`, part 1:
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
- [ ] **P6-5** — `application/service.py`, part 2:
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
- [ ] **P6-5b** — `application/service.py`, part 2b:
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
- [ ] **P6-6** — `application/service.py`, part 3: `get(application_id)`,
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

---

## Phase 7 — Worker Composition Root & End-to-End Workflow Verification

**Depends on:** Phases 4, 6. **Unblocks:** Phases 9, 10, 11.

- [ ] **P7-1** — `worker_main.py`: imports `workflow/`'s `run_worker()`
      bootstrap and `application/activities.py`'s three concrete
      functions, wires them together, reads the same `WORKER_MODE`/
      `LOAN_PRODUCT_TYPE` env vars.
      DoD: `python -m loan_onboarding.worker_main` starts cleanly
      against the real local Temporal server.
- [ ] **P7-2** — Add `worker-workflow`/`worker-activity` (or one
      `worker` service, per `CLAUDE.md`'s Deployment section) to
      `docker-compose.yml`.
- [ ] **P7-3** — **First true end-to-end run**, no UI yet — drive it
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

This phase is a real milestone: everything below the two BFFs works.
Treat it as a natural point to pause and let a human spot-check the
result before continuing into Keycloak/UI work.

---

## Phase 8 — Import-Linter & CI

**Depends on:** Phases 2–7 (needs real imports to check against — don't
do this against an empty skeleton).

- [ ] **P8-1** — `.importlinter` (or `pyproject.toml`
      `[tool.importlinter]`) encoding the full dependency graph from
      `CLAUDE.md`'s "Module dependency graph" section — every "never
      imports" rule as a `forbidden` contract, every "leaf module" as a
      layer with nothing below it.
      DoD: run it against the current codebase and confirm it passes
      clean (if it doesn't, that's a real violation introduced in
      Phases 2–7 — fix the violation, don't loosen the contract to make
      it pass).
- [ ] **P8-2** — Wire the import-linter run into the CI workflow from
      P0-6, required (not just informational) — a failure here should
      fail the build.
      DoD: deliberately introduce a one-line boundary violation in a
      throwaway branch, confirm CI fails on it, then revert.

---

## Phase 9 — Keycloak Realm & Back-Office Auth Plumbing

**Depends on:** Phase 0 (independent of the domain modules — can run in
parallel with Phases 2–8 if sessions overlap, though Phase 10 needs both
this and Phase 7 done).

- [ ] **P9-1** — `keycloak/import/loanrealm-realm.json`: realm roles
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
- [ ] **P9-2** — `bff_backoffice/keycloak_auth.py`: JWT decode
      (`PyJWKClient`), `get_permissions()` (UMA ticket exchange,
      `response_mode=permissions`, read the per-resource `scopes`
      array), `refresh_access_token()`. `KEYCLOAK_ISSUER`/client
      id/secret read lazily, not at import time.
- [ ] **P9-3** — `bff_backoffice/session_store.py`: Redis-backed
      `/ui/*` session store (`ui-session:<id>` →
      `username`/`role`/`access_token`/`access_expires_at`/
      `refresh_token`/`refresh_expires_at`).
- [ ] **P9-4** — `bff_backoffice/keycloak_session.py`:
      `get_session_user()` (async, transparent refresh),
      `require_session_role(role)`, `require_permission(permission)`/
      `check_permission()`. Role gates screens, permission gates
      actions — no `require_session_role` pre-gate on any decision
      route (see `CLAUDE.md`'s explicit warning about this).
      DoD: unit tests mock Keycloak at the HTTP layer with `respx`
      (JWT-validation tests patch key resolution directly and let real
      `jwt.decode()` run against a locally-generated test keypair — same
      approach as the reference project's `test_keycloak_auth.py`).

---

## Phase 10 — Back-Office BFF UI (staff screens)

**Depends on:** Phases 7, 9. **Load the `list-pagination-bulk-actions`
and `htmx4` skills before starting this phase.**

- [ ] **P10-1** — `bff_backoffice/routes.py`: `/ui/login` (Keycloak
      Authorization Code flow), `/ui/underwriter`, `/ui/manager` — list
      screens calling `application.service.list_by_status(...)`,
      auto-refresh every 5s.
- [ ] **P10-2** — Row detail dialog: applicant/loan fields (via
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
- [ ] **P10-3** — Bulk selection: server-side store
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
- [ ] **P10-4** — Pagination: `query_id`-cached counts per the skill's
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

---

## Phase 11 — Customer BFF UI (self-service mobile flow)

**Depends on:** Phases 2, 3, 5, 7. **Load the `htmx4` skill.** No direct
precedent in either reference project — the most novel phase.

- [ ] **P11-1** — `bff_customer/identity.py`: signed cookie session
      holding `applicant_identifier`, no password. `/apply` redirects
      to an identify screen if the cookie is missing.
- [ ] **P11-2** — "My Applications" screen:
      `application.service.list_for_applicant(applicant_identifier,
      ...)`, status badges, "Apply for a new loan" CTA.
- [ ] **P11-3** — New Application flow: product-type picker → common +
      product-specific fields → document upload (camera capture for ID
      via `<input type="file" capture>`, file picker for
      statements/reports) → review & submit, calling
      `application.service.create_application(...)`. Missing-documents
      error surfaces the specific categories, not a generic failure.
- [ ] **P11-4** — Application detail/status screen: timeline, resubmit
      action when `MORE_INFO_REQUESTED` (calling
      `application.service.resubmit_application(...)`), Cancel action
      while non-terminal (calling
      `workflow.service.signal_decision(..., decision="CANCELLED")`
      directly — no `application.service` hop, per `CLAUDE.md`'s call
      graph).
      DoD (integration-verify, whole phase): full flow driven from an
      **actual 375×812 mobile viewport** (resize the browser preview
      tool, not just a narrowed desktop window), for all three product
      types, through every terminal outcome in PRD §6.2's state
      diagram. Confirm a second identify-screen entry with a *different*
      email/phone shows an empty application list (visibility invariant
      actually filters, not just hides via CSS — PRD §10's success
      criterion 2).

---

## Phase 12 — End-to-End Verification & Polish

**Depends on:** everything above.

- [ ] **P12-1** — Walk every numbered item in `PRD.md` §10 "Success
      criteria for this POC" explicitly, one at a time, and record the
      result (pass/fail + how verified) in this task's Session Log
      entry — don't just say "looks done."
- [ ] **P12-2** — Walk `CLAUDE.md`'s "Known gaps" section, confirm each
      listed gap is genuinely still a gap (not something that got
      accidentally fixed or accidentally became worse) and that none of
      them are actually surprises that should have been caught earlier.
- [ ] **P12-3** — `docker compose up --build` from a completely clean
      state (`docker compose down -v`) brings up the entire stack with
      zero manual steps beyond the documented one-time Mayan hierarchy
      bootstrap — verify this on a clean checkout, not an
      already-warmed-up dev environment.
- [ ] **P12-4** — Final pass on `CLAUDE.md`'s "Known gaps" and this
      file's **Decisions Needed** section — anything still open gets
      surfaced to the user explicitly in the session's final message,
      not silently left in a markdown file for someone to notice later.

---

## Session Log

*(Newest entry at the top. Each entry: date, tasks touched, what
actually happened, any deviations from the plan or `CLAUDE.md` and why,
what the next session should know. Keep entries factual and specific —
"worked on Phase 6" is not useful to a future session; "P6-4 done,
P6-5 blocked on Phase 7 not existing yet, see note in Decisions Needed"
is.)*

- *(no sessions yet — the first session should add its entry here)*
