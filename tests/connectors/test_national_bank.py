from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from selectolax.parser import HTMLParser

from tender_monitor.connectors import _html, mitwork, national_bank
from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.http import make_client
from tender_monitor.connectors.national_bank import (
    STATUS_MAPPING,
    NationalBankConnector,
    _merge_listing_and_detail,
    _parse_detail,
    _parse_listing_row,
)
from tender_monitor.core.enums import Country, Language, TenderStatus

LISTING_PATH = "/ru/publics/lots"
LOT_PATH_PREFIX = "/ru/publics/lot/"

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "national_bank"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _client_factory(
    transport: httpx.MockTransport,
) -> Callable[[], httpx.AsyncClient]:
    def _make() -> httpx.AsyncClient:
        return make_client(
            headers=NationalBankConnector.REQUIRED_HEADERS, transport=transport
        )

    return _make


def _is_listing(request: httpx.Request) -> bool:
    return request.url.path == LISTING_PATH


def _is_detail(request: httpx.Request) -> bool:
    return request.url.path.startswith(LOT_PATH_PREFIX)


def _detail_id(request: httpx.Request) -> str:
    return request.url.path[len(LOT_PATH_PREFIX):]


def _wrap_rows_as_listing(rows_html: str) -> str:
    return (
        '<html><body><table class="table table-striped table-bordered">'
        f"<tbody>{rows_html}</tbody></table></body></html>"
    )


# Listing fragment used by short-page and soft-since tests.
def _build_listing_row(
    *,
    data_key: str,
    title: str = "Test lot",
    value: str = "10 000,00 KZT",
    buyer_bin: str = "000000000001",
) -> str:
    return f"""
    <tr class="item" data-key="{data_key}">
      <td>NUM-{data_key}</td>
      <td><a class="word-break" href="https://zakup.nationalbank.kz/ru/publics/lot/{data_key}">{title}</a>
        <span class="label label-default">000000.000.000000</span></td>
      <td class="col-sm-6 hidden-xs">char-{data_key}</td>
      <td class="text-right text-nowrap">{value}</td>
      <td class="hidden-xs"><a href="https://zakup.nationalbank.kz/ru/publics/subject/1" title="Test Buyer">{buyer_bin}</a></td>
      <td class="hidden-xs">Опубликован</td>
    </tr>
    """


def _build_detail_html(
    *,
    name_ru: str = "Test lot",
    start: str = "2026-05-10 09:00:00",
    end: str = "2026-05-17 09:00:00",
    status: str = "Опубликовано",
) -> str:
    return f"""
    <html><body>
    <table class="table detail-view">
      <tr><th>Наименование закупаемых товаров, работ, услуг на русском языке</th><td>{name_ru}</td></tr>
      <tr><th>Характеристика закупаемых товаров, работ, услуг на русском языке</th><td>char</td></tr>
      <tr><th>Количество (объем), сумма выделенная на закупку</th><td><h4>1.000 шт x 10.00 тг. = 10.00 тг.</h4></td></tr>
    </table>
    <h3>Информация об объявлении <a href="https://zakup.nationalbank.kz/ru/publics/buy/9001">9001</a></h3>
    <table class="table detail-view">
      <tr><th>Наименование объявления на русском языке</th><td>{name_ru}</td></tr>
      <tr><th>Дата и время начала приема заявок</th><td>{start}</td></tr>
      <tr><th>Дата и время вскрытия и завершения приема заявок</th><td>{end} <span class="label label-warning">через 7 дней</span></td></tr>
      <tr><th>Организатор</th><td><a href="https://zakup.nationalbank.kz/ru/publics/subject/1">Test Buyer</a></td></tr>
      <tr><th>Способ закупки</th><td>Запрос ценовых предложений</td></tr>
      <tr><th>Статус</th><td>{status}</td></tr>
    </table>
    </body></html>
    """


# ---------------------------------------------------------------------------
# 1 — Listing row parsing
# ---------------------------------------------------------------------------


