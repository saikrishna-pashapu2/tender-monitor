from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from selectolax.parser import HTMLParser

from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.goszakup import (
    STATUS_MAPPING,
    GoszakupConnector,
    _parse_announcement,
    _parse_listing_row,
)
from tender_monitor.connectors.http import make_client
from tender_monitor.core.enums import Country, Language, TenderStatus

LISTING_PATH = "/ru/search/lots"
ANNOUNCE_PATH_PREFIX = "/ru/announce/index/"

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "goszakup"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _client_factory(
    transport: httpx.MockTransport,
) -> Callable[[], httpx.AsyncClient]:
    def _make() -> httpx.AsyncClient:
        return make_client(
            headers=GoszakupConnector.REQUIRED_HEADERS, transport=transport
        )

    return _make


def _is_listing(request: httpx.Request) -> bool:
    return request.url.path == LISTING_PATH


def _is_announcement(request: httpx.Request) -> bool:
    return request.url.path.startswith(ANNOUNCE_PATH_PREFIX)


def _announcement_id_from_url(request: httpx.Request) -> str:
    return request.url.path[len(ANNOUNCE_PATH_PREFIX) :]


def _wrap_listing(rows_html: str) -> str:
    return (
        "<html><body><table id='search-result'><tbody>"
        f"{rows_html}"
        "</tbody></table></body></html>"
    )


def _build_listing_row(
    *,
    lot_id: str,
    announcement_id: str,
    lot_title: str = "Test lot",
    announcement_title: str = "Test announcement",
    buyer_name: str = "Test Buyer",
    amount: str = "1 234.56",
    status: str = "Опубликован",
) -> str:
    return f"""
    <tr data-key="{lot_id}">
      <td><strong>REF-{lot_id}</strong></td>
      <td>
        <a href="/ru/announce/index/{announcement_id}">{announcement_id}-1 {announcement_title}</a>
        <br><small><b>Заказчик:</b> {buyer_name}</small>
      </td>
      <td>
        <a href="/ru/subpriceoffer/index/{announcement_id}/{lot_id}">{lot_title}</a>
        <a class="history" href="#" data-trb-buy="{announcement_id}" data-lot-id="{lot_id}">История</a>
      </td>
      <td>1</td>
      <td>{amount}</td>
      <td>Запрос ценовых предложений</td>
      <td>{status}</td>
    </tr>
    """


def _build_announcement_html(
    *,
    announcement_id: str,
    publish_date: str = "2026-05-12 09:15:00",
    offer_start: str = "2026-05-12 09:30:00",
    offer_end: str = "2026-05-19 18:00:00",
    organizer_bin: str = "100140006825",
    organizer_name: str = "Test Buyer",
) -> str:
    """Synthetic announcement HTML using the real label set.

    Mirrors what the live ``/ru/announce/index/<id>`` page renders:
    six form-control rows (number, name, status, three dates) in the
    top panel and a small ``Общие сведения`` table at the bottom with
    the organizer line. Anything else the live page exposes is left
    out — tests only need the fields the parser consumes.
    """
    return f"""
    <html><body>
      <div class="panel">
        <div class="form-group">
          <label class="control-label">Номер объявления</label>
          <input type="text" value="{announcement_id}-1" readonly/>
        </div>
        <div class="form-group">
          <label class="control-label">Наименование объявления</label>
          <input type="text" value="Test announcement" readonly/>
        </div>
        <div class="form-group">
          <label class="control-label">Статус объявления</label>
          <input type="text" value="Опубликован (прием ценовых предложений)" readonly/>
        </div>
        <div class="form-group">
          <label class="control-label">Дата публикации объявления</label>
          <input type="text" value="{publish_date}" readonly/>
        </div>
        <div class="form-group">
          <label class="control-label">Срок начала приема заявок</label>
          <input type="text" value="{offer_start}" readonly/>
        </div>
        <div class="form-group">
          <label class="control-label">Срок окончания приема заявок</label>
          <input type="text" value="{offer_end}" readonly/>
        </div>
      </div>
      <div class="panel">
        <table class="table">
          <tr><th>Способ проведения закупки</th><td>Запрос ценовых предложений</td></tr>
          <tr><th>Организатор</th><td>{organizer_bin} {organizer_name}</td></tr>
          <tr><th>Кол-во лотов в объявлении</th><td>1</td></tr>
          <tr><th>Сумма закупки</th><td>1 000.00</td></tr>
        </table>
      </div>
    </body></html>
    """


# ---------------------------------------------------------------------------
# Listing/detail parsing
# ---------------------------------------------------------------------------


def test_parse_listing_row_extracts_all_fields() -> None:
    html = _read_fixture("listing.html")
    parser = HTMLParser(html)
    first_row = parser.css_first("table#search-result tbody tr")
    assert first_row is not None

    row = _parse_listing_row(first_row)
    assert row["lot_reference_number"] == "84701402-ЗЦП1"
    assert row["announcement_id"] == "17013627"
    assert row["announcement_number"] == "17013627-1"
    assert "мытью окон" in row["announcement_title"]
    assert "Казтелерадио" in row["buyer_name"]
    assert row["lot_id"] == "41804689"
    assert "мытью окон" in row["lot_title"]
    assert row["lot_detail_url"].endswith("/ru/subpriceoffer/index/17013627/41804689")
    assert row["quantity_text"] == "50"
    assert row["amount_text"] == "17 241.37"
    assert row["procurement_method"] == "Запрос ценовых предложений"
    assert "Опубликован" in row["status_text"]


