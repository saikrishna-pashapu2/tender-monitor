from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tender_monitor.api.app import app
from tender_monitor.api.deps import get_session


@pytest_asyncio.fixture(loop_scope="function")
async def override_session(
    test_database_url: str,
) -> AsyncIterator[None]:
    """Point the FastAPI ``get_session`` dependency at the test DB.

    The TestClient drives requests through the app's async stack, but
    the dependency hard-wires to the production engine — override it
    so each request opens a fresh session against the test database.
    """
    engine = create_async_engine(test_database_url, future=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _get_session
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.fixture
def client(override_session: None, seeded_session: AsyncSession) -> Iterator[TestClient]:
    # ``seeded_session`` ensures the DB is populated before the client
    # is used; ``override_session`` re-routes the API to the same DB.
    with TestClient(app) as client:
        yield client


def test_list_endpoint_returns_200_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<html" in resp.text.lower()
    assert "Tender Monitor" in resp.text


def test_list_endpoint_htmx_returns_partial(client: TestClient) -> None:
    resp = client.get("/", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    assert "<html" not in body.lower()
    assert "<header" not in body.lower()
    # Partial still emits the results header + cards / empty state.
    assert "tenders" in body.lower()


def test_list_endpoint_applies_filters_from_query(client: TestClient) -> None:
    resp = client.get("/?country=UZ")
    assert resp.status_code == 200
    body = resp.text
    # UZ buyers should appear, KZ-only ones shouldn't.
    assert "Uzbekistan Railways" in body
    assert "Halyk Bank" not in body


def test_list_endpoint_filter_matched_any(client: TestClient) -> None:
    resp = client.get("/?matched=any", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    # "Office supplies" is an unmatched seed row; should be filtered out.
    assert "Office supplies" not in resp.text


def test_detail_endpoint_returns_404_for_unknown_id(client: TestClient) -> None:
    resp = client.get(f"/tenders/{uuid4()}")
    assert resp.status_code == 404


def test_detail_endpoint_returns_200_for_existing(client: TestClient) -> None:
    # Pick the deterministic credit-rating tender for the snapshot demo.
    tender_id = _credit_rating_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "Open at source" in resp.text
    assert "Why this matched" in resp.text
    assert "Source details" in resp.text
    assert "Raw source payload" in resp.text


def test_detail_endpoint_renders_related_sidebar(client: TestClient) -> None:
    # The seeded credit-rating tender lives on the goszakup source,
    # which has 6 total rows. The "More from goszakup" sidebar should
    # render with the other 5.
    tender_id = _credit_rating_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "More from goszakup" in resp.text


def test_detail_endpoint_renders_documents_section(client: TestClient) -> None:
    tender_id = _xt_xarid_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "Documents (1)" in resp.text
    assert "climate-strategy.pdf" in resp.text
    assert "Open file" in resp.text


def test_detail_endpoint_includes_unmatched_in_sidebar(client: TestClient) -> None:
    # ``g-5`` (Office supplies) is the unmatched goszakup row. It must
    # NOT show on the home list (default matched-only) but MUST appear
    # in the related sidebar of any other goszakup tender's detail page.
    tender_id = _credit_rating_tender_id(client)
    home = client.get("/?matched=any", headers={"HX-Request": "true"})
    assert "Office supplies" not in home.text
    detail = client.get(f"/tenders/{tender_id}")
    assert "Office supplies" in detail.text


def test_api_tenders_returns_json(client: TestClient) -> None:
    resp = client.get("/api/tenders")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")


def test_api_tenders_response_shape(client: TestClient) -> None:
    # ``matched=all`` opts out of the default matched-only filter so
    # this test still asserts the full seeded corpus.
    resp = client.get("/api/tenders?per_page=5&matched=all")
    body = resp.json()
    assert set(body.keys()) >= {"tenders", "total", "page", "per_page", "pages"}
    assert body["total"] == 12
    assert body["per_page"] == 5
    assert body["pages"] == 3
    assert len(body["tenders"]) == 5
    sample = body["tenders"][0]
    assert {"id", "source_name", "external_id", "title", "country"} <= set(sample.keys())


def test_api_tenders_default_returns_matched_only(client: TestClient) -> None:
    # Without an explicit ``matched`` param the API hides unmatched
    # tenders. 6 of 12 seeded rows have matched_groups.
    resp = client.get("/api/tenders?per_page=100")
    body = resp.json()
    assert body["total"] == 6
    for entry in body["tenders"]:
        assert entry["matched_groups"], (
            "default API view should only return matched tenders, "
            f"got {entry['external_id']} with no matched_groups"
        )


def test_list_endpoint_default_hides_unmatched(client: TestClient) -> None:
    # The unmatched seed row ``g-5`` (Office supplies) must NOT appear
    # in the default home view.
    resp = client.get("/", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Office supplies" not in resp.text


def test_list_endpoint_matched_all_shows_unmatched(client: TestClient) -> None:
    # Explicit opt-out brings unmatched rows back.
    resp = client.get("/?matched=all", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Office supplies" in resp.text


def test_api_tender_detail_returns_tender_read_shape(client: TestClient) -> None:
    tender_id = _credit_rating_tender_id(client)
    resp = client.get(f"/api/tenders/{tender_id}")
    assert resp.status_code == 200
    body = resp.json()
    # TenderRead carries the full row including raw_json + change_log.
    assert body["id"] == str(tender_id)
    assert "raw_json" in body
    assert "change_log" in body
    assert "matched_groups" in body
    assert "credit_rating" in body["matched_groups"]


def test_api_sources_returns_list(client: TestClient) -> None:
    resp = client.get("/api/sources")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    names = {entry["name"] for entry in body}
    assert names == {"goszakup", "xt-xarid"}


def test_openapi_renders(client: TestClient) -> None:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "/api/tenders" in schema["paths"]


def _credit_rating_tender_id(client: TestClient) -> str:
    """Look up the seeded ``g-1`` tender's UUID through the JSON API.

    The seeded_session fixture is bound to its own event loop, so we
    can't reuse it from these sync tests. Going through the API
    exercises the same path the UI will use anyway.
    """
    resp = client.get(
        "/api/tenders?source=goszakup&group=credit_rating&per_page=100"
    )
    resp.raise_for_status()
    for entry in resp.json()["tenders"]:
        if entry["external_id"] == "g-1":
            return entry["id"]
    raise AssertionError("seeded credit-rating tender g-1 was not returned")


def _xt_xarid_tender_id(client: TestClient) -> str:
    resp = client.get("/api/tenders?source=xt-xarid&matched=all&per_page=100")
    resp.raise_for_status()
    for entry in resp.json()["tenders"]:
        if entry["external_id"] == "x-1":
            return entry["id"]
    raise AssertionError("seeded xt-xarid tender x-1 was not returned")
