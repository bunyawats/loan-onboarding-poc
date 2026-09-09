"""Composition root: cross-references every Mayan document against
Postgres to find documents whose owning application/account (or whose
secondary customer_id tag) no longer exists -- see CLAUDE.md's
"Document/database reconciliation" for the full design and why this
has to live outside every leaf module. The only file besides app.py/
worker_main.py allowed to import from every domain module.

Usage:
    python -m loan_onboarding.reconcile            # report only, no mutation
    python -m loan_onboarding.reconcile --fix       # trash orphans, strip stale tags, delete ghost rows

Two problems, addressed here only for the first:
- Drift detection (this file): Postgres and Mayan can each be modified
  independently of the other, by anything with direct access to
  either -- this walks every Mayan document and checks whether the
  Postgres row it claims to belong to still exists, regardless of how
  it went missing.
- Cascade-on-delete (not built): would only fire when this app itself
  deletes a customer/account/application through its own service
  layer -- no such delete operation exists yet. See CLAUDE.md's Known
  Gaps for why that's deliberately still open.

**Phase 26, "Extend reconcile.py to cross-check the three document
tables"** (see CLAUDE.md / IMPLEMENTATION_PLAN.md): until this phase,
this file only ever checked Mayan documents against
`customers`/`accounts`/`applications` -- it had no idea `document/`'s
own `application_document`/`account_document`/`customer_document`
tables (Phase 24) existed at all. Two new drift categories, both
keyed on `mayan_id` (Phase 25):

- **Ghost mirror row**: a `document/db.py` row whose `mayan_id` no
  longer has a matching real Mayan document -- either Mayan's own
  document was deleted directly (outside this app), or (until this
  phase's own fix, see `fix()` below) this very tool's own orphan
  cleanup left it behind. `--fix` deletes it -- the same kind of
  cleanup this tool has always done (removing a row/tag that shouldn't
  exist), not a new class of mutation.
- **Hidden document**: a real Mayan document with no matching
  `document/db.py` row at all -- the dual-write-ordering risk
  `CLAUDE.md`'s `document/` module section already names as an
  accepted, previously *undetected* gap (a Postgres write failing after
  its Mayan write succeeded). **`--fix` deliberately never touches
  these** -- confirmed with the user: this tool only ever deletes
  (orphans, stale tags, ghost rows), it never performs a new class of
  mutation (a Postgres `INSERT`). A hidden document shows up in every
  report until a human resolves it by hand.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from loan_onboarding.account import service as account_service
from loan_onboarding.account.models import AccountNotFound
from loan_onboarding.application import service as application_service
from loan_onboarding.application.models import ApplicationNotFound
from loan_onboarding.customer import service as customer_service
from loan_onboarding.customer.models import CustomerNotFound
from loan_onboarding.document import db as document_db
from loan_onboarding.document import service as document_service
from loan_onboarding.document.mayan_client import mayan_client
from loan_onboarding.document.models import DocumentRef

Orphan = tuple[DocumentRef, str]
# (table_name, mayan_id) -- table_name is one of "application_document"/
# "account_document"/"customer_document", per _mirror_table_for below.
GhostRow = tuple[str, int]

# One dict per direction: which table a document's own id shape maps to,
# and the matching list/get/delete function for that table. Keeping
# these as data (rather than three near-identical if/elif chains
# repeated in scan()/fix()) is what lets both functions share the exact
# same table-name -> function mapping without drifting apart.
#
# Each entry is a small wrapper, not the function object itself --
# `document_db.list_all_application_documents` (etc.) would bind the
# *current* function object into this dict at import time, which
# `monkeypatch.setattr(reconcile.document_db, "list_all_application_documents", ...)`
# (this file's own test convention) can't reach after the fact. Calling
# through `document_db.<name>(...)` inside a wrapper instead looks the
# attribute up on the module fresh on every call, exactly like every
# other `document_service.<name>(...)`/`mayan_client.<name>(...)` call
# already in this file -- found and fixed while writing this file's own
# tests, not a hypothetical concern.
_LIST_ALL_BY_TABLE = {
    "application_document": lambda: document_db.list_all_application_documents(),
    "account_document": lambda: document_db.list_all_account_documents(),
    "customer_document": lambda: document_db.list_all_customer_documents(),
}
_GET_BY_MAYAN_ID_BY_TABLE = {
    "application_document": lambda mayan_id: document_db.get_application_document_by_mayan_id(mayan_id),
    "account_document": lambda mayan_id: document_db.get_account_document_by_mayan_id(mayan_id),
    "customer_document": lambda mayan_id: document_db.get_customer_document_by_mayan_id(mayan_id),
}
_DELETE_BY_MAYAN_ID_BY_TABLE = {
    "application_document": lambda mayan_id: document_db.delete_application_document_by_mayan_id(mayan_id),
    "account_document": lambda mayan_id: document_db.delete_account_document_by_mayan_id(mayan_id),
    "customer_document": lambda mayan_id: document_db.delete_customer_document_by_mayan_id(mayan_id),
}


@dataclass
class ReconcileReport:
    orphaned: list[Orphan]
    stale_tags: list[DocumentRef]
    ghost_rows: list[GhostRow]
    hidden: list[DocumentRef]


def _mirror_table_for(doc: DocumentRef) -> str | None:
    """Which `document/db.py` table `doc` belongs in, by the same
    id-shape classification this codebase already uses twice (the
    original P24-5 live backfill script, `list_customer_documents`'s
    own pre-Phase-24 heuristic) -- `None` for a document with none of
    the three ids, which `document/db.py` never tracked at all (not a
    new gap this phase introduces)."""
    if doc.application_id is not None:
        return "application_document"
    if doc.account_id is not None:
        return "account_document"
    if doc.customer_id is not None:
        return "customer_document"
    return None


async def _application_exists(application_id: str) -> bool:
    try:
        await application_service.get(application_id)
        return True
    except ApplicationNotFound:
        return False


async def _account_exists(account_id: str) -> bool:
    try:
        await account_service.get(account_id)
        return True
    except AccountNotFound:
        return False


async def _customer_exists(customer_id: str) -> bool:
    try:
        await customer_service.get(customer_id)
        return True
    except CustomerNotFound:
        return False


async def scan() -> ReconcileReport:
    """orphaned: documents whose primary owner no longer resolves -- the
    document itself has nothing left to belong to and should be
    removed. Primary owner is `application_id` for an Application
    Document, `account_id` for an Account Document, **or `customer_id`
    for the customer-level Government ID copy specifically** (a
    document carrying `customer_id` but neither `application_id` nor
    `account_id` -- `document.service.promote_government_id_to_customer_photo`'s
    copy has no other owner at all, so a missing customer means the
    whole document is orphaned, not just a stale tag to strip).

    stale_tags: documents whose primary owner (application_id or
    account_id) still resolves fine, but whose *secondary* customer_id
    tag points at a customer row that's gone -- narrower than orphaned,
    fixed by stripping just that metadata entry. **Corrected from an
    earlier draft, written before `promote_government_id_to_customer_photo`
    became a copy operation**: that draft treated every `customer_id`
    as secondary, which was true when the only place `customer_id` ever
    lived alone (no application_id/account_id) didn't exist yet -- the
    copy makes that case real, and stripping its only tag instead of
    trashing it would leave a genuinely untethered document with zero
    identifying metadata at all, invisible to every future scan.

    ghost_rows (Phase 26): `document/db.py` rows whose `mayan_id` isn't
    in the real Mayan document set at all -- computed once per table via
    P26-1's new `list_all_*` scans, checked against the same
    `document_service.list_all_documents()` call already made for
    orphaned/stale_tags above (one Mayan scan, not one per table).

    hidden (Phase 26): real Mayan documents with no matching
    `document/db.py` row, checked the reverse way via each table's own
    by-`mayan_id` lookup -- see `_mirror_table_for`'s own docstring for
    the classification rule."""
    documents = await document_service.list_all_documents()
    mayan_ids = {doc.document_id for doc in documents}

    orphaned: list[Orphan] = []
    stale_tags: list[DocumentRef] = []

    for doc in documents:
        if doc.application_id is not None and not await _application_exists(doc.application_id):
            orphaned.append((doc, f"application_id {doc.application_id} not found"))
            continue
        if doc.account_id is not None and not await _account_exists(doc.account_id):
            orphaned.append((doc, f"account_id {doc.account_id} not found"))
            continue
        if doc.customer_id is not None and not await _customer_exists(doc.customer_id):
            if doc.application_id is None and doc.account_id is None:
                orphaned.append((doc, f"customer_id {doc.customer_id} not found (no other owner)"))
            else:
                stale_tags.append(doc)

    ghost_rows: list[GhostRow] = []
    for table_name, list_all in _LIST_ALL_BY_TABLE.items():
        for row in await list_all():
            if row["mayan_id"] not in mayan_ids:
                ghost_rows.append((table_name, row["mayan_id"]))

    # Skip documents already orphaned above -- an orphan is getting
    # trashed from Mayan entirely regardless of whether it also has a
    # Postgres mirror row, so flagging it as hidden too would just be
    # noise in the report, not a second real problem to fix.
    orphaned_document_ids = {doc.document_id for doc, _ in orphaned}

    hidden: list[DocumentRef] = []
    for doc in documents:
        if doc.document_id in orphaned_document_ids:
            continue
        table_name = _mirror_table_for(doc)
        if table_name is None:
            continue
        if await _GET_BY_MAYAN_ID_BY_TABLE[table_name](doc.document_id) is None:
            hidden.append(doc)

    return ReconcileReport(orphaned=orphaned, stale_tags=stale_tags, ghost_rows=ghost_rows, hidden=hidden)


async def fix(
    orphaned: list[Orphan],
    stale_tags: list[DocumentRef],
    ghost_rows: list[GhostRow],
    hidden: list[DocumentRef],
) -> None:
    """`hidden` is accepted for signature symmetry with `ReconcileReport`
    (so a caller can pass every field of one `scan()` result through
    without omitting any) but is deliberately never acted on -- see this
    module's own docstring for why `--fix` never auto-recreates a
    missing Postgres row."""
    for doc, _ in orphaned:
        response = await mayan_client.delete(f"/documents/{doc.document_id}/")
        response.raise_for_status()
        # Phase 26's own found-and-fixed bug: trashing the Mayan document
        # without also deleting its document/db.py mirror row used to
        # leave exactly the kind of ghost row this phase's own new check
        # now detects -- every orphan cleanup before this fix has been
        # doing that silently.
        table_name = _mirror_table_for(doc)
        if table_name is not None:
            await _DELETE_BY_MAYAN_ID_BY_TABLE[table_name](doc.document_id)

    for doc in stale_tags:
        entries = await mayan_client.get_document_metadata(doc.document_id)
        for entry in entries:
            if entry["metadata_type"]["name"] == "customer_id":
                await mayan_client.delete_metadata_entry(doc.document_id, entry["id"])

    for table_name, mayan_id in ghost_rows:
        await _DELETE_BY_MAYAN_ID_BY_TABLE[table_name](mayan_id)

    if orphaned or stale_tags:
        await mayan_client.rebuild_index()


def _print_report(report: ReconcileReport) -> None:
    print(f"Orphaned documents: {len(report.orphaned)}")
    for doc, reason in report.orphaned:
        print(f"  [{doc.document_id}] {doc.filename} -- {reason}")
    print(f"Stale customer_id tags: {len(report.stale_tags)}")
    for doc in report.stale_tags:
        print(f"  [{doc.document_id}] {doc.filename} -- customer_id {doc.customer_id} not found")
    print(f"Ghost mirror rows: {len(report.ghost_rows)}")
    for table_name, mayan_id in report.ghost_rows:
        print(f"  [{table_name}] mayan_id {mayan_id} -- no matching Mayan document")
    print(
        f"Hidden documents (Postgres mirror row missing -- --fix never "
        f"auto-recreates these, manual reconciliation needed): {len(report.hidden)}"
    )
    for doc in report.hidden:
        print(f"  [{doc.document_id}] {doc.filename}")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile Mayan documents against Postgres (loan_onboarding's customer/account/application/document tables)"
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Trash orphaned documents, strip stale customer_id tags, and delete ghost mirror rows "
        "(default: report only, no mutation; hidden documents are never auto-fixed either way)",
    )
    args = parser.parse_args()

    report = await scan()
    _print_report(report)

    if args.fix:
        if report.orphaned or report.stale_tags or report.ghost_rows:
            await fix(report.orphaned, report.stale_tags, report.ghost_rows, report.hidden)
            print("Fix applied.")
        else:
            print("Nothing to fix.")


if __name__ == "__main__":
    asyncio.run(main())
