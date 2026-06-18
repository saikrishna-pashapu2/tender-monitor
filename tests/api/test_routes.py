# ruff: noqa: RUF001
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
from tender_monitor.api.routes.web import get_share_email_sender
from tender_monitor.notifications.email import EmailMessageContent


class RouteRecordingSender:
    def __init__(self, *, fail: bool = False, fail_for: set[str] | None = None) -> None:
        self.fail = fail
        self.fail_for = fail_for or set()
        self.sent: list[tuple[str, EmailMessageContent]] = []

    async def send(self, *, to: str, message: EmailMessageContent) -> None:
        if self.fail or to in self.fail_for:
            raise RuntimeError("forced route failure")
        self.sent.append((to, message))


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
    assert "Просмотр объявления" in resp.text
    assert "Общие сведения" in resp.text
    assert "Raw source payload" in resp.text


def test_detail_endpoint_renders_share_button_and_modal(client: TestClient) -> None:
    tender_id = _credit_rating_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert 'data-share-open' in resp.text
    assert 'id="share-modal"' in resp.text
    assert f'action="/tenders/{tender_id}/share"' in resp.text
    assert 'name="sender_name"' in resp.text
    assert 'name="recipients"' in resp.text
    assert "Share" in resp.text


def test_share_tender_requires_sender_name(client: TestClient) -> None:
    tender_id = _credit_rating_tender_id(client)
    resp = client.post(
        f"/tenders/{tender_id}/share",
        data={"recipients": "analyst@example.com"},
    )
    assert resp.status_code == 400
    assert "Enter your name." in resp.text
    assert 'role="dialog"' in resp.text
    assert 'id="share-sender-name"' in resp.text


def test_share_tender_requires_recipient(client: TestClient) -> None:
    tender_id = _credit_rating_tender_id(client)
    resp = client.post(
        f"/tenders/{tender_id}/share",
        data={"sender_name": "Sai"},
    )
    assert resp.status_code == 400
    assert "Add at least one recipient email address." in resp.text
    assert 'id="share-recipient-input"' in resp.text


def test_share_tender_rejects_invalid_recipient_without_sending(
    client: TestClient,
) -> None:
    tender_id = _credit_rating_tender_id(client)
    sender = RouteRecordingSender()
    app.dependency_overrides[get_share_email_sender] = lambda: sender
    try:
        resp = client.post(
            f"/tenders/{tender_id}/share",
            data={"sender_name": "Sai", "recipients": "not-email"},
        )
    finally:
        app.dependency_overrides.pop(get_share_email_sender, None)

    assert resp.status_code == 400
    assert "Remove invalid recipient email address(es): not-email." in resp.text
    assert sender.sent == []


def test_share_tender_returns_404_for_unknown_tender(client: TestClient) -> None:
    resp = client.post(
        f"/tenders/{uuid4()}/share",
        data={"sender_name": "Sai", "recipients": "analyst@example.com"},
    )
    assert resp.status_code == 404


def test_share_tender_sends_multiple_deduped_recipients(
    client: TestClient,
) -> None:
    tender_id = _credit_rating_tender_id(client)
    sender = RouteRecordingSender()
    app.dependency_overrides[get_share_email_sender] = lambda: sender
    try:
        resp = client.post(
            f"/tenders/{tender_id}/share",
            data={
                "sender_name": "Sai Kumar",
                "recipients": [
                    "Analyst@Example.Com",
                    "analyst@example.com",
                    "manager@example.com",
                ],
                "message": "Worth reviewing before the deadline.",
            },
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_share_email_sender, None)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/tenders/{tender_id}?share=sent"
    assert [entry[0] for entry in sender.sent] == [
        "analyst@example.com",
        "manager@example.com",
    ]
    assert "Sai Kumar shared a tender:" in sender.sent[0][1].subject
    assert "Worth reviewing before the deadline." in sender.sent[0][1].text

    follow = client.get(resp.headers["location"])
    assert follow.status_code == 200
    assert 'data-share-success="true"' in follow.text
    assert "Tender sent" in follow.text
    assert "The tender has been shared by email." in follow.text
    assert "Tender shared by email." not in follow.text
    assert 'id="share-sender-name"' not in follow.text
    assert 'id="share-recipient-input"' not in follow.text
    assert 'id="share-message"' not in follow.text
    assert 'data-lucide="send"' not in follow.text


def test_share_tender_renders_partial_smtp_failure(client: TestClient) -> None:
    tender_id = _credit_rating_tender_id(client)
    sender = RouteRecordingSender(fail_for={"broken@example.com"})
    app.dependency_overrides[get_share_email_sender] = lambda: sender
    try:
        resp = client.post(
            f"/tenders/{tender_id}/share",
            data={
                "sender_name": "Sai",
                "recipients": ["broken@example.com", "works@example.com"],
            },
        )
    finally:
        app.dependency_overrides.pop(get_share_email_sender, None)

    assert resp.status_code == 502
    assert "Shared with 1 recipient(s), but failed for broken@example.com." in resp.text
    assert sender.sent[0][0] == "works@example.com"
    assert 'id="share-sender-name"' in resp.text
    assert 'id="share-recipient-input"' in resp.text


