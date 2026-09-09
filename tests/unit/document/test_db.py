import itertools

import pytest

from loan_onboarding.document import db

pytestmark = pytest.mark.usefixtures("_clean_document_tables")

_mayan_id_counter = itertools.count(1)
_mayan_uuid_counter = itertools.count(1)
_application_id_counter = itertools.count()
_account_id_counter = itertools.count()
_customer_id_counter = itertools.count()


def _next_mayan_id() -> int:
    return next(_mayan_id_counter)


def _next_mayan_uuid() -> str:
    return f"uuid-{next(_mayan_uuid_counter)}"


def _fake_application_id() -> str:
    return f"APP-{next(_application_id_counter):09d}"


def _fake_account_id() -> str:
    return f"ACC-{next(_account_id_counter):09d}"


def _fake_customer_id() -> str:
    return f"CUS-{next(_customer_id_counter):09d}"


# ---------------------------------------------------------------
# application_document
# ---------------------------------------------------------------


async def test_insert_application_document_round_trips():
    application_id = _fake_application_id()
    mayan_id = _next_mayan_id()
    mayan_document_uuid = _next_mayan_uuid()

    record = await db.insert_application_document(
        mayan_document_uuid=mayan_document_uuid,
        mayan_id=mayan_id,
        application_id=application_id,
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="id.pdf",
    )

    assert record["application_document_id"].startswith("APD-")
    assert record["mayan_id"] == mayan_id
    assert record["mayan_document_uuid"] == mayan_document_uuid
    assert record["application_id"] == application_id
    assert record["applicant_identifier"] == "alice@example.com"
    assert record["category"] == "Government ID"
    assert record["filename"] == "id.pdf"
    assert record["account_id"] is None
    assert record["customer_id"] is None


async def test_insert_application_document_accepts_customer_id_at_upload_time():
    """A returning applicant who already resolves to an existing
    customer -- document.service.upload's own optional parameter,
    threaded straight through."""
    application_id = _fake_application_id()
    customer_id = _fake_customer_id()

    record = await db.insert_application_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        application_id=application_id,
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="id.pdf",
        customer_id=customer_id,
    )

    assert record["customer_id"] == customer_id
    assert record["account_id"] is None


async def test_application_document_accepts_two_rows_for_the_same_application_and_category():
    """The actual point of this table's lack of uniqueness -- a
    category is satisfied by one or more documents, not exactly one
    (CLAUDE.md). Proves the real unique index doesn't reject this, not
    just that the function would allow it."""
    application_id = _fake_application_id()

    first = await db.insert_application_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        application_id=application_id,
        applicant_identifier="alice@example.com",
        category="Bank Statements",
        filename="stmt1.pdf",
    )
    second = await db.insert_application_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        application_id=application_id,
        applicant_identifier="alice@example.com",
        category="Bank Statements",
        filename="stmt2.pdf",
    )

    assert first["application_document_id"] != second["application_document_id"]
    documents = await db.get_application_documents(application_id)
    assert len(documents) == 2
    assert {d["filename"] for d in documents} == {"stmt1.pdf", "stmt2.pdf"}


async def test_set_application_document_provisioning_updates_every_row_at_once():
    application_id = _fake_application_id()
    account_id = _fake_account_id()
    customer_id = _fake_customer_id()

    await db.insert_application_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        application_id=application_id,
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="id.pdf",
    )
    await db.insert_application_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        application_id=application_id,
        applicant_identifier="alice@example.com",
        category="Bank Statements",
        filename="stmt1.pdf",
    )
    # A different application's own document must be untouched.
    other_application_id = _fake_application_id()
    await db.insert_application_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        application_id=other_application_id,
        applicant_identifier="bob@example.com",
        category="Government ID",
        filename="bob_id.pdf",
    )

    await db.set_application_document_provisioning(application_id, account_id, customer_id)

    documents = await db.get_application_documents(application_id)
    assert len(documents) == 2
    for document in documents:
        assert document["account_id"] == account_id
        assert document["customer_id"] == customer_id

    other_documents = await db.get_application_documents(other_application_id)
    assert other_documents[0]["account_id"] is None
    assert other_documents[0]["customer_id"] is None


async def test_get_application_document_by_mayan_id():
    application_id = _fake_application_id()
    mayan_id = _next_mayan_id()
    await db.insert_application_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=mayan_id,
        application_id=application_id,
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="id.pdf",
    )

    record = await db.get_application_document_by_mayan_id(mayan_id)
    assert record["application_id"] == application_id

    assert await db.get_application_document_by_mayan_id(_next_mayan_id()) is None


# ---------------------------------------------------------------
# account_document
# ---------------------------------------------------------------


async def test_upsert_account_document_inserts_a_new_row_on_first_upload():
    account_id = _fake_account_id()
    customer_id = _fake_customer_id()

    record = await db.upsert_account_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        account_id=account_id,
        applicant_identifier="alice@example.com",
        customer_id=customer_id,
        category="Consent",
        filename="consent_v1.pdf",
    )

    assert record["account_document_id"].startswith("ACD-")
    assert record["account_id"] == account_id
    assert record["customer_id"] == customer_id
    assert record["category"] == "Consent"
    assert record["filename"] == "consent_v1.pdf"


