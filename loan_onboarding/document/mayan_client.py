"""Thin async wrapper around Mayan EDMS's REST API (`/api/v4/`) -- the
ONLY code in this codebase that speaks HTTP to Mayan (`document/` is a
leaf module per CLAUDE.md's module dependency graph, and this is its one
outbound integration).

Holds one shared service-account auth token (obtained lazily, refreshed
on a 401 -- see CLAUDE.md's "Identity") and caches metadata-type /
document-type ids by name for the process lifetime, since those ids
differ per Mayan instance (set up once by
`scripts/setup_document_hierarchy.sh`) and aren't safe to hardcode --
same convention that script's own `json_get` id lookups use.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

METADATA_FIELDS = ("applicant_identifier", "application_id", "account_id", "customer_id", "category")

DOCUMENT_TYPE_APPLICATION = "Application Document"
DOCUMENT_TYPE_ACCOUNT = "Account Document"

INDEX_TEMPLATE_SLUG = "loan-onboarding-archive"


class MayanClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_lock = asyncio.Lock()
        self._metadata_type_ids: dict[str, int] | None = None
        self._document_type_ids: dict[str, int] | None = None
        self._ids_lock = asyncio.Lock()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{os.environ['MAYAN_BASE_URL']}/api/v4",
                timeout=30.0,
                # Do not omit -- without it Mayan's browsable-API HTML
                # renders instead of JSON on some endpoints (CLAUDE.md).
                headers={"Accept": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _obtain_token(self) -> str:
        client = self._get_client()
        response = await client.post(
            "/auth/token/obtain/",
            json={
                "username": os.environ["MAYAN_SERVICE_ACCOUNT_USERNAME"],
                "password": os.environ["MAYAN_SERVICE_ACCOUNT_PASSWORD"],
            },
        )
        response.raise_for_status()
        return response.json()["token"]

    async def _ensure_token(self) -> str:
        if self._token is None:
            async with self._token_lock:
                if self._token is None:
                    self._token = await self._obtain_token()
        return self._token

    # Mayan's REST API rate-limits authenticated callers by default (20
    # req/sec out of the box, `REST_API_THROTTLING_RATE_USER` --
    # confirmed by reading mayan/apps/rest_api/literals.py and hit for
    # real in P5-4/P5-5's own verification: a real customer-shaped burst
    # -- upload several documents in a row, each doing
    # create+upload+3x-attach-metadata+rebuild, then check_completeness's
    # fetch-all-then-per-document-metadata scan -- comfortably exceeds
    # that in under a second at even POC scale). Every 429 carries a
    # standard `Retry-After` header (confirmed empirically); honor it
    # with a small bounded retry rather than letting it surface as an
    # unhandled error on an otherwise-normal upload flow.
    _MAX_429_RETRIES = 5

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self._ensure_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Token {token}"
        client = self._get_client()
        response = await client.request(method, path, headers=headers, **kwargs)
        if response.status_code == 401:
            async with self._token_lock:
                self._token = await self._obtain_token()
            headers["Authorization"] = f"Token {self._token}"
            response = await client.request(method, path, headers=headers, **kwargs)

        attempt = 0
        while response.status_code == 429 and attempt < self._MAX_429_RETRIES:
            delay = float(response.headers.get("Retry-After", 1))
            await asyncio.sleep(delay)
            response = await client.request(method, path, headers=headers, **kwargs)
            attempt += 1
        return response

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("DELETE", path, **kwargs)

    async def stream(self, path: str) -> httpx.Response:
        """Issue a streaming GET -- caller owns the response and must
        close it (e.g. via an async context or explicit `.aclose()`)."""
        token = await self._ensure_token()
        client = self._get_client()
        request = client.build_request("GET", path, headers={"Authorization": f"Token {token}"})
        return await client.send(request, stream=True)

    @staticmethod
    def relative_path(url: str) -> str:
        """Strip the '<mayan_base_url>/api/v4' prefix Mayan includes in
        absolute URLs (pagination 'next', documents_url, image_url, etc.)
        so the path can be re-issued through this client's own base_url."""
        return url.removeprefix(f"{os.environ['MAYAN_BASE_URL']}/api/v4")

    # ------------------------------------------------------------------
    # Id lookups (cached for the process lifetime)
    # ------------------------------------------------------------------

    async def _load_id_map(self, path: str, key: str) -> dict[str, int]:
        ids: dict[str, int] = {}
        next_path: str | None = f"{path}?page_size=100"
        while next_path:
            response = await self.get(self.relative_path(next_path) if next_path.startswith("http") else next_path)
            response.raise_for_status()
            data = response.json()
            for result in data["results"]:
                ids[result[key]] = result["id"]
            next_path = data.get("next")
        return ids

    async def metadata_type_ids(self) -> dict[str, int]:
        if self._metadata_type_ids is None:
            async with self._ids_lock:
                if self._metadata_type_ids is None:
                    self._metadata_type_ids = await self._load_id_map("/metadata_types/", "name")
        return self._metadata_type_ids

    async def document_type_ids(self) -> dict[str, int]:
        if self._document_type_ids is None:
            async with self._ids_lock:
                if self._document_type_ids is None:
                    self._document_type_ids = await self._load_id_map("/document_types/", "label")
        return self._document_type_ids

    async def index_template_id(self) -> int:
        response = await self.get("/index_templates/?page_size=100")
        response.raise_for_status()
        for result in response.json()["results"]:
            if result["slug"] == INDEX_TEMPLATE_SLUG:
                return result["id"]
        raise RuntimeError(f"index template not found: {INDEX_TEMPLATE_SLUG}")

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    async def create_document(self, document_type_id: int, label: str) -> dict[str, Any]:
        response = await self.post(
            "/documents/",
            json={"document_type_id": document_type_id, "label": label},
        )
        response.raise_for_status()
        return response.json()

    async def get_document(self, document_id: int) -> dict[str, Any]:
        response = await self.get(f"/documents/{document_id}/")
        response.raise_for_status()
        return response.json()

    async def list_documents(self, page: int = 1, page_size: int = 100) -> dict[str, Any]:
        response = await self.get(f"/documents/?page={page}&page_size={page_size}")
        response.raise_for_status()
        return response.json()

    async def upload_file(self, document_id: int, filename: str, content: bytes, action_name: str = "replace") -> None:
        """`action_name` is a string ID for a registered
        `DocumentFileAction` backend -- one of `replace` (the default:
        the new version's rendered pages are just the new file's pages),
        `append`, or `keep`. There is no `new` action -- confirmed
        against Mayan's own `document_file_actions.py` and empirically
        against a live instance (P5-5's session note): a "new version"
        of an *existing* document is created by POSTing here again with
        the *same* `document_id` and `action_name="replace"`, not by a
        different action name (see gotcha #3: an invalid string fails
        silently, HTTP 200 with a broken async version-creation task)."""
        response = await self.post(
            f"/documents/{document_id}/files/",
            data={"action_name": action_name},
            files={"file_new": (filename, content)},
        )
        response.raise_for_status()

    async def get_document_metadata(self, document_id: int) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        path: str | None = f"/documents/{document_id}/metadata/?page_size=100"
        while path:
            response = await self.get(self.relative_path(path) if path.startswith("http") else path)
            response.raise_for_status()
            data = response.json()
            entries.extend(data["results"])
            path = data.get("next")
        return entries

    async def attach_metadata(self, document_id: int, metadata_type_id: int, value: str) -> None:
        response = await self.post(
            f"/documents/{document_id}/metadata/",
            json={"metadata_type_id": metadata_type_id, "value": value},
        )
        response.raise_for_status()

    async def update_metadata_entry(self, document_id: int, metadata_entry_id: int, value: str) -> None:
        response = await self.patch(
            f"/documents/{document_id}/metadata/{metadata_entry_id}/",
            json={"value": value},
        )
        response.raise_for_status()

    async def delete_metadata_entry(self, document_id: int, metadata_entry_id: int) -> None:
        """A plain wrapper over the already-generic `self.delete(...)` --
        mirrors `attach_metadata`/`update_metadata_entry`'s shape. Never
        needed until Phase 14's `promote_government_id_to_customer_photo`
        rewrite, which has to strip a customer's old `id_photo` document's
        `customer_id` metadata entry before tagging a fresh one (Mayan
        holds exactly one value per (document, metadata_type) -- see
        CLAUDE.md's "Returning-customer profile refresh and ID reuse")."""
        response = await self.delete(f"/documents/{document_id}/metadata/{metadata_entry_id}/")
        response.raise_for_status()

    async def rebuild_index(self) -> None:
        index_id = await self.index_template_id()
        response = await self.post(f"/index_templates/{index_id}/rebuild/")
        response.raise_for_status()

    async def search_documents(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """Exact-match search over Mayan's advanced-search endpoint.

        NEVER used for metadata AND-filtering (e.g. "application_id=X AND
        category=Y") -- verified against mayan-edms-customer-archive that
        combining metadata__metadata_type__name/metadata__value params
        does not AND against the same metadata row, it matches broadly
        across all documents' metadata instead. Callers needing an exact
        multi-field metadata match must fetch candidates and filter in
        Python against `get_document_metadata()` results (see
        `document/service.py`'s `check_completeness`/`list_documents`),
        never rely on this search endpoint for that. This method exists
        only for the single-field lookups that don't have this problem
        (e.g. a plain label search)."""
        response = await self.get("/search/documents.documentsearchresult/", params=params)
        response.raise_for_status()
        return response.json()["results"]


mayan_client = MayanClient()
