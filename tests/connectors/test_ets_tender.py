"""Tests for the ETS-Tender connector.

All offline; HTTP is exercised through ``httpx.MockTransport`` so the
suite is deterministic and CI-friendly. Fixtures live under
``tests/fixtures/ets_tender/``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from selectolax.parser import HTMLParser

from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.ets_tender import (
    EtsTenderConnector,
    _parse_detail,
    _parse_listing_row,
)
from tender_monitor.connectors.http import make_client
from tender_monitor.core.enums import Country, Language, TenderStatus

LISTING_PATH = "/market/"
TENDER_2085996_PATH = "/market/list-stalnoi-g-k/tender-2085996/"
CLOSED_TENDER_PATH = "/market/zakrytaia-protsedura-e0df281ed2/tender-2085989/"

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "ets_tender"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _client_factory(
    transport: httpx.MockTransport,
) -> Callable[[], httpx.AsyncClient]:
    def _make() -> httpx.AsyncClient:
        return make_client(
            headers=EtsTenderConnector.REQUIRED_HEADERS, transport=transport
        )

    return _make


def _is_listing(request: httpx.Request) -> bool:
    return request.url.path == LISTING_PATH


def _first_row() -> dict[str, object]:
    html = _read_fixture("listing.html")
    rows = HTMLParser(html).css("table.search-results tbody tr")
    assert rows, "listing fixture must have at least one row"
    parsed = _parse_listing_row(rows[0])
    assert parsed is not None
    return parsed


def _row_for_id(external_id: str) -> dict[str, object]:
    html = _read_fixture("listing.html")
    for tr in HTMLParser(html).css("table.search-results tbody tr"):
        parsed = _parse_listing_row(tr)
        if parsed and parsed.get("external_id") == external_id:
            return parsed
    raise AssertionError(f"no row in fixture with external_id={external_id}")


# ---------------------------------------------------------------------------
# Listing-row parsing
# ---------------------------------------------------------------------------


def test_parse_listing_row_extracts_all_fields() -> None:
    parsed = _first_row()

    assert parsed["external_id"] == "2085996"
    # Detail URL has the #btid= fragment stripped.
    assert parsed["detail_url"] == TENDER_2085996_PATH
    assert "#" not in str(parsed["detail_url"])
    assert parsed["buyer_name"] == "АО «GALANZ bottlers»"
    assert parsed["buyer_url"] == "/firms/ao-galanz-bottlers/135/"
    assert parsed["published_text"] == "18.05.2026 11:12"
    assert parsed["deadline_text"] == "20.05.2026 15:00"
    # The procedure-type prefix is preserved.
    assert parsed["procedure_type_text"] == "Запрос предложений"
    # Description from search-results-title-desc is non-empty.
    assert parsed["title_description"] is not None
    title_desc_obj = parsed["title_description"]
    assert isinstance(title_desc_obj, str)
    assert "Лист стальной" in title_desc_obj


def test_parse_listing_row_handles_hidden_dates() -> None:
    parsed = _row_for_id("2085989")
    assert parsed["external_id"] == "2085989"
    # Hidden dates pass through as-is at parse-time; conversion to None
    # happens downstream in the datetime parser.
    assert parsed["published_text"] == "Скрыто"
    assert parsed["deadline_text"] == "Скрыто"
    assert parsed["buyer_name"] == 'ТОО "Востокцветмет"'


# ---------------------------------------------------------------------------
# Detail-page parsing
# ---------------------------------------------------------------------------


def test_parse_detail_extracts_all_fields() -> None:
    html = _read_fixture("tender_2085996.html")
    detail = _parse_detail(html)

    assert detail["title_full"] == "Лист стальной г/к"
    description_obj = detail["description_full"]
    assert isinstance(description_obj, str)
    assert "горячекатаный" in description_obj
    assert detail["enstru_code"] == "241031.900.000011"
    enstru_label_obj = detail["enstru_label"]
    assert isinstance(enstru_label_obj, str)
    assert "Лист стальной" in enstru_label_obj
    assert detail["quantity_text"] == "10"
    assert detail["unit_price_text"] == "155 000,00 тенге"
    assert detail["total_price_text"] is not None
    total_price_obj = detail["total_price_text"]
    assert isinstance(total_price_obj, str)
    assert "1 550 000,00 тенге" in total_price_obj
    # Parenthetical VAT note is extracted.
    assert detail["vat_note"] == "(цена с НДС, НДС: 16%)"
    assert detail["published_text"] == "18.05.2026 11:12"
    assert detail["deadline_text"] == "20.05.2026 15:00"
    assert detail["last_edited_text"] == "18.05.2026 11:12"
    delivery_obj = detail["delivery_address"]
    assert isinstance(delivery_obj, str)
    assert "Алматы" in delivery_obj
    payment_obj = detail["payment_terms"]
    assert isinstance(payment_obj, str)
    assert "30 календарных" in payment_obj
    assert detail["organizer_link_text"] == "АО «GALANZ bottlers»"
    assert detail["_documents"] == []


def test_parse_detail_extracts_document_links() -> None:
    html = """
    <html><body>
      <h2 class="tender-title">Тест</h2>
      <a href="/uploads/specification.pdf">Техническая спецификация</a>
      <a href="https://cdn.example.com/files/contract.docx?download=1">Проект договора</a>
      <a href="/market/example">Не документ</a>
    </body></html>
    """
    detail = _parse_detail(html)

    documents = detail["_documents"]
    assert len(documents) == 2
    assert documents[0]["name"] == "Техническая спецификация"
    assert documents[0]["url"] == "https://www.ets-tender.kz/uploads/specification.pdf"
    assert documents[0]["ext"] == "PDF"
    assert documents[1]["url"] == "https://cdn.example.com/files/contract.docx?download=1"
    assert documents[1]["ext"] == "DOCX"


def test_parse_detail_handles_live_fname_table_layout() -> None:
    html = """
    <html><body>
      <table>
        <tr id="trade-info-lot-price">
          <td class="fname">Общая стоимость закупки:</td>
          <td>1 000 000,00 тенге</td>
        </tr>
        <tr id="trade-info-ens-tru">
          <td class="fname">Категория ЕНС ТРУ:</td>
          <td>123456.789.000000 — Тестовая категория</td>
        </tr>
      </table>
    </body></html>
    """

    detail = _parse_detail(html)

    assert detail["total_price_text"] == "1 000 000,00 тенге"
    assert detail["enstru_code"] == "123456.789.000000"
    assert detail["enstru_label"] == "Тестовая категория"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_happy_path() -> None:
    listing = _first_row()
    detail = _parse_detail(_read_fixture("tender_2085996.html"))
    raw = {**listing, **detail}

    upsert = EtsTenderConnector()._normalize(raw)

    assert upsert.source_name == "ets_tender"
    assert upsert.external_id == "2085996"
    assert upsert.title == "Лист стальной г/к"
    assert upsert.buyer_name == "АО «GALANZ bottlers»"
    assert upsert.country is Country.KZ
    assert upsert.value_amount == Decimal("1550000.00")
    assert upsert.value_currency == "KZT"
    assert upsert.published_at is not None
    assert upsert.published_at.tzinfo is UTC
    # 18.05.2026 11:12 KZ → 06:12 UTC (UTC+5).
    assert upsert.published_at == datetime(2026, 5, 18, 6, 12, tzinfo=UTC)
    assert upsert.deadline_at is not None
    assert upsert.deadline_at == datetime(2026, 5, 20, 10, 0, tzinfo=UTC)
    assert upsert.status is TenderStatus.open
    assert upsert.language is Language.ru
    assert upsert.source_url == (
        "https://www.ets-tender.kz" + TENDER_2085996_PATH
    )
    # Synthetic _lots wrap is populated for the keyword matcher.
    lots = upsert.raw_json["_lots"]
    assert len(lots) == 1
    assert lots[0]["name_ru"] == "Лист стальной г/к"
    assert lots[0]["description_ru"]


def test_normalize_keeps_documents() -> None:
    listing = _first_row()
    detail = _parse_detail(
        """
        <html><body>
          <h2 class="tender-title">Лист стальной г/к</h2>
          <a href="/uploads/specification.pdf">Техническая спецификация</a>
        </body></html>
        """
    )
    raw = {**listing, **detail}

    upsert = EtsTenderConnector()._normalize(raw)
    assert len(upsert.raw_json["_documents"]) == 1
    assert upsert.raw_json["_documents"][0]["ext"] == "PDF"


def test_normalize_handles_hidden_dates() -> None:
    listing = _row_for_id("2085989")
    # No detail merge — closed procedure that 403s in real life.
    upsert = EtsTenderConnector()._normalize(listing)

    assert upsert.external_id == "2085989"
    assert upsert.published_at is None
    assert upsert.deadline_at is None
    # No detail → no total price → no value_amount/currency.
    assert upsert.value_amount is None
    assert upsert.value_currency is None
    # Title still falls back to the listing's title_short.
    assert upsert.title


def test_normalize_missing_title_raises() -> None:
    raw = {
        "external_id": "999",
        "title_full": None,
        "title_description": None,
        "title_short": "",
        "detail_url": "/market/x/tender-999/",
        "buyer_name": None,
        "published_text": "",
        "deadline_text": "",
    }
    with pytest.raises(ParseError, match="no title"):
        EtsTenderConnector()._normalize(raw)


# ---------------------------------------------------------------------------
# Full pipeline — MockTransport, no real HTTP
# ---------------------------------------------------------------------------


def _route(
    request: httpx.Request,
    *,
    listing_html: str,
    detail_html_map: dict[str, str],
    closed_status: int = 200,
) -> httpx.Response:
    if _is_listing(request):
        # Paginated listing: only page 1 returns content.
        page = request.url.params.get("page")
        if page is None or page == "1":
            return httpx.Response(200, html=listing_html)
        return httpx.Response(
            200, html='<html><body><table class="search-results"><tbody></tbody></table></body></html>'
        )
    if request.url.path == CLOSED_TENDER_PATH:
        return httpx.Response(closed_status, html="forbidden")
    detail = detail_html_map.get(request.url.path)
    if detail is not None:
        return httpx.Response(200, html=detail)
    # Catch-all: a generic stub so non-fixtured detail URLs still
    # succeed (no detail merge but no warning either).
    return httpx.Response(
        200,
        html='<html><body><h2 class="tender-title">stub</h2></body></html>',
    )


async def test_fetch_latest_full_pipeline() -> None:
    listing_html = _read_fixture("listing.html")
    detail_html = _read_fixture("tender_2085996.html")
    detail_map = {TENDER_2085996_PATH: detail_html}
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _route(
            request,
            listing_html=listing_html,
            detail_html_map=detail_map,
        )

    transport = httpx.MockTransport(handler)
    connector = EtsTenderConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    assert result.source_name == "ets_tender"
    # 10 rows total in fixture; one (Скрыто) has no dates but is still
    # included — every row makes it through to a tender row.
    fixture_rows = HTMLParser(listing_html).css("table.search-results tbody tr")
    assert result.raw_item_count == len(fixture_rows)
    assert len(result.tenders) == len(fixture_rows)
    assert result.partial_errors == []
    assert all(t.source_name == "ets_tender" for t in result.tenders)
    assert all(t.status is TenderStatus.open for t in result.tenders)


async def test_fetch_latest_soft_since_stops_pagination_after_threshold() -> None:
    listing_html = _read_fixture("listing.html")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _route(request, listing_html=listing_html, detail_html_map={})

    transport = httpx.MockTransport(handler)
    connector = EtsTenderConnector(http_client_factory=_client_factory(transport))

    # since is AFTER every parseable published date in the fixture. The
    # closed tender has hidden listing dates, so it is kept rather than
    # dropped on a parser miss.
    since = datetime(2026, 5, 19, 0, 0, tzinfo=UTC)
    result = await connector.fetch_latest(since=since)

    assert [t.external_id for t in result.tenders] == ["2085989"]
    listing_calls = [r for r in captured if _is_listing(r)]
    pages_called = [r.url.params.get("page") for r in listing_calls]
    assert pages_called == [None, "2"]


async def test_fetch_latest_detail_403_keeps_listing_fields() -> None:
    listing_html = _read_fixture("listing.html")
    detail_html = _read_fixture("tender_2085996.html")
    detail_map = {TENDER_2085996_PATH: detail_html}

    def handler(request: httpx.Request) -> httpx.Response:
        return _route(
            request,
            listing_html=listing_html,
            detail_html_map=detail_map,
            closed_status=403,
        )

    transport = httpx.MockTransport(handler)
    connector = EtsTenderConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    closed = [t for t in result.tenders if t.external_id == "2085989"]
    assert len(closed) == 1
    closed_tender = closed[0]
    # Detail failed (403) → no detail fields → no value_amount.
    assert closed_tender.value_amount is None
    assert closed_tender.value_currency is None
    # Dates were "Скрыто" on the listing → None after parsing.
    assert closed_tender.published_at is None
    assert closed_tender.deadline_at is None


async def test_fetch_latest_detail_misses_do_not_overwrite_listing_dates() -> None:
    short_html = (
        '<html><body><table class="search-results"><tbody>'
        '<tr>'
        '<td><a class="search-results-title" '
        'href="/market/foo/tender-9001/">Запрос цен № 9001'
        '<div class="search-results-title-desc">Тестовый закуп</div>'
        '</a></td>'
        '<td><a href="/firms/foo/1/">ТОО Foo</a></td>'
        '<td class="nowrap">18.05.2026 09:00</td>'
        '<td class="nowrap">20.05.2026 09:00</td>'
        '<td class="favorite-column"></td>'
        '</tr>'
        '</tbody></table></body></html>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(200, html=short_html)
        # This mimics the current live detail layout when the fields we
        # project are absent: _parse_detail returns published_text=None
        # and deadline_text=None. Those misses must not erase listing dates.
        return httpx.Response(
            200,
            html='<html><body><div class="expandable-text">Detail text</div></body></html>',
        )

    transport = httpx.MockTransport(handler)
    connector = EtsTenderConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    assert len(result.tenders) == 1
    tender = result.tenders[0]
    assert tender.published_at == datetime(2026, 5, 18, 4, 0, tzinfo=UTC)
    assert tender.deadline_at == datetime(2026, 5, 20, 4, 0, tzinfo=UTC)


async def test_fetch_latest_listing_500_raises_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = EtsTenderConnector(http_client_factory=_client_factory(transport))

    with pytest.raises(FetchError):
        await connector.fetch_latest()


async def test_fetch_latest_short_page_breaks() -> None:
    short_html = (
        '<html><body><table class="search-results"><tbody>'
        '<tr>'
        '<td><a class="search-results-title" '
        'href="/market/foo/tender-9001/#btid=2">Запрос цен № 9001'
        '<div class="search-results-title-desc">Тестовый закуп</div>'
        '</a></td>'
        '<td><a href="/firms/foo/1/">ТОО Foo</a></td>'
        '<td class="nowrap">18.05.2026 09:00</td>'
        '<td class="nowrap">20.05.2026 09:00</td>'
        '<td class="favorite-column"></td>'
        '</tr>'
        '</tbody></table></body></html>'
    )

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, html=short_html)
        return httpx.Response(
            200,
            html=(
                '<html><body><h2 class="tender-title">stub</h2></body></html>'
            ),
        )

    transport = httpx.MockTransport(handler)
    connector = EtsTenderConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_listing(r)]
    pages_called = [r.url.params.get("page") for r in listing_calls]
    assert len(listing_calls) == 1
    assert "2" not in pages_called
    assert len(result.tenders) == 1


async def test_fetch_latest_stops_when_pagination_repeats_same_ids() -> None:
    repeated_page = (
        '<html><body><table class="search-results"><tbody>'
        + "".join(
            (
                '<tr>'
                f'<td><a class="search-results-title" href="/market/foo/tender-{9000 + i}/">'
                f'Запрос цен № {9000 + i}<div class="search-results-title-desc">Тест {i}</div>'
                '</a></td>'
                '<td><a href="/firms/foo/1/">ТОО Foo</a></td>'
                '<td class="nowrap">18.05.2026 09:00</td>'
                '<td class="nowrap">20.05.2026 09:00</td>'
                '<td class="favorite-column"></td>'
                '</tr>'
            )
            for i in range(10)
        )
        + "</tbody></table></body></html>"
    )

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, html=repeated_page)
        return httpx.Response(
            200,
            html='<html><body><h2 class="tender-title">stub</h2></body></html>',
        )

    transport = httpx.MockTransport(handler)
    connector = EtsTenderConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_listing(r)]
    pages_called = [r.url.params.get("page") for r in listing_calls]
    assert pages_called == [None, "2"]
    assert len(result.tenders) == 10
