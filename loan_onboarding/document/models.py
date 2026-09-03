"""Plain data shapes for `document/`'s public API. No Postgres model
here -- this module has no table of its own (CLAUDE.md's "Document
module": Mayan's own Postgres/Redis is the only persistence behind it)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable


@dataclass(frozen=True)
class UploadedFile:
    """What a caller (a BFF route) hands `service.upload()`/
    `service.upload_consent()` -- deliberately not a FastAPI `UploadFile`
    or any web-framework type, since `document/` doesn't depend on one."""

    filename: str
    content: bytes


@dataclass(frozen=True)
class DocumentRef:
    document_id: int
    filename: str
    category: str | None
    applicant_identifier: str | None = None
    application_id: str | None = None
    account_id: str | None = None
    customer_id: str | None = None
    creation_date: str | None = None

    @staticmethod
    def from_mayan(document: dict, metadata: dict[str, str]) -> "DocumentRef":
        return DocumentRef(
            document_id=document["id"],
            filename=document["label"],
            category=metadata.get("category"),
            applicant_identifier=metadata.get("applicant_identifier"),
            application_id=metadata.get("application_id"),
            account_id=metadata.get("account_id"),
            customer_id=metadata.get("customer_id"),
            creation_date=metadata.get("creation_date"),
        )


@dataclass(frozen=True)
class DocumentStream:
    """A caller (a BFF route) reads `content_type`/`filename` for
    response headers and iterates `aiter_bytes()`; must always call
    `aclose()` when done (success or error) to release the underlying
    HTTP connection -- same contract as `httpx.Response(stream=True)`,
    which this wraps."""

    filename: str
    content_type: str
    aiter_bytes: Callable[[], AsyncIterator[bytes]]
    aclose: Callable[[], Awaitable[None]]
