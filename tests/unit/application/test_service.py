"""application/service.py's tests mock workflow.service/document.service
at the function-call boundary (CLAUDE.md's Testing convention) -- no
real Temporal server or Mayan needed. customer.service/account.service
run for real against Postgres (same deliberate exception as every other
db-backed test in this package) since resolving an existing customer_id
and the active-account-per-product-type rule are exactly the
integration points worth exercising for real."""

import itertools
from decimal import Decimal

import pytest
from pydantic import ValidationError

from loan_onboarding.account import db as account_db
from loan_onboarding.application import db as application_db, service
from loan_onboarding.application.models import ApplicationNotFound
from loan_onboarding.customer import db as customer_db

_application_id_counter = itertools.count()


def _fake_application_id() -> str:
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
def _fast_wait_until(monkeypatch):
    # _wait_until reads these as module globals at call time -- shrinking
    # them here makes the timeout-path tests fast without changing
    # _wait_until's actual "poll, then give up" behavior.
    monkeypatch.setattr(service, "_CONFIRM_TIMEOUT_S", 0.2)
    monkeypatch.setattr(service, "_CONFIRM_INTERVAL_S", 0.02)


@pytest.fixture
def start_workflow_calls(monkeypatch):
    calls = []

    async def fake_get_client():
        return "fake-temporal-client"

    async def fake_start_workflow(
        client,
        application_id,
        product_type,
        payload,
        amount,
        applicant_identifier,
        applicant_name,
        applicant_email,
        applicant_phone,
        customer_id,
    ):
        calls.append(
            dict(
                client=client,
                application_id=application_id,
                product_type=product_type,
                payload=payload,
                amount=amount,
                applicant_identifier=applicant_identifier,
                applicant_name=applicant_name,
                applicant_email=applicant_email,
                applicant_phone=applicant_phone,
                customer_id=customer_id,
            )
        )
        return f"loan-application-{application_id}"

    monkeypatch.setattr(service, "_get_temporal_client", fake_get_client)
    monkeypatch.setattr(service.workflow_service, "start_workflow", fake_start_workflow)
    return calls


def _mock_completeness(monkeypatch, missing=None):
    async def fake_check_completeness(application_id, product_type):
        return missing or []

    monkeypatch.setattr(service.document_service, "check_completeness", fake_check_completeness)


def _personal_loan_payload(**overrides):
    defaults = dict(purpose="home improvement", employment_status="employed", monthly_income="5000")
    defaults.update(overrides)
    return defaults


