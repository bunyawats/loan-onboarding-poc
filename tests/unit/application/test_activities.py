"""application/activities.py's tests hit a real Postgres for
application/customer/account (same deliberate exception as
test_db.py -- these three tables all live in the same database) but
mock document.service's two managed-document calls at the function-call
level, matching CLAUDE.md's Testing convention for a module's
dependencies that need a live service (Mayan) this test suite doesn't
stand up."""

import itertools
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from loan_onboarding.account import db as account_db, service as account_service
from loan_onboarding.application import activities, db as application_db
from loan_onboarding.customer import db as customer_db
from loan_onboarding.workflow.workflows import (
    PersistApplicationInput,
    PersistDecisionInput,
    PersistResubmitInput,
)

_application_id_counter = itertools.count()


def _new_application_id() -> str:
    return f"APP-{next(_application_id_counter):09d}"


@pytest.fixture(autouse=True)
async def _clean_tables():
    app_pool = await application_db._get_pool()
    yield
    await app_pool.execute("DELETE FROM applications")
    acct_pool = await account_db._get_pool()
    await acct_pool.execute("DELETE FROM accounts")
    cust_pool = await customer_db._get_pool()
    await cust_pool.execute("DELETE FROM customers")


@pytest.fixture(autouse=True)
def _mock_document_service(monkeypatch):
    calls = {"promote": [], "welcome_letter": []}

    async def fake_promote(application_id, customer_id):
        calls["promote"].append((application_id, customer_id))

    async def fake_generate_welcome_letter(account_id, customer_id, applicant_name, product_type, amount):
        calls["welcome_letter"].append((account_id, customer_id, applicant_name, product_type, amount))

    monkeypatch.setattr(activities.document_service, "promote_government_id_to_customer_photo", fake_promote)
    monkeypatch.setattr(activities.document_service, "generate_welcome_letter", fake_generate_welcome_letter)
    return calls


def _application_input(**overrides):
    application_id = _new_application_id()
    defaults = dict(
        application_id=application_id,
        workflow_id=f"wf-{application_id}",
        product_type="personal_loan",
        payload={"purpose": "x", "employment_status": "employed", "monthly_income": "5000"},
        amount=10000.0,
        applicant_identifier=f"applicant-{application_id}@example.com",
        applicant_name="Alice Applicant",
        applicant_email=f"applicant-{application_id}@example.com",
        applicant_phone="555-0100",
        customer_id=None,
    )
    defaults.update(overrides)
    return PersistApplicationInput(**defaults)


async def _seed_application(**overrides) -> str:
    inp = _application_input(**overrides)
    await activities.persist_application(inp)
    return inp.application_id


async def test_persist_application_inserts_row():
    inp = _application_input()
    await activities.persist_application(inp)

    record = await application_db.get(inp.application_id)
    assert record is not None
    assert record["applicant_identifier"] == inp.applicant_identifier
    assert record["amount"] == Decimal("10000.0")
    assert record["status"] == "PENDING_UNDERWRITING"


async def test_persist_decision_underwriter_reject_writes_columns_with_no_provisioning(_mock_document_service):
    application_id = await _seed_application()

    await activities.persist_decision(
        PersistDecisionInput(
            application_id=application_id,
            actor_role="underwriter",
            decision="REJECT",
            actor_name="u1",
            comment="not eligible",
            resulting_status="REJECTED",
        )
    )

    record = await application_db.get(application_id)
    assert record["status"] == "REJECTED"
    assert record["underwriter_name"] == "u1"
    assert record["underwriter_comment"] == "not eligible"
    assert record["underwriter_decided_at"] is not None
    assert record["customer_id"] is None
    assert await account_service.get_by_application_id(application_id) is None
    assert _mock_document_service["promote"] == []
    assert _mock_document_service["welcome_letter"] == []


async def test_persist_decision_underwriter_escalation_writes_no_provisioning():
    application_id = await _seed_application(amount=60000.0)

    await activities.persist_decision(
        PersistDecisionInput(
            application_id=application_id,
            actor_role="underwriter",
            decision="APPROVE",
            actor_name="u1",
            comment="escalating",
            resulting_status="PENDING_MANAGER_APPROVAL",
        )
    )

    record = await application_db.get(application_id)
    assert record["status"] == "PENDING_MANAGER_APPROVAL"
    assert record["underwriter_name"] == "u1"
    assert await account_service.get_by_application_id(application_id) is None


async def test_persist_decision_approve_provisions_customer_and_account(_mock_document_service):
    application_id = await _seed_application()

    await activities.persist_decision(
        PersistDecisionInput(
            application_id=application_id,
            actor_role="underwriter",
            decision="APPROVE",
            actor_name="u1",
            comment="approved",
            resulting_status="APPROVED",
        )
    )

    record = await application_db.get(application_id)
    assert record["status"] == "APPROVED"
    assert record["customer_id"] is not None

    account = await account_service.get_by_application_id(application_id)
    assert account is not None
    assert account.customer_id == record["customer_id"]
    assert account.product_type == "personal_loan"

    assert _mock_document_service["promote"] == [(application_id, record["customer_id"])]
    assert len(_mock_document_service["welcome_letter"]) == 1
    assert _mock_document_service["welcome_letter"][0][0] == account.account_id


