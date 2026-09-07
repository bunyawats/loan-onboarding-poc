"""Unit tests for risk/service.py -- no live NATS Adapter needed. The
one outbound httpx call is mocked via respx, same convention
bff_backoffice/test_keycloak_auth.py already uses for its own outbound
Keycloak calls."""

from decimal import Decimal

import pytest
import respx
from httpx import Response

from loan_onboarding.risk import service as risk_service

RISK_ADAPTER_URL = "http://risk-adapter-test:9000"


@pytest.fixture(autouse=True)
def risk_adapter_env(monkeypatch):
    monkeypatch.setenv("RISK_ADAPTER_URL", RISK_ADAPTER_URL)


async def test_submit_risk_assessment_posts_expected_body():
    with respx.mock:
        route = respx.post(f"{RISK_ADAPTER_URL}/assessments").mock(return_value=Response(202))
        await risk_service.submit_risk_assessment(
            application_id="app-000000001",
            applicant_identifier="a@b.com",
            product_type="personal_loan",
            amount=Decimal("15000.00"),
            payload={"employment_status": "employed"},
        )

    assert route.called
    request = route.calls.last.request
    assert request.url == f"{RISK_ADAPTER_URL}/assessments"
    import json

    body = json.loads(request.content)
    assert body == {
        "application_id": "app-000000001",
        "applicant_identifier": "a@b.com",
        "product_type": "personal_loan",
        "amount": "15000.00",
        "payload": {"employment_status": "employed"},
    }


async def test_submit_risk_assessment_raises_on_error_response():
    with respx.mock:
        respx.post(f"{RISK_ADAPTER_URL}/assessments").mock(return_value=Response(500))
        with pytest.raises(Exception):
            await risk_service.submit_risk_assessment(
                application_id="app-000000001",
                applicant_identifier="a@b.com",
                product_type="personal_loan",
                amount=Decimal("15000.00"),
                payload={},
            )


async def test_submit_risk_assessment_uses_default_url_when_env_unset(monkeypatch):
    monkeypatch.delenv("RISK_ADAPTER_URL", raising=False)
    with respx.mock:
        route = respx.post("http://risk-adapter:8000/assessments").mock(return_value=Response(202))
        await risk_service.submit_risk_assessment(
            application_id="app-000000001",
            applicant_identifier="a@b.com",
            product_type="personal_loan",
            amount=Decimal("1000.00"),
            payload={},
        )

    assert route.called
