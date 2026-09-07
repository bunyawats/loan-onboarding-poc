"""worker_main.py is a thin composition root -- its only real behavior
is reading two env vars and forwarding the right activity lists to
workflow.worker.run_worker() / run_account_closure_worker() (Phase 18,
P18-5 -- see CLAUDE.md's "Account closure"). Both bootstrap functions'
own behavior is already covered by tests/unit/workflow/test_worker.py;
this just proves the wiring itself.

Both run_worker and run_account_closure_worker must be mocked in every
test here, not just run_worker -- main() gathers them together, and the
real run_account_closure_worker() would otherwise try to connect to a
real Temporal server and then run its Worker forever, hanging the test
indefinitely rather than failing fast."""

import loan_onboarding.worker_main as worker_main
from loan_onboarding.account.activities import (
    persist_closure_decision,
    persist_closure_request,
)
from loan_onboarding.application.activities import (
    persist_application,
    persist_decision,
    persist_resubmit,
    persist_risk_assessment_cleared,
    submit_risk_assessment,
)


async def test_main_forwards_the_three_concrete_activities_and_env_vars(monkeypatch):
    calls = []
    closure_calls = []

    async def fake_run_worker(activities, worker_mode, product_type):
        calls.append((activities, worker_mode, product_type))

    async def fake_run_account_closure_worker(activities, worker_mode):
        closure_calls.append((activities, worker_mode))

    monkeypatch.setattr(worker_main, "run_worker", fake_run_worker)
    monkeypatch.setattr(worker_main, "run_account_closure_worker", fake_run_account_closure_worker)
    monkeypatch.setenv("WORKER_MODE", "activity")
    monkeypatch.setenv("LOAN_PRODUCT_TYPE", "mortgage")

    await worker_main.main()

    assert len(calls) == 1
    activities, worker_mode, product_type = calls[0]
    assert activities == [
        persist_application,
        persist_decision,
        persist_resubmit,
        submit_risk_assessment,
        persist_risk_assessment_cleared,
    ]
    assert worker_mode == "activity"
    assert product_type == "mortgage"

    assert len(closure_calls) == 1
    closure_activities, closure_worker_mode = closure_calls[0]
    assert closure_activities == [persist_closure_request, persist_closure_decision]
    # Same WORKER_MODE value governs both workers in this one process --
    # see worker_main.py's own module docstring.
    assert closure_worker_mode == "activity"


async def test_main_defaults_worker_mode_both_and_product_type_none(monkeypatch):
    calls = []
    closure_calls = []

    async def fake_run_worker(activities, worker_mode, product_type):
        calls.append((worker_mode, product_type))

    async def fake_run_account_closure_worker(activities, worker_mode):
        closure_calls.append(worker_mode)

    monkeypatch.setattr(worker_main, "run_worker", fake_run_worker)
    monkeypatch.setattr(worker_main, "run_account_closure_worker", fake_run_account_closure_worker)
    monkeypatch.delenv("WORKER_MODE", raising=False)
    monkeypatch.delenv("LOAN_PRODUCT_TYPE", raising=False)

    await worker_main.main()

    assert calls == [("both", None)]
    assert closure_calls == ["both"]


async def test_main_treats_empty_string_product_type_as_none(monkeypatch):
    """.env.example ships LOAN_PRODUCT_TYPE= (empty) as its documented
    default, meaning "poll every product type" -- must not be passed
    through as the literal empty string."""
    calls = []

    async def fake_run_worker(activities, worker_mode, product_type):
        calls.append(product_type)

    async def fake_run_account_closure_worker(activities, worker_mode):
        pass

    monkeypatch.setattr(worker_main, "run_worker", fake_run_worker)
    monkeypatch.setattr(worker_main, "run_account_closure_worker", fake_run_account_closure_worker)
    monkeypatch.setenv("LOAN_PRODUCT_TYPE", "")

    await worker_main.main()

    assert calls == [None]