async def test_upsert_account_document_reupload_updates_existing_row_in_place():
    """The actual point of this table's uniqueness -- a re-upload for
    the same (account_id, category) updates mayan_id/mayan_document_uuid/
    filename/updated_at on the SAME row, rather than inserting a second
    one."""
    account_id = _fake_account_id()
    customer_id = _fake_customer_id()

    first = await db.upsert_account_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        account_id=account_id,
        applicant_identifier="alice@example.com",
        customer_id=customer_id,
        category="Consent",
        filename="consent_v1.pdf",
    )
    new_mayan_id = _next_mayan_id()
    new_mayan_uuid = _next_mayan_uuid()
    second = await db.upsert_account_document(
        mayan_document_uuid=new_mayan_uuid,
        mayan_id=new_mayan_id,
        account_id=account_id,
        applicant_identifier="alice@example.com",
        customer_id=customer_id,
        category="Consent",
        filename="consent_v2.pdf",
    )

    assert second["account_document_id"] == first["account_document_id"]
    assert second["mayan_id"] == new_mayan_id
    assert second["mayan_document_uuid"] == new_mayan_uuid
    assert second["filename"] == "consent_v2.pdf"
    assert second["updated_at"] >= first["updated_at"]

    documents = await db.get_account_documents(account_id)
    assert len(documents) == 1


async def test_upsert_account_document_different_categories_are_separate_rows():
    account_id = _fake_account_id()
    customer_id = _fake_customer_id()

    await db.upsert_account_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        account_id=account_id,
        applicant_identifier="alice@example.com",
        customer_id=customer_id,
        category="Welcome Letter",
        filename="welcome.pdf",
    )
    await db.upsert_account_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        account_id=account_id,
        applicant_identifier="alice@example.com",
        customer_id=customer_id,
        category="Consent",
        filename="consent.pdf",
    )

    documents = await db.get_account_documents(account_id)
    assert len(documents) == 2


async def test_get_account_document_by_category():
    account_id = _fake_account_id()
    await db.upsert_account_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        account_id=account_id,
        applicant_identifier="alice@example.com",
        customer_id=_fake_customer_id(),
        category="Consent",
        filename="consent.pdf",
    )

    assert await db.get_account_document_by_category(account_id, "Consent") is not None
    assert await db.get_account_document_by_category(account_id, "Welcome Letter") is None


async def test_get_account_document_by_mayan_id():
    account_id = _fake_account_id()
    mayan_id = _next_mayan_id()
    await db.upsert_account_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=mayan_id,
        account_id=account_id,
        applicant_identifier="alice@example.com",
        customer_id=_fake_customer_id(),
        category="Consent",
        filename="consent.pdf",
    )

    record = await db.get_account_document_by_mayan_id(mayan_id)
    assert record["account_id"] == account_id

    assert await db.get_account_document_by_mayan_id(_next_mayan_id()) is None


# ---------------------------------------------------------------
# customer_document
# ---------------------------------------------------------------


async def test_upsert_customer_document_inserts_a_new_row_on_first_upload():
    customer_id = _fake_customer_id()

    record = await db.upsert_customer_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        customer_id=customer_id,
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="id_v1.pdf",
    )

    assert record["customer_document_id"].startswith("CUD-")
    assert record["customer_id"] == customer_id
    assert record["category"] == "Government ID"


async def test_upsert_customer_document_reupload_updates_existing_row_in_place():
    """Same "exactly one current copy" enforcement account_document's
    own re-upload test proves -- this is what makes
    promote_government_id_to_customer_photo's Postgres mirror a real,
    enforced invariant. Unlike account_document's true-versioning case,
    every call here is a genuinely new Mayan document (trash-then-
    recreate), so mayan_id/mayan_document_uuid always change too."""
    customer_id = _fake_customer_id()

    first = await db.upsert_customer_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        customer_id=customer_id,
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="id_v1.pdf",
    )
    new_mayan_id = _next_mayan_id()
    new_mayan_uuid = _next_mayan_uuid()
    second = await db.upsert_customer_document(
        mayan_document_uuid=new_mayan_uuid,
        mayan_id=new_mayan_id,
        customer_id=customer_id,
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="id_v2.pdf",
    )

    assert second["customer_document_id"] == first["customer_document_id"]
    assert second["mayan_id"] == new_mayan_id
    assert second["mayan_document_uuid"] == new_mayan_uuid
    assert second["filename"] == "id_v2.pdf"

    documents = await db.get_customer_documents(customer_id)
    assert len(documents) == 1


async def test_get_customer_document_by_category():
    customer_id = _fake_customer_id()
    await db.upsert_customer_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        customer_id=customer_id,
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="id.pdf",
    )

    assert await db.get_customer_document_by_category(customer_id, "Government ID") is not None
    assert await db.get_customer_document_by_category(customer_id, "Something Else") is None


