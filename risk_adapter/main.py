"""The NATS Adapter -- a standalone service, deliberately NOT part of
the `loan_onboarding` Python package (no import edge either direction --
see `CLAUDE.md`'s "Automated risk assessment via NATS" / the
`risk-assessment-nats` skill, and the repo-layout note under "Also
planned, Phase 21..."). This is the *only* thing anywhere in this whole
system that depends on the NATS protocol: `loan_onboarding.risk.service`
talks to this service over plain HTTP, never NATS directly, and the
Risk Engine (real or mock) talks to this service over plain HTTP too
(through KrakenD) -- never NATS either.

Two small HTTP endpoints plus two background NATS subscriber loops, all
in one process:

  1. POST /assessments  -- from `risk.service.submit_risk_assessment`.
     Publishes onto `risk.assessment.submitted`, returns 202.
  2. A subscriber on `risk.assessment.submitted` -- calls the Risk
     Engine's own POST /assess, through KrakenD (RISK_ENGINE_URL points
     at KrakenD, not the engine directly).
  3. POST /decisions  -- the webhook the Risk Engine calls (through
     KrakenD) once it has a tier. Publishes onto
     `risk.assessment.decided`, returns 202.
  4. A subscriber on `risk.assessment.decided` -- computes
     `workflow_id = f"loan-application-{application_id}"` (the same
     deterministic scheme `workflow/service.py`'s own
     `_workflow_id_for_application` uses -- duplicated here, not
     imported, since this service can't import `loan_onboarding.workflow`)
     and sends the `signal_risk_decision` signal directly via its own
     `temporalio.client.Client`.

**Subject-naming decision (Decisions Needed, resolved here, P21-4)**:
one shared subject per leg, `application_id` carried in the message
body -- not a per-application subject. Simpler, and NATS core pub/sub
has no per-subject setup cost that would make a shared subject a
bottleneck at this POC's scale.

**risk_assessment_id decision (Decisions Needed, resolved here,
P21-4)**: no separate id minted. `application_id` alone is sufficient
correlation for both legs -- there is exactly one outstanding risk
assessment per application at a time (a new application always starts
a fresh `PENDING_RISK_ASSESSMENT` wait), so no ambiguity a second id
would resolve.

Each piece below is a small, independently testable function taking its
collaborators (the NATS connection, an `httpx` client, the Temporal
client) as explicit arguments -- not closures baked into the FastAPI
app -- so unit tests can call `handle_submitted_message`/
`handle_decided_message` directly with fakes, never a real NATS/Temporal
connection (see `risk_adapter/tests/test_main.py`)."""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
import nats
from fastapi import FastAPI, Request, Response
from nats.aio.client import Client as NATSClient
from nats.aio.msg import Msg
from temporalio.client import Client as TemporalClient

logger = logging.getLogger("risk_adapter")

SUBJECT_SUBMITTED = "risk.assessment.submitted"
SUBJECT_DECIDED = "risk.assessment.decided"

_DEFAULT_NATS_URL = "nats://nats:4222"
_DEFAULT_RISK_ENGINE_URL = "http://krakend:8080"
_DEFAULT_TEMPORAL_HOST = "temporal:7233"
_DEFAULT_TEMPORAL_NAMESPACE = "default"


def workflow_id_for_application(application_id: str) -> str:
    """Duplicated from `workflow/service.py`'s own
    `_workflow_id_for_application` on purpose -- this service cannot
    import `loan_onboarding.workflow` (it isn't part of that package),
    so the deterministic scheme has to be reproduced here rather than
    shared. If that scheme ever changes, both copies need updating --
    a real, accepted coupling this design note exists to flag."""
    return f"loan-application-{application_id}"


async def publish_assessment(nc: NATSClient, body: dict[str, Any]) -> None:
    await nc.publish(SUBJECT_SUBMITTED, json.dumps(body).encode())


async def publish_decision(nc: NATSClient, body: dict[str, Any]) -> None:
    await nc.publish(SUBJECT_DECIDED, json.dumps(body).encode())