def test_parse_announcement_extracts_all_fields() -> None:
    """Pins the real-page label set captured in
    ``announce_17013627.html`` (saved from the live portal). The
    fixture has 6 form-control rows + 14 table rows; we assert one
    value out of each major group so a future label rename on the
    live site fails this test loudly.
    """
    html = _read_fixture("announce_17013627.html")
    parsed = _parse_announcement(html)
    # Form-control (top) panel fields.
    assert parsed["announcement_number"] == "17013627-1"
    assert "мытью окон" in parsed["announcement_title_ru"]
    assert "Опубликовано" in parsed["announcement_status"]
    assert parsed["publish_date_text"] == "2026-05-18 10:46:26"
    assert parsed["offer_start_text"] == "2026-05-18 10:46:26"
    assert parsed["offer_end_text"] == "2026-05-20 10:46:26"
    # Общие сведения (bottom) panel fields.
    assert parsed["procurement_method"] == (
        "Из одного источника по несостоявшимся закупкам"
    )
    assert parsed["purchase_type"] == "ИОИ от ЗЦП"
    assert parsed["failed_procurement_method"] == "Запрос ценовых предложений"
    assert parsed["subject_type"] == "Услуга"
    assert parsed["organizer_bin"] == "151241012513"
    assert "Казтелерадио" in parsed["organizer_name"]
    assert parsed["organizer_legal_address"].startswith("КАЗАХСТАН")
    assert parsed["lot_count_text"] == "1"
    assert parsed["total_amount_text"] == "155 000.00"
    assert parsed["organizer_representative"] == "Оспанов Кайрош Бакитович"
    assert parsed["organizer_position"] == "Юрист"
    assert parsed["organizer_email"] == "k.b.ospanov@mail.ru"
    assert parsed["announcement_creator"] == "Оспанов Кайрош Бакитович"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_happy_path() -> None:
    listing_html = _read_fixture("listing.html")
    announce_html = _read_fixture("announce_17013627.html")
    parser = HTMLParser(listing_html)
    first_row = parser.css_first("table#search-result tbody tr")
    assert first_row is not None

    row = _parse_listing_row(first_row)
    detail = _parse_announcement(announce_html)
    combined = {**row, **detail}

    upsert = GoszakupConnector()._normalize(combined)

    assert upsert.source_name == "goszakup"
    assert upsert.external_id == "41804689"
    assert "мытью окон" in upsert.title
    # buyer_name comes from the listing row (<small>Заказчик:</small>),
    # not the detail page's organizer field.
    assert "Казтелерадио" in upsert.buyer_name
    # BIN comes from the detail page's "Организатор" cell.
    assert upsert.buyer_external_id == "151241012513"
    assert upsert.country is Country.KZ
    assert upsert.value_amount == Decimal("17241.37")
    assert upsert.value_currency == "KZT"
    # KZ-local 2026-05-18 10:46:26 → UTC 05:46:26.
    assert upsert.published_at is not None
    assert upsert.published_at.tzinfo is UTC
    assert upsert.deadline_at is not None
    assert upsert.deadline_at.tzinfo is UTC
    assert upsert.status is TenderStatus.open
    assert upsert.source_url.endswith("/41804689")
    assert upsert.language is Language.ru
    assert upsert.raw_json["_lots"][0]["name_ru"] == upsert.title


def test_normalize_missing_title_raises() -> None:
    row = {
        "lot_id": "1",
        "lot_title": "",
        "lot_detail_url": "https://example.test/1",
        "amount_text": "100.00",
    }
    with pytest.raises(ParseError, match="empty lot_title"):
        GoszakupConnector()._normalize(row)

    row2 = dict(row)
    row2["lot_title"] = None  # type: ignore[assignment]
    with pytest.raises(ParseError):
        GoszakupConnector()._normalize(row2)


def test_status_mapping_known_and_unknown() -> None:
    assert STATUS_MAPPING["Опубликован"] is TenderStatus.open
    assert STATUS_MAPPING["Опубликован (прием ценовых предложений)"] is TenderStatus.open

    connector = GoszakupConnector()
    base = {
        "lot_id": "1",
        "lot_title": "Test",
        "lot_detail_url": "https://example.test/1",
        "amount_text": "100.00",
    }

    pub = connector._normalize({**base, "status_text": "Опубликован"})
    assert pub.status is TenderStatus.open

    pub2 = connector._normalize(
        {**base, "status_text": "Опубликован (прием ценовых предложений)"}
    )
    assert pub2.status is TenderStatus.open

    weird = connector._normalize({**base, "status_text": "Какой-то новый статус"})
    assert weird.status is TenderStatus.unknown

    missing = connector._normalize(base)
    assert missing.status is TenderStatus.unknown


