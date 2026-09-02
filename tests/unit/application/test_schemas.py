import importlib

import pytest
from pydantic import ValidationError

from loan_onboarding.application import schemas
from loan_onboarding.workflow import task_queues


def test_registry_keys_match_known_product_types():
    assert set(schemas.PRODUCT_TYPE_SCHEMAS) == set(task_queues.KNOWN_PRODUCT_TYPES)


def test_assert_fires_when_registries_desync(monkeypatch):
    """Deliberately desyncs the two registries and confirms the
    import-time assert actually raises -- not just that it theoretically
    would (P6-2's DoD)."""
    monkeypatch.setattr(task_queues, "KNOWN_PRODUCT_TYPES", ("personal_loan",))
    try:
        with pytest.raises(AssertionError):
            importlib.reload(schemas)
    finally:
        monkeypatch.undo()
        importlib.reload(schemas)  # restore real module state for later tests


def test_validate_payload_personal_loan():
    result = schemas.validate_payload(
        "personal_loan",
        {"purpose": "home improvement", "employment_status": "employed", "monthly_income": "5000.00"},
    )
    assert result == {
        "purpose": "home improvement",
        "employment_status": "employed",
        "monthly_income": "5000.00",
    }


def test_validate_payload_auto_loan():
    result = schemas.validate_payload(
        "auto_loan",
        {"vehicle_make_model": "Toyota Camry", "vin": "1HGCM82633A004352", "down_payment": "2000"},
    )
    assert result["vehicle_make_model"] == "Toyota Camry"
    assert result["vin"] == "1HGCM82633A004352"


def test_validate_payload_mortgage():
    result = schemas.validate_payload(
        "mortgage",
        {"property_address": "123 Main St", "appraised_value": "500000", "down_payment": "100000"},
    )
    assert result["property_address"] == "123 Main St"


def test_validate_payload_unknown_product_type_raises():
    with pytest.raises(schemas.UnknownProductType):
        schemas.validate_payload("crypto_loan", {})


def test_validate_payload_missing_required_field_raises():
    with pytest.raises(ValidationError):
        schemas.validate_payload("personal_loan", {"purpose": "vacation"})


def test_validate_payload_result_is_json_safe_for_jsonb_storage():
    import json

    result = schemas.validate_payload(
        "personal_loan",
        {"purpose": "x", "employment_status": "employed", "monthly_income": "1234.56"},
    )
    # Must not raise -- Decimal fields need to already be JSON-serializable
    # (str, not a raw Decimal) before being handed to application/db.py's
    # jsonb codec (json.dumps can't serialize Decimal on its own).
    json.dumps(result)
