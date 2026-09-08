---
name: testing-conventions
description: loan-onboarding-poc's test-suite conventions -- the tests/unit/ (mocked, mirrors module structure) vs. tests/integration/ (real stack, @pytest.mark.integration) split, the deliberate exception where a module's own db.py tests run against a real Postgres, preferring temporalio.testing.WorkflowEnvironment over a real Temporal server, why no tests/contract/ suite is needed, and tests/integration/test_document_service.py (the first test to touch real Mayan). Triggers on "pytest", "tests/unit", "tests/integration", "test suite", "db.py tests", "WorkflowEnvironment", "loan_onboarding_test", "test_document_service.py", "contract tests", "ci.yml", "integration marker", "@pytest.mark.integration".
---

## Testing

`tests/unit/` (mirrors module structure, no live services — mock
`document.service`/`workflow.service` calls at the function-call level
for a module under test, the in-process equivalent of the reference
project's `respx`-mocked HTTP calls) and `tests/integration/` (needs the
real local stack, marked `@pytest.mark.integration`).

**One deliberate exception**: a module's own `db.py` tests (e.g.
`customer/db.py`'s `get_or_create`) run against a **real Postgres**, not
a mock — "no live services" is about not needing to fake *other*
modules' HTTP/service calls, not about a module faking its own
database. Idempotency and uniqueness guarantees (e.g. "two concurrent
`get_or_create` calls for the same identifier create exactly one row")
are statements about database state; a mock recording call order can't
verify them, only assert that `service.py` called `db.py` in some
order. These still live under `tests/unit/<module>/` (mirrors module
structure, matches each such task's own DoD, which isn't tagged
"integration-verify") — they just need `DATABASE_URL` pointing at a
database with `db/schema.sql` applied, not the *full* local stack
`tests/integration/` needs (Temporal, Keycloak, Mayan). CI provisions a
real Postgres service container for exactly this reason (see
`.github/workflows/ci.yml`) — these tests are not integration tests in
the "needs the whole stack" sense, but they were never really "unit"
tests in the "no I/O at all" sense either; call them what they are
rather than mislabeling either way.

**Two real, live-hit testing hazards to know about before running these
against a local `docker compose` stack you're also using for manual
verification**: pointing `DATABASE_URL` at the compose stack's own
`loan_onboarding` (instead of a separate `loan_onboarding_test`) lets
these tests' cleanup fixtures silently wipe the live stack's data, and
stacking up ad hoc `docker exec <container> python3 -c
"asyncio.run(...)"` one-off scripts can exhaust Postgres's
`max_connections` via `asyncpg`'s default `min_size=10` pool. **Load
the `known-gaps-and-gotchas` skill** for the full mechanism and recovery
steps for both.

Prefer `temporalio.testing.WorkflowEnvironment` (time-skipping) over a
real Temporal server for `workflow/`'s workflow/activity tests — inject
a fake/in-memory version of `application/activities.py`'s functions
here rather than hitting the real `applications` table, same "test the
orchestration, not the downstream write" split
`review-approval-temporal`'s own bulk-decision tests use
(`monkeypatching submit_decision() rather than faking Temporal`).

No `tests/contract/` needed anymore (see "Breaking the application ↔
workflow cycle" in `CLAUDE.md`) — the `application/schemas.py` assert
against `workflow.task_queues.KNOWN_PRODUCT_TYPES` does that job at
import time, in every test run, for free.

**`tests/integration/test_document_service.py` is this project's first
integration test to touch real Mayan** — every prior Mayan verification
(Phases 5, 14, 15, 16, the index redesigns) was a documented manual
sweep instead, and `test_end_to_end_workflow.py` (the only other file
in `tests/integration/`) deliberately stubs `document_service` out to
avoid needing Mayan at all. Added specifically because `FakeMayanClient`
can't catch what only real Mayan enforces — a document type rejecting a
metadata attach it was never associated with (P16-4's real bug) and
Mayan's own reject-on-duplicate-attach behavior are exactly the two bugs
this project already hit for real that no unit test caught. Covers
`upload_consent`/`preview_account_document` (the account-level document
support the consent-upload feature added): a real create, a real
same-document re-version (not a duplicate, confirmed via
`list_account_documents` staying at one document), and a real streamed
download returning the latest version's actual bytes. Uses synthetic,
uuid4-based `account_id`/`customer_id` values with no real Postgres row
behind them — `document/` never imports `application/`/`account/`/
`customer/`, so it doesn't care whether they resolve to anything, only
that they're stable strings to tag and filter on; a `cleanup_documents`
fixture trashes every document a test creates afterward, same
soft-delete this codebase uses everywhere else. Needs
`docker compose up -d mayan` plus
`MAYAN_BASE_URL`/`MAYAN_SERVICE_ACCOUNT_USERNAME`/
`MAYAN_SERVICE_ACCOUNT_PASSWORD` set (same values `.env` already
carries) — run it on its own
(`pytest tests/integration/test_document_service.py -m integration`),
not mixed into one invocation with `tests/unit`: doing that once in the
same session produced an unrelated flake in
`tests/workflow/test_workflows.py`'s embedded time-skipping Temporal
test server that didn't reproduce running either suite alone,
consistent with CI's own separation (`.github/workflows/ci.yml` only
ever runs `pytest tests/unit`, never `tests/integration`).
