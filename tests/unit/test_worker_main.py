"""worker_main.py is a thin composition root -- its only real behavior
is reading two env vars and forwarding the right activity list to
workflow.worker.run_worker(). run_worker's own behavior is already
covered by tests/unit/workflow/test_worker.py; this just proves the
wiring itself."""

import loan_onboarding.worker_main as worker_main
from loan_onboarding.application.activities import (
    persist_application,
    persist_decision,
    persist_resubmit,
)


async def test_main_forwards_the_three_concrete_activities_and_env_vars(monkeypatch):
    calls = []

    async def fake_run_worker(activities, worker_mode, product_type):
        calls.append((activities, worker_mode, product_type))

    monkeypatch.setattr(worker_main, "run_worker", fake_run_worker)
    monkeypatch.setenv("WORKER_MODE", "activity")
    monkeypatch.setenv("LOAN_PRODUCT_TYPE", "mortgage")

    await worker_main.main()

    assert len(calls) == 1
    activities, worker_mode, product_type = calls[0]
    assert activities == [persist_application, persist_decision, persist_resubmit]
    assert worker_mode == "activity"
    assert product_type == "mortgage"


async def test_main_defaults_worker_mode_both_and_product_type_none(monkeypatch):
    calls = []

    async def fake_run_worker(activities, worker_mode, product_type):
        calls.append((worker_mode, product_type))

    monkeypatch.setattr(worker_main, "run_worker", fake_run_worker)
    monkeypatch.delenv("WORKER_MODE", raising=False)
    monkeypatch.delenv("LOAN_PRODUCT_TYPE", raising=False)

    await worker_main.main()

    assert calls == [("both", None)]


async def test_main_treats_empty_string_product_type_as_none(monkeypatch):
    """.env.example ships LOAN_PRODUCT_TYPE= (empty) as its documented
    default, meaning "poll every product type" -- must not be passed
    through as the literal empty string."""
    calls = []

    async def fake_run_worker(activities, worker_mode, product_type):
        calls.append(product_type)

    monkeypatch.setattr(worker_main, "run_worker", fake_run_worker)
    monkeypatch.setenv("LOAN_PRODUCT_TYPE", "")

    await worker_main.main()

    assert calls == [None]
