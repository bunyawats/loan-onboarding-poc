"""Unit tests for risk_adapter/main.py -- no live NATS/Temporal/Risk
Engine needed anywhere in this file. The two subscriber-loop bodies
(`handle_submitted_message`/`handle_decided_message`) and the two
publish helpers are plain functions taking their collaborators as
arguments, so they're tested directly against fakes; the two FastAPI
endpoints are tested via `TestClient` with `nats.connect`/
`TemporalClient.connect` patched so the app's lifespan never touches a
real network."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from risk_adapter import main


class FakeNATSClient:
    def __init__(self):
        self.published: list[tuple[str, bytes]] = []
        self.subscribed: list[str] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, data))

    async def subscribe(self, subject: str, cb=None) -> None:
        self.subscribed.append(subject)

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------
# publish_assessment / publish_decision
# ---------------------------------------------------------------


async def test_publish_assessment_publishes_expected_subject_and_body():
    nc = FakeNATSClient()
    body = {"application_id": "app-000000001", "risk_tier": None}
    await main.publish_assessment(nc, body)

    assert nc.published == [(main.SUBJECT_SUBMITTED, json.dumps(body).encode())]


async def test_publish_decision_publishes_expected_subject_and_body():
    nc = FakeNATSClient()
    body = {"application_id": "app-000000001", "risk_tier": "LOW"}
    await main.publish_decision(nc, body)

    assert nc.published == [(main.SUBJECT_DECIDED, json.dumps(body).encode())]


# ---------------------------------------------------------------
# handle_submitted_message -- the risk.assessment.submitted subscriber
# ---------------------------------------------------------------


async def test_handle_submitted_message_posts_to_risk_engine():
    body = {
        "application_id": "app-000000001",
        "applicant_identifier": "a@b.com",
        "product_type": "personal_loan",
        "amount": "15000.00",
        "payload": {},
    }
    with respx.mock:
        route = respx.post("http://risk-engine-test/assess").mock(return_value=Response(202))
        async with httpx.AsyncClient() as http_client:
            await main.handle_submitted_message(
                json.dumps(body).encode(), http_client, "http://risk-engine-test"
            )

    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == body


async def test_handle_submitted_message_logs_but_does_not_raise_on_error_response():
    body = {"application_id": "app-000000001"}
    with respx.mock:
        respx.post("http://risk-engine-test/assess").mock(return_value=Response(500))
        async with httpx.AsyncClient() as http_client:
            # Must not raise -- there is nothing upstream to retry this call.
            await main.handle_submitted_message(
                json.dumps(body).encode(), http_client, "http://risk-engine-test"
            )


async def test_handle_submitted_message_logs_but_does_not_raise_on_transport_error():
    # Confirmed live against the real stack before KrakenD/the mock Risk
    # Engine existed to answer this call: an unguarded ConnectError here
    # used to propagate out into nats-py's own generic error handler
    # instead of this module's structured logging.
    body = {"application_id": "app-000000001"}
    with respx.mock:
        respx.post("http://risk-engine-test/assess").mock(side_effect=httpx.ConnectError("boom"))
        async with httpx.AsyncClient() as http_client:
            await main.handle_submitted_message(
                json.dumps(body).encode(), http_client, "http://risk-engine-test"
            )


# ---------------------------------------------------------------
# handle_decided_message -- the risk.assessment.decided subscriber
# ---------------------------------------------------------------


class FakeWorkflowHandle:
    def __init__(self, raise_on_signal: Exception | None = None):
        self.signal_calls: list[tuple[str, object]] = []
        self._raise_on_signal = raise_on_signal

    async def signal(self, name: str, arg) -> None:
        if self._raise_on_signal is not None:
            raise self._raise_on_signal
        self.signal_calls.append((name, arg))


class FakeTemporalClient:
    def __init__(self, handle: FakeWorkflowHandle):
        self._handle = handle
        self.requested_workflow_ids: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> FakeWorkflowHandle:
        self.requested_workflow_ids.append(workflow_id)
        return self._handle


async def test_handle_decided_message_signals_the_correct_workflow():
    handle = FakeWorkflowHandle()
    temporal_client = FakeTemporalClient(handle)
    body = {"application_id": "app-000000001", "risk_tier": "HIGH"}

    await main.handle_decided_message(json.dumps(body).encode(), temporal_client)

    assert temporal_client.requested_workflow_ids == ["loan-application-app-000000001"]
    assert handle.signal_calls == [("signal_risk_decision", "HIGH")]


async def test_handle_decided_message_swallows_signal_failure():
    handle = FakeWorkflowHandle(raise_on_signal=RuntimeError("workflow not found"))
    temporal_client = FakeTemporalClient(handle)
    body = {"application_id": "app-000000001", "risk_tier": "LOW"}

    # Must not raise -- a missing/already-completed workflow (e.g. a
    # duplicate NATS delivery) is an expected outcome here, not fatal.
    await main.handle_decided_message(json.dumps(body).encode(), temporal_client)


def test_workflow_id_for_application_matches_workflow_service_scheme():
    # Duplicated deliberately from workflow/service.py's own
    # _workflow_id_for_application -- this assertion is what would catch
    # the two copies drifting apart.
    assert main.workflow_id_for_application("app-000000001") == "loan-application-app-000000001"


# ---------------------------------------------------------------
# HTTP endpoints -- app lifespan patched so no real NATS/Temporal
# connection is ever attempted
# ---------------------------------------------------------------


def test_post_assessments_publishes_to_nats():
    fake_nc = FakeNATSClient()
    with patch.object(main.nats, "connect", AsyncMock(return_value=fake_nc)), patch.object(
        main.TemporalClient, "connect", AsyncMock(return_value=AsyncMock())
    ):
        with TestClient(main.app) as client:
            body = {"application_id": "app-000000001", "applicant_identifier": "a@b.com"}
            response = client.post("/assessments", json=body)

    assert response.status_code == 202
    assert fake_nc.published == [(main.SUBJECT_SUBMITTED, json.dumps(body).encode())]


def test_post_decisions_publishes_to_nats():
    fake_nc = FakeNATSClient()
    with patch.object(main.nats, "connect", AsyncMock(return_value=fake_nc)), patch.object(
        main.TemporalClient, "connect", AsyncMock(return_value=AsyncMock())
    ):
        with TestClient(main.app) as client:
            body = {"application_id": "app-000000001", "risk_tier": "MEDIUM"}
            response = client.post("/decisions", json=body)

    assert response.status_code == 202
    assert fake_nc.published == [(main.SUBJECT_DECIDED, json.dumps(body).encode())]