def test_parse_listing_row_extracts_all_fields() -> None:
    html = _read_fixture("listing.html")
    rows = HTMLParser(html).css("tr.item")
    assert rows, "listing fixture must have at least one tr.item"
    parsed = _parse_listing_row(rows[0])

    assert parsed["data_key"] == rows[0].attributes.get("data-key")
    assert parsed["data_key"]

    assert isinstance(parsed["title_ru"], str) and parsed["title_ru"].strip()
    # Title should contain Cyrillic characters.
    assert any("Ѐ" <= ch <= "ӿ" for ch in parsed["title_ru"])

    assert parsed["detail_url"].startswith(
        "https://zakup.nationalbank.kz/ru/publics/lot/"
    )
    assert parsed["enstru_code"]  # the ЕНСТРУ label

    assert parsed["announcement_number"]  # e.g. "264623-ЗЦП1"
    assert parsed["characteristic_ru"]
    assert "KZT" in parsed["value_text"]

    assert parsed["buyer_bin"] is not None
    assert parsed["buyer_bin"].isdigit() and len(parsed["buyer_bin"]) == 12

    assert parsed["buyer_name"]
    assert parsed["subject_url"].startswith(
        "https://zakup.nationalbank.kz/ru/publics/subject/"
    )
    assert parsed["status_text"]


# ---------------------------------------------------------------------------
# 2 — Detail page parsing
# ---------------------------------------------------------------------------


def test_parse_detail_extracts_lot_and_announcement() -> None:
    html = _read_fixture("lot_228344.html")
    detail = _parse_detail(html)

    assert detail["name_ru"] == "Текущий ремонт административного здания"
    assert detail["characteristic_ru"] is not None
    assert "Сатпаева" in detail["characteristic_ru"]
    assert detail["enstru_code"] == "410040.300.000009"

    assert detail["announcement_id"] == "102968"
    assert detail["announcement_url"] == (
        "https://zakup.nationalbank.kz/ru/publics/buy/102968"
    )
    assert detail["announcement_start_text"] == "2026-05-15 09:00:00"
    # The deadline cell has a trailing "через 7 дней" badge; the parser
    # must peel it off and keep just the timestamp.
    assert detail["announcement_end_text"] == "2026-05-22 09:00:00"

    assert detail["procurement_method"] == "Запрос ценовых предложений"
    assert detail["announcement_status"] == "Опубликовано"
    assert detail["organizer_name"] is not None
    assert "НАЦИОНАЛЬНЫЙ БАНК" in detail["organizer_name"]
    assert detail["organizer_email"] == "dinara.beisbayeva@nationalbank.kz"
    assert detail["organizer_url"] == (
        "https://zakup.nationalbank.kz/ru/publics/subject/280"
    )

    assert detail["delivery_places"]
    assert detail["delivery_places"][0]["country"] == "Казахстан"
    assert len(detail["_documents"]) == 5
    assert detail["_documents"][0]["name"] == "ПД_ ТР Сатпаева.docx"
    assert detail["_documents"][0]["url"] == (
        "https://zakup.nationalbank.kz/ru/files/download/"
        "752e59e8d9a789bad39d1832be8280b5/?buyid=102968"
    )
    assert detail["_documents"][0]["ext"] == "DOCX"


# ---------------------------------------------------------------------------
# 3 — Normalization (merged listing + detail)
# ---------------------------------------------------------------------------


