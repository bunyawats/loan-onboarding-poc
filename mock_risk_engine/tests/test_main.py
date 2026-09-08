"""Unit tests for mock_risk_engine/main.py -- no live KrakenD/risk-adapter
needed. Same "plain functions taking their collaborators as arguments"
shape risk_adapter/tests/test_main.py already established, so the
bucketing rule and the outbound webhook call are both testable directly
against fakes."""

from decimal import Decimal

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from mock_risk_engine import main

KRAKEND_URL = "http://krakend-test:8080"


@pytest.mark.parametrize(
    "amount,expected_tier",
    [
        (Decimal("1"), "LOW"),
        (Decimal("14999.99"), "LOW"),
        (Decimal("15000"), "MEDIUM"),
        (Decimal("49999.99"), "MEDIUM"),
        (Decimal("50000"), "MEDIUM"),  # escalation-eligible, still MEDIUM -- the overlap band
        (Decimal("99999.99"), "MEDIUM"),
        (Decimal("100000"), "HIGH"),
        (Decimal("150000"), "HIGH"),
    ],
)
def test_decide_risk_tier_thresholds(amount, expected_tier):
    assert main.decide_risk_tier(amount) == expected_tier


async def test_handle_assess_calls_decisions_webhook_with_decided_tier():
    body = {"application_id": "app-000000001", "amount": "15000.00"}
    with respx.mock:
        route = respx.post(f"{KRAKEND_URL}/decisions").mock(return_value=Response(202))
        async with httpx.AsyncClient() as http_client:
            await main.handle_assess(body, http_client, KRAKEND_URL, simulated_delay_seconds=0)

    assert route.called
    import json

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == {"application_id": "app-000000001", "risk_tier": "MEDIUM"}


async def test_handle_assess_logs_but_does_not_raise_on_error_response():
    body = {"application_id": "app-000000001", "amount": "1.00"}
    with respx.mock:
        respx.post(f"{KRAKEND_URL}/decisions").mock(return_value=Response(500))
        async with httpx.AsyncClient() as http_client:
            await main.handle_assess(body, http_client, KRAKEND_URL, simulated_delay_seconds=0)


def test_post_assess_endpoint_returns_202(monkeypatch):
    monkeypatch.setenv("KRAKEND_URL", KRAKEND_URL)
    monkeypatch.setenv("SIMULATED_DELAY_SECONDS", "0")
    with respx.mock:
        route = respx.post(f"{KRAKEND_URL}/decisions").mock(return_value=Response(202))
        with TestClient(main.app) as client:
            response = client.post(
                "/assess",
                json={
                    "application_id": "app-000000001",
                    "applicant_identifier": "a@b.com",
                    "product_type": "personal_loan",
                    "amount": "100000.00",
                    "payload": {},
                },
            )

    assert response.status_code == 202
    assert route.called
