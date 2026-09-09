"""reconcile.py cross-references document.service/application.service/
account.service/customer.service/document.db at the function-call
boundary (same convention as every other cross-module test in this
codebase) -- no real Mayan or Postgres needed.

**Phase 26**: mocking `reconcile.document_db.<name>` (not
`reconcile._LIST_ALL_BY_TABLE`/etc. directly) only works because those
dispatch dicts wrap each call in a lambda that looks the attribute up
on the `document_db` module fresh every call -- see `reconcile.py`'s
own comment on why a dict of bound function references wouldn't have
been monkeypatchable at all. Confirmed by these tests actually passing,
not just asserted in a comment."""

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


def _mock_document_db(
    monkeypatch,
    application_rows=(),
    account_rows=(),
    customer_rows=(),
):
    """Each `*_rows` arg is a list of plain dicts (`{"mayan_id": ...}` at
    minimum) standing in for that table's real rows -- `reconcile.py`
    only ever reads `row["mayan_id"]` off them. Records every
    `delete_*_by_mayan_id` call so tests can assert on it."""
    tables = {
        "application_document": list(application_rows),
        "account_document": list(account_rows),
        "customer_document": list(customer_rows),
    }
    deleted_calls: list[tuple[str, int]] = []

    def _make_list_all(table_name):
        async def _list_all():
            return tables[table_name]

        return _list_all

    def _make_get_by_mayan_id(table_name):
        async def _get_by_mayan_id(mayan_id):
            for row in tables[table_name]:
                if row["mayan_id"] == mayan_id:
                    return row
            return None

        return _get_by_mayan_id

    def _make_delete_by_mayan_id(table_name):
        async def _delete_by_mayan_id(mayan_id):
            deleted_calls.append((table_name, mayan_id))
            tables[table_name] = [row for row in tables[table_name] if row["mayan_id"] != mayan_id]

        return _delete_by_mayan_id

    for table_name, list_attr, get_attr, delete_attr in [
        ("application_document", "list_all_application_documents", "get_application_document_by_mayan_id", "delete_application_document_by_mayan_id"),
        ("account_document", "list_all_account_documents", "get_account_document_by_mayan_id", "delete_account_document_by_mayan_id"),
        ("customer_document", "list_all_customer_documents", "get_customer_document_by_mayan_id", "delete_customer_document_by_mayan_id"),
    ]:
        monkeypatch.setattr(reconcile.document_db, list_attr, _make_list_all(table_name))
        monkeypatch.setattr(reconcile.document_db, get_attr, _make_get_by_mayan_id(table_name))
        monkeypatch.setattr(reconcile.document_db, delete_attr, _make_delete_by_mayan_id(table_name))

    return deleted_calls


def _mock_mayan_delete_and_metadata(monkeypatch):
    """Shared plumbing for tests exercising fix()'s Mayan-side calls --
    no-op fakes, since these tests care about document_db interactions,
    not Mayan's own."""
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
    return deleted_paths


