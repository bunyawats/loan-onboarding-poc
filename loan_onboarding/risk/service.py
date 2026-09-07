"""The thinnest leaf module in the codebase (Phase 21, "Automated risk
assessment via NATS" -- see `CLAUDE.md` / the `risk-assessment-nats`
skill). No NATS awareness at all -- that connectivity lives entirely in
the standalone `risk-adapter` service, outside this Python package.
This module makes exactly one outbound call: a plain HTTP `POST` to the
Adapter's `/assessments` endpoint, which is itself just "accepted for
NATS publish," not "assessed" -- the caller (an activity) never waits
for a decision here; that arrives later as a Temporal signal the
Adapter sends directly (see `workflow/workflows.py`'s
`signal_risk_decision`).

Same shape as `idgen/`: no I/O beyond this one call, no other module's
types, imports nothing else in this codebase except `idgen/` (which
this module doesn't currently need, but is the one sanctioned
exception every other leaf gets -- see `CLAUDE.md`'s module dependency
graph)."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import httpx

_DEFAULT_RISK_ADAPTER_URL = "http://risk-adapter:8000"


async def submit_risk_assessment(
    application_id: str,
    applicant_identifier: str,
    product_type: str,
    amount: Decimal,
    payload: dict[str, Any],
) -> None:
    """POSTs the application's risk criteria to the NATS Adapter's
    `/assessments` endpoint. Returns once the Adapter confirms it
    accepted the submission for NATS publish -- same "accepted, not yet
    applied" caveat every other outbound `service.py` call in this
    codebase already carries (e.g. `workflow.service.start_workflow`).
    Raises on any non-2xx response or transport failure; the caller
    (`application/activities.py`'s `submit_risk_assessment` activity)
    is a normal Temporal activity, so a raised exception here retries
    the activity per its own retry policy -- unlike the best-effort
    notification calls in this same provisioning-style code path, a
    failed submission here must not be silently swallowed, since
    nothing else will ever move the application out of
    `PENDING_RISK_ASSESSMENT` if it is."""
    base_url = os.environ.get("RISK_ADAPTER_URL", _DEFAULT_RISK_ADAPTER_URL)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{base_url}/assessments",
            json={
                "application_id": application_id,
                "applicant_identifier": applicant_identifier,
                "product_type": product_type,
                "amount": str(amount),
                "payload": payload,
            },
        )
    response.raise_for_status()