def test_normalize_happy_path() -> None:
    listing_html = _read_fixture("listing.html")
    detail_html = _read_fixture("lot_228344.html")
    rows = HTMLParser(listing_html).css("tr.item")
    listing_row = next(
        _parse_listing_row(r) for r in rows if r.attributes.get("data-key") == "228344"
    )
    detail = _parse_detail(detail_html)
    merged = _merge_listing_and_detail(listing_row, detail)

    upsert = NationalBankConnector()._normalize(merged)

    assert upsert.source_name == "national_bank"
    assert upsert.external_id == "228344"
    assert upsert.title == "Текущий ремонт административного здания"
    assert upsert.buyer_name and "НАЦИОНАЛЬНЫЙ БАНК" in upsert.buyer_name
    assert upsert.buyer_external_id == "941240001151"
    assert upsert.country is Country.KZ
    assert upsert.value_amount == Decimal("7964231.03")
    assert upsert.value_currency == "KZT"
    assert upsert.published_at is not None
    assert upsert.published_at.tzinfo == UTC
    assert upsert.deadline_at is not None
    assert upsert.deadline_at.tzinfo == UTC
    assert upsert.status is TenderStatus.open
    assert upsert.source_url == "https://zakup.nationalbank.kz/ru/publics/lot/228344"
    assert upsert.language is Language.ru

    # Synthetic _lots so match_tender's haystack walk reaches the
    # characteristic alongside the title.
    lots = upsert.raw_json["_lots"]
    assert len(lots) == 1
    assert lots[0]["name_ru"] == upsert.title
    assert lots[0]["description_ru"] is not None
    assert "Сатпаева" in lots[0]["description_ru"]
    assert len(upsert.raw_json["_documents"]) == 5
    assert upsert.raw_json["_documents"][2]["ext"] == "XLSX"


# ---------------------------------------------------------------------------
# 4 — Empty title rejected
# ---------------------------------------------------------------------------


def test_normalize_missing_title_raises() -> None:
    raw: dict[str, object] = {
        "data_key": "999",
        "title_ru": "",
        "characteristic_ru": "char",
        "value_text": "10,00 KZT",
        "buyer_name": "Test",
        "buyer_bin": "000000000000",
        "detail_url": "https://zakup.nationalbank.kz/ru/publics/lot/999",
        "status_text": "Опубликован",
        "announcement_start_text": "2026-05-10 09:00:00",
        "announcement_end_text": "2026-05-17 09:00:00",
    }
    with pytest.raises(ParseError, match="empty title"):
        NationalBankConnector()._normalize(raw)


# ---------------------------------------------------------------------------
# 5 — Status mapping
# ---------------------------------------------------------------------------


def test_status_mapping() -> None:
    assert STATUS_MAPPING["Опубликован"] is TenderStatus.open
    assert STATUS_MAPPING["Итоги. Закупка состоялась"] is TenderStatus.awarded
    assert (
        STATUS_MAPPING.get("Завершен", TenderStatus.unknown)
        is TenderStatus.unknown
    )
    assert STATUS_MAPPING.get("", TenderStatus.unknown) is TenderStatus.unknown


# ---------------------------------------------------------------------------
# 6 — Full pipeline with fixtures
# ---------------------------------------------------------------------------


async def test_fetch_latest_full_pipeline() -> None:
    listing_html = _read_fixture("listing.html")
    detail_html = _read_fixture("lot_228344.html")
    expected_rows = len(HTMLParser(listing_html).css("tr.item"))
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            # First page only; subsequent pages empty to terminate.
            page = request.url.params.get("page")
            if page in (None, "1"):
                return httpx.Response(200, html=listing_html)
            return httpx.Response(
                200,
                html=(
                    '<html><body><table class="table"><tbody></tbody></table>'
                    "</body></html>"
                ),
            )
        if _is_detail(request):
            return httpx.Response(200, html=detail_html)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = NationalBankConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    assert result.source_name == "national_bank"
    assert result.raw_item_count == expected_rows
    assert len(result.tenders) == expected_rows
    assert result.partial_errors == []
    assert result.fetched_at.tzinfo is UTC
    assert result.duration_ms > 0

    # Every tender must have both date fields populated — proof the
    # detail fetch happened for every row.
    for tender in result.tenders:
        assert tender.published_at is not None
        assert tender.deadline_at is not None

    listing_calls = [r for r in captured if _is_listing(r)]
    detail_calls = [r for r in captured if _is_detail(r)]
    assert len(listing_calls) >= 1
    assert len(detail_calls) == expected_rows


