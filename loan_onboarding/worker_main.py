"""Composition root for the Temporal worker process (CLAUDE.md's
"Breaking the application <-> workflow cycle") -- one of the two files
in this codebase allowed to import from every module (the other is
`app.py`, for the web process). Wires `workflow/`'s generic
`run_worker()` bootstrap to `application/activities.py`'s three
concrete activity implementations -- `workflow/` itself never imports
`application/` to get this list.

    python -m loan_onboarding.worker_main

Same `WORKER_MODE`/`LOAN_PRODUCT_TYPE` env vars as `workflow/worker.py`
documents (`both`/`workflow`/`activity`; unset product type polls every
known one)."""

import asyncio
import os

from loan_onboarding.application.activities import (
    persist_application,
    persist_decision,
    persist_resubmit,
)
from loan_onboarding.workflow.worker import run_worker


async def main() -> None:
    worker_mode = os.environ.get("WORKER_MODE", "both")
    product_type = os.environ.get("LOAN_PRODUCT_TYPE") or None
    await run_worker(
        [persist_application, persist_decision, persist_resubmit],
        worker_mode=worker_mode,
        product_type=product_type,
    )


if __name__ == "__main__":
    asyncio.run(main())
