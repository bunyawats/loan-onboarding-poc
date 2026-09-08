"""Composition root for the Temporal worker process (CLAUDE.md's
"Breaking the application <-> workflow cycle") -- one of the two files
in this codebase allowed to import from every module (the other is
`app.py`, for the web process). Wires `workflow/`'s generic
`run_worker()` bootstrap to `application/activities.py`'s three
concrete activity implementations -- `workflow/` itself never imports
`application/` to get this list. Same split for account closure (Phase
18, P18-5): `run_account_closure_worker()` wired to
`account/activities.py`'s two concrete activities, gathered alongside
`run_worker()` in this one process.

    python -m loan_onboarding.worker_main

Same `WORKER_MODE`/`LOAN_PRODUCT_TYPE` env vars as `workflow/worker.py`
documents (`both`/`workflow`/`activity`; unset product type polls every
known one) -- `WORKER_MODE` governs both the loan-application and the
account-closure worker in this process; `LOAN_PRODUCT_TYPE` only
affects the former (account closure has no product-type-keyed queue to
narrow, see `task_queue_for_account_closure()`)."""

import asyncio
import os

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
from loan_onboarding.workflow.worker import DEFAULT_WORKER_MODE, run_account_closure_worker, run_worker


async def main() -> None:
    worker_mode = os.environ.get("WORKER_MODE", DEFAULT_WORKER_MODE)
    product_type = os.environ.get("LOAN_PRODUCT_TYPE") or None
    await asyncio.gather(
        run_worker(
            [
                persist_application,
                persist_decision,
                persist_resubmit,
                submit_risk_assessment,
                persist_risk_assessment_cleared,
            ],
            worker_mode=worker_mode,
            product_type=product_type,
        ),
        run_account_closure_worker(
            [persist_closure_request, persist_closure_decision],
            worker_mode=worker_mode,
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