async def test_persist_decision_approve_reuses_existing_customer_id_without_calling_get_or_create(monkeypatch):
    existing_customer = await customer_db.get_or_create("returning@example.com")
    application_id = await _seed_application(
        applicant_identifier="returning@example.com", customer_id=existing_customer["customer_id"]
    )

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("get_or_create must not be called when customer_id is already set")

    monkeypatch.setattr(activities.customer_service, "get_or_create", fail_if_called)

    await activities.persist_decision(
        PersistDecisionInput(
            application_id=application_id,
            actor_role="manager",
            decision="APPROVE",
            actor_name="m1",
            comment="approved",
            resulting_status="APPROVED",
        )
    )

    record = await application_db.get(application_id)
    assert record["customer_id"] == existing_customer["customer_id"]


async def test_persist_decision_approve_is_idempotent_on_retry(_mock_document_service):
    application_id = await _seed_application()
    decision_input = PersistDecisionInput(
        application_id=application_id,
        actor_role="underwriter",
        decision="APPROVE",
        actor_name="u1",
        comment="approved",
        resulting_status="APPROVED",
    )

    await activities.persist_decision(decision_input)
    first_account = await account_service.get_by_application_id(application_id)

    # Simulates Temporal retrying an already-completed execution.
    await activities.persist_decision(decision_input)
    second_account = await account_service.get_by_application_id(application_id)

    assert first_account.account_id == second_account.account_id

    acct_pool = await account_db._get_pool()
    account_count = await acct_pool.fetchval(
        "SELECT count(*) FROM accounts WHERE customer_id = $1", first_account.customer_id
    )
    assert account_count == 1
    assert len(_mock_document_service["promote"]) == 1
    assert len(_mock_document_service["welcome_letter"]) == 1


async def test_persist_decision_retry_after_document_service_failure_does_not_double_create_account(
    monkeypatch, _mock_document_service
):
    """Reproduces a real bug found in P7-3's integration run: if a
    document.service call raises (a real Mayan hiccup) after
    account_service.create_account has already succeeded, a Temporal
    retry of the whole activity must NOT try to create a second account
    for the same customer+product_type. Since P13, the committed
    `accounts` row itself (application_id NOT NULL UNIQUE) is the
    idempotency marker -- there is no longer an intermediate write back
    onto `applications` before the document.service calls, so
    `applications.status` may still lag behind APPROVED until a retry
    reaches the final write; what must never happen is a second account
    row."""
    application_id = await _seed_application()
    decision_input = PersistDecisionInput(
        application_id=application_id,
        actor_role="underwriter",
        decision="APPROVE",
        actor_name="u1",
        comment="approved",
        resulting_status="APPROVED",
    )

    async def failing_promote(application_id, customer_id):
        raise RuntimeError("simulated Mayan outage")

    monkeypatch.setattr(activities.document_service, "promote_government_id_to_customer_photo", failing_promote)

    with pytest.raises(RuntimeError, match="simulated Mayan outage"):
        await activities.persist_decision(decision_input)

    # The account must exist even though the activity as a whole raised
    # (and applications.status/customer_id may not be updated yet).
    account_after_failure = await account_service.get_by_application_id(application_id)
    assert account_after_failure is not None

    acct_pool = await account_db._get_pool()
    account_count_after_failure = await acct_pool.fetchval(
        "SELECT count(*) FROM accounts WHERE customer_id = $1", account_after_failure.customer_id
    )
    assert account_count_after_failure == 1

    # Now simulate Temporal retrying with document.service healthy again
    # -- restore the working fake (the fixture's, minus the failure).
    async def working_promote(application_id, customer_id):
        _mock_document_service["promote"].append((application_id, customer_id))

    monkeypatch.setattr(activities.document_service, "promote_government_id_to_customer_photo", working_promote)
    await activities.persist_decision(decision_input)

    final_record = await application_db.get(application_id)
    assert final_record["status"] == "APPROVED"
    account_count_final = await acct_pool.fetchval(
        "SELECT count(*) FROM accounts WHERE customer_id = $1", account_after_failure.customer_id
    )
    assert account_count_final == 1, "retry must not create a second account"
    final_account = await account_service.get_by_application_id(application_id)
    assert final_account.account_id == account_after_failure.account_id


