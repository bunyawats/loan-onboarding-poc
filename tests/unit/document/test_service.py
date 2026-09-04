import pytest

from loan_onboarding.document import service
from loan_onboarding.document.models import UploadedFile

from .fake_mayan_client import FakeMayanClient


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    fake = FakeMayanClient()
    monkeypatch.setattr(service, "mayan_client", fake)
    return fake


async def test_upload_attaches_all_three_metadata_fields_and_rebuilds(fake_client):
    ref = await service.upload(
        "alice@example.com", "app-1", "Government ID", UploadedFile("id.pdf", b"content")
    )

    assert ref.document_id in fake_client.documents
    stored = fake_client.documents[ref.document_id]
    assert stored.metadata == {
        "applicant_identifier": "alice@example.com",
        "application_id": "app-1",
        "category": "Government ID",
    }
    assert stored.document_type_label == "Application Document"
    assert fake_client.rebuild_count == 1


async def test_upload_with_customer_id_attaches_fourth_metadata_field(fake_client):
    ref = await service.upload(
        "alice@example.com", "app-1", "Government ID", UploadedFile("id.pdf", b"content"), customer_id="cust-1"
    )

    stored = fake_client.documents[ref.document_id]
    assert stored.metadata == {
        "applicant_identifier": "alice@example.com",
        "application_id": "app-1",
        "category": "Government ID",
        "customer_id": "cust-1",
    }
    assert ref.customer_id == "cust-1"


async def test_upload_same_category_twice_creates_two_documents(fake_client):
    await service.upload("alice@example.com", "app-1", "Bank Statements", UploadedFile("a.pdf", b"1"))
    await service.upload("alice@example.com", "app-1", "Bank Statements", UploadedFile("b.pdf", b"2"))

    docs = await service.list_documents("app-1")
    bank_docs = [d for d in docs if d.category == "Bank Statements"]
    assert len(bank_docs) == 2


async def test_list_documents_filters_by_application_id(fake_client):
    await service.upload("alice@example.com", "app-1", "Government ID", UploadedFile("a.pdf", b"1"))
    await service.upload("bob@example.com", "app-2", "Government ID", UploadedFile("b.pdf", b"2"))

    docs = await service.list_documents("app-1")
    assert len(docs) == 1
    assert docs[0].applicant_identifier == "alice@example.com"


async def test_list_all_documents_returns_everything_regardless_of_metadata(fake_client):
    ref_a = await service.upload("alice@example.com", "app-1", "Government ID", UploadedFile("a.pdf", b"1"))
    ref_b = await service.upload("bob@example.com", "app-2", "Government ID", UploadedFile("b.pdf", b"2"))
    # A document with zero metadata attached at all -- proves the empty
    # filter dict doesn't silently require *some* metadata to match.
    doc_type_ids = await fake_client.document_type_ids()
    bare = await fake_client.create_document(doc_type_ids["Application Document"], "bare.pdf")

    docs = await service.list_all_documents()
    assert {d.document_id for d in docs} == {ref_a.document_id, ref_b.document_id, bare["id"]}


@pytest.mark.parametrize(
    "product_type,expected_missing",
    [
        ("personal_loan", ["Government ID", "Proof of Income", "Bank Statements", "Credit Report"]),
        (
            "auto_loan",
            [
                "Government ID",
                "Proof of Income",
                "Bank Statements",
                "Credit Report",
                "Vehicle Title/Invoice",
            ],
        ),
        (
            "mortgage",
            [
                "Government ID",
                "Proof of Income",
                "Bank Statements",
                "Credit Report",
                "Property Appraisal",
            ],
        ),
    ],
)
async def test_check_completeness_reports_all_missing_when_nothing_uploaded(product_type, expected_missing):
    missing = await service.check_completeness("app-empty", product_type)
    assert missing == expected_missing


async def test_check_completeness_satisfied_when_all_categories_present():
    for category in service.REQUIRED_CATEGORIES["personal_loan"]:
        await service.upload("alice@example.com", "app-1", category, UploadedFile(f"{category}.pdf", b"x"))

    missing = await service.check_completeness("app-1", "personal_loan")
    assert missing == []


