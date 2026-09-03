"""Composition root: cross-references every Mayan document against
Postgres to find documents whose owning application/account (or whose
secondary customer_id tag) no longer exists -- see CLAUDE.md's
"Document/database reconciliation" for the full design and why this
has to live outside every leaf module. The only file besides app.py/
worker_main.py allowed to import from every domain module.

Usage:
    python -m loan_onboarding.reconcile            # report only, no mutation
    python -m loan_onboarding.reconcile --fix       # trash orphans, strip stale tags

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
"""

from __future__ import annotations

import argparse
import asyncio

from loan_onboarding.account import service as account_service
from loan_onboarding.account.models import AccountNotFound
from loan_onboarding.application import service as application_service
from loan_onboarding.application.models import ApplicationNotFound
from loan_onboarding.customer import service as customer_service
from loan_onboarding.customer.models import CustomerNotFound
from loan_onboarding.document import service as document_service
from loan_onboarding.document.mayan_client import mayan_client
from loan_onboarding.document.models import DocumentRef

Orphan = tuple[DocumentRef, str]


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


async def scan() -> tuple[list[Orphan], list[DocumentRef]]:
    """Returns (orphaned, stale_tags).

    orphaned: documents whose primary owner (application_id for an
    Application Document, account_id for an Account Document) no
    longer resolves -- the document itself has nothing left to belong
    to and should be removed.

    stale_tags: documents whose primary owner still resolves fine, but
    whose secondary customer_id tag (only ever present on a promoted
    id_photo document) points at a customer row that's gone -- narrower
    than orphaned, fixed by stripping just that metadata entry."""
    documents = await document_service.list_all_documents()
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
            stale_tags.append(doc)

    return orphaned, stale_tags


async def fix(orphaned: list[Orphan], stale_tags: list[DocumentRef]) -> None:
    for doc, _ in orphaned:
        response = await mayan_client.delete(f"/documents/{doc.document_id}/")
        response.raise_for_status()

    for doc in stale_tags:
        entries = await mayan_client.get_document_metadata(doc.document_id)
        for entry in entries:
            if entry["metadata_type"]["name"] == "customer_id":
                await mayan_client.delete_metadata_entry(doc.document_id, entry["id"])

    if orphaned or stale_tags:
        await mayan_client.rebuild_index()


def _print_report(orphaned: list[Orphan], stale_tags: list[DocumentRef]) -> None:
    print(f"Orphaned documents: {len(orphaned)}")
    for doc, reason in orphaned:
        print(f"  [{doc.document_id}] {doc.filename} -- {reason}")
    print(f"Stale customer_id tags: {len(stale_tags)}")
    for doc in stale_tags:
        print(f"  [{doc.document_id}] {doc.filename} -- customer_id {doc.customer_id} not found")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile Mayan documents against Postgres (loan_onboarding's customer/account/application tables)"
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Trash orphaned documents and strip stale customer_id tags (default: report only, no mutation)",
    )
    args = parser.parse_args()

    orphaned, stale_tags = await scan()
    _print_report(orphaned, stale_tags)

    if args.fix:
        if orphaned or stale_tags:
            await fix(orphaned, stale_tags)
            print("Fix applied.")
        else:
            print("Nothing to fix.")


if __name__ == "__main__":
    asyncio.run(main())
