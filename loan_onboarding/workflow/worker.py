"""
Bootstrap for the Temporal worker process. Run via worker_main.py (the
composition root -- see CLAUDE.md's "Breaking the application <->
workflow cycle"):

    python -m loan_onboarding.worker_main

This module never imports application/ -- run_worker() takes the
concrete activity callables (application/activities.py's
persist_application / persist_decision / persist_resubmit, Phase 6) as
a runtime parameter instead of importing them, so it can be built and
unit-tested here, in Phase 4, before that file exists.

Same WORKER_MODE / product-type env-var split as
review-approval-temporal's WORKER_MODE / REVIEW_TYPE, just
LOAN_PRODUCT_TYPE instead of REVIEW_TYPE (worker_main.py reads the env
vars; this module takes them as plain arguments so it never touches
`os.environ` itself beyond the default `Client.connect` fallback below):

WORKER_MODE ("both" / "workflow" / "activity", default "both") -- which
half of the work this process registers:
  - "both"     -- registers the workflow + activities. Simplest.
  - "workflow" -- registers ONLY the workflow. Workflow code is
                pure/deterministic (no I/O), so this process needs no
                DATABASE_URL.
  - "activity" -- registers ONLY the activities.

product_type (unset by default) -- which product-type-specific task
queue(s) this process polls:
  - unset -- polls EVERY known product type's queue (one Worker per
           product type, run concurrently in this one process).
  - set   -- polls ONLY that product type's queue, letting worker
           capacity scale independently per product type.

Multiple Worker processes (any mix of modes, any mix of product types)
can poll the SAME task queue simultaneously -- Temporal dispatches
workflow tasks and activity tasks separately, so a "workflow"-mode
worker simply never receives activity tasks, and vice versa.
"""

import asyncio
import os
from typing import Callable, Optional, Sequence

from temporalio.client import Client
from temporalio.worker import Worker

from loan_onboarding.workflow.task_queues import (
    DEFAULT_TEMPORAL_HOST,
    DEFAULT_TEMPORAL_NAMESPACE,
    KNOWN_PRODUCT_TYPES,
    task_queue_for_account_closure,
    task_queue_for_product_type,
)
from loan_onboarding.workflow.workflows import CloseAccountWorkflow, LoanApplicationWorkflow

VALID_MODES = ("both", "workflow", "activity")
DEFAULT_WORKER_MODE = VALID_MODES[0]


def _build_workers(
    client: Client,
    activities: Sequence[Callable],
    worker_mode: str,
    product_type: Optional[str],
) -> list[Worker]:
    if worker_mode not in VALID_MODES:
        raise ValueError(f"worker_mode={worker_mode!r} invalid, must be one of {VALID_MODES}")
    if product_type is not None and product_type not in KNOWN_PRODUCT_TYPES:
        raise ValueError(
            f"product_type={product_type!r} unknown, must be one of {KNOWN_PRODUCT_TYPES} "
            f"(or None, to poll all of them)"
        )

    workflows = [LoanApplicationWorkflow] if worker_mode in ("both", "workflow") else []
    acts = list(activities) if worker_mode in ("both", "activity") else []

    product_types = [product_type] if product_type else list(KNOWN_PRODUCT_TYPES)
    return [
        Worker(
            client,
            task_queue=task_queue_for_product_type(pt),
            workflows=workflows,
            activities=acts,
        )
        for pt in product_types
    ]


async def run_worker(
    activities: Sequence[Callable],
    worker_mode: str = DEFAULT_WORKER_MODE,
    product_type: Optional[str] = None,
    client: Optional[Client] = None,
) -> None:
    """Runs forever (one Worker.run() per polled product type), until
    cancelled. `client` is injectable so tests can hand this a
    WorkflowEnvironment's client instead of a real server; worker_main.py
    (production) leaves it unset and this connects using
    TEMPORAL_HOST/TEMPORAL_NAMESPACE.
    """
    if client is None:
        client = await Client.connect(
            os.environ.get("TEMPORAL_HOST", DEFAULT_TEMPORAL_HOST),
            namespace=os.environ.get("TEMPORAL_NAMESPACE", DEFAULT_TEMPORAL_NAMESPACE),
        )
    workers = _build_workers(client, activities, worker_mode, product_type)
    print(
        f"Worker process started (mode={worker_mode}), "
        f"serving product types: {product_type or list(KNOWN_PRODUCT_TYPES)}"
    )
    await asyncio.gather(*(w.run() for w in workers))


# ----------------------------------------------------------------------
# Account closure (Phase 18, P18-5, "Account closure" -- see CLAUDE.md).
# A single, non-product-type-keyed Worker -- no per-product_type fan-out
# the way _build_workers/run_worker need for LoanApplicationWorkflow,
# since closure review doesn't vary by product. Deliberately a sibling
# function rather than a parameter grafted onto _build_workers/run_worker
# above -- the "one Worker per product type" shape those two are built
# around doesn't apply here at all, so forcing this into the same
# function would mean threading a not-really-optional
# is-this-the-closure-worker branch through code that's otherwise clean
# generic Temporal bootstrap.
# ----------------------------------------------------------------------


def _build_account_closure_worker(
    client: Client,
    activities: Sequence[Callable],
    worker_mode: str,
) -> Worker:
    if worker_mode not in VALID_MODES:
        raise ValueError(f"worker_mode={worker_mode!r} invalid, must be one of {VALID_MODES}")

    workflows = [CloseAccountWorkflow] if worker_mode in ("both", "workflow") else []
    acts = list(activities) if worker_mode in ("both", "activity") else []

    return Worker(
        client,
        task_queue=task_queue_for_account_closure(),
        workflows=workflows,
        activities=acts,
    )


async def run_account_closure_worker(
    activities: Sequence[Callable],
    worker_mode: str = DEFAULT_WORKER_MODE,
    client: Optional[Client] = None,
) -> None:
    """Runs forever, until cancelled -- worker_main.py gathers this
    alongside run_worker()'s own loan-application worker(s) in the same
    process (one `python -m loan_onboarding.worker_main`, same
    `WORKER_MODE` value governing both). `client` is injectable, same
    reasoning run_worker's own parameter documents."""
    if client is None:
        client = await Client.connect(
            os.environ.get("TEMPORAL_HOST", DEFAULT_TEMPORAL_HOST),
            namespace=os.environ.get("TEMPORAL_NAMESPACE", DEFAULT_TEMPORAL_NAMESPACE),
        )
    worker = _build_account_closure_worker(client, activities, worker_mode)
    print(f"Account-closure worker started (mode={worker_mode})")
    await worker.run()