async def test_check_completeness_mortgage_needs_property_appraisal_specifically():
    for category in ["Government ID", "Proof of Income", "Bank Statements", "Credit Report"]:
        await service.upload("alice@example.com", "app-1", category, UploadedFile(f"{category}.pdf", b"x"))

    missing = await service.check_completeness("app-1", "mortgage")
    assert missing == ["Property Appraisal"]


async def test_check_completeness_satisfied_by_multiple_documents_in_one_category():
    await service.upload("alice@example.com", "app-1", "Proof of Income", UploadedFile("a.pdf", b"1"))
    await service.upload("alice@example.com", "app-1", "Proof of Income", UploadedFile("b.pdf", b"2"))
    for category in ["Government ID", "Bank Statements", "Credit Report"]:
        await service.upload("alice@example.com", "app-1", category, UploadedFile(f"{category}.pdf", b"x"))

    missing = await service.check_completeness("app-1", "personal_loan")
    assert missing == []


async def test_preview_streams_matching_document(fake_client):
    ref = await service.upload("alice@example.com", "app-1", "Government ID", UploadedFile("id.pdf", b"content"))

    stream = await service.preview("app-1", ref.document_id)
    chunks = [chunk async for chunk in stream.aiter_bytes()]
    assert chunks == [b"%PDF-1.4 fake bytes"]
    assert stream.content_type == "application/pdf"
    await stream.aclose()


async def test_preview_raises_when_document_belongs_to_different_application(fake_client):
    ref = await service.upload("alice@example.com", "app-1", "Government ID", UploadedFile("id.pdf", b"content"))

    with pytest.raises(service.DocumentNotFound):
        await service.preview("app-2", ref.document_id)


async def test_preview_raises_when_document_has_no_uploaded_file(fake_client):
    # Directly create a document with metadata but no file version, to
    # exercise the file_latest-missing branch.
    doc_type_ids = await fake_client.document_type_ids()
    metadata_type_ids = await fake_client.metadata_type_ids()
    document = await fake_client.create_document(doc_type_ids["Application Document"], "empty.pdf")
    await fake_client.attach_metadata(document["id"], metadata_type_ids["application_id"], "app-1")

    with pytest.raises(service.DocumentNotFound):
        await service.preview("app-1", document["id"])


async def test_tag_application_documents_tags_every_category_and_rebuilds_once(fake_client):
    gov_id = await service.upload("alice@example.com", "app-1", "Government ID", UploadedFile("id.pdf", b"1"))
    income = await service.upload("alice@example.com", "app-1", "Proof of Income", UploadedFile("inc.pdf", b"2"))
    other_app = await service.upload("bob@example.com", "app-2", "Government ID", UploadedFile("b.pdf", b"3"))
    fake_client.rebuild_count = 0

    await service.tag_application_documents("app-1", "acct-1", "cust-1")

    for ref in (gov_id, income):
        stored = fake_client.documents[ref.document_id]
        assert stored.metadata["account_id"] == "acct-1"
        assert stored.metadata["customer_id"] == "cust-1"
    # A document under a different application_id is left untouched.
    other_stored = fake_client.documents[other_app.document_id]
    assert "account_id" not in other_stored.metadata
    assert "customer_id" not in other_stored.metadata
    assert fake_client.rebuild_count == 1


async def test_promote_government_id_creates_a_separate_customer_level_copy(fake_client):
    """Rewritten from a re-tag-in-place to a genuine copy: the original
    document (still owned by its application) is untouched, and a
    *second* Mayan document is created carrying the same file content
    but only customer_id/applicant_identifier/category -- deliberately no
    application_id/account_id, so it lives purely at the customer level."""
    ref = await service.upload("alice@example.com", "app-1", "Government ID", UploadedFile("id.pdf", b"real content"))
    doc_count_before = len(fake_client.documents)

    await service.promote_government_id_to_customer_photo("app-1", "cust-1")

    assert len(fake_client.documents) == doc_count_before + 1

    original = fake_client.documents[ref.document_id]
    assert original.metadata["category"] == "Government ID"
    assert original.metadata["application_id"] == "app-1"
    assert "customer_id" not in original.metadata

    copy_id = next(doc_id for doc_id in fake_client.documents if doc_id != ref.document_id)
    copy = fake_client.documents[copy_id]
    assert copy.metadata["customer_id"] == "cust-1"
    assert copy.metadata["category"] == "Government ID"
    assert copy.metadata["applicant_identifier"] == "alice@example.com"
    assert "application_id" not in copy.metadata
    assert "account_id" not in copy.metadata
    assert copy.file_versions == [b"real content"]


