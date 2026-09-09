"""`document/`'s public API -- the only way any other module touches
Mayan EDMS (CLAUDE.md's module dependency graph: `document/` is a leaf,
never imports `application/` or `workflow/`).

**Phase 24, "Document metadata persistence in Postgres"** (see
CLAUDE.md / IMPLEMENTATION_PLAN.md): every read function below
(`list_documents`, `check_completeness`, `list_account_documents`,
`list_customer_documents`, `has_id_photo`) now queries `document/db.py`
directly -- the new `application_document`/`account_document`/
`customer_document` tables are the PRIMARY source of truth for "what
documents exist," not a fallback cache, and zero Mayan calls happen on
these paths anymore. Every write function still calls Mayan first
(create/upload/attach-metadata, unchanged -- Mayan is still the system
of record for actual file bytes and the visual Index Template tree
staff browse), *then* writes the Postgres mirror -- this ordering is
deliberate, see `document/db.py`'s own module docstring for the
accepted dual-write risk it trades for. `list_all_documents()` is the
one deliberate exception -- it keeps scanning Mayan directly (via
`_documents_matching`, `_fetch_all_documents`, `_metadata_map_for_id`,
still used for that and by `tag_application_documents`' own Mayan
metadata loop), since its entire job (backing `reconcile.py`'s
cross-system orphan scan) only makes sense comparing Mayan's own truth
against Postgres, not reading the mirror it's checking.

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
from typing import Any

import asyncpg
import httpx

from . import db as document_db
from .mayan_client import (
    DOCUMENT_TYPE_ACCOUNT,
    DOCUMENT_TYPE_APPLICATION,
    METADATA_FIELD_ACCOUNT_ID,
    METADATA_FIELD_APPLICANT_IDENTIFIER,
    METADATA_FIELD_APPLICATION_ID,
    METADATA_FIELD_CATEGORY,
    METADATA_FIELD_CUSTOMER_ID,
    mayan_client,
)
from .models import DocumentRef, DocumentStream, UploadedFile

# The one category with system behavior hung off it (photo promotion
# on approval, CLAUDE.md's "Applying without being a customer yet") --
# named here so bff_customer/routes.py's camera-capture hint and this
# module's own promote_government_id_to_customer_photo() can't drift
# apart on the exact string.
CATEGORY_GOVERNMENT_ID = "Government ID"

# The other category a caller outside this module needs to name --
# bff_customer/routes.py's consent-upload route matches documents
# against this to render the current one, same "shared constant, not a
# re-typed literal" reasoning as CATEGORY_GOVERNMENT_ID above.
CATEGORY_CONSENT = "Consent"

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


def _application_document_ref(record: asyncpg.Record) -> DocumentRef:
    return DocumentRef(
        document_id=record["mayan_document_id"],
        filename=record["filename"],
        category=record["category"],
        applicant_identifier=record["applicant_identifier"],
        application_id=record["application_id"],
        account_id=record["account_id"],
        customer_id=record["customer_id"],
    )


def _account_document_ref(record: asyncpg.Record) -> DocumentRef:
    return DocumentRef(
        document_id=record["mayan_document_id"],
        filename=record["filename"],
        category=record["category"],
        applicant_identifier=record["applicant_identifier"],
        account_id=record["account_id"],
        customer_id=record["customer_id"],
    )


def _customer_document_ref(record: asyncpg.Record) -> DocumentRef:
    return DocumentRef(
        document_id=record["mayan_document_id"],
        filename=record["filename"],
        category=record["category"],
        applicant_identifier=record["applicant_identifier"],
        customer_id=record["customer_id"],
    )


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


async def _metadata_entry_id(document_id: int, field: str) -> int | None:
    """The metadata *entry*'s own id (needed for
    `delete_metadata_entry`/`update_metadata_entry`, which address an
    entry by id, not by field name) -- `None` if `document_id` has no
    entry for `field` at all."""
    entries = await mayan_client.get_document_metadata(document_id)
    for entry in entries:
        if entry["metadata_type"]["name"] == field:
            return entry["id"]
    return None


async def _set_metadata(document_id: int, field: str, value: str, metadata_type_id: int) -> None:
    """Idempotent attach: updates the entry in place if `document_id`
    already carries `field`, otherwise creates it. Mayan's metadata
    endpoint rejects a second POST for a metadata_type the document
    already has (a real 400, confirmed live in P16-4) -- needed here
    because `tag_application_documents` and
    `promote_government_id_to_customer_photo` both now attach
    `customer_id` and can both touch the same Government ID document in
    one approval (CLAUDE.md's "Document metadata assignment lifecycle"
    originally claimed this double-attach was "a harmless idempotent
    no-op" -- live verification proved that wrong; this is the fix, not
    a hypothetical)."""
    entry_id = await _metadata_entry_id(document_id, field)
    if entry_id is not None:
        await mayan_client.update_metadata_entry(document_id, entry_id, value)
    else:
        await mayan_client.attach_metadata(document_id, metadata_type_id, value)


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


async def upload(
    applicant_identifier: str,
    application_id: str,
    category: str,
    file: UploadedFile,
    customer_id: str | None = None,
) -> DocumentRef:
    """create-document -> upload-file (`action_name=replace`) -> attach
    metadata -> rebuild index. Safe to call repeatedly for the same
    `application_id`/`category` -- each call creates a distinct Mayan
    document, satisfying that category alongside any prior upload rather
    than replacing it (CLAUDE.md: "a category is satisfied by one or
    more documents, not exactly one"). No `account_id` param -- it
    doesn't exist yet at upload time under the account-on-approval
    model. `customer_id` (CLAUDE.md's "Document metadata assignment
    lifecycle") is optional and caller-supplied, not resolved here --
    `document/` is a leaf module and never imports `application/`; a
    returning applicant's caller already knows it (a resolved customer),
    a brand-new applicant's caller passes `None`, same as before this
    parameter existed."""
    doc_type_ids, metadata_type_ids = await asyncio.gather(
        mayan_client.document_type_ids(), mayan_client.metadata_type_ids()
    )

    document = await mayan_client.create_document(doc_type_ids[DOCUMENT_TYPE_APPLICATION], file.filename)
    document_id = document["id"]

    await mayan_client.upload_file(document_id, file.filename, file.content, action_name="replace")

    # Sequential, not concurrent: each attach call re-triggers async
    # index evaluation server-side (gotcha #2) -- firing them
    # concurrently would make the race worse, not better.
    fields = [
        (METADATA_FIELD_APPLICANT_IDENTIFIER, applicant_identifier),
        (METADATA_FIELD_APPLICATION_ID, application_id),
        (METADATA_FIELD_CATEGORY, category),
    ]
    if customer_id is not None:
        fields.append((METADATA_FIELD_CUSTOMER_ID, customer_id))
    for field, value in fields:
        await mayan_client.attach_metadata(document_id, metadata_type_ids[field], value)

    await mayan_client.rebuild_index()

    # Postgres mirror, written only after Mayan succeeds (Phase 24's
    # write-ordering rule -- see this module's own docstring and
    # document/db.py's).
    await document_db.insert_application_document(
        mayan_document_id=document_id,
        application_id=application_id,
        applicant_identifier=applicant_identifier,
        category=category,
        filename=file.filename,
        customer_id=customer_id,
    )

    return DocumentRef(
        document_id=document_id,
        filename=file.filename,
        category=category,
        applicant_identifier=applicant_identifier,
        application_id=application_id,
        customer_id=customer_id,
    )


async def list_documents(application_id: str) -> list[DocumentRef]:
    """Reads `document/db.py` only (Phase 24) -- zero Mayan calls."""
    records = await document_db.get_application_documents(application_id)
    return [_application_document_ref(r) for r in records]


async def list_all_documents() -> list[DocumentRef]:
    """Read-only -- every document in the Mayan instance, regardless of
    which application/account/customer (if any) it's tagged to. An
    empty filter dict already matches every document via
    `_documents_matching`; this just exposes that path under a real
    name instead of a leading-underscore function. Used only by
    `loan_onboarding/reconcile.py`'s cross-system orphan scan (see
    CLAUDE.md's "Document/database reconciliation") -- no other caller
    needs an unfiltered listing."""
    return await _documents_matching({})


async def check_completeness(
    application_id: str, product_type: str, exclude_categories: list[str] | None = None
) -> list[str]:
    """Missing required categories, empty if satisfied. Reads
    `document/db.py` only (Phase 24) -- zero Mayan calls, and no risk of
    the Index Template tree's async rebuild lag producing a false "still
    missing" result immediately after the customer's last upload
    (CLAUDE.md's "Document hierarchy") -- that risk only ever applied to
    reading the *tree*, never applied to Mayan's own document/metadata
    search either, and doesn't apply to a direct Postgres read.

    `exclude_categories` is a small, general parameter rather than a
    Government-ID-specific special case, even though Government ID is
    the only category `application.service.create_application`'s
    returning-customer reuse path excludes today (CLAUDE.md's
    "Returning-customer profile refresh and ID reuse")."""
    required = [c for c in REQUIRED_CATEGORIES[product_type] if c not in (exclude_categories or [])]
    records = await document_db.get_application_documents(application_id)
    present = {record["category"] for record in records}
    return [category for category in required if category not in present]


async def _stream_document(document_id: int) -> DocumentStream:
    """Shared by `preview`/`preview_account_document` once each has
    already verified ownership its own way -- the actual Mayan
    file-streaming call is identical either way."""
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


async def preview(application_id: str, document_id: int) -> DocumentStream:
    """Streams the file from Mayan for in-app viewing (file bytes still
    only live there) -- verifies `document_id` actually belongs to
    `application_id` first via `document/db.py` (Phase 24 -- Postgres
    now, replacing a live Mayan metadata fetch) so neither BFF needs its
    own Mayan credentials nor exposes an arbitrary document by id."""
    record = await document_db.get_application_document_by_mayan_id(document_id)
    if record is None or record["application_id"] != application_id:
        raise DocumentNotFound(f"document {document_id} not found for application {application_id}")
    return await _stream_document(document_id)


async def preview_account_document(account_id: str, document_id: int) -> DocumentStream:
    """Same ownership-then-stream shape as `preview`, scoped to
    `account_id` instead -- needed for account-level documents (Consent,
    Welcome Letter) that carry no `application_id` at all, so `preview`
    itself can never authorize them."""
    record = await document_db.get_account_document_by_mayan_id(document_id)
    if record is None or record["account_id"] != account_id:
        raise DocumentNotFound(f"document {document_id} not found for account {account_id}")
    return await _stream_document(document_id)


async def tag_application_documents(application_id: str, account_id: str, customer_id: str) -> None:
    """Attaches `account_id` + `customer_id` to *every* document under
    `application_id` (all categories, not just Government ID), rebuilding
    the index once at the end -- CLAUDE.md's "Document metadata
    assignment lifecycle", point 3. Deliberately separate from
    `promote_government_id_to_customer_photo` immediately below, whose
    own job (re-tagging one specific document, possibly stripping a
    *different* application's stale `id_photo` tag) is orthogonal --
    this function never looks outside `application_id`'s own documents.
    Re-attaching `customer_id` to the Government ID document a second
    time (once here, once via `promote_government_id_to_customer_photo`)
    is a harmless no-op in effect, but NOT at the Mayan API level -- a
    bare `attach_metadata` (POST-create) on a field the document already
    carries is a real 400 (confirmed live in P16-4, corrected from an
    earlier draft of this docstring that assumed otherwise without
    testing it), so both attaches here go through `_set_metadata`
    (update-in-place if already present).

    **Phase 24**: still keeps this exact Mayan metadata loop (the
    visual Index Template tree still needs it -- CLAUDE.md's "Document
    hierarchy") and additionally calls
    `document_db.set_application_document_provisioning` once, after the
    Mayan side succeeds -- one `UPDATE` touching every
    `application_document` row for this application at once, rather
    than a per-document Mayan-metadata-style loop, since Postgres has
    no equivalent per-document `attach_metadata` friction to work
    around."""
    matches = await _documents_matching({METADATA_FIELD_APPLICATION_ID: application_id})
    if not matches:
        return

    metadata_type_ids = await mayan_client.metadata_type_ids()
    for doc in matches:
        await _set_metadata(doc.document_id, METADATA_FIELD_ACCOUNT_ID, account_id, metadata_type_ids[METADATA_FIELD_ACCOUNT_ID])
        await _set_metadata(doc.document_id, METADATA_FIELD_CUSTOMER_ID, customer_id, metadata_type_ids[METADATA_FIELD_CUSTOMER_ID])
    await mayan_client.rebuild_index()

    await document_db.set_application_document_provisioning(application_id, account_id, customer_id)


async def promote_government_id_to_customer_photo(application_id: str, customer_id: str) -> None:
    """**Rewritten from a re-tag-in-place to a genuine copy**, per a
    direct design request: the just-approved application's Government ID
    document is left completely untouched (still owned only by its
    application, per the "exclusive placement" rule in CLAUDE.md's
    "Document hierarchy") -- a *new*, separate Mayan document is created
    with the same file content, tagged with `customer_id` (and
    `applicant_identifier`, `category`) but deliberately **no**
    `application_id`/`account_id`, so it lives purely at the customer
    level (Customer Index's own "Government ID" leaf, sibling to the
    account/application branches, never nested under either). This is
    what makes "exactly one current Government ID per customer" a
    genuinely separate, unambiguous document rather than one document
    satisfying multiple leaf conditions at once (the old design, changed
    after live feedback that multi-placement was confusing to browse).

    **Two paths, per "Returning-customer profile refresh and ID reuse"**:
    if no Government ID document exists under `application_id` (the
    reuse path -- the customer chose to reuse their existing photo
    instead of uploading a new one), this is a no-op -- the existing
    customer-level copy is left alone, still the customer's current
    photo. If one *does* exist (a fresh upload), the customer's previous
    copy (if any) is trashed first (`DELETE /documents/{id}/`, Mayan's
    own soft-delete -- reversible, same convention this project already
    uses elsewhere) before the new copy is created, so there's still
    never more than one at a time.

    **Phase 24**: the "does a Government ID document exist under this
    application" and "does the customer already have a copy" lookups
    both now read `document/db.py` (Postgres) instead of
    `_documents_matching` (a live Mayan scan) -- `get_application_documents`
    filtered to this category for the source,
    `get_customer_document_by_category` (backed by the real
    `(customer_id, category)` unique index) for the existing copy. The
    Mayan-side trash-then-recreate sequence below is otherwise
    unchanged; the Postgres mirror is written once at the end, via
    `document_db.upsert_customer_document` -- its own `ON CONFLICT
    (customer_id, category) DO UPDATE` is what actually replaces the
    old copy's row in place, so there's no separate Postgres delete
    call needed the way there is on the Mayan side."""
    application_documents = await document_db.get_application_documents(application_id)
    source_record = next(
        (record for record in application_documents if record["category"] == CATEGORY_GOVERNMENT_ID), None
    )
    if source_record is None:
        return

    source = _application_document_ref(source_record)
    content = await mayan_client.download_file(source.document_id)

    # The customer-level copy is identified by the (customer_id,
    # category) unique index on customer_document -- at most one row,
    # by construction (see db/schema.sql's
    # ux_customer_document_customer_category).
    existing_copy = await document_db.get_customer_document_by_category(customer_id, CATEGORY_GOVERNMENT_ID)
    if existing_copy is not None:
        response = await mayan_client.delete(f"/documents/{existing_copy['mayan_document_id']}/")
        response.raise_for_status()

    doc_type_ids, metadata_type_ids = await asyncio.gather(
        mayan_client.document_type_ids(), mayan_client.metadata_type_ids()
    )
    copy_document = await mayan_client.create_document(doc_type_ids[DOCUMENT_TYPE_APPLICATION], source.filename)
    copy_document_id = copy_document["id"]
    await mayan_client.upload_file(copy_document_id, source.filename, content, action_name="replace")

    for field, value in [
        (METADATA_FIELD_APPLICANT_IDENTIFIER, source.applicant_identifier),
        (METADATA_FIELD_CATEGORY, CATEGORY_GOVERNMENT_ID),
        (METADATA_FIELD_CUSTOMER_ID, customer_id),
    ]:
        await mayan_client.attach_metadata(copy_document_id, metadata_type_ids[field], value)

    await mayan_client.rebuild_index()

    await document_db.upsert_customer_document(
        mayan_document_id=copy_document_id,
        customer_id=customer_id,
        applicant_identifier=source.applicant_identifier,
        category=CATEGORY_GOVERNMENT_ID,
        filename=source.filename,
    )


async def has_id_photo(customer_id: str) -> bool:
    """Read-only -- a thin wrapper over `list_customer_documents`, which
    (since the copy-based rewrite of `promote_government_id_to_customer_photo`)
    returns specifically the customer-level Government ID copy, not just
    any document carrying `customer_id` -- a customer can carry
    `customer_id` on plenty of other documents (every document under an
    approved application, `tag_application_documents`) without ever
    having a Government ID copy, so this couldn't just check "any result"
    the way an earlier draft did. Called by
    `application.service.create_application` to decide whether reuse is
    even offerable, and by `bff_customer` to decide whether to show the
    "already on file" choice at all."""
    documents = await list_customer_documents(customer_id)
    return len(documents) > 0


async def generate_welcome_letter(
    applicant_identifier: str, account_id: str, customer_id: str, applicant_name: str, product_type: str, amount: str
) -> DocumentRef:
    """Renders a simple templated PDF and uploads it tagged to
    `account_id` -- system-generated, no human in the loop, exactly one
    per account. Plain-argument signature only, no `application/`/
    `customer/`/`account/` imports (`document/` is a leaf module).

    `applicant_identifier` is required here too, not just `account_id` --
    the index template's account branch (`<applicant_identifier> ->
    <account_id> -> category`, CLAUDE.md's "Document hierarchy") is
    nested *under* the applicant node, whose own expression evaluates
    `applicant_identifier` metadata. Found live: an earlier version of
    this function attached only `account_id`/`category`, which Mayan's
    index-tree gotcha #1 (leaf conditions don't inherit) turned into a
    real, confirmed bug -- the document landed under a top-level "None"
    bucket instead of the applicant's own branch, because the ancestor
    `applicant_identifier` node had nothing to evaluate."""
    content = _render_welcome_letter_pdf(applicant_name, product_type, amount)
    filename = f"welcome_letter_{account_id}.pdf"

    doc_type_ids, metadata_type_ids = await asyncio.gather(
        mayan_client.document_type_ids(), mayan_client.metadata_type_ids()
    )
    document = await mayan_client.create_document(doc_type_ids[DOCUMENT_TYPE_ACCOUNT], filename)
    document_id = document["id"]

    await mayan_client.upload_file(document_id, filename, content, action_name="replace")

    for field, value in [
        (METADATA_FIELD_APPLICANT_IDENTIFIER, applicant_identifier),
        (METADATA_FIELD_ACCOUNT_ID, account_id),
        (METADATA_FIELD_CATEGORY, "Welcome Letter"),
        (METADATA_FIELD_CUSTOMER_ID, customer_id),
    ]:
        await mayan_client.attach_metadata(document_id, metadata_type_ids[field], value)

    await mayan_client.rebuild_index()

    # Postgres mirror (Phase 24), written only after Mayan succeeds.
    # Always the insert branch in practice (exactly one Welcome Letter
    # per account, never re-generated) -- the upsert's ON CONFLICT DO
    # UPDATE path exists for account_document's uniqueness rule
    # generally, not because this call is expected to hit it.
    await document_db.upsert_account_document(
        mayan_document_id=document_id,
        account_id=account_id,
        applicant_identifier=applicant_identifier,
        customer_id=customer_id,
        category="Welcome Letter",
        filename=filename,
    )

    return DocumentRef(
        document_id=document_id,
        filename=filename,
        category="Welcome Letter",
        applicant_identifier=applicant_identifier,
        account_id=account_id,
        customer_id=customer_id,
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


async def upload_consent(
    applicant_identifier: str, account_id: str, customer_id: str, file: UploadedFile
) -> DocumentRef:
    """True Mayan document versioning: if the account already has a
    "consent" document, uploads a new *file version* of that same
    document; creates the document first if none exists yet. Not
    restricted to one caller -- either BFF can call this once
    `account_id` exists.

    `applicant_identifier`/`customer_id` are only used on the
    create-first-version path -- same reason `generate_welcome_letter`
    needs them (the index's account branch is nested under the
    applicant node, see that function's docstring; `customer_id` is
    attached for consistency with Welcome Letter, the other account-level
    document type -- both are account-level docs, so both carry it). The
    new-file-version-of-an-existing-document path doesn't re-attach
    metadata at all, so it needs nothing new.

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
    wrong and has been corrected in place.

    **Phase 24**: the "does a document already exist" check reads
    `document_db.get_account_document_by_category` (Postgres) instead
    of `_documents_matching` (a live Mayan scan). Both branches below
    now converge on one `document_db.upsert_account_document` call at
    the end, written only after Mayan succeeds -- on the re-upload
    branch this is genuinely an update-in-place (same
    `account_document_id`, same `mayan_document_id` since Mayan
    versioned the *existing* document rather than creating a new one,
    new `filename`/`updated_at`); on the create-first-version branch
    it's the insert path. Replaces the old two-different-return-shapes
    design (`dataclasses.replace(...)` on the re-upload branch, a fresh
    `DocumentRef(...)` on the other) with one shared
    `_account_document_ref(...)` conversion of whichever row the upsert
    returns."""
    existing = await document_db.get_account_document_by_category(account_id, CATEGORY_CONSENT)

    if existing is not None:
        document_id = existing["mayan_document_id"]
        await mayan_client.upload_file(document_id, file.filename, file.content, action_name="replace")
    else:
        doc_type_ids, metadata_type_ids = await asyncio.gather(
            mayan_client.document_type_ids(), mayan_client.metadata_type_ids()
        )
        document = await mayan_client.create_document(doc_type_ids[DOCUMENT_TYPE_ACCOUNT], file.filename)
        document_id = document["id"]

        await mayan_client.upload_file(document_id, file.filename, file.content, action_name="replace")

        for field, value in [
            (METADATA_FIELD_APPLICANT_IDENTIFIER, applicant_identifier),
            (METADATA_FIELD_ACCOUNT_ID, account_id),
            (METADATA_FIELD_CATEGORY, CATEGORY_CONSENT),
            (METADATA_FIELD_CUSTOMER_ID, customer_id),
        ]:
            await mayan_client.attach_metadata(document_id, metadata_type_ids[field], value)

        await mayan_client.rebuild_index()

    record = await document_db.upsert_account_document(
        mayan_document_id=document_id,
        account_id=account_id,
        applicant_identifier=applicant_identifier,
        customer_id=customer_id,
        category=CATEGORY_CONSENT,
        filename=file.filename,
    )
    return _account_document_ref(record)


async def list_customer_documents(customer_id: str) -> list[DocumentRef]:
    """The customer-level Government ID copy specifically. **Phase 24**:
    reads the `customer_document` table directly -- table identity
    itself is now the disambiguator (a customer-level copy is a
    `customer_document` row, full stop), replacing the old "carries
    `customer_id` but neither `application_id` nor `account_id`"
    heuristic `_documents_matching` needed when every document type
    shared one flat Mayan metadata search. At most one row at any time
    (`promote_government_id_to_customer_photo`'s `upsert_customer_document`
    call enforces this via the real `(customer_id, category)` unique
    index)."""
    records = await document_db.get_customer_documents(customer_id)
    return [_customer_document_ref(r) for r in records]


async def list_account_documents(account_id: str) -> list[DocumentRef]:
    """Reads `document/db.py` only (Phase 24) -- zero Mayan calls."""
    records = await document_db.get_account_documents(account_id)
    return [_account_document_ref(r) for r in records]