# ---------------------------------------------------------------------------
# Fetch pipeline
# ---------------------------------------------------------------------------


async def test_fetch_latest_full_pipeline() -> None:
    listing_html = _read_fixture("listing.html")
    announce_html = _read_fixture("announce_17013627.html")
    seen_announcements: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(200, text=listing_html)
        if _is_announcement(request):
            ann_id = _announcement_id_from_url(request)
            seen_announcements.add(ann_id)
            if ann_id == "17013627":
                return httpx.Response(200, text=announce_html)
            return httpx.Response(
                200,
                text=_build_announcement_html(announcement_id=ann_id),
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = GoszakupConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    assert result.source_name == "goszakup"
    # Listing fixture has 4 rows; all four are combined with their
    # announcement details and emitted.
    assert result.raw_item_count == 4
    assert len(result.tenders) == 4
    for tender in result.tenders:
        assert tender.published_at is not None
        assert tender.deadline_at is not None


async def test_fetch_latest_dedups_announcement_fetches() -> None:
    listing_html = _read_fixture("listing.html")
    announce_html = _read_fixture("announce_17013627.html")
    announcement_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(200, text=listing_html)
        if _is_announcement(request):
            ann_id = _announcement_id_from_url(request)
            announcement_calls.append(ann_id)
            if ann_id == "17013627":
                return httpx.Response(200, text=announce_html)
            return httpx.Response(
                200,
                text=_build_announcement_html(announcement_id=ann_id),
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = GoszakupConnector(http_client_factory=_client_factory(transport))
    await connector.fetch_latest()

    # 17013627 has TWO lots in the fixture; the detail URL must be
    # fetched exactly once across those two lots.
    assert announcement_calls.count("17013627") == 1
    # Other announcements: one fetch each (16994590, 16994577).
    assert sorted(set(announcement_calls)) == ["16994577", "16994590", "17013627"]


async def test_fetch_latest_handles_announcement_404() -> None:
    listing_html = _read_fixture("listing.html")
    announce_html = _read_fixture("announce_17013627.html")

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(200, text=listing_html)
        if _is_announcement(request):
            ann_id = _announcement_id_from_url(request)
            if ann_id == "16994590":
                return httpx.Response(404, text="not found")
            if ann_id == "17013627":
                return httpx.Response(200, text=announce_html)
            return httpx.Response(
                200,
                text=_build_announcement_html(announcement_id=ann_id),
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = GoszakupConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    # 17013627 contributes 2 lots, 16994577 contributes 1, 16994590's
    # single lot is dropped because the announcement detail 404'd.
    assert len(result.tenders) == 3
    assert "41804700" not in {t.external_id for t in result.tenders}
    # No FetchError raised, no partial errors emitted.
    assert result.partial_errors == []


async def test_fetch_latest_listing_500_raises_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = GoszakupConnector(http_client_factory=_client_factory(transport))
    with pytest.raises(FetchError):
        await connector.fetch_latest()


async def test_fetch_latest_stops_on_short_page() -> None:
    """A <50-row listing must NOT trigger a page=2 request."""
    listing_html = _read_fixture("listing.html")
    pages_requested: list[int | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            page_param = request.url.params.get("page")
            pages_requested.append(int(page_param) if page_param else None)
            return httpx.Response(200, text=listing_html)
        if _is_announcement(request):
            ann_id = _announcement_id_from_url(request)
            return httpx.Response(
                200, text=_build_announcement_html(announcement_id=ann_id)
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = GoszakupConnector(http_client_factory=_client_factory(transport))
    await connector.fetch_latest()

    # Only page=1 should have been requested (the fixture has 4 rows
    # << PAGE_SIZE=50).
    assert pages_requested == [1]


async def test_fetch_latest_soft_since_filter() -> None:
    """A run of 10 consecutive older-than-since lots triggers the soft break."""
    # 12 rows on page 1, each with its OWN announcement so we don't
    # accidentally dedup. We want the first 10 lots to be "older" so
    # the threshold trips on lot #10 and lot #11 / #12 never enter.
    rows = "".join(
        _build_listing_row(
            lot_id=f"500000{i}", announcement_id=f"9000{i:03d}"
        )
        for i in range(12)
    )
    listing_html = _wrap_listing(rows)
    pages_requested: list[int | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            page_param = request.url.params.get("page")
            pages_requested.append(int(page_param) if page_param else None)
            return httpx.Response(200, text=listing_html)
        if _is_announcement(request):
            ann_id = _announcement_id_from_url(request)
            # Every announcement says it was published in early April.
            return httpx.Response(
                200,
                text=_build_announcement_html(
                    announcement_id=ann_id,
                    publish_date="2026-04-01 10:00:00",
                ),
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = GoszakupConnector(http_client_factory=_client_factory(transport))
    # 'since' set to May 1: every fixture publish_date is older.
    result = await connector.fetch_latest(since=datetime(2026, 5, 1, tzinfo=UTC))

    # All rows are old; the soft-since path drops them. After
    # SINCE_OLD_THRESHOLD=10 consecutive olds the loop breaks; the
    # remaining 2 lots are never even considered.
    assert len(result.tenders) == 0