async def test_promote_government_id_is_a_no_op_when_none_found(fake_client):
    """Changed from this function's original behavior (raise
    DocumentNotFound) -- the reuse path (CLAUDE.md's "Returning-customer
    profile refresh and ID reuse") means a just-approved application can
    legitimately have no Government ID document of its own."""
    await service.promote_government_id_to_customer_photo("app-missing", "cust-1")
    assert fake_client.rebuild_count == 0
    assert len(fake_client.documents) == 0


async def test_promote_government_id_trashes_old_copy_before_creating_new_one(fake_client):
    """Exactly one current Government ID copy per customer, enforced for
    real: promoting a second, fresh Government ID upload for the same
    customer must trash the first copy, not leave two around."""
    first_source = await service.upload("alice@example.com", "app-1", "Government ID", UploadedFile("id1.pdf", b"1"))
    await service.promote_government_id_to_customer_photo("app-1", "cust-1")
    first_copies = await service.list_customer_documents("cust-1")
    assert len(first_copies) == 1
    first_copy_id = first_copies[0].document_id

    second_source = await service.upload("alice@example.com", "app-2", "Government ID", UploadedFile("id2.pdf", b"2"))
    await service.promote_government_id_to_customer_photo("app-2", "cust-1")

    # The old copy is gone (trashed); the two source documents (still
    # owned by their own applications) are both still present untouched.
    assert first_copy_id not in fake_client.documents
    assert first_source.document_id in fake_client.documents
    assert second_source.document_id in fake_client.documents

    second_copies = await service.list_customer_documents("cust-1")
    assert len(second_copies) == 1
    assert fake_client.documents[second_copies[0].document_id].file_versions == [b"2"]


async def test_promote_government_id_does_not_touch_unrelated_documents(fake_client):
    """Regression, carried forward from the re-tag design: promoting a
    fresh Government ID must never touch other documents that also
    happen to carry customer_id (Proof of Income via
    tag_application_documents, Welcome Letter via generate_welcome_letter)."""
    await service.upload("alice@example.com", "app-1", "Government ID", UploadedFile("id1.pdf", b"1"))
    income = await service.upload("alice@example.com", "app-1", "Proof of Income", UploadedFile("inc.pdf", b"2"))
    await service.tag_application_documents("app-1", "acct-1", "cust-1")
    await service.promote_government_id_to_customer_photo("app-1", "cust-1")
    welcome_letter = await service.generate_welcome_letter(
        "alice@example.com", "acct-1", "cust-1", "Alice", "personal_loan", "10000"
    )

    await service.upload("alice@example.com", "app-2", "Government ID", UploadedFile("id2.pdf", b"3"))
    await service.promote_government_id_to_customer_photo("app-2", "cust-1")

    # Untouched -- these were never the customer-level copy.
    assert fake_client.documents[income.document_id].metadata["customer_id"] == "cust-1"
    assert fake_client.documents[welcome_letter.document_id].metadata["customer_id"] == "cust-1"
    assert income.document_id in fake_client.documents
    assert welcome_letter.document_id in fake_client.documents


async def test_has_id_photo_true_after_promotion_false_before(fake_client):
    assert await service.has_id_photo("cust-1") is False

    await service.upload("alice@example.com", "app-1", "Government ID", UploadedFile("id.pdf", b"1"))
    await service.promote_government_id_to_customer_photo("app-1", "cust-1")

    assert await service.has_id_photo("cust-1") is True


async def test_has_id_photo_false_when_customer_id_only_on_application_documents(fake_client):
    """The tightened `list_customer_documents`/`has_id_photo` must not be
    fooled by a customer who has customer_id on other documents
    (tag_application_documents) but never actually got a Government ID
    copy -- e.g. a reuse-path application where promote is a no-op."""
    await service.upload("alice@example.com", "app-1", "Proof of Income", UploadedFile("inc.pdf", b"1"))
    await service.tag_application_documents("app-1", "acct-1", "cust-1")

    assert await service.has_id_photo("cust-1") is False