async def test_missing_documents_returns_missing_categories_without_starting_workflow(
    monkeypatch, start_workflow_calls
):
    _mock_completeness(monkeypatch, missing=["Government ID", "Credit Report"])

    result = await service.create_application(
        applicant_identifier="alice@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Alice",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert result.application is None
    assert result.missing_categories == ["Government ID", "Credit Report"]
    assert result.application_id is not None
    assert start_workflow_calls == []


async def test_complete_application_starts_workflow_and_waits_for_persisted_row(
    monkeypatch, start_workflow_calls
):
    _mock_completeness(monkeypatch, missing=[])

    # Simulate persist_application (the workflow's first activity)
    # committing almost immediately -- what a real worker would do --
    # so _wait_until's poll finds it within the (shrunk) timeout.
    async def fake_start_workflow_with_commit(
        client, application_id, product_type, payload, amount,
        applicant_identifier, applicant_name, applicant_email, applicant_phone, customer_id,
    ):
        await application_db.insert(
            application_id=application_id,
            applicant_identifier=applicant_identifier,
            customer_id=customer_id,
            workflow_id=f"loan-application-{application_id}",
            product_type=product_type,
            payload=payload,
            applicant_name=applicant_name,
            applicant_email=applicant_email,
            applicant_phone=applicant_phone,
            amount=Decimal(str(amount)),
        )
        start_workflow_calls.append(locals())
        return f"loan-application-{application_id}"

    monkeypatch.setattr(service.workflow_service, "start_workflow", fake_start_workflow_with_commit)

    result = await service.create_application(
        applicant_identifier="alice@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Alice",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert result.missing_categories == []
    assert result.application is not None
    assert result.application.status == "PENDING_UNDERWRITING"
    assert result.application.customer_id is None
    assert len(start_workflow_calls) == 1


async def test_resolves_existing_customer_id_and_passes_it_to_start_workflow(monkeypatch, start_workflow_calls):
    _mock_completeness(monkeypatch, missing=[])
    existing = await customer_db.get_or_create("returning@example.com")

    await service.create_application(
        applicant_identifier="returning@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Returning Customer",
        applicant_email="returning@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert start_workflow_calls[0]["customer_id"] == existing["customer_id"]


async def test_new_applicant_passes_none_customer_id(monkeypatch, start_workflow_calls):
    _mock_completeness(monkeypatch, missing=[])

    await service.create_application(
        applicant_identifier="brand-new@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="New Applicant",
        applicant_email="brand-new@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert start_workflow_calls[0]["customer_id"] is None


async def test_uses_provided_application_id_when_given(monkeypatch, start_workflow_calls):
    _mock_completeness(monkeypatch, missing=[])
    provided_id = _fake_application_id()

    result = await service.create_application(
        applicant_identifier="alice@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Alice",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
        application_id=provided_id,
    )

    assert result.application_id == provided_id
    assert start_workflow_calls[0]["application_id"] == provided_id


async def test_generates_application_id_when_not_given(monkeypatch, start_workflow_calls):
    _mock_completeness(monkeypatch, missing=[])

    result = await service.create_application(
        applicant_identifier="alice@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Alice",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert result.application_id.startswith("APP-")


async def test_invalid_payload_raises_and_never_starts_workflow(monkeypatch, start_workflow_calls):
    _mock_completeness(monkeypatch, missing=[])

    with pytest.raises(ValidationError):
        await service.create_application(
            applicant_identifier="alice@example.com",
            product_type="personal_loan",
            payload={"purpose": "vacation"},  # missing required fields
            applicant_name="Alice",
            applicant_email="alice@example.com",
            applicant_phone="555-0100",
            amount=Decimal("10000"),
        )

    assert start_workflow_calls == []


async def test_wait_until_timeout_returns_none_application_even_though_workflow_started(
    monkeypatch, start_workflow_calls
):
    """Documents the accepted behavior: start_workflow only confirms
    Temporal *accepted* the call; if persist_application never commits
    within the wait budget, create_application still returns cleanly
    (application=None) rather than hanging or raising."""
    _mock_completeness(monkeypatch, missing=[])

    result = await service.create_application(
        applicant_identifier="alice@example.com",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Alice",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )

    assert len(start_workflow_calls) == 1
    assert result.missing_categories == []
    assert result.application is None


# ----------------------------------------------------------------------
# resubmit_application
# ----------------------------------------------------------------------

async def _seed_application(**overrides) -> str:
    application_id = overrides.pop("application_id", _fake_application_id())
    defaults = dict(
        application_id=application_id,
        applicant_identifier="alice@example.com",
        customer_id=None,
        workflow_id=f"loan-application-{application_id}",
        product_type="personal_loan",
        payload=_personal_loan_payload(),
        applicant_name="Alice",
        applicant_email="alice@example.com",
        applicant_phone="555-0100",
        amount=Decimal("10000"),
    )
    defaults.update(overrides)
    await application_db.insert(**defaults)
    return application_id


async def test_resubmit_raises_application_not_found_for_unknown_id():
    with pytest.raises(ApplicationNotFound):
        await service.resubmit_application(_fake_application_id(), _personal_loan_payload())


async def test_resubmit_missing_documents_returns_missing_categories_without_signaling(monkeypatch):
    application_id = await _seed_application()
    _mock_completeness(monkeypatch, missing=["Credit Report"])

    signal_calls = []

    async def fake_signal_resubmit(client, workflow_id, payload):
        signal_calls.append((workflow_id, payload))

    monkeypatch.setattr(service.workflow_service, "signal_resubmit", fake_signal_resubmit)
    monkeypatch.setattr(service, "_get_temporal_client", _fake_get_client)

    result = await service.resubmit_application(application_id, _personal_loan_payload())

    assert result.application is None
    assert result.missing_categories == ["Credit Report"]
    assert signal_calls == []


async def test_resubmit_signals_existing_workflow_and_waits_for_payload_update(monkeypatch):
    application_id = await _seed_application(payload={"purpose": "old", "employment_status": "employed", "monthly_income": "1"})
    _mock_completeness(monkeypatch, missing=[])
    monkeypatch.setattr(service, "_get_temporal_client", _fake_get_client)

    signal_calls = []

    async def fake_signal_resubmit(client, workflow_id, payload):
        signal_calls.append((workflow_id, payload))
        # Simulate persist_resubmit (the activity a real worker would run).
        await application_db.update_resubmission(application_id, payload)

    monkeypatch.setattr(service.workflow_service, "signal_resubmit", fake_signal_resubmit)

    new_payload = {"purpose": "updated", "employment_status": "employed", "monthly_income": "2"}
    result = await service.resubmit_application(application_id, new_payload)

    assert len(signal_calls) == 1
    assert signal_calls[0][0] == f"loan-application-{application_id}"
    assert result.missing_categories == []
    assert result.application is not None
    assert result.application.payload == new_payload
    assert result.application.status == "PENDING_UNDERWRITING"
    assert result.application.workflow_id == f"loan-application-{application_id}"


async def test_resubmit_validates_payload_against_stored_product_type(monkeypatch):
    application_id = await _seed_application(product_type="personal_loan")
    _mock_completeness(monkeypatch, missing=[])

    with pytest.raises(ValidationError):
        await service.resubmit_application(application_id, {"purpose": "missing other fields"})


async def _fake_get_client():
    return "fake-temporal-client"


# ----------------------------------------------------------------------
# check_decision_allowed
# ----------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["REJECT", "REQUEST_MORE_INFO", "CANCELLED"])
async def test_check_decision_allowed_noop_for_non_approve_decisions(monkeypatch, decision):
    application_id = await _seed_application()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("account.service must not be called for a non-APPROVE decision")

    monkeypatch.setattr(service.account_service, "has_active_account_of_type", fail_if_called)

    result = await service.check_decision_allowed(application_id, decision)
    assert result == []


async def test_check_decision_allowed_permits_when_customer_id_null_and_genuinely_no_customer(monkeypatch):
    application_id = await _seed_application(applicant_identifier="brand-new-approver@example.com", customer_id=None)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("account.service must not be called when no customer resolves at all")

    monkeypatch.setattr(service.account_service, "has_active_account_of_type", fail_if_called)

    result = await service.check_decision_allowed(application_id, "APPROVE")
    assert result == []


async def test_check_decision_allowed_resolves_by_identifier_when_customer_id_null_but_customer_exists():
    """Reproduces a real bug found live in Phase 13's P13-7 verification
    sweep (see CLAUDE.md's Known Gaps): two applications submitted under
    the same applicant_identifier before either is decided both get
    customer_id = NULL at submission. If one is approved first
    (provisioning a customer + an ACTIVE account) and the *other*, older
    sibling application -- whose own customer_id column was never
    backfilled -- is later Approved, check_decision_allowed must not
    trust that NULL column as proof of no conflict; it has to re-resolve
    via applicant_identifier, the same way persist_decision's own
    provisioning step does."""
    identifier = "sibling-applications@example.com"
    customer = await customer_db.get_or_create(identifier)
    customer_id = customer["customer_id"]
    # Simulates the already-provisioned ACTIVE account a sibling
    # application's approval would have created.
    await account_db.create(customer_id, "personal_loan", _fake_application_id())

    # This application's own customer_id is NULL -- it was submitted
    # before the sibling was approved and never got backfilled.
    application_id = await _seed_application(
        applicant_identifier=identifier, customer_id=None, product_type="personal_loan"
    )

    result = await service.check_decision_allowed(application_id, "APPROVE")
    assert result != []
    assert "personal_loan" in result[0]


async def test_check_decision_allowed_blocks_when_active_account_of_same_type_exists():
    customer = await customer_db.get_or_create("conflict@example.com")
    customer_id = customer["customer_id"]
    application_id = await _seed_application(
        applicant_identifier="conflict@example.com", customer_id=customer_id, product_type="personal_loan"
    )
    await account_db.create(customer_id, "personal_loan", _fake_application_id())

    result = await service.check_decision_allowed(application_id, "APPROVE")
    assert result != []
    assert "personal_loan" in result[0]


async def test_check_decision_allowed_permits_when_only_closed_account_of_same_type_exists():
    customer = await customer_db.get_or_create("closed-ok@example.com")
    customer_id = customer["customer_id"]
    account = await account_db.create(customer_id, "personal_loan", _fake_application_id())
    acct_pool = await account_db._get_pool()
    await acct_pool.execute("UPDATE accounts SET status = 'CLOSED' WHERE account_id = $1", account["account_id"])

    application_id = await _seed_application(
        applicant_identifier="closed-ok@example.com", customer_id=customer_id, product_type="personal_loan"
    )

    result = await service.check_decision_allowed(application_id, "APPROVE")
    assert result == []


async def test_check_decision_allowed_permits_when_no_conflicting_account_exists():
    customer = await customer_db.get_or_create("clean@example.com")
    customer_id = customer["customer_id"]
    application_id = await _seed_application(
        applicant_identifier="clean@example.com", customer_id=customer_id, product_type="personal_loan"
    )

    result = await service.check_decision_allowed(application_id, "APPROVE")
    assert result == []


# ----------------------------------------------------------------------
# check_decision_allowed_bulk -- closes the in-batch active-account race
# ----------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["REJECT", "REQUEST_MORE_INFO", "CANCELLED"])
async def test_check_decision_allowed_bulk_noop_for_non_approve_decisions(monkeypatch, decision):
    application_id = await _seed_application()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("account.service must not be called for a non-APPROVE decision")

    monkeypatch.setattr(service.account_service, "has_active_account_of_type", fail_if_called)

    result = await service.check_decision_allowed_bulk([application_id], decision)
    assert result == {application_id: []}


async def test_check_decision_allowed_bulk_blocks_second_sibling_in_same_batch():
    """Reproduces the exact race found live via the underwriter's own
    Bulk Approve action (CLAUDE.md's Known Gaps): two applications for
    the same applicant_identifier + product_type, neither decided yet,
    both selected into one bulk action. An independent per-item
    check_decision_allowed call would pass both (neither's account
    exists yet); check_decision_allowed_bulk must block the second one
    in this same batch instead, before either signal is ever sent."""
    identifier = "bulk-race@example.com"
    application_id_1 = await _seed_application(applicant_identifier=identifier, product_type="personal_loan")
    application_id_2 = await _seed_application(applicant_identifier=identifier, product_type="personal_loan")

    result = await service.check_decision_allowed_bulk([application_id_1, application_id_2], "APPROVE")

    assert result[application_id_1] == []
    assert result[application_id_2] != []
    assert "personal_loan" in result[application_id_2][0]


async def test_check_decision_allowed_bulk_permits_different_product_types_for_same_applicant():
    identifier = "bulk-different-products@example.com"
    application_id_1 = await _seed_application(applicant_identifier=identifier, product_type="personal_loan")
    application_id_2 = await _seed_application(applicant_identifier=identifier, product_type="auto_loan")

    result = await service.check_decision_allowed_bulk([application_id_1, application_id_2], "APPROVE")

    assert result[application_id_1] == []
    assert result[application_id_2] == []


async def test_check_decision_allowed_bulk_permits_different_applicants_same_product_type():
    application_id_1 = await _seed_application(applicant_identifier="bulk-a@example.com", product_type="personal_loan")
    application_id_2 = await _seed_application(applicant_identifier="bulk-b@example.com", product_type="personal_loan")

    result = await service.check_decision_allowed_bulk([application_id_1, application_id_2], "APPROVE")

    assert result[application_id_1] == []
    assert result[application_id_2] == []


async def test_check_decision_allowed_bulk_still_blocks_on_a_real_pre_existing_active_account():
    customer = await customer_db.get_or_create("bulk-conflict@example.com")
    customer_id = customer["customer_id"]
    await account_db.create(customer_id, "personal_loan", _fake_application_id())
    application_id = await _seed_application(
        applicant_identifier="bulk-conflict@example.com", customer_id=customer_id, product_type="personal_loan"
    )

    result = await service.check_decision_allowed_bulk([application_id], "APPROVE")

    assert result[application_id] != []
    assert "personal_loan" in result[application_id][0]


# ----------------------------------------------------------------------
# list_for_applicant / list_by_status -- pagination + count cache
# ----------------------------------------------------------------------

async def test_list_for_applicant_pagination_math():
    for _ in range(5):
        await _seed_application(applicant_identifier="paged@example.com")

    page1 = await service.list_for_applicant("paged@example.com", page=1, page_size=2)
    page2 = await service.list_for_applicant("paged@example.com", page=2, page_size=2)
    page3 = await service.list_for_applicant("paged@example.com", page=3, page_size=2)

    assert len(page1.items) == 2
    assert len(page2.items) == 2
    assert len(page3.items) == 1
    assert page1.total == page2.total == page3.total == 5


async def test_list_for_applicant_empty_result_set():
    result = await service.list_for_applicant("nobody-here@example.com", page=1, page_size=20)
    assert result.items == []
    assert result.total == 0
    assert result.query_id is not None


async def test_list_for_applicant_reuses_cached_total_via_query_id():
    for _ in range(3):
        await _seed_application(applicant_identifier="cached@example.com")

    first = await service.list_for_applicant("cached@example.com", page=1, page_size=2)
    # A second application appears after the first page mint -- if the
    # cache is genuinely reused (not recomputed), `total` stays at the
    # stale-but-consistent value from the first call.
    await _seed_application(applicant_identifier="cached@example.com")

    second = await service.list_for_applicant(
        "cached@example.com", page=2, page_size=2, query_id=first.query_id
    )
    assert second.total == first.total == 3
    assert second.query_id == first.query_id


async def test_list_for_applicant_query_id_from_different_filter_is_ignored():
    """The visibility-invariant defense: a query_id minted for one
    applicant_identifier must never be trusted for a different one, even
    if it's still within its TTL."""
    for _ in range(2):
        await _seed_application(applicant_identifier="alice-list@example.com")
    for _ in range(5):
        await _seed_application(applicant_identifier="bob-list@example.com")

    alice_page = await service.list_for_applicant("alice-list@example.com", page=1, page_size=20)
    assert alice_page.total == 2

    # Reuse alice's query_id while asking for bob's applications.
    bob_page = await service.list_for_applicant(
        "bob-list@example.com", page=1, page_size=20, query_id=alice_page.query_id
    )
    assert bob_page.total == 5  # recomputed for real, not alice's stale 2
    assert bob_page.query_id != alice_page.query_id


async def test_list_for_applicant_unknown_query_id_recomputes_instead_of_failing():
    await _seed_application(applicant_identifier="fresh@example.com")

    result = await service.list_for_applicant(
        "fresh@example.com", page=1, page_size=20, query_id="q_does_not_exist"
    )
    assert result.total == 1
    assert result.query_id != "q_does_not_exist"


async def test_list_by_status_pagination_and_filtering():
    for _ in range(3):
        await _seed_application()
    approved_id = await _seed_application()
    await application_db.update_decision(approved_id, status="APPROVED")

    pending = await service.list_by_status("PENDING_UNDERWRITING", page=1, page_size=20)
    approved = await service.list_by_status("APPROVED", page=1, page_size=20)

    assert pending.total == 3
    assert approved.total == 1
    assert all(a.status == "PENDING_UNDERWRITING" for a in pending.items)


async def test_page_size_clamped_to_max_and_page_clamped_to_minimum():
    await _seed_application(applicant_identifier="clamp@example.com")

    result = await service.list_for_applicant("clamp@example.com", page=0, page_size=10_000)
    assert result.page == 1
    assert result.page_size == service._MAX_PAGE_SIZE


# ----------------------------------------------------------------------
# get
# ----------------------------------------------------------------------

async def test_get_returns_application():
    application_id = await _seed_application()
    application = await service.get(application_id)
    assert application.application_id == application_id


async def test_get_raises_application_not_found_for_unknown_id():
    with pytest.raises(ApplicationNotFound):
        await service.get(_fake_application_id())
