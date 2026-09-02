"""document/mayan_client.py's tests mock Mayan's HTTP API at the
transport layer with respx -- this is the leaf-module boundary itself
(no live Mayan needed), unlike customer/account's db.py tests which
deliberately hit a real database (see CLAUDE.md's Testing section)."""

import httpx
import pytest
import respx

from loan_onboarding.document.mayan_client import MayanClient

BASE = "http://mayan.test"


@pytest.fixture(autouse=True)
def _mayan_env(monkeypatch):
    monkeypatch.setenv("MAYAN_BASE_URL", BASE)
    monkeypatch.setenv("MAYAN_SERVICE_ACCOUNT_USERNAME", "admin")
    monkeypatch.setenv("MAYAN_SERVICE_ACCOUNT_PASSWORD", "changeme")


@pytest.fixture
def client():
    return MayanClient()


@respx.mock
async def test_token_obtained_lazily_on_first_request(client):
    token_route = respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    doc_route = respx.get(f"{BASE}/api/v4/documents/1/").mock(
        return_value=httpx.Response(200, json={"id": 1, "label": "x"})
    )

    assert token_route.call_count == 0
    result = await client.get_document(1)

    assert result == {"id": 1, "label": "x"}
    assert token_route.call_count == 1
    assert doc_route.calls.last.request.headers["Authorization"] == "Token abc123"

    # A second call reuses the cached token -- no second /auth/ call.
    await client.get_document(1)
    assert token_route.call_count == 1


@respx.mock
async def test_token_refreshed_on_401(client):
    token_route = respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        side_effect=[
            httpx.Response(200, json={"token": "stale"}),
            httpx.Response(200, json={"token": "fresh"}),
        ]
    )
    doc_route = respx.get(f"{BASE}/api/v4/documents/1/").mock(
        side_effect=[
            httpx.Response(401, json={"detail": "expired"}),
            httpx.Response(200, json={"id": 1}),
        ]
    )

    result = await client.get_document(1)

    assert result == {"id": 1}
    assert token_route.call_count == 2
    assert doc_route.call_count == 2
    assert doc_route.calls.last.request.headers["Authorization"] == "Token fresh"


@respx.mock
async def test_metadata_type_ids_cached_for_process_lifetime(client):
    respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    list_route = respx.get(f"{BASE}/api/v4/metadata_types/?page_size=100").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 1, "name": "applicant_identifier"},
                    {"id": 2, "name": "category"},
                ],
                "next": None,
            },
        )
    )

    ids = await client.metadata_type_ids()
    assert ids == {"applicant_identifier": 1, "category": 2}
    assert list_route.call_count == 1

    ids_again = await client.metadata_type_ids()
    assert ids_again == ids
    assert list_route.call_count == 1


@respx.mock
async def test_metadata_type_ids_follows_pagination(client):
    respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    respx.get(f"{BASE}/api/v4/metadata_types/?page_size=100").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": 1, "name": "applicant_identifier"}],
                "next": f"{BASE}/api/v4/metadata_types/?page=2&page_size=100",
            },
        )
    )
    respx.get(f"{BASE}/api/v4/metadata_types/?page=2&page_size=100").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": 2, "name": "category"}], "next": None},
        )
    )

    ids = await client.metadata_type_ids()
    assert ids == {"applicant_identifier": 1, "category": 2}


@respx.mock
async def test_index_template_id_found_by_slug(client):
    respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    respx.get(f"{BASE}/api/v4/index_templates/?page_size=100").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": 7, "slug": "loan-onboarding-archive"}]},
        )
    )

    assert await client.index_template_id() == 7


@respx.mock
async def test_index_template_id_raises_if_not_found(client):
    respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    respx.get(f"{BASE}/api/v4/index_templates/?page_size=100").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1, "slug": "something-else"}]})
    )

    with pytest.raises(RuntimeError, match="loan-onboarding-archive"):
        await client.index_template_id()


@respx.mock
async def test_upload_file_sends_action_name_and_multipart_file(client):
    respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    upload_route = respx.post(f"{BASE}/api/v4/documents/5/files/").mock(
        return_value=httpx.Response(201, json={})
    )

    await client.upload_file(5, "gov_id.pdf", b"%PDF-1.4 fake", action_name="replace")

    request = upload_route.calls.last.request
    body = request.content.decode(errors="ignore")
    assert 'name="action_name"' in body
    assert "replace" in body
    assert 'filename="gov_id.pdf"' in body


@respx.mock
async def test_upload_file_default_action_name_is_replace(client):
    respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    upload_route = respx.post(f"{BASE}/api/v4/documents/5/files/").mock(
        return_value=httpx.Response(201, json={})
    )

    await client.upload_file(5, "welcome.pdf", b"%PDF-1.4 fake")

    body = upload_route.calls.last.request.content.decode(errors="ignore")
    assert "replace" in body


def test_relative_path_strips_base_prefix(monkeypatch):
    monkeypatch.setenv("MAYAN_BASE_URL", BASE)
    url = f"{BASE}/api/v4/documents/1/metadata/?page=2"
    assert MayanClient.relative_path(url) == "/documents/1/metadata/?page=2"
