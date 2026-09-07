"""The mock Risk Engine -- a standalone service, deliberately NOT part
of the `loan_onboarding` Python package (CLAUDE.md's "Automated risk
assessment via NATS" / the risk-assessment-nats skill, and the
repo-layout note under "Also planned, Phase 21..."). Same "genuinely
external system this codebase doesn't own" treatment Mayan and Keycloak
already get, even though this one happens to be a mock: a real Risk
Engine would be a real third party speaking the same two-endpoint HTTP
contract, so this is built the same way from day one rather than as
in-process code that would need extracting later.

HTTP-only, no NATS client at all -- this service never touches NATS in
either direction, only plain HTTP (through KrakenD, per the decided
Phase 21 topology). One endpoint:

  POST /assess -- receives the same risk-criteria body
  risk-adapter's submitted-message subscriber forwards verbatim from
  `risk/service.py`'s original JSON (application_id, applicant_identifier,
  product_type, amount as a string, payload). After a short simulated
  delay, applies the amount-bucketing decision rule (Decisions Needed,
  assumed default, not yet confirmed by a human: < $15,000 -> LOW,
  $15,000-$50,000 -> MEDIUM, >= $50,000 -> HIGH) and calls
  risk-adapter's `POST /decisions` webhook **through KrakenD**
  (KRAKEND_URL, not risk-adapter directly -- this service has no idea
  risk-adapter's real address is, by design, same as a genuine external
  Risk Engine wouldn't).

Blocking, not fire-and-forget: /assess's own handler sleeps, decides,
then calls the webhook, and only then returns its own 202 to the
caller (risk-adapter) -- simpler than a background task, and
risk-adapter's own submitted-message subscriber already treats /assess
as "just check for a 2xx ack," not something it waits on synchronously
for a decision, so a slower response here costs nothing in practice at
this POC's scale."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

logger = logging.getLogger("mock_risk_engine")

_DEFAULT_KRAKEND_URL = "http://krakend:8080"
_DEFAULT_SIMULATED_DELAY_SECONDS = "1"

# Decisions Needed (IMPLEMENTATION_PLAN.md), assumed default, not yet
# confirmed by a human -- picked so the mock is trivially testable (a
# known amount always produces a known tier), not to simulate a real
# scoring model.
LOW_THRESHOLD = Decimal("15000")
HIGH_THRESHOLD = Decimal("50000")

RISK_TIER_LOW = "LOW"
RISK_TIER_MEDIUM = "MEDIUM"
RISK_TIER_HIGH = "HIGH"


def decide_risk_tier(amount: Decimal) -> str:
    if amount < LOW_THRESHOLD:
        return RISK_TIER_LOW
    if amount < HIGH_THRESHOLD:
        return RISK_TIER_MEDIUM
    return RISK_TIER_HIGH


async def handle_assess(
    body: dict[str, Any],
    http_client: httpx.AsyncClient,
    krakend_url: str,
    simulated_delay_seconds: float,
) -> None:
    await asyncio.sleep(simulated_delay_seconds)
    risk_tier = decide_risk_tier(Decimal(body["amount"]))
    response = await http_client.post(
        f"{krakend_url}/decisions",
        json={"application_id": body["application_id"], "risk_tier": risk_tier},
    )
    if response.status_code >= 300:
        logger.error(
            "risk-adapter /decisions (via KrakenD) returned %s for application_id=%s",
            response.status_code,
            body.get("application_id"),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient()
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.post("/assess", status_code=202)
async def post_assess(request: Request) -> Response:
    body = await request.json()
    krakend_url = os.environ.get("KRAKEND_URL", _DEFAULT_KRAKEND_URL)
    simulated_delay_seconds = float(
        os.environ.get("SIMULATED_DELAY_SECONDS", _DEFAULT_SIMULATED_DELAY_SECONDS)
    )
    await handle_assess(body, request.app.state.http_client, krakend_url, simulated_delay_seconds)
    return Response(status_code=202)
