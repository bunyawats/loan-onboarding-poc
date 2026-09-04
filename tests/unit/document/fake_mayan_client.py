"""An in-memory double for MayanClient's public surface -- used by
document/service.py's own unit tests to mock at the module boundary
(CLAUDE.md's "mock at the boundary" convention, applied here to
document/service.py's one dependency edge rather than to callers of
document.service). No real HTTP, no live Mayan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class _StoredDocument:
    document_id: int
    document_type_label: str
    filename: str
    metadata: dict[str, str] = field(default_factory=dict)
    file_versions: list[bytes] = field(default_factory=list)


class FakeMayanClient:
    def __init__(self) -> None:
        self._next_id = 1
        self.documents: dict[int, _StoredDocument] = {}
        self.rebuild_count = 0
        self._document_type_ids = {"Application Document": 1, "Account Document": 2}
        self._metadata_type_ids = {
            "applicant_identifier": 1,
            "application_id": 2,
            "account_id": 3,
            "customer_id": 4,
            "category": 5,
        }
        self._metadata_id_to_name = {v: k for k, v in self._metadata_type_ids.items()}

    async def document_type_ids(self) -> dict[str, int]:
        return dict(self._document_type_ids)

    async def metadata_type_ids(self) -> dict[str, int]:
        return dict(self._metadata_type_ids)

    async def create_document(self, document_type_id: int, label: str) -> dict[str, Any]:
        label_by_id = {v: k for k, v in self._document_type_ids.items()}
        document_id = self._next_id
        self._next_id += 1
        self.documents[document_id] = _StoredDocument(
            document_id=document_id, document_type_label=label_by_id[document_type_id], filename=label
        )
        return {"id": document_id, "label": label}

    async def upload_file(self, document_id: int, filename: str, content: bytes, action_name: str = "replace") -> None:
        # Real Mayan always creates a new DocumentFile/DocumentVersion on
        # every POST to /documents/<id>/files/, regardless of
        # action_name -- confirmed empirically in P5-5 (there is no
        # "new" action; "replace" is used both for a document's first
        # upload and for every later version).
        doc = self.documents[document_id]
        doc.file_versions.append(content)
        doc.filename = filename

    async def attach_metadata(self, document_id: int, metadata_type_id: int, value: str) -> None:
        # Real Mayan rejects a second POST for a metadata_type the
        # document already carries with a 400 (confirmed live in
        # P16-4) -- replicated here so a caller that double-attaches
        # the same field to the same document (a real bug caught only
        # by live verification, since this fake previously allowed it
        # silently) fails in unit tests too, not just against a real
        # instance.
        name = self._metadata_id_to_name[metadata_type_id]
        if name in self.documents[document_id].metadata:
            request = httpx.Request("POST", f"http://fake/documents/{document_id}/metadata/")
            response = httpx.Response(400, request=request)
            raise httpx.HTTPStatusError("metadata type already attached", request=request, response=response)
        self.documents[document_id].metadata[name] = value

    async def update_metadata_entry(self, document_id: int, metadata_entry_id: int, value: str) -> None:
        name = self._metadata_id_to_name[metadata_entry_id]
        self.documents[document_id].metadata[name] = value

    async def get_document_metadata(self, document_id: int) -> list[dict[str, Any]]:
        doc = self.documents[document_id]
        # "id" here is the metadata *entry*'s id -- real Mayan holds
        # exactly one value per (document, metadata_type), so the
        # metadata_type_id doubles as a stable per-(document, field)
        # entry id in this fake, same as it would functionally behave
        # against a real instance.
        return [
            {"id": self._metadata_type_ids[name], "metadata_type": {"name": name}, "value": value}
            for name, value in doc.metadata.items()
        ]

    async def delete_metadata_entry(self, document_id: int, metadata_entry_id: int) -> None:
        name = self._metadata_id_to_name[metadata_entry_id]
        self.documents[document_id].metadata.pop(name, None)

    async def get_document(self, document_id: int) -> dict[str, Any]:
        doc = self.documents[document_id]
        file_latest = {"id": 900 + document_id} if doc.file_versions else {}
        return {"id": document_id, "label": doc.filename, "file_latest": file_latest}

    async def list_documents(self, page: int = 1, page_size: int = 100) -> dict[str, Any]:
        results = [{"id": d.document_id, "label": d.filename} for d in self.documents.values()]
        return {"results": results, "next": None}

    async def rebuild_index(self) -> None:
        self.rebuild_count += 1

    async def stream(self, path: str):
        return _FakeStreamResponse(content=b"%PDF-1.4 fake bytes", content_type="application/pdf")


class _FakeStreamResponse:
    def __init__(self, content: bytes, content_type: str) -> None:
        self._content = content
        self.headers = {"content-type": content_type}
        self.closed = False

    async def aiter_bytes(self):
        yield self._content

    async def aclose(self) -> None:
        self.closed = True