async def test_check_completeness_excludes_categories_when_asked():
    for category in ["Proof of Income", "Bank Statements", "Credit Report"]:
        await service.upload("alice@example.com", "app-1", category, UploadedFile(f"{category}.pdf", b"x"))

    # Government ID never uploaded, but excluded -- the reuse path.
    missing = await service.check_completeness(
        "app-1", "personal_loan", exclude_categories=[service.CATEGORY_GOVERNMENT_ID]
    )
    assert missing == []

    # Without the exclusion, it's still reported missing.
    missing = await service.check_completeness("app-1", "personal_loan")
    assert missing == ["Government ID"]


async def test_generate_welcome_letter_uploads_tagged_to_account(fake_client):
    ref = await service.generate_welcome_letter("alice@example.com", "acct-1", "cust-1", "Alice", "personal_loan", "10000")

    assert ref.category == "Welcome Letter"
    assert ref.applicant_identifier == "alice@example.com"
    assert ref.account_id == "acct-1"
    stored = fake_client.documents[ref.document_id]
    assert stored.document_type_label == "Account Document"
    # Regression: an earlier version only attached account_id/category,
    # which left the document with nothing for the index's applicant_id
    # ancestor node to evaluate -- it landed under a top-level "None"
    # bucket instead of the applicant's own branch (found live against a
    # real Mayan instance).
    assert stored.metadata["applicant_identifier"] == "alice@example.com"
    assert stored.metadata["account_id"] == "acct-1"
    assert stored.metadata["category"] == "Welcome Letter"
    # customer_id is now attached too (CLAUDE.md's "Document metadata
    # assignment lifecycle") -- every other document tied to this
    # account/application carries it, the Welcome Letter shouldn't be
    # the one exception.
    assert stored.metadata["customer_id"] == "cust-1"
    assert ref.customer_id == "cust-1"
    assert len(stored.file_versions) == 1
    # Genuinely a PDF -- not a hand-typed stub (gotcha #4).
    assert stored.file_versions[0].startswith(b"%PDF-1.4")


async def test_upload_consent_creates_document_on_first_call(fake_client):
    ref = await service.upload_consent(
        "alice@example.com", "acct-1", "cust-1", UploadedFile("consent.pdf", b"v1")
    )

    stored = fake_client.documents[ref.document_id]
    assert stored.metadata["category"] == "Consent"
    assert stored.metadata["applicant_identifier"] == "alice@example.com"
    assert stored.metadata["account_id"] == "acct-1"
    # Attached for consistency with Welcome Letter -- both are
    # account-level documents, so both carry customer_id.
    assert stored.metadata["customer_id"] == "cust-1"
    assert ref.customer_id == "cust-1"
    assert stored.file_versions == [b"v1"]


async def test_upload_consent_versions_same_document_on_second_call(fake_client):
    first = await service.upload_consent(
        "alice@example.com", "acct-1", "cust-1", UploadedFile("consent.pdf", b"v1")
    )
    second = await service.upload_consent(
        "alice@example.com", "acct-1", "cust-1", UploadedFile("consent_v2.pdf", b"v2")
    )

    assert first.document_id == second.document_id
    # The new-version path returns the existing document's own metadata
    # (including customer_id), not a bare re-construction of it.
    assert second.customer_id == "cust-1"
    stored = fake_client.documents[first.document_id]
    assert stored.file_versions == [b"v1", b"v2"]


async def test_list_customer_documents_and_list_account_documents(fake_client):
    await service.promote_government_id_to_customer_photo(
        (await service.upload("alice@example.com", "app-1", "Government ID", UploadedFile("id.pdf", b"1"))).application_id,
        "cust-1",
    )
    await service.generate_welcome_letter("alice@example.com", "acct-1", "cust-1", "Alice", "personal_loan", "10000")

    # list_customer_documents is specifically the customer-level
    # Government ID copy -- Welcome Letter carries customer_id too
    # (CLAUDE.md's "Document metadata assignment lifecycle") but also
    # carries account_id, so it's excluded here and belongs under
    # list_account_documents instead (exclusive placement).
    customer_docs = await service.list_customer_documents("cust-1")
    assert len(customer_docs) == 1
    assert customer_docs[0].category == "Government ID"

    account_docs = await service.list_account_documents("acct-1")
    assert len(account_docs) == 1
    assert account_docs[0].category == "Welcome Letter"