# ---------------------------------------------------------------------------
# 7 — Detail 404 logged + skipped, never raised
# ---------------------------------------------------------------------------


async def test_fetch_latest_handles_detail_404() -> None:
    listing_html = _read_fixture("listing.html")
    detail_html = _read_fixture("lot_228344.html")
    rows = HTMLParser(listing_html).css("tr.item")
    expected_rows = len(rows)
    sacrifice_id = rows[0].attributes.get("data-key")
    assert sacrifice_id

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            page = request.url.params.get("page")
            if page in (None, "1"):
                return httpx.Response(200, html=listing_html)
            return httpx.Response(
                200,
                html='<html><body><table class="table"><tbody></tbody></table></body></html>',
            )
        if _is_detail(request):
            if _detail_id(request) == sacrifice_id:
                return httpx.Response(404, html="<html><body>not found</body></html>")
            return httpx.Response(200, html=detail_html)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = NationalBankConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    assert len(result.tenders) == expected_rows - 1
    assert {t.external_id for t in result.tenders}.isdisjoint({sacrifice_id})
    # 404s skip with a warning log, not a partial_errors entry.
    assert result.partial_errors == []


# ---------------------------------------------------------------------------
# 8 — Listing 500 raises FetchError
# ---------------------------------------------------------------------------


async def test_fetch_latest_listing_500_raises_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = NationalBankConnector(http_client_factory=_client_factory(transport))

    with pytest.raises(FetchError):
        await connector.fetch_latest()


# ---------------------------------------------------------------------------
# 9 — Short page stops without requesting page=2
# ---------------------------------------------------------------------------


async def test_fetch_latest_stops_on_short_page() -> None:
    short_listing = _wrap_rows_as_listing(
        _build_listing_row(data_key="L1") + _build_listing_row(data_key="L2")
    )
    detail_html = _build_detail_html()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, html=short_listing)
        if _is_detail(request):
            return httpx.Response(200, html=detail_html)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = NationalBankConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_listing(r)]
    pages = [r.url.params.get("page") for r in listing_calls]
    assert len(listing_calls) == 1
    assert "2" not in pages
    assert len(result.tenders) == 2


async def test_fetch_latest_stops_when_listing_pages_repeat_same_ids() -> None:
    repeated_listing = _wrap_rows_as_listing(
        "".join(_build_listing_row(data_key=str(i)) for i in range(1, 51))
    )
    detail_html = _build_detail_html()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, html=repeated_listing)
        if _is_detail(request):
            return httpx.Response(200, html=detail_html)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = NationalBankConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_listing(r)]
    pages = [r.url.params.get("page") for r in listing_calls]
    assert pages == ["1", "2"]
    assert len(result.tenders) == 50


# ---------------------------------------------------------------------------
# 10 — Soft since-filter: 5 olds in a row trigger a stop
# ---------------------------------------------------------------------------


async def test_fetch_latest_soft_since_filter() -> None:
    # Eight listing rows: first 5 are old, next 3 would be new.
    # `since` falls between them so old < since < new.
    old_start = "2026-04-01 09:00:00"
    new_start = "2026-05-10 09:00:00"
    since = datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC)

    listing_rows = "".join(
        _build_listing_row(data_key=f"OLD{i}") for i in range(5)
    ) + "".join(_build_listing_row(data_key=f"NEW{i}") for i in range(3))
    short_listing = _wrap_rows_as_listing(listing_rows)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, html=short_listing)
        if _is_detail(request):
            lot_id = _detail_id(request)
            start = old_start if lot_id.startswith("OLD") else new_start
            return httpx.Response(200, html=_build_detail_html(start=start))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = NationalBankConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest(since=since)

    detail_requests = [r for r in captured if _is_detail(r)]
    fetched_ids = [_detail_id(r) for r in detail_requests]
    # The 5 olds were fetched (otherwise we couldn't have known); the
    # NEWs were never reached because the threshold tripped.
    assert fetched_ids == ["OLD0", "OLD1", "OLD2", "OLD3", "OLD4"]
    assert all(not lot_id.startswith("NEW") for lot_id in fetched_ids)
    # No row is returned because none of the fetched lots passed the
    # since cutoff.
    assert result.tenders == []


