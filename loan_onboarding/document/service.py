"""`document/`'s public API -- the only way any other module touches
Mayan EDMS (CLAUDE.md's module dependency graph: `document/` is a leaf,
never imports `application/` or `workflow/`).

Per-product-type required-category table (PRD §6.4) is owned here as a
plain hardcoded dict, not imported from `workflow.task_queues` --
`document/` never imports `workflow/`, even for a registry, so this is a
deliberate duplication of the three product-type strings rather than a
shared import. Unlike `application/schemas.py`'s registry (which *can*
assert against `workflow.task_queues.KNOWN_PRODUCT_TYPES` because
`application/` is allowed to import `workflow/`), there's no import-time
check wiring these two together -- a fourth product type added to
`workflow/task_queues.py` without a matching entry here would silently
make `check_completeness` treat it as needing zero documents. Not
flagged as a Known Gap in CLAUDE.md today because `KNOWN_PRODUCT_TYPES`
essentially never changes after being fixed at project start; revisit
if that assumption stops holding.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from .mayan_client import (
    DOCUMENT_TYPE_ACCOUNT,
    DOCUMENT_TYPE_APPLICATION,
    mayan_client,
)
from .models import DocumentRef, DocumentStream, UploadedFile

# The one category with system behavior hung off it (photo promotion
# on approval, CLAUDE.md's "Applying without being a customer yet") --
# named here so bff_customer/routes.py's camera-capture hint and this
# module's own promote_government_id_to_customer_photo() can't drift
# apart on the exact string.
CATEGORY_GOVERNMENT_ID = "Government ID"

REQUIRED_CATEGORIES: dict[str, list[str]] = {
    "personal_loan": [CATEGORY_GOVERNMENT_ID, "Proof of Income", "Bank Statements", "Credit Report"],
    "auto_loan": [
        CATEGORY_GOVERNMENT_ID,
        "Proof of Income",
        "Bank Statements",
        "Credit Report",
        "Vehicle Title/Invoice",
    ],
    "mortgage": [
        CATEGORY_GOVERNMENT_ID,
        "Proof of Income",
        "Bank Statements",
        "Credit Report",
        "Property Appraisal",
    ],
}

# POC-scale safety bound -- see mayan-edms-customer-archive's own
# documents_service.py for the same constant and the same reasoning:
# Mayan's advanced-search endpoint doesn't AND separate metadata fields
# against the same row (verified there), so an exact multi-field match
# means fetch-candidates-then-filter-in-Python, not real server-side
# filtering. Fine at this data volume; would need a different approach
# at production scale.
_MAX_SEARCH_CANDIDATES = 1000


class DocumentNotFound(Exception):
    pass


def _today() -> str:
    """UTC calendar date, ISO 8601 (`YYYY-MM-DD`) -- attached as the
    `creation_date` metadata field at document-creation time only (never
    updated on a later file version, e.g. `upload_consent`'s replace
    path), so it always reflects when the Mayan document itself was
    first created, not when its content last changed."""
    return datetime.now(timezone.utc).date().isoformat()


async def _fetch_all_documents(cap: int = _MAX_SEARCH_CANDIDATES) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    page = 1
    while len(documents) < cap:
        data = await mayan_client.list_documents(page=page, page_size=100)
        documents.extend(data["results"])
        if not data.get("next"):
            break
        page += 1
    return documents[:cap]


async def _metadata_map_for_id(document_id: int) -> dict[str, str] | None:
    try:
        entries = await mayan_client.get_document_metadata(document_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # Deleted concurrently with this request -- drop it rather
            # than failing the whole query (same reasoning as the
            # reference project's identical guard).
            return None
        raise
    return {entry["metadata_type"]["name"]: entry["value"] for entry in entries}


async def _documents_matching(filters: dict[str, str]) -> list[DocumentRef]:
    """Exact-match on every (field, value) pair in `filters`, filtered in
    Python against each candidate's real metadata -- NEVER via Mayan's
    index-tree endpoints (async rebuild lag, CLAUDE.md's "Document
    hierarchy") and never via its advanced-search metadata params either
    (doesn't AND across fields, see module docstring / `_MAX_SEARCH_CANDIDATES`
    above)."""
    candidates = await _fetch_all_documents()
    metadata_maps = await asyncio.gather(*(_metadata_map_for_id(d["id"]) for d in candidates))
    matches: list[DocumentRef] = []
    for document, metadata in zip(candidates, metadata_maps):
        if metadata is None:
            continue
        if all(metadata.get(field) == value for field, value in filters.items()):
            matches.append(DocumentRef.from_mayan(document, metadata))
    return matches


async def upload(applicant_identifier: str, application_id: str, category: str, file: UploadedFile) -> DocumentRef:
    """create-document -> upload-file (`action_name=replace`) -> attach
    metadata -> rebuild index. Safe to call repeatedly for the same
    `application_id`/`category` -- each call creates a distinct Mayan
    document, satisfying that category alongside any prior upload rather
    than replacing it (CLAUDE.md: "a category is satisfied by one or
    more documents, not exactly one"). No `account_id`/`customer_id`
    param -- neither exists yet at upload time under the
    account-on-approval model."""
    doc_type_ids, metadata_type_ids = await asyncio.gather(
        mayan_client.document_type_ids(), mayan_client.metadata_type_ids()
    )

    document = await mayan_client.create_document(doc_type_ids[DOCUMENT_TYPE_APPLICATION], file.filename)
    document_id = document["id"]

    await mayan_client.upload_file(document_id, file.filename, file.content, action_name="replace")

    # Sequential, not concurrent: each attach call re-triggers async
    # index evaluation server-side (gotcha #2) -- firing them
    # concurrently would make the race worse, not better.
    creation_date = _today()
    for field, value in [
        ("applicant_identifier", applicant_identifier),
        ("application_id", application_id),
        ("category", category),
        ("creation_date", creation_date),
    ]:
        await mayan_client.attach_metadata(document_id, metadata_type_ids[field], value)

    await mayan_client.rebuild_index()

    return DocumentRef(
        document_id=document_id,
        filename=file.filename,
        category=category,
        applicant_identifier=applicant_identifier,
        application_id=application_id,
        creation_date=creation_date,
    )


async def list_documents(application_id: str) -> list[DocumentRef]:
    return await _documents_matching({"application_id": application_id})


async def check_completeness(application_id: str, product_type: str) -> list[str]:
    """Missing required categories, empty if satisfied. Queries Mayan's
    document/metadata search directly (via `_documents_matching`) --
    never the Index Template tree, whose rebuild is async and would risk
    a false "still missing" result immediately after the customer's last
    upload (CLAUDE.md's "Document hierarchy")."""
    required = REQUIRED_CATEGORIES[product_type]
    documents = await _documents_matching({"application_id": application_id})
    present = {doc.category for doc in documents}
    return [category for category in required if category not in present]


async def preview(application_id: str, document_id: int) -> DocumentStream:
    """Streams the file from Mayan for in-app viewing -- verifies
    `document_id` actually belongs to `application_id` first (via its
    real metadata, not trust in the caller's URL) so neither BFF needs
    its own Mayan credentials nor exposes an arbitrary document by id."""
    metadata = await _metadata_map_for_id(document_id)
    if metadata is None or metadata.get("application_id") != application_id:
        raise DocumentNotFound(f"document {document_id} not found for application {application_id}")

    document = await mayan_client.get_document(document_id)
    file_latest = document.get("file_latest") or {}
    if not file_latest.get("id"):
        raise DocumentNotFound(f"document {document_id} has no uploaded file")

    response = await mayan_client.stream(f"/documents/{document_id}/files/{file_latest['id']}/download/")
    return DocumentStream(
        filename=document["label"],
        content_type=response.headers.get("content-type", "application/octet-stream"),
        aiter_bytes=response.aiter_bytes,
        aclose=response.aclose,
    )


async def promote_government_id_to_customer_photo(application_id: str, customer_id: str) -> None:
    """Re-tags the just-approved application's Government ID document
    with `customer_id` metadata -- does NOT fetch/re-upload the file.
    One Mayan document ends up satisfying two leaf paths in the index at
    once (`<application_id>/Government ID` and
    `<applicant_identifier>/id_photo`) -- confirmed against a real
    instance in P5-2, see CLAUDE.md's "Document hierarchy"."""
    matches = await _documents_matching({"application_id": application_id, "category": CATEGORY_GOVERNMENT_ID})
    if not matches:
        raise DocumentNotFound(f"no Government ID document found for application {application_id}")

    metadata_type_ids = await mayan_client.metadata_type_ids()
    for doc in matches:
        await mayan_client.attach_metadata(doc.document_id, metadata_type_ids["customer_id"], customer_id)
    await mayan_client.rebuild_index()


async def generate_welcome_letter(
    account_id: str, customer_id: str, applicant_name: str, product_type: str, amount: str
) -> DocumentRef:
    """Renders a simple templated PDF and uploads it tagged to
    `account_id` -- system-generated, no human in the loop, exactly one
    per account. Plain-argument signature only, no `application/`/
    `customer/`/`account/` imports (`document/` is a leaf module)."""
    content = _render_welcome_letter_pdf(applicant_name, product_type, amount)
    filename = f"welcome_letter_{account_id}.pdf"

    doc_type_ids, metadata_type_ids = await asyncio.gather(
        mayan_client.document_type_ids(), mayan_client.metadata_type_ids()
    )
    document = await mayan_client.create_document(doc_type_ids[DOCUMENT_TYPE_ACCOUNT], filename)
    document_id = document["id"]

    await mayan_client.upload_file(document_id, filename, content, action_name="replace")

    creation_date = _today()
    for field, value in [
        ("account_id", account_id),
        ("category", "Welcome Letter"),
        ("creation_date", creation_date),
    ]:
        await mayan_client.attach_metadata(document_id, metadata_type_ids[field], value)

    await mayan_client.rebuild_index()

    return DocumentRef(
        document_id=document_id,
        filename=filename,
        category="Welcome Letter",
        account_id=account_id,
        creation_date=creation_date,
    )


def _render_welcome_letter_pdf(applicant_name: str, product_type: str, amount: str) -> bytes:
    """A genuinely valid one-page PDF (real object structure, correct
    xref table) -- gotcha #4 in mayan-edms-customer-archive's
    docs/document-hierarchy-setup.md: a hand-typed stub PDF passes every
    upload check but renders zero pages."""
    text = f"Welcome! Your {product_type} for {amount} has been approved. -- {applicant_name}"
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 400 200] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream_content = f"BT /F1 12 Tf 20 100 Td ({escaped}) Tj ET".encode()
    objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream_content), stream_content))

    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return out


