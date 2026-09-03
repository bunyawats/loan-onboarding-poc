"""reconcile.py cross-references document.service/application.service/
account.service/customer.service at the function-call boundary (same
convention as every other cross-module test in this codebase) -- no
real Mayan or Postgres needed."""

from loan_onboarding import reconcile
from loan_onboarding.account.models import AccountNotFound
from loan_onboarding.application.models import ApplicationNotFound
from loan_onboarding.customer.models import CustomerNotFound
from loan_onboarding.document.models import DocumentRef


def _doc(document_id, application_id=None, account_id=None, customer_id=None, filename="d.pdf"):
    return DocumentRef(
        document_id=document_id,
        filename=filename,
        category="Government ID",
        application_id=application_id,
        account_id=account_id,
        customer_id=customer_id,
    )


def _mock_lookups(monkeypatch, known_applications=(), known_accounts=(), known_customers=()):
    async def fake_application_get(application_id):
        if application_id not in known_applications:
            raise ApplicationNotFound(application_id)

    async def fake_account_get(account_id):
        if account_id not in known_accounts:
            raise AccountNotFound(account_id)

    async def fake_customer_get(customer_id):
        if customer_id not in known_customers:
            raise CustomerNotFound(customer_id)

    monkeypatch.setattr(reconcile.application_service, "get", fake_application_get)
    monkeypatch.setattr(reconcile.account_service, "get", fake_account_get)
    monkeypatch.setattr(reconcile.customer_service, "get", fake_customer_get)


def _mock_documents(monkeypatch, documents):
    async def fake_list_all_documents():
        return documents

    monkeypatch.setattr(reconcile.document_service, "list_all_documents", fake_list_all_documents)


