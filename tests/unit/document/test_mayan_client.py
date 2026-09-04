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
async def test_429_retried_honoring_retry_after_header(client, monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    doc_route = respx.get(f"{BASE}/api/v4/documents/1/").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"id": 1}),
        ]
    )

    result = await client.get_document(1)

    assert result == {"id": 1}
    assert doc_route.call_count == 2
    assert sleeps == [2.0]


@respx.mock
async def test_429_gives_up_after_max_retries(client, monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    doc_route = respx.get(f"{BASE}/api/v4/documents/1/").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.get_document(1)

    assert doc_route.call_count == 1 + MayanClient._MAX_429_RETRIES


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
async def test_index_template_ids_found_by_slug_in_declared_order(client):
    respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    respx.get(f"{BASE}/api/v4/index_templates/?page_size=100").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 1, "slug": "creation_date"},
                    {"id": 5, "slug": "application-index"},
                    {"id": 3, "slug": "customer-index"},
                    {"id": 4, "slug": "account-index"},
                ]
            },
        )
    )

    # Order matches mayan_client.INDEX_TEMPLATE_SLUGS
    # (customer-index, account-index, application-index), not the API
    # response's own ordering.
    assert await client.index_template_ids() == [3, 4, 5]


@respx.mock
async def test_index_template_ids_raises_if_any_slug_not_found(client):
    respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    respx.get(f"{BASE}/api/v4/index_templates/?page_size=100").mock(
        return_value=httpx.Response(
            200, json={"results": [{"id": 3, "slug": "customer-index"}, {"id": 4, "slug": "account-index"}]}
        )
    )

    with pytest.raises(RuntimeError, match="application-index"):
        await client.index_template_ids()


@respx.mock
async def test_rebuild_index_rebuilds_all_three_index_templates(client):
    respx.post(f"{BASE}/api/v4/auth/token/obtain/").mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )
    respx.get(f"{BASE}/api/v4/index_templates/?page_size=100").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 3, "slug": "customer-index"},
                    {"id": 4, "slug": "account-index"},
                    {"id": 5, "slug": "application-index"},
                ]
            },
        )
    )
    rebuild_routes = [
        respx.post(f"{BASE}/api/v4/index_templates/{index_id}/rebuild/").mock(
            return_value=httpx.Response(200, json={})
        )
        for index_id in (3, 4, 5)
    ]

    await client.rebuild_index()

    for route in rebuild_routes:
        assert route.called


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