async def upload_consent(account_id: str, file: UploadedFile) -> DocumentRef:
    """True Mayan document versioning: if the account already has a
    "consent" document, uploads a new *file version* of that same
    document; creates the document first if none exists yet. Not
    restricted to one caller -- either BFF can call this once
    `account_id` exists.

    **`action_name="replace"` on both the first and every subsequent
    upload -- there is no "new" action.** Confirmed against Mayan's own
    `document_file_actions.py` (only three registered
    `DocumentFileAction` backends exist: `append`, `keep`, `replace`)
    and empirically against a live instance during P5-5: what makes a
    call a "new version of an existing document" rather than "a fresh
    document" is POSTing to `/documents/<EXISTING id>/files/` again, not
    a different `action_name` value -- each such POST adds a new
    `DocumentFile`/`DocumentVersion` under the same document id
    regardless of which of the three action names is used;
    `action_name` only controls how the new version's rendered pages are
    computed (`replace`: use only the new file's pages -- the one that
    actually behaves like "this is now the current version", matching
    what `upload_consent` needs). CLAUDE.md's original placeholder
    (`action_name="new"*`, flagged "confirm during this task") was
    wrong and has been corrected in place."""
    existing = await _documents_matching({"account_id": account_id, "category": "Consent"})

    if existing:
        document_id = existing[0].document_id
        await mayan_client.upload_file(document_id, file.filename, file.content, action_name="replace")
        return DocumentRef(
            document_id=document_id, filename=file.filename, category="Consent", account_id=account_id
        )

    doc_type_ids, metadata_type_ids = await asyncio.gather(
        mayan_client.document_type_ids(), mayan_client.metadata_type_ids()
    )
    document = await mayan_client.create_document(doc_type_ids[DOCUMENT_TYPE_ACCOUNT], file.filename)
    document_id = document["id"]

    await mayan_client.upload_file(document_id, file.filename, file.content, action_name="replace")

    creation_date = _today()
    for field, value in [
        ("account_id", account_id),
        ("category", "Consent"),
        ("creation_date", creation_date),
    ]:
        await mayan_client.attach_metadata(document_id, metadata_type_ids[field], value)

    await mayan_client.rebuild_index()

    return DocumentRef(
        document_id=document_id,
        filename=file.filename,
        category="Consent",
        account_id=account_id,
        creation_date=creation_date,
    )


async def list_customer_documents(customer_id: str) -> list[DocumentRef]:
    return await _documents_matching({"customer_id": customer_id})


async def list_account_documents(account_id: str) -> list[DocumentRef]:
    return await _documents_matching({"account_id": account_id})