async def test_persist_decision_approve_converts_to_rejected_on_active_account_conflict(_mock_document_service):
    """Reproduces the race-window gap found live in Phase 13's own
    verification sweep (CLAUDE.md's Known Gaps): two applications for
    the same applicant_identifier + product_type can both pass
    check_decision_allowed before either commits (that function is
    application/service.py's concern, not exercised here -- this test
    goes straight to the activity to prove persist_decision itself
    handles losing the race gracefully). The second Approve to reach
    create_account loses to the real ux_accounts_customer_active_product_type
    partial unique index; persist_decision must convert that into a
    clean REJECTED write instead of letting the exception propagate --
    which used to retry 5 times and fail the whole Temporal workflow,
    leaving the application stuck at its pre-decision status forever,
    no error ever surfaced to staff."""
    identifier = "race-conflict@example.com"
    application_id_1 = await _seed_application(applicant_identifier=identifier)
    application_id_2 = await _seed_application(applicant_identifier=identifier)

    # First Approve provisions a customer + an ACTIVE personal_loan account.
    first_status = await activities.persist_decision(
        PersistDecisionInput(
            application_id=application_id_1,
            actor_role="underwriter",
            decision="APPROVE",
            actor_name="u1",
            comment="approved",
            resulting_status="APPROVED",
        )
    )
    assert first_status == "APPROVED"

    # Second Approve for the same applicant+product_type genuinely loses
    # the race against the real partial unique index -- not mocked.
    second_status = await activities.persist_decision(
        PersistDecisionInput(
            application_id=application_id_2,
            actor_role="underwriter",
            decision="APPROVE",
            actor_name="u1",
            comment="approved",
            resulting_status="APPROVED",
        )
    )

    assert second_status == "REJECTED"

    second_record = await application_db.get(application_id_2)
    assert second_record["status"] == "REJECTED"
    assert "active personal_loan account" in second_record["underwriter_comment"]
    assert await account_service.get_by_application_id(application_id_2) is None

    # No second account was created for the shared customer.
    acct_pool = await account_db._get_pool()
    account_count = await acct_pool.fetchval(
        "SELECT count(*) FROM accounts WHERE customer_id = $1", second_record["customer_id"]
    )
    assert account_count == 1

    # The conflict path never reaches document.service -- only the
    # first, winning Approve's welcome letter exists.
    assert len(_mock_document_service["welcome_letter"]) == 1


async def test_persist_decision_reraises_a_different_unique_violation_unrecognized(monkeypatch):
    """The conflict-to-REJECTED conversion is deliberately narrow -- it
    only recognizes ux_accounts_customer_active_product_type by name.
    Any other UniqueViolationError (a bug, or a constraint this code
    doesn't know about) must still propagate as a real activity error,
    not be silently swallowed."""
    import asyncpg

    application_id = await _seed_application()

    async def fail_with_unrelated_constraint(*args, **kwargs):
        exc = asyncpg.exceptions.UniqueViolationError("simulated")
        exc.constraint_name = "some_other_constraint"
        raise exc

    monkeypatch.setattr(activities.account_service, "create_account", fail_with_unrelated_constraint)

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await activities.persist_decision(
            PersistDecisionInput(
                application_id=application_id,
                actor_role="underwriter",
                decision="APPROVE",
                actor_name="u1",
                comment="approved",
                resulting_status="APPROVED",
            )
        )


async def test_persist_decision_cancelled_touches_no_decision_columns_or_provisioning(_mock_document_service):
    application_id = await _seed_application()

    await activities.persist_decision(
        PersistDecisionInput(
            application_id=application_id,
            actor_role="customer",
            decision="CANCELLED",
            actor_name="alice",
            comment="changed my mind",
            resulting_status="CANCELLED",
        )
    )

    record = await application_db.get(application_id)
    assert record["status"] == "CANCELLED"
    assert record["underwriter_name"] is None
    assert record["manager_name"] is None
    assert record["customer_id"] is None
    assert await account_service.get_by_application_id(application_id) is None
    assert _mock_document_service["promote"] == []
    assert _mock_document_service["welcome_letter"] == []


async def test_persist_decision_native_cancel_honors_explicit_decided_at():
    application_id = await _seed_application()
    forced_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    await activities.persist_decision(
        PersistDecisionInput(
            application_id=application_id,
            actor_role="customer",
            decision="CANCELLED",
            actor_name="temporal-admin",
            comment="forced by temporal system",
            resulting_status="CANCELLED",
            decided_at=forced_time,
        )
    )

    record = await application_db.get(application_id)
    assert record["updated_at"] == forced_time


async def test_persist_resubmit_updates_payload_and_resets_status():
    application_id = await _seed_application()
    await activities.persist_decision(
        PersistDecisionInput(
            application_id=application_id,
            actor_role="underwriter",
            decision="REQUEST_MORE_INFO",
            actor_name="u1",
            comment="need bank statements",
            resulting_status="MORE_INFO_REQUESTED",
        )
    )

    await activities.persist_resubmit(
        PersistResubmitInput(application_id=application_id, payload={"purpose": "updated"})
    )

    record = await application_db.get(application_id)
    assert record["payload"] == {"purpose": "updated"}
    assert record["status"] == "PENDING_UNDERWRITING"