async def handle_submitted_message(data: bytes, http_client: httpx.AsyncClient, risk_engine_url: str) -> None:
    """The `risk.assessment.submitted` subscriber body. Calls the Risk
    Engine's `POST /assess` and only checks for a 2xx ack -- the actual
    decision arrives later, asynchronously, via the Risk Engine's own
    call to this service's `/decisions` webhook (through KrakenD both
    ways), not as this call's response body. Both a non-2xx ack and a
    transport-level failure (DNS/connection/timeout -- confirmed live
    while verifying this task against the real stack, before KrakenD/
    the mock Risk Engine existed to answer this call: an unguarded
    `httpx.ConnectError` here propagated out of this function into
    nats-py's own generic subscription error handler instead of this
    module's structured logging -- harmless, since nats-py's message
    loop survives a callback exception and keeps polling for the next
    message, but inconsistent with the "logged, not raised" design this
    docstring already states) are caught and logged here, never
    raised -- there is nothing further up the call stack to retry this
    the way a Temporal activity would; a stuck assessment here shows
    the same "no timeout" gap `CLAUDE.md`'s Known Gaps already
    documents for the existing human-decision wait."""
    body = json.loads(data)
    try:
        response = await http_client.post(f"{risk_engine_url}/assess", json=body)
    except httpx.HTTPError:
        logger.exception(
            "Risk Engine /assess unreachable for application_id=%s",
            body.get("application_id"),
        )
        return
    if response.status_code >= 300:
        logger.error(
            "Risk Engine /assess returned %s for application_id=%s",
            response.status_code,
            body.get("application_id"),
        )


async def handle_decided_message(data: bytes, temporal_client: TemporalClient) -> None:
    """The `risk.assessment.decided` subscriber body. `risk_tier` is
    sent as-is to the workflow's `signal_risk_decision` signal -- this
    service never inspects or validates the tier value itself; the
    workflow's own signal handler (P21-5) is what actually branches on
    it. A missing/already-completed workflow
    (e.g. a duplicate NATS delivery arriving after the workflow already
    moved on) is logged, not raised -- NATS is at-least-once, and so is
    this handler's own caller if it ever retries a failed delivery, so
    a workflow that no longer accepts signals is an expected, not
    exceptional, outcome here."""
    body = json.loads(data)
    application_id = body["application_id"]
    risk_tier = body["risk_tier"]
    workflow_id = workflow_id_for_application(application_id)
    handle = temporal_client.get_workflow_handle(workflow_id)
    try:
        await handle.signal("signal_risk_decision", risk_tier)
    except Exception:
        logger.exception(
            "Failed to signal signal_risk_decision for workflow_id=%s (application_id=%s)",
            workflow_id,
            application_id,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    nats_url = os.environ.get("NATS_URL", _DEFAULT_NATS_URL)
    risk_engine_url = os.environ.get("RISK_ENGINE_URL", _DEFAULT_RISK_ENGINE_URL)
    temporal_host = os.environ.get("TEMPORAL_HOST", _DEFAULT_TEMPORAL_HOST)
    temporal_namespace = os.environ.get("TEMPORAL_NAMESPACE", _DEFAULT_TEMPORAL_NAMESPACE)

    nc = await nats.connect(nats_url)
    http_client = httpx.AsyncClient()
    temporal_client = await TemporalClient.connect(temporal_host, namespace=temporal_namespace)

    async def _on_submitted(msg: Msg) -> None:
        await handle_submitted_message(msg.data, http_client, risk_engine_url)

    async def _on_decided(msg: Msg) -> None:
        await handle_decided_message(msg.data, temporal_client)

    await nc.subscribe(SUBJECT_SUBMITTED, cb=_on_submitted)
    await nc.subscribe(SUBJECT_DECIDED, cb=_on_decided)

    app.state.nc = nc
    app.state.http_client = http_client
    app.state.temporal_client = temporal_client
    try:
        yield
    finally:
        await http_client.aclose()
        await nc.close()


app = FastAPI(lifespan=lifespan)


@app.post("/assessments", status_code=202)
async def post_assessments(request: Request) -> Response:
    body = await request.json()
    await publish_assessment(request.app.state.nc, body)
    return Response(status_code=202)


@app.post("/decisions", status_code=202)
async def post_decisions(request: Request) -> Response:
    body = await request.json()
    await publish_decision(request.app.state.nc, body)
    return Response(status_code=202)
