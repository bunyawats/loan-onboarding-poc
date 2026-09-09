import itertools
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from loan_onboarding.application import db
from loan_onboarding.workflow.workflows import STATUS_PENDING_UNDERWRITING

pytestmark = pytest.mark.usefixtures("_clean_applications_table")

_application_id_counter = itertools.count()
_customer_id_counter = itertools.count()


def _new_application_id():
    return f"APP-{next(_application_id_counter):09d}"


def _fake_customer_id():
    return f"CUS-{next(_customer_id_counter):09d}"


async def _insert_sample(application_id=None, **overrides):
    application_id = application_id or _new_application_id()
    defaults = dict(
        application_id=application_id,
        applicant_identifier="alice@example.com",
        customer_id=None,
        workflow_id=f"wf-{application_id}",
        product_type="personal_loan",
        payload={"purpose": "home improvement", "monthly_income": 5000},
        applicant_name="Alice Applicant",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000.00"),
        # Phase 21 added a required `status` param (workflows.py's own
        # self._status, no longer an implicit database DEFAULT) -- this
        # file isn't testing risk-assessment behavior, so its rows are
        # seeded straight at PENDING_UNDERWRITING, same as every one of
        # these tests already assumed before that column existed.
        status="PENDING_UNDERWRITING",
    )
    defaults.update(overrides)
    return await db.insert(**defaults)


async def test_insert_round_trips_jsonb_payload_as_a_dict():
    application_id = _new_application_id()
    record = await _insert_sample(application_id=application_id, payload={"a": 1, "b": [1, 2, 3]})

    assert record["payload"] == {"a": 1, "b": [1, 2, 3]}
    assert isinstance(record["payload"], dict)

    fetched = await db.get(application_id)
    assert fetched["payload"] == {"a": 1, "b": [1, 2, 3]}


async def test_insert_defaults_status_and_nullable_columns():
    record = await _insert_sample()
    assert record["status"] == "PENDING_UNDERWRITING"
    assert record["customer_id"] is None


async def test_insert_writes_caller_supplied_status_verbatim():
    """Phase 21: `status` is no longer left to the table's own `DEFAULT`
    -- the real caller (persist_application) always passes
    PENDING_RISK_ASSESSMENT explicitly now. This test proves insert()
    writes whatever it's given, not just this file's own
    PENDING_UNDERWRITING fixture convention."""
    record = await _insert_sample(status="PENDING_RISK_ASSESSMENT")
    assert record["status"] == "PENDING_RISK_ASSESSMENT"


async def test_insert_is_idempotent_on_retry_same_application_id():
    """Simulates a Temporal activity retry of persist_application after
    an already-succeeded first attempt -- must return the existing row,
    not raise a raw UniqueViolationError on the primary key."""
    application_id = _new_application_id()
    first = await _insert_sample(application_id=application_id, applicant_name="First Name")
    second = await _insert_sample(application_id=application_id, applicant_name="Different Name")

    assert first["application_id"] == second["application_id"]
    # The retry's (different) arguments are ignored -- the original row wins.
    assert second["applicant_name"] == "First Name"

    pool = await db._get_pool()
    count = await pool.fetchval(
        "SELECT count(*) FROM applications WHERE application_id = $1", application_id
    )
    assert count == 1


async def test_get_returns_none_for_unknown_id():
    assert await db.get("APP-999999999") is None


async def test_get_by_workflow_id():
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id, workflow_id="wf-lookup-1")

    record = await db.get_by_workflow_id("wf-lookup-1")
    assert record["application_id"] == application_id

    assert await db.get_by_workflow_id("no-such-workflow") is None


async def test_update_decision_writes_underwriter_columns_only():
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id)

    record = await db.update_decision(
        application_id,
        status="MORE_INFO_REQUESTED",
        underwriter_name="u1",
        underwriter_comment="need more docs",
        underwriter_decided_at=datetime.now(timezone.utc),
    )

    assert record["status"] == "MORE_INFO_REQUESTED"
    assert record["underwriter_name"] == "u1"
    assert record["underwriter_comment"] == "need more docs"
    assert record["underwriter_decided_at"] is not None
    assert record["manager_name"] is None
    assert record["manager_decided_at"] is None


async def test_update_decision_preserves_columns_not_passed():
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id)

    await db.update_decision(
        application_id,
        status="PENDING_MANAGER_APPROVAL",
        underwriter_name="u1",
        underwriter_comment="escalating",
        underwriter_decided_at=datetime.now(timezone.utc),
    )

    # A later manager decision must not clobber the underwriter columns
    # already written -- only the columns actually passed here change.
    record = await db.update_decision(
        application_id,
        status="APPROVED",
        manager_name="m1",
        manager_comment="approved",
        manager_decided_at=datetime.now(timezone.utc),
    )

    assert record["status"] == "APPROVED"
    assert record["underwriter_name"] == "u1"
    assert record["underwriter_comment"] == "escalating"
    assert record["manager_name"] == "m1"
    assert record["manager_comment"] == "approved"


async def test_update_decision_sets_customer_id_on_provisioning():
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id)
    customer_id = _fake_customer_id()

    record = await db.update_decision(
        application_id,
        status="APPROVED",
        underwriter_name="u1",
        underwriter_comment="ok",
        underwriter_decided_at=datetime.now(timezone.utc),
        customer_id=customer_id,
    )

    assert record["customer_id"] == customer_id