async def test_application_document_with_missing_application_id_is_orphaned(monkeypatch):
    doc = _doc(1, application_id="APP-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)

    orphaned, stale_tags = await reconcile.scan()

    assert orphaned == [(doc, "application_id APP-missing not found")]
    assert stale_tags == []


async def test_account_document_with_missing_account_id_is_orphaned(monkeypatch):
    doc = _doc(1, account_id="ACC-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)

    orphaned, stale_tags = await reconcile.scan()

    assert orphaned == [(doc, "account_id ACC-missing not found")]
    assert stale_tags == []


async def test_valid_application_but_missing_customer_id_is_a_stale_tag_not_orphaned(monkeypatch):
    doc = _doc(1, application_id="APP-1", customer_id="CUS-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch, known_applications=["APP-1"])

    orphaned, stale_tags = await reconcile.scan()

    assert orphaned == []
    assert stale_tags == [doc]


async def test_document_with_no_ids_at_all_is_neither_orphaned_nor_stale(monkeypatch):
    doc = _doc(1)
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)

    orphaned, stale_tags = await reconcile.scan()

    assert orphaned == []
    assert stale_tags == []


async def test_document_with_everything_valid_is_untouched(monkeypatch):
    doc = _doc(1, application_id="APP-1", customer_id="CUS-1")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch, known_applications=["APP-1"], known_customers=["CUS-1"])

    orphaned, stale_tags = await reconcile.scan()

    assert orphaned == []
    assert stale_tags == []


async def test_orphaned_application_id_short_circuits_the_customer_id_check(monkeypatch):
    """A document whose primary owner is gone is fully orphaned -- its
    customer_id (if any) is irrelevant, since the whole document is
    getting removed anyway."""
    doc = _doc(1, application_id="APP-missing", customer_id="CUS-also-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)

    orphaned, stale_tags = await reconcile.scan()

    assert len(orphaned) == 1
    assert stale_tags == []


async def test_fix_deletes_every_orphaned_document(monkeypatch):
    deleted_paths = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    async def fake_delete(path):
        deleted_paths.append(path)
        return FakeResponse()

    async def fake_get_document_metadata(document_id):
        return []

    async def fake_rebuild_index():
        pass

    monkeypatch.setattr(reconcile.mayan_client, "delete", fake_delete)
    monkeypatch.setattr(reconcile.mayan_client, "get_document_metadata", fake_get_document_metadata)
    monkeypatch.setattr(reconcile.mayan_client, "rebuild_index", fake_rebuild_index)

    doc1 = _doc(1, application_id="APP-missing")
    doc2 = _doc(2, account_id="ACC-missing")
    orphaned = [(doc1, "reason 1"), (doc2, "reason 2")]

    await reconcile.fix(orphaned, [])

    assert deleted_paths == ["/documents/1/", "/documents/2/"]


async def test_fix_strips_only_the_customer_id_entry_for_stale_tags(monkeypatch):
    deleted_entries = []

    async def fake_delete(path):
        raise AssertionError("delete must not be called for a stale-tag-only document")

    async def fake_get_document_metadata(document_id):
        return [
            {"id": 100, "metadata_type": {"name": "application_id"}, "value": "APP-1"},
            {"id": 101, "metadata_type": {"name": "customer_id"}, "value": "CUS-missing"},
        ]

    async def fake_delete_metadata_entry(document_id, metadata_entry_id):
        deleted_entries.append((document_id, metadata_entry_id))

    async def fake_rebuild_index():
        pass

    monkeypatch.setattr(reconcile.mayan_client, "delete", fake_delete)
    monkeypatch.setattr(reconcile.mayan_client, "get_document_metadata", fake_get_document_metadata)
    monkeypatch.setattr(reconcile.mayan_client, "delete_metadata_entry", fake_delete_metadata_entry)
    monkeypatch.setattr(reconcile.mayan_client, "rebuild_index", fake_rebuild_index)

    doc = _doc(1, application_id="APP-1", customer_id="CUS-missing")

    await reconcile.fix([], [doc])

    assert deleted_entries == [(1, 101)]


async def test_fix_rebuilds_index_exactly_once_when_there_is_something_to_fix(monkeypatch):
    rebuild_count = 0

    class FakeResponse:
        def raise_for_status(self):
            pass

    async def fake_delete(path):
        return FakeResponse()

    async def fake_get_document_metadata(document_id):
        return [{"id": 1, "metadata_type": {"name": "customer_id"}, "value": "CUS-missing"}]

    async def fake_delete_metadata_entry(document_id, metadata_entry_id):
        pass

    async def fake_rebuild_index():
        nonlocal rebuild_count
        rebuild_count += 1

    monkeypatch.setattr(reconcile.mayan_client, "delete", fake_delete)
    monkeypatch.setattr(reconcile.mayan_client, "get_document_metadata", fake_get_document_metadata)
    monkeypatch.setattr(reconcile.mayan_client, "delete_metadata_entry", fake_delete_metadata_entry)
    monkeypatch.setattr(reconcile.mayan_client, "rebuild_index", fake_rebuild_index)

    orphaned = [(_doc(1, application_id="APP-missing"), "reason")]
    stale_tags = [_doc(2, application_id="APP-1", customer_id="CUS-missing")]

    await reconcile.fix(orphaned, stale_tags)

    assert rebuild_count == 1


async def test_fix_does_not_rebuild_index_when_nothing_to_fix(monkeypatch):
    rebuild_count = 0

    async def fake_rebuild_index():
        nonlocal rebuild_count
        rebuild_count += 1

    monkeypatch.setattr(reconcile.mayan_client, "rebuild_index", fake_rebuild_index)

    await reconcile.fix([], [])

    assert rebuild_count == 0


async def test_main_report_mode_does_not_mutate(monkeypatch):
    fix_calls = []

    doc = _doc(1, application_id="APP-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)

    async def fake_fix(orphaned, stale_tags):
        fix_calls.append((orphaned, stale_tags))

    monkeypatch.setattr(reconcile, "fix", fake_fix)
    monkeypatch.setattr("sys.argv", ["reconcile"])

    await reconcile.main()

    assert fix_calls == []


async def test_main_fix_mode_calls_fix_with_scan_results(monkeypatch):
    fix_calls = []

    doc = _doc(1, application_id="APP-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)

    async def fake_fix(orphaned, stale_tags):
        fix_calls.append((orphaned, stale_tags))

    monkeypatch.setattr(reconcile, "fix", fake_fix)
    monkeypatch.setattr("sys.argv", ["reconcile", "--fix"])

    await reconcile.main()

    assert len(fix_calls) == 1
    orphaned, stale_tags = fix_calls[0]
    assert orphaned == [(doc, "application_id APP-missing not found")]
    assert stale_tags == []