async def test_application_document_with_missing_application_id_is_orphaned(monkeypatch):
    doc = _doc(1, application_id="APP-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)
    _mock_document_db(monkeypatch)

    report = await reconcile.scan()

    assert report.orphaned == [(doc, "application_id APP-missing not found")]
    assert report.stale_tags == []


async def test_account_document_with_missing_account_id_is_orphaned(monkeypatch):
    doc = _doc(1, account_id="ACC-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)
    _mock_document_db(monkeypatch)

    report = await reconcile.scan()

    assert report.orphaned == [(doc, "account_id ACC-missing not found")]
    assert report.stale_tags == []


async def test_valid_application_but_missing_customer_id_is_a_stale_tag_not_orphaned(monkeypatch):
    doc = _doc(1, application_id="APP-1", customer_id="CUS-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch, known_applications=["APP-1"])
    _mock_document_db(monkeypatch)

    report = await reconcile.scan()

    assert report.orphaned == []
    assert report.stale_tags == [doc]


async def test_customer_level_copy_with_missing_customer_id_is_orphaned_not_stale(monkeypatch):
    """The Government ID copy (customer_id only, no application_id or
    account_id at all -- document.service.promote_government_id_to_customer_photo)
    has no other owner, so a missing customer_id makes the whole document
    orphaned, not a stale tag to merely strip."""
    doc = _doc(1, customer_id="CUS-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)
    _mock_document_db(monkeypatch)

    report = await reconcile.scan()

    assert report.orphaned == [(doc, "customer_id CUS-missing not found (no other owner)")]
    assert report.stale_tags == []


async def test_document_with_no_ids_at_all_is_neither_orphaned_nor_stale(monkeypatch):
    doc = _doc(1)
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)
    _mock_document_db(monkeypatch)

    report = await reconcile.scan()

    assert report.orphaned == []
    assert report.stale_tags == []


async def test_document_with_everything_valid_is_untouched(monkeypatch):
    doc = _doc(1, application_id="APP-1", customer_id="CUS-1")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch, known_applications=["APP-1"], known_customers=["CUS-1"])
    _mock_document_db(monkeypatch, application_rows=[{"mayan_id": 1}])

    report = await reconcile.scan()

    assert report.orphaned == []
    assert report.stale_tags == []
    assert report.ghost_rows == []
    assert report.hidden == []


async def test_orphaned_application_id_short_circuits_the_customer_id_check(monkeypatch):
    """A document whose primary owner is gone is fully orphaned -- its
    customer_id (if any) is irrelevant, since the whole document is
    getting removed anyway."""
    doc = _doc(1, application_id="APP-missing", customer_id="CUS-also-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)
    _mock_document_db(monkeypatch)

    report = await reconcile.scan()

    assert len(report.orphaned) == 1
    assert report.stale_tags == []


async def test_fix_deletes_every_orphaned_document(monkeypatch):
    deleted_paths = _mock_mayan_delete_and_metadata(monkeypatch)
    deleted_mirror_rows = _mock_document_db(
        monkeypatch,
        application_rows=[{"mayan_id": 1}],
        account_rows=[{"mayan_id": 2}],
    )

    doc1 = _doc(1, application_id="APP-missing")
    doc2 = _doc(2, account_id="ACC-missing")
    orphaned = [(doc1, "reason 1"), (doc2, "reason 2")]

    await reconcile.fix(orphaned, [], [], [])

    assert deleted_paths == ["/documents/1/", "/documents/2/"]
    # Phase 26's own regression fix: trashing an orphan also deletes its
    # document/db.py mirror row, not just the Mayan document.
    assert deleted_mirror_rows == [("application_document", 1), ("account_document", 2)]


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
    _mock_document_db(monkeypatch)

    doc = _doc(1, application_id="APP-1", customer_id="CUS-missing")

    await reconcile.fix([], [doc], [], [])

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
    _mock_document_db(monkeypatch, application_rows=[{"mayan_id": 1}])

    orphaned = [(_doc(1, application_id="APP-missing"), "reason")]
    stale_tags = [_doc(2, application_id="APP-1", customer_id="CUS-missing")]

    await reconcile.fix(orphaned, stale_tags, [], [])

    assert rebuild_count == 1


async def test_fix_does_not_rebuild_index_when_nothing_to_fix(monkeypatch):
    rebuild_count = 0

    async def fake_rebuild_index():
        nonlocal rebuild_count
        rebuild_count += 1

    monkeypatch.setattr(reconcile.mayan_client, "rebuild_index", fake_rebuild_index)
    _mock_document_db(monkeypatch)

    await reconcile.fix([], [], [], [])

    assert rebuild_count == 0


async def test_fix_does_not_rebuild_index_for_ghost_rows_alone(monkeypatch):
    """Deleting a ghost row is a pure Postgres operation -- the Mayan
    document is already gone, so there's nothing for a Mayan index
    rebuild to reflect."""
    rebuild_count = 0

    async def fake_rebuild_index():
        nonlocal rebuild_count
        rebuild_count += 1

    monkeypatch.setattr(reconcile.mayan_client, "rebuild_index", fake_rebuild_index)
    _mock_document_db(monkeypatch, application_rows=[{"mayan_id": 999}])

    await reconcile.fix([], [], [("application_document", 999)], [])

    assert rebuild_count == 0


async def test_main_report_mode_does_not_mutate(monkeypatch):
    fix_calls = []

    doc = _doc(1, application_id="APP-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)
    _mock_document_db(monkeypatch)

    async def fake_fix(orphaned, stale_tags, ghost_rows, hidden):
        fix_calls.append((orphaned, stale_tags, ghost_rows, hidden))

    monkeypatch.setattr(reconcile, "fix", fake_fix)
    monkeypatch.setattr("sys.argv", ["reconcile"])

    await reconcile.main()

    assert fix_calls == []


async def test_main_fix_mode_calls_fix_with_scan_results(monkeypatch):
    fix_calls = []

    doc = _doc(1, application_id="APP-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)
    _mock_document_db(monkeypatch)

    async def fake_fix(orphaned, stale_tags, ghost_rows, hidden):
        fix_calls.append((orphaned, stale_tags, ghost_rows, hidden))

    monkeypatch.setattr(reconcile, "fix", fake_fix)
    monkeypatch.setattr("sys.argv", ["reconcile", "--fix"])

    await reconcile.main()

    assert len(fix_calls) == 1
    orphaned, stale_tags, ghost_rows, hidden = fix_calls[0]
    assert orphaned == [(doc, "application_id APP-missing not found")]
    assert stale_tags == []
    assert ghost_rows == []
    assert hidden == []


# ---------------------------------------------------------------
# Phase 26 -- ghost mirror rows and hidden documents.
# ---------------------------------------------------------------


async def test_ghost_row_detected_when_mirror_row_has_no_matching_mayan_document(monkeypatch):
    """A document/db.py row whose mayan_id isn't in the real Mayan
    document set at all -- e.g. the Mayan document was deleted directly,
    outside this app."""
    _mock_documents(monkeypatch, [])  # no real Mayan documents at all
    _mock_lookups(monkeypatch)
    _mock_document_db(monkeypatch, application_rows=[{"mayan_id": 42}])

    report = await reconcile.scan()

    assert report.ghost_rows == [("application_document", 42)]
    assert report.orphaned == []
    assert report.hidden == []


async def test_ghost_row_not_flagged_when_mirror_row_matches_a_real_document(monkeypatch):
    doc = _doc(1, application_id="APP-1")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch, known_applications=["APP-1"])
    _mock_document_db(monkeypatch, application_rows=[{"mayan_id": 1}])

    report = await reconcile.scan()

    assert report.ghost_rows == []


async def test_ghost_row_checked_across_all_three_tables(monkeypatch):
    _mock_documents(monkeypatch, [])
    _mock_lookups(monkeypatch)
    _mock_document_db(
        monkeypatch,
        application_rows=[{"mayan_id": 1}],
        account_rows=[{"mayan_id": 2}],
        customer_rows=[{"mayan_id": 3}],
    )

    report = await reconcile.scan()

    assert sorted(report.ghost_rows) == [
        ("account_document", 2),
        ("application_document", 1),
        ("customer_document", 3),
    ]


async def test_fix_deletes_ghost_rows_via_the_correct_tables_delete_function(monkeypatch):
    deleted_mirror_rows = _mock_document_db(
        monkeypatch,
        application_rows=[{"mayan_id": 1}],
        account_rows=[{"mayan_id": 2}],
        customer_rows=[{"mayan_id": 3}],
    )

    async def fake_rebuild_index():
        raise AssertionError("ghost-row-only fix must not rebuild the Mayan index")

    monkeypatch.setattr(reconcile.mayan_client, "rebuild_index", fake_rebuild_index)

    ghost_rows = [
        ("application_document", 1),
        ("account_document", 2),
        ("customer_document", 3),
    ]

    await reconcile.fix([], [], ghost_rows, [])

    assert sorted(deleted_mirror_rows) == sorted(ghost_rows)


async def test_hidden_document_detected_when_no_mirror_row_matches(monkeypatch):
    doc = _doc(1, application_id="APP-1")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch, known_applications=["APP-1"])
    _mock_document_db(monkeypatch)  # no rows in any table at all

    report = await reconcile.scan()

    assert report.hidden == [doc]
    assert report.orphaned == []
    assert report.ghost_rows == []


async def test_hidden_document_not_flagged_when_mirror_row_exists(monkeypatch):
    doc = _doc(1, application_id="APP-1")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch, known_applications=["APP-1"])
    _mock_document_db(monkeypatch, application_rows=[{"mayan_id": 1}])

    report = await reconcile.scan()

    assert report.hidden == []


async def test_orphaned_document_is_not_also_flagged_as_hidden(monkeypatch):
    """Found while writing this file's own tests: an orphaned document
    (owner already gone) naturally has no document/db.py row to find
    either, since nothing was ever provisioned for it -- without this
    exemption, every orphan would double-count as hidden too, which is
    pure noise (it's getting trashed from Mayan entirely regardless of
    its Postgres mirror status, not a second real problem)."""
    doc = _doc(1, application_id="APP-missing")
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)
    _mock_document_db(monkeypatch)  # no rows anywhere

    report = await reconcile.scan()

    assert report.orphaned == [(doc, "application_id APP-missing not found")]
    assert report.hidden == []


async def test_document_with_no_ids_at_all_is_never_flagged_as_hidden(monkeypatch):
    """Same "nothing for document/db.py to track" exemption
    orphaned/stale_tags already give a no-id document -- not a new gap
    this phase introduces."""
    doc = _doc(1)
    _mock_documents(monkeypatch, [doc])
    _mock_lookups(monkeypatch)
    _mock_document_db(monkeypatch)

    report = await reconcile.scan()

    assert report.hidden == []


async def test_fix_never_calls_any_document_db_or_mayan_mutation_for_hidden_documents(monkeypatch):
    """The user-confirmed design point this phase exists to get right:
    --fix only ever deletes, it never recreates a missing row."""

    def _explode(*args, **kwargs):
        raise AssertionError("fix() must never touch Mayan or document/db.py for a hidden document")

    monkeypatch.setattr(reconcile.mayan_client, "delete", _explode)
    monkeypatch.setattr(reconcile.mayan_client, "get_document_metadata", _explode)
    monkeypatch.setattr(reconcile.mayan_client, "rebuild_index", _explode)
    monkeypatch.setattr(reconcile.document_db, "insert_application_document", _explode)
    _mock_document_db(monkeypatch)

    hidden = [_doc(1, application_id="APP-1")]

    await reconcile.fix([], [], [], hidden)
    # No assertion beyond "didn't raise" -- _explode above is the real check.


async def test_orphan_fix_deletes_its_own_mirror_row_not_just_the_mayan_document(monkeypatch):
    """The regression case this whole phase exists to close: trashing an
    orphan used to leave its document/db.py row behind (a ghost row on
    the very next scan) -- confirmed here it now cleans up both."""
    _mock_mayan_delete_and_metadata(monkeypatch)
    deleted_mirror_rows = _mock_document_db(monkeypatch, customer_rows=[{"mayan_id": 5}])

    doc = _doc(5, customer_id="CUS-missing")  # customer-level copy, no other owner
    orphaned = [(doc, "customer_id CUS-missing not found (no other owner)")]

    await reconcile.fix(orphaned, [], [], [])

    assert deleted_mirror_rows == [("customer_document", 5)]


async def test_orphan_fix_is_a_no_op_on_document_db_for_a_no_id_document(monkeypatch):
    """A document with none of the three ids was never tracked by
    document/db.py in the first place -- trashing it in Mayan shouldn't
    call any delete function at all."""
    _mock_mayan_delete_and_metadata(monkeypatch)
    deleted_mirror_rows = _mock_document_db(monkeypatch)

    doc = _doc(9)  # no application_id/account_id/customer_id
    orphaned = [(doc, "some reason")]

    await reconcile.fix(orphaned, [], [], [])

    assert deleted_mirror_rows == []
