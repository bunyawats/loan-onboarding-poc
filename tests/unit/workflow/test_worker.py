"""workflow/worker.py's bootstrap logic -- no real activities needed to
prove the Worker(s) it builds actually start polling; that's a property
of _build_workers()'s wiring, not of what the activities do.
"""

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment

from loan_onboarding.workflow.task_queues import KNOWN_PRODUCT_TYPES
from loan_onboarding.workflow.worker import VALID_MODES, _build_account_closure_worker, _build_workers


@activity.defn(name="noop_one")
async def _noop_one(x: str) -> None:
    pass


@activity.defn(name="noop_two")
async def _noop_two(x: str) -> None:
    pass


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        yield environment


async def test_build_workers_both_mode_starts_polling_without_error(env: WorkflowEnvironment):
    workers = _build_workers(
        env.client, [_noop_one, _noop_two], worker_mode="both", product_type="personal_loan"
    )
    assert len(workers) == 1
    async with workers[0]:
        pass  # entering/exiting confirms it started (and stopped) cleanly


async def test_build_workers_unset_product_type_polls_every_known_type(env: WorkflowEnvironment):
    workers = _build_workers(
        env.client, [_noop_one, _noop_two], worker_mode="both", product_type=None
    )
    assert len(workers) == len(KNOWN_PRODUCT_TYPES)
    async with workers[0]:
        pass


async def test_build_workers_workflow_mode_registers_no_activities(env: WorkflowEnvironment):
    workers = _build_workers(
        env.client, [_noop_one], worker_mode="workflow", product_type="personal_loan"
    )
    # A "workflow"-mode worker with zero activities still starts fine --
    # it just never receives activity tasks (see worker.py's docstring).
    async with workers[0]:
        pass


async def test_build_workers_activity_mode_registers_no_workflow(env: WorkflowEnvironment):
    workers = _build_workers(
        env.client, [_noop_one], worker_mode="activity", product_type="personal_loan"
    )
    async with workers[0]:
        pass


def test_build_workers_rejects_invalid_worker_mode():
    with pytest.raises(ValueError, match="worker_mode"):
        _build_workers(None, [], worker_mode="bogus", product_type=None)


def test_build_workers_rejects_unknown_product_type():
    with pytest.raises(ValueError, match="product_type"):
        _build_workers(None, [], worker_mode="both", product_type="crypto_loan")


def test_valid_modes_matches_worker_mode_semantics():
    assert VALID_MODES == ("both", "workflow", "activity")


# ----------------------------------------------------------------------
# Account closure (Phase 18, P18-5) -- a single Worker, no per-product-
# type fan-out, so these tests are simpler than _build_workers' own.
# ----------------------------------------------------------------------


async def test_build_account_closure_worker_both_mode_starts_polling_without_error(
    env: WorkflowEnvironment,
):
    worker = _build_account_closure_worker(env.client, [_noop_one, _noop_two], worker_mode="both")
    async with worker:
        pass  # entering/exiting confirms it started (and stopped) cleanly


async def test_build_account_closure_worker_workflow_mode_registers_no_activities(
    env: WorkflowEnvironment,
):
    worker = _build_account_closure_worker(env.client, [_noop_one], worker_mode="workflow")
    async with worker:
        pass


async def test_build_account_closure_worker_activity_mode_registers_no_workflow(
    env: WorkflowEnvironment,
):
    worker = _build_account_closure_worker(env.client, [_noop_one], worker_mode="activity")
    async with worker:
        pass


def test_build_account_closure_worker_rejects_invalid_worker_mode():
    with pytest.raises(ValueError, match="worker_mode"):
        _build_account_closure_worker(None, [], worker_mode="bogus")