# ---------------------------------------------------------------------------
# 11 — The refactor actually moved (not copied) the shared helpers
# ---------------------------------------------------------------------------


def test_mitwork_helpers_imported_from_html_module() -> None:
    assert mitwork.parse_kzt_amount is _html.parse_kzt_amount
    assert mitwork.parse_kz_local_datetime is _html.parse_kz_local_datetime
    assert national_bank.parse_kzt_amount is _html.parse_kzt_amount
    assert national_bank.parse_kz_local_datetime is _html.parse_kz_local_datetime


# ---------------------------------------------------------------------------
# 12 — known_external_ids hint does not suppress detail refreshes
# ---------------------------------------------------------------------------


def _five_row_listing() -> str:
    """Build a 5-row listing with data_keys "1".."5".

    Sized below PAGE_SIZE so the connector breaks after page 1.
    """
    return _wrap_rows_as_listing(
        "".join(_build_listing_row(data_key=str(i)) for i in range(1, 6))
    )


async def test_fetch_ignores_known_ids_and_fetches_all_details() -> None:
    listing = _five_row_listing()
    detail_html = _build_detail_html()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, html=listing)
        if _is_detail(request):
            return httpx.Response(200, html=detail_html)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = NationalBankConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest(known_external_ids={"1", "2", "3"})

    listing_calls = [r for r in captured if _is_listing(r)]
    detail_calls = [r for r in captured if _is_detail(r)]
    assert len(listing_calls) == 1
    # Known IDs must still fetch detail so existing rows can be
    # refreshed and rematched after keyword/source parser changes.
    fetched_ids = sorted(_detail_id(r) for r in detail_calls)
    assert fetched_ids == ["1", "2", "3", "4", "5"]
    assert {t.external_id for t in result.tenders} == {
        "1",
        "2",
        "3",
        "4",
        "5",
    }


async def test_fetch_no_known_ids_fetches_all() -> None:
    listing = _five_row_listing()
    detail_html = _build_detail_html()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, html=listing)
        if _is_detail(request):
            return httpx.Response(200, html=detail_html)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = NationalBankConnector(http_client_factory=_client_factory(transport))
    # No hint at all → existing behavior; every listing row gets a
    # detail GET.
    result = await connector.fetch_latest()

    detail_calls = [r for r in captured if _is_detail(r)]
    assert len(detail_calls) == 5
    assert len(result.tenders) == 5


async def test_fetch_empty_known_ids_fetches_all() -> None:
    listing = _five_row_listing()
    detail_html = _build_detail_html()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, html=listing)
        if _is_detail(request):
            return httpx.Response(200, html=detail_html)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = NationalBankConnector(http_client_factory=_client_factory(transport))
    # Explicit empty set should be equivalent to None for our purposes.
    result = await connector.fetch_latest(known_external_ids=set())

    detail_calls = [r for r in captured if _is_detail(r)]
    assert len(detail_calls) == 5
    assert len(result.tenders) == 5


async def test_fetch_known_ids_does_not_log_skip_summary(
    captured_logs: list[dict[str, object]],
) -> None:
    listing = _five_row_listing()
    detail_html = _build_detail_html()

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(200, html=listing)
        if _is_detail(request):
            return httpx.Response(200, html=detail_html)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = NationalBankConnector(http_client_factory=_client_factory(transport))
    await connector.fetch_latest(known_external_ids={"1", "2", "3"})

    skip_events = [
        log
        for log in captured_logs
        if log.get("event") == "national_bank.detail_skipped_known"
    ]
    assert skip_events == []