# ---------------------------------------------------------------
# Phase 26 -- the new unfiltered list functions, the missing
# get_customer_document_by_mayan_id sibling, and the three
# delete-by-mayan-id functions reconcile.py's new ghost-row cleanup and
# orphan-fix regression fix both rely on.
# ---------------------------------------------------------------


async def test_list_all_application_documents_returns_every_row_regardless_of_application_id():
    await db.insert_application_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        application_id=_fake_application_id(),
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="a.pdf",
    )
    await db.insert_application_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        application_id=_fake_application_id(),
        applicant_identifier="bob@example.com",
        category="Government ID",
        filename="b.pdf",
    )

    records = await db.list_all_application_documents()
    assert len(records) == 2
    assert {r["filename"] for r in records} == {"a.pdf", "b.pdf"}


async def test_delete_application_document_by_mayan_id_removes_only_that_row():
    application_id = _fake_application_id()
    mayan_id_to_delete = _next_mayan_id()
    await db.insert_application_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=mayan_id_to_delete,
        application_id=application_id,
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="delete-me.pdf",
    )
    await db.insert_application_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        application_id=application_id,
        applicant_identifier="alice@example.com",
        category="Proof of Income",
        filename="keep-me.pdf",
    )

    await db.delete_application_document_by_mayan_id(mayan_id_to_delete)

    remaining = await db.get_application_documents(application_id)
    assert len(remaining) == 1
    assert remaining[0]["filename"] == "keep-me.pdf"


async def test_list_all_account_documents_returns_every_row_regardless_of_account_id():
    await db.upsert_account_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        account_id=_fake_account_id(),
        applicant_identifier="alice@example.com",
        customer_id=_fake_customer_id(),
        category="Welcome Letter",
        filename="a.pdf",
    )
    await db.upsert_account_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        account_id=_fake_account_id(),
        applicant_identifier="bob@example.com",
        customer_id=_fake_customer_id(),
        category="Welcome Letter",
        filename="b.pdf",
    )

    records = await db.list_all_account_documents()
    assert len(records) == 2
    assert {r["filename"] for r in records} == {"a.pdf", "b.pdf"}


async def test_delete_account_document_by_mayan_id_removes_only_that_row():
    account_id = _fake_account_id()
    other_account_id = _fake_account_id()
    mayan_id_to_delete = _next_mayan_id()
    await db.upsert_account_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=mayan_id_to_delete,
        account_id=account_id,
        applicant_identifier="alice@example.com",
        customer_id=_fake_customer_id(),
        category="Welcome Letter",
        filename="delete-me.pdf",
    )
    await db.upsert_account_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        account_id=other_account_id,
        applicant_identifier="bob@example.com",
        customer_id=_fake_customer_id(),
        category="Welcome Letter",
        filename="keep-me.pdf",
    )

    await db.delete_account_document_by_mayan_id(mayan_id_to_delete)

    assert await db.get_account_documents(account_id) == []
    remaining = await db.get_account_documents(other_account_id)
    assert len(remaining) == 1
    assert remaining[0]["filename"] == "keep-me.pdf"


async def test_get_customer_document_by_mayan_id():
    customer_id = _fake_customer_id()
    mayan_id = _next_mayan_id()
    await db.upsert_customer_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=mayan_id,
        customer_id=customer_id,
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="id.pdf",
    )

    record = await db.get_customer_document_by_mayan_id(mayan_id)
    assert record["customer_id"] == customer_id

    assert await db.get_customer_document_by_mayan_id(_next_mayan_id()) is None


async def test_list_all_customer_documents_returns_every_row_regardless_of_customer_id():
    await db.upsert_customer_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        customer_id=_fake_customer_id(),
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="a.pdf",
    )
    await db.upsert_customer_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        customer_id=_fake_customer_id(),
        applicant_identifier="bob@example.com",
        category="Government ID",
        filename="b.pdf",
    )

    records = await db.list_all_customer_documents()
    assert len(records) == 2
    assert {r["filename"] for r in records} == {"a.pdf", "b.pdf"}


async def test_delete_customer_document_by_mayan_id_removes_only_that_row():
    customer_id = _fake_customer_id()
    other_customer_id = _fake_customer_id()
    mayan_id_to_delete = _next_mayan_id()
    await db.upsert_customer_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=mayan_id_to_delete,
        customer_id=customer_id,
        applicant_identifier="alice@example.com",
        category="Government ID",
        filename="delete-me.pdf",
    )
    await db.upsert_customer_document(
        mayan_document_uuid=_next_mayan_uuid(),
        mayan_id=_next_mayan_id(),
        customer_id=other_customer_id,
        applicant_identifier="bob@example.com",
        category="Government ID",
        filename="keep-me.pdf",
    )

    await db.delete_customer_document_by_mayan_id(mayan_id_to_delete)

    assert await db.get_customer_documents(customer_id) == []
    remaining = await db.get_customer_documents(other_customer_id)
    assert len(remaining) == 1
    assert remaining[0]["filename"] == "keep-me.pdf"
