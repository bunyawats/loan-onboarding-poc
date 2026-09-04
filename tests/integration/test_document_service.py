"""Integration test for document/service.py's account-level document
support (Consent, added alongside the customer/back-office consent-
upload enhancement) -- needs a real local Mayan instance
(`docker compose up -d mayan`), not just Postgres, with
`MAYAN_BASE_URL`/`MAYAN_SERVICE_ACCOUNT_USERNAME`/
`MAYAN_SERVICE_ACCOUNT_PASSWORD` set in the environment (same values
`.env` already carries for a local stack -- see CLAUDE.md's Testing
section on `@pytest.mark.integration`).

`tests/unit/document/test_service.py` already covers this logic
thoroughly against `FakeMayanClient` -- this file exists because the
fake can't catch what only real Mayan enforces: a document type
rejecting a metadata attach it was never associated with (the P16-4
bug this project already hit twice for real), Mayan's own reject-on-
duplicate-metadata-attach behavior, and real HTTP file versioning/
streaming. `document/` never imports `application/`/`account/`/
`customer/`, so this test uses synthetic, uuid4-based
`account_id`/`customer_id` values with no real Postgres row behind
them -- document/service.py doesn't care whether they resolve to
anything, only that they're stable strings it can tag and filter on.
"""

from __future__ import annotations

import uuid

import pytest

from loan_onboarding.document import service
from loan_onboarding.document.mayan_client import mayan_client
from loan_onboarding.document.models import UploadedFile
from loan_onboarding.document.service import CATEGORY_CONSENT

pytestmark = pytest.mark.integration


@pytest.fixture
async def cleanup_documents():
    """Trashes (soft-deletes, same as every other delete in this
    codebase) every document a test creates -- keeps repeated runs from
    permanently accumulating test documents in a real shared Mayan
    instance. Each test's ids are uuid4-based, so nothing here can
    collide with real application data even without this cleanup; it's
    tidiness, not correctness."""
    created_ids: list[int] = []
    yield created_ids
    for document_id in created_ids:
        await mayan_client.delete(f"/documents/{document_id}/")


def _ids() -> tuple[str, str, str]:
    unique = uuid.uuid4().hex[:9]
    return f"ACC-test{unique}", f"CUS-test{unique}", f"consent-it-{unique}@example.com"


async def test_upload_consent_creates_and_versions_a_real_document(cleanup_documents):
    account_id, customer_id, applicant_identifier = _ids()

    first = await service.upload_consent(
        applicant_identifier, account_id, customer_id, UploadedFile("consent.pdf", b"%PDF-1.4\nversion one")
    )
    cleanup_documents.append(first.document_id)

    assert first.category == CATEGORY_CONSENT
    assert first.account_id == account_id
    assert first.customer_id == customer_id

    # Real Mayan rejects a metadata attach for a type the document's
    # own document type was never associated with (P16-4's real bug) --
    # this only actually exercises that check against the live
    # instance, unlike FakeMayanClient.
    account_documents = await service.list_account_documents(account_id)
    assert [d.document_id for d in account_documents] == [first.document_id]

    second = await service.upload_consent(
        applicant_identifier, account_id, customer_id, UploadedFile("consent_v2.pdf", b"%PDF-1.4\nversion two")
    )

    # Same document, a new file version -- not a duplicate. Confirmed
    # both via the returned ref and via a second real Mayan document
    # count for this account.
    assert second.document_id == first.document_id
    assert second.customer_id == customer_id
    account_documents = await service.list_account_documents(account_id)
    assert [d.document_id for d in account_documents] == [first.document_id]

    stream = await service.preview_account_document(account_id, first.document_id)
    try:
        content = b"".join([chunk async for chunk in stream.aiter_bytes()])
    finally:
        await stream.aclose()
    assert content == b"%PDF-1.4\nversion two"


async def test_preview_account_document_raises_for_a_different_account(cleanup_documents):
    account_id, customer_id, applicant_identifier = _ids()
    other_account_id, _, _ = _ids()

    ref = await service.upload_consent(
        applicant_identifier, account_id, customer_id, UploadedFile("consent.pdf", b"%PDF-1.4\ncontent")
    )
    cleanup_documents.append(ref.document_id)

    with pytest.raises(service.DocumentNotFound):
        await service.preview_account_document(other_account_id, ref.document_id)