def test_share_contacts_returns_contacts_for_normalized_sender(
    client: TestClient,
) -> None:
    tender_id = _credit_rating_tender_id(client)
    sender = RouteRecordingSender()
    app.dependency_overrides[get_share_email_sender] = lambda: sender
    try:
        resp = client.post(
            f"/tenders/{tender_id}/share",
            data={
                "sender_name": " Sai   Kumar ",
                "recipients": ["analyst@example.com", "manager@example.com"],
            },
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.pop(get_share_email_sender, None)
    assert resp.status_code == 303

    contacts = client.get("/share/contacts?sender_name=sai%20kumar")
    assert contacts.status_code == 200
    assert set(contacts.json()["contacts"]) == {
        "analyst@example.com",
        "manager@example.com",
    }

    other = client.get("/share/contacts?sender_name=Someone%20Else")
    assert other.status_code == 200
    assert other.json()["contacts"] == []


def test_detail_endpoint_renders_goszakup_source_layout(client: TestClient) -> None:
    tender_id = _credit_rating_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "Портал государственных закупок" in resp.text
    assert "Выбранный лот" in resp.text
    assert "Документация" in resp.text
    assert "Техническая спецификация" in resp.text


def test_detail_endpoint_renders_mitwork_source_layout(client: TestClient) -> None:
    tender_id = _mitwork_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "Eurasian Electronic Portal" in resp.text
    assert "Сведения о закупке" in resp.text
    assert "Лоты" in resp.text
    assert "contract_project_s_2026_193447_v1.pdf" in resp.text
    assert "Consulting services for assessment/analysis of activities" in resp.text


def test_detail_endpoint_renders_national_bank_source_layout(
    client: TestClient,
) -> None:
    tender_id = _national_bank_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "National Bank of Kazakhstan Procurement Portal" in resp.text
    assert "Информация о лоте" in resp.text
    assert "Место поставки" in resp.text
    assert "ПД_ ТР Сатпаева.docx" in resp.text
    assert "dinara.beisbayeva@nationalbank.kz" in resp.text


def test_detail_endpoint_renders_zakup_unified_source_layout(
    client: TestClient,
) -> None:
    tender_id = _zakup_unified_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "Unified Procurement Portal of Kazakhstan" in resp.text
    assert "Объявление № 39385974" in resp.text
    assert "Поставка цемента марки М500" in resp.text
    assert "Изделия из бетона" in resp.text
    assert "г. Алматы, склад №3" in resp.text


def test_detail_endpoint_renders_samruk_kazyna_source_layout(
    client: TestClient,
) -> None:
    tender_id = _samruk_kazyna_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "Samruk-Kazyna Electronic Procurement" in resp.text
    assert "Объявление № 1220290" in resp.text
    assert "Работы по капитальному ремонту сетей электроснабжения" in resp.text
    assert "Тендерная_документация_1198864_2026-04-03.pdf" in resp.text
    assert "luteuliyeva@azhk.kz" in resp.text


def test_detail_endpoint_renders_ets_tender_source_layout(
    client: TestClient,
) -> None:
    tender_id = _ets_tender_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "ETS-Tender Commercial Procurement" in resp.text
    assert "Запрос предложений № 2085996" in resp.text
    assert "241031.900.000011" in resp.text
    assert "Безналичный расчёт" in resp.text
    assert "Техническая спецификация" in resp.text


def test_detail_endpoint_renders_xt_xarid_source_layout(
    client: TestClient,
) -> None:
    tender_id = _xt_xarid_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "XT-Xarid Public Procurement" in resp.text
    assert "Тендер № x-1" in resp.text
    assert "Documentation objections" in resp.text
    assert "74.90.13.000-00001" in resp.text
    assert "Uzbekistan Railways" in resp.text
    assert "Documents (1)" in resp.text
    assert "climate-strategy.pdf" in resp.text


def test_detail_endpoint_renders_tendersinfo_source_layout(
    client: TestClient,
) -> None:
    tender_id = _tendersinfo_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "TendersInfo Commercial Aggregator" in resp.text
    assert "Notice 532912293" in resp.text
    assert "Environment And Pollution" in resp.text
    assert "United Nations Development Programme" in resp.text
    assert "Aggregator payload" in resp.text


def test_detail_endpoint_renders_uzbekistan_tenders_source_layout(
    client: TestClient,
) -> None:
    tender_id = _uzbekistan_tenders_tender_id(client)
    resp = client.get(f"/tenders/{tender_id}")
    assert resp.status_code == 200
    assert "UzbekistanTenders Commercial Aggregator" in resp.text
    assert "UZT Ref No. 86ddab7" in resp.text
    assert "Finance and Related Services" in resp.text
    assert "Central Bank of the Republic of Uzbekistan" in resp.text
    assert "Provision of services for assigning an international credit rating" in resp.text
    assert "JSON-LD detail" in resp.text


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
    assert body["total"] == 19
    assert body["per_page"] == 5
    assert body["pages"] == 4
    assert len(body["tenders"]) == 5
    sample = body["tenders"][0]
    assert {"id", "source_name", "external_id", "title", "country"} <= set(sample.keys())


def test_api_tenders_default_returns_matched_only(client: TestClient) -> None:
    # Without an explicit ``matched`` param the API hides unmatched
    # tenders. 8 of 19 seeded rows have matched_groups.
    resp = client.get("/api/tenders?per_page=100")
    body = resp.json()
    assert body["total"] == 8
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


def test_list_endpoint_exposes_hidden_dev_all_tenders_option(
    client: TestClient,
) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert (
        '<details class="group border-t border-slate-200 '
        'pt-4 text-sm text-slate-500">'
    ) in resp.text
    assert 'name="matched"' in resp.text
    assert "Developer" in resp.text
    assert "All tenders" in resp.text


def test_list_endpoint_marks_dev_all_tenders_option_active(
    client: TestClient,
) -> None:
    resp = client.get("/?matched=all")
    assert resp.status_code == 200
    assert (
        '<details class="group border-t border-slate-200 '
        'pt-4 text-sm text-slate-500" open>'
    ) in resp.text
    assert '<option value="all" selected>All tenders</option>' in resp.text


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
    assert names == {
        "ets_tender",
        "goszakup",
        "mitwork",
        "national_bank",
        "samruk_kazyna",
        "tendersinfo",
        "uzbekistan_tenders",
        "xt_xarid",
        "zakup_unified",
    }


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
    resp = client.get("/api/tenders?source=xt_xarid&matched=all&per_page=100")
    resp.raise_for_status()
    for entry in resp.json()["tenders"]:
        if entry["external_id"] == "x-1":
            return entry["id"]
    raise AssertionError("seeded xt_xarid tender x-1 was not returned")


def _mitwork_tender_id(client: TestClient) -> str:
    resp = client.get("/api/tenders?source=mitwork&matched=all&per_page=100")
    resp.raise_for_status()
    for entry in resp.json()["tenders"]:
        if entry["external_id"] == "194361":
            return entry["id"]
    raise AssertionError("seeded mitwork tender 194361 was not returned")


def _national_bank_tender_id(client: TestClient) -> str:
    resp = client.get("/api/tenders?source=national_bank&matched=all&per_page=100")
    resp.raise_for_status()
    for entry in resp.json()["tenders"]:
        if entry["external_id"] == "228344":
            return entry["id"]
    raise AssertionError("seeded national_bank tender 228344 was not returned")


def _zakup_unified_tender_id(client: TestClient) -> str:
    resp = client.get("/api/tenders?source=zakup_unified&matched=all&per_page=100")
    resp.raise_for_status()
    for entry in resp.json()["tenders"]:
        if entry["external_id"] == "39385974":
            return entry["id"]
    raise AssertionError("seeded zakup_unified tender 39385974 was not returned")


def _samruk_kazyna_tender_id(client: TestClient) -> str:
    resp = client.get("/api/tenders?source=samruk_kazyna&matched=all&per_page=100")
    resp.raise_for_status()
    for entry in resp.json()["tenders"]:
        if entry["external_id"] == "1220290":
            return entry["id"]
    raise AssertionError("seeded samruk_kazyna tender 1220290 was not returned")


def _ets_tender_tender_id(client: TestClient) -> str:
    resp = client.get("/api/tenders?source=ets_tender&matched=all&per_page=100")
    resp.raise_for_status()
    for entry in resp.json()["tenders"]:
        if entry["external_id"] == "2085996":
            return entry["id"]
    raise AssertionError("seeded ets_tender tender 2085996 was not returned")


def _tendersinfo_tender_id(client: TestClient) -> str:
    resp = client.get("/api/tenders?source=tendersinfo&matched=all&per_page=100")
    resp.raise_for_status()
    for entry in resp.json()["tenders"]:
        if entry["external_id"] == "532912293":
            return entry["id"]
    raise AssertionError("seeded tendersinfo tender 532912293 was not returned")


def _uzbekistan_tenders_tender_id(client: TestClient) -> str:
    resp = client.get(
        "/api/tenders?source=uzbekistan_tenders&matched=all&per_page=100"
    )
    resp.raise_for_status()
    for entry in resp.json()["tenders"]:
        if entry["external_id"] == "86ddab7":
            return entry["id"]
    raise AssertionError(
        "seeded uzbekistan_tenders tender 86ddab7 was not returned"
    )