async def test_update_decision_cancelled_writes_neither_underwriter_nor_manager_columns():
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id)

    record = await db.update_decision(application_id, status="CANCELLED")

    assert record["status"] == "CANCELLED"
    assert record["underwriter_name"] is None
    assert record["manager_name"] is None


async def test_update_decision_honors_explicit_updated_at_for_native_cancel():
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id)
    forced_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    record = await db.update_decision(application_id, status="CANCELLED", updated_at=forced_time)

    assert record["updated_at"] == forced_time


async def test_update_decision_writes_risk_tier_when_passed():
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id, status="PENDING_RISK_ASSESSMENT")

    record = await db.update_decision(
        application_id,
        status="APPROVED",
        underwriter_name="risk-engine-auto",
        underwriter_comment="Automated decision by risk assessment",
        underwriter_decided_at=datetime.now(timezone.utc),
        risk_tier="LOW",
    )

    assert record["risk_tier"] == "LOW"


async def test_update_decision_leaves_risk_tier_null_when_not_passed():
    """A human decision never passes risk_tier -- COALESCE(NULL, ...)
    must leave the column untouched (already NULL in that case), not
    overwrite it with NULL explicitly (which would be indistinguishable
    from "never assessed" but is worth confirming isn't accidentally a
    destructive overwrite of a previously-set value)."""
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id, status="PENDING_UNDERWRITING")

    record = await db.update_decision(
        application_id,
        status="APPROVED",
        underwriter_name="u1",
        underwriter_comment="looks fine",
        underwriter_decided_at=datetime.now(timezone.utc),
    )

    assert record["risk_tier"] is None


async def test_clear_risk_assessment_flips_status_without_touching_other_columns():
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id, status="PENDING_RISK_ASSESSMENT")

    record = await db.clear_risk_assessment(application_id)

    assert record["status"] == "PENDING_UNDERWRITING"
    assert record["underwriter_name"] is None
    assert record["manager_name"] is None
    assert record["risk_tier"] is None


async def test_update_resubmission_replaces_payload_and_resets_status():
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id, payload={"old": True})
    await db.update_decision(application_id, status="MORE_INFO_REQUESTED")

    record = await db.update_resubmission(application_id, {"new": True}, STATUS_PENDING_UNDERWRITING)

    assert record["payload"] == {"new": True}
    assert record["status"] == "PENDING_UNDERWRITING"


async def test_list_for_applicant_filters_and_orders_by_created_at_desc():
    await _insert_sample(applicant_identifier="alice@example.com")
    await _insert_sample(applicant_identifier="alice@example.com")
    await _insert_sample(applicant_identifier="bob@example.com")

    records = await db.list_for_applicant("alice@example.com", limit=10, offset=0)
    assert len(records) == 2
    assert all(r["applicant_identifier"] == "alice@example.com" for r in records)

    count = await db.count_for_applicant("alice@example.com")
    assert count == 2
    assert await db.count_for_applicant("nobody@example.com") == 0


async def test_list_for_applicant_pagination():
    for _ in range(3):
        await _insert_sample(applicant_identifier="paged@example.com")

    page1 = await db.list_for_applicant("paged@example.com", limit=2, offset=0)
    page2 = await db.list_for_applicant("paged@example.com", limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 1


async def test_survives_loan_apply_requests_truncation():
    """The actual point of Phase 23: applications.status is a durable,
    mirrored copy -- get()/list_for_applicant()/list_by_status() must
    keep returning the application (status intact) even after its
    loan_apply_requests row is gone, not vanish or raise. This is what
    a LEFT JOIN (not JOIN) actually buys over the old single-table
    shape."""
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id, applicant_identifier="carol@example.com")
    await db.update_decision(
        application_id,
        status="APPROVED",
        underwriter_name="u1",
        underwriter_comment="ok",
        underwriter_decided_at=datetime.now(timezone.utc),
    )

    pool = await db._get_pool()
    await pool.execute("DELETE FROM loan_apply_requests WHERE application_id = $1", application_id)

    fetched = await db.get(application_id)
    assert fetched["status"] == "APPROVED"
    assert fetched["workflow_id"] is None
    assert fetched["underwriter_name"] is None
    assert fetched["updated_at"] is None

    listed = await db.list_for_applicant("carol@example.com", limit=10, offset=0)
    assert len(listed) == 1
    assert listed[0]["status"] == "APPROVED"

    by_status = await db.list_by_status("APPROVED", limit=10, offset=0)
    assert application_id in [r["application_id"] for r in by_status]


async def test_list_by_status_filters_and_counts():
    await _insert_sample()
    await _insert_sample()
    application_id = _new_application_id()
    await _insert_sample(application_id=application_id)
    await db.update_decision(application_id, status="APPROVED")

    pending = await db.list_by_status("PENDING_UNDERWRITING", limit=10, offset=0)
    approved = await db.list_by_status("APPROVED", limit=10, offset=0)

    assert len(pending) == 2
    assert len(approved) == 1
    assert await db.count_by_status("PENDING_UNDERWRITING") == 2
    assert await db.count_by_status("APPROVED") == 1
