from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from selectolax.parser import HTMLParser

from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.http import make_client
from tender_monitor.connectors.mitwork import (
    KZ_TZ,
    STATUS_MAPPING,
    MitworkConnector,
    _parse_detail_page,
    _parse_row,
    parse_kz_local_datetime,
    parse_kzt_amount,
)
from tender_monitor.core.enums import Country, Language, TenderStatus

LISTING_PATH = "/ru/publics/buys"
DETAIL_PATH_PREFIX = "/ru/publics/buy/"

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "mitwork"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _client_factory(
    transport: httpx.MockTransport,
) -> Callable[[], httpx.AsyncClient]:
    def _make() -> httpx.AsyncClient:
        return make_client(
            headers=MitworkConnector.REQUIRED_HEADERS, transport=transport
        )

    return _make


def _is_listing(request: httpx.Request) -> bool:
    return request.url.path == LISTING_PATH


def _is_detail(request: httpx.Request) -> bool:
    return request.url.path.startswith(DETAIL_PATH_PREFIX)


def _wrap_rows_as_listing(rows_html: str) -> str:
    """Wrap one or more <tr class="item"> snippets into a minimal page."""
    return (
        '<html><body><table class="table table-striped table-bordered">'
        f"<tbody>{rows_html}</tbody></table></body></html>"
    )


SHORT_LISTING_HTML = _wrap_rows_as_listing(
    """
    <tr class="item" data-key="100001">
      <td class="col-sm-2">100001<br><span class="label label-default">Лотов: 1</span></td>
      <td class="col-sm-4"><a class="word-break" href="https://eep.mitwork.kz/ru/publics/buy/100001">Тестовый закуп один</a></td>
      <td class="text-right text-nowrap">10 000,00 KZT</td>
      <td class="hidden-xs">Тендер</td>
      <td class="hidden-xs">2026-05-12 10:00:00</td>
      <td class="hidden-xs">2026-05-22 10:00:00</td>
      <td class="hidden-xs"><a href="https://eep.mitwork.kz/ru/publics/subject/1" title="ТОО Тест Один">000000000001</a></td>
      <td class="hidden-xs">Опубликовано</td>
    </tr>
    <tr class="item" data-key="100002">
      <td class="col-sm-2">100002<br><span class="label label-default">Лотов: 1</span></td>
      <td class="col-sm-4"><a class="word-break" href="https://eep.mitwork.kz/ru/publics/buy/100002">Тестовый закуп два</a></td>
      <td class="text-right text-nowrap">20 000,00 KZT</td>
      <td class="hidden-xs">Тендер</td>
      <td class="hidden-xs">2026-05-12 11:00:00</td>
      <td class="hidden-xs">2026-05-22 11:00:00</td>
      <td class="hidden-xs"><a href="https://eep.mitwork.kz/ru/publics/subject/2" title="ТОО Тест Два">000000000002</a></td>
      <td class="hidden-xs">Опубликовано</td>
    </tr>
    """
)


EMPTY_LISTING_HTML = (
    '<html><body><table class="table"><tbody></tbody></table></body></html>'
)
DETAIL_HTML = _read_fixture("buy_192049.html")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_parse_kzt_amount_variants() -> None:
    # The live fixture uses no-break space ( ).
    assert parse_kzt_amount("16 900,00 KZT") == Decimal("16900.00")
    # Multi-thousands grouping with NBSP.
    assert parse_kzt_amount("1 164 620,82 KZT") == Decimal(
        "1164620.82"
    )
    # Regular spaces (the spec asks for the missing variants to be covered).
    assert parse_kzt_amount("46 347,00 KZT") == Decimal("46347.00")
    # Narrow no-break space (U+202F).
    assert parse_kzt_amount("2 864 224,19 KZT") == Decimal(
        "2864224.19"
    )
    # Zero amount.
    assert parse_kzt_amount("0,00 KZT") == Decimal("0.00")
    # Empty / garbage / None.
    assert parse_kzt_amount("") is None
    assert parse_kzt_amount(None) is None
    assert parse_kzt_amount("не указана") is None
    assert parse_kzt_amount("abc") is None


def test_parse_kz_local_datetime_converts_to_utc() -> None:
    result = parse_kz_local_datetime("2026-05-12 15:10:00")
    assert result is not None
    assert result == datetime(2026, 5, 12, 10, 10, 0, tzinfo=UTC)
    assert result.tzinfo == UTC


def test_parse_kz_local_datetime_handles_empty() -> None:
    assert parse_kz_local_datetime(None) is None
    assert parse_kz_local_datetime("") is None
    assert parse_kz_local_datetime("   ") is None
    # Garbage that doesn't match the expected format.
    assert parse_kz_local_datetime("not a date") is None


def test_status_mapping() -> None:
    assert STATUS_MAPPING["Опубликовано"] is TenderStatus.open
    # Unknown / empty / None fall through.
    assert (
        STATUS_MAPPING.get("Завершено", TenderStatus.unknown)
        is TenderStatus.unknown
    )
    assert STATUS_MAPPING.get("", TenderStatus.unknown) is TenderStatus.unknown


# ---------------------------------------------------------------------------
# Row parsing & normalization
# ---------------------------------------------------------------------------


def test_parse_row_extracts_all_fields() -> None:
    html = _read_fixture("listing.html")
    rows = HTMLParser(html).css("tr.item")
    assert rows, "listing fixture must have at least one tr.item"
    first = rows[0]

    parsed = _parse_row(first)

    # data_key matches the actual attribute.
    assert parsed["data_key"] == first.attributes.get("data-key")
    assert parsed["data_key"]  # non-empty

    # title_ru is non-empty Cyrillic text.
    assert isinstance(parsed["title_ru"], str)
    assert parsed["title_ru"].strip()
    assert any("Ѐ" <= ch <= "ӿ" for ch in parsed["title_ru"])

    # detail_url is the absolute MITWORK buy URL.
    assert parsed["detail_url"].startswith("https://eep.mitwork.kz/ru/publics/buy/")

    # value_text contains the KZT currency token.
    assert "KZT" in parsed["value_text"]

    # buyer_bin is a 12-digit number (BIN).
    assert parsed["buyer_bin"] is not None
    assert parsed["buyer_bin"].isdigit() and len(parsed["buyer_bin"]) == 12

    # buyer_name and subject_url are populated.
    assert parsed["buyer_name"]
    assert parsed["subject_url"].startswith(
        "https://eep.mitwork.kz/ru/publics/subject/"
    )

    # status_text is non-empty.
    assert parsed["status_text"]

    # offer_start_local is a timezone-aware Almaty datetime.
    start = parsed["offer_start_local"]
    assert isinstance(start, datetime)
    assert start.tzinfo is not None
    # Round-tripping back to Asia/Almaty should leave us in that zone.
    assert start.astimezone(KZ_TZ).tzinfo == KZ_TZ


def test_normalize_happy_path() -> None:
    html = _read_fixture("listing.html")
    rows = HTMLParser(html).css("tr.item")
    raw = _parse_row(rows[0])

    upsert = MitworkConnector()._normalize(raw)

    assert upsert.source_name == "mitwork"
    assert upsert.external_id == raw["data_key"]
    assert upsert.title == raw["title_ru"]
    assert upsert.buyer_name == raw["buyer_name"]
    assert upsert.buyer_external_id == raw["buyer_bin"]
    assert upsert.country is Country.KZ
    assert upsert.value_currency == "KZT"
    assert isinstance(upsert.value_amount, Decimal)
    assert upsert.value_amount > 0
    assert upsert.published_at is not None
    assert upsert.published_at.tzinfo == UTC
    assert upsert.deadline_at is not None
    assert upsert.deadline_at.tzinfo == UTC
    assert upsert.status is TenderStatus.open
    assert upsert.language is Language.ru
    assert upsert.source_url.startswith("https://eep.mitwork.kz/ru/publics/buy/")
    # raw_json contains the parsed row but with the non-JSON-safe
    # offer_start_local convenience field stripped.
    assert upsert.raw_json["data_key"] == raw["data_key"]
    assert "offer_start_local" not in upsert.raw_json


def test_normalize_missing_title_raises() -> None:
    raw = {
        "data_key": "999",
        "announcement_number": "999",
        "lots_label": None,
        "title_ru": "",
        "detail_url": "https://eep.mitwork.kz/ru/publics/buy/999",
        "value_text": "100,00 KZT",
        "procurement_method": "Тендер",
        "offer_start_text": "2026-05-12 10:00:00",
        "offer_end_text": "2026-05-22 10:00:00",
        "buyer_name": "Test Buyer",
        "buyer_bin": "123456789012",
        "subject_url": "",
        "status_text": "Опубликовано",
        "offer_start_local": None,
    }

    with pytest.raises(ParseError, match="empty title"):
        MitworkConnector()._normalize(raw)


def test_parse_detail_page_extracts_fields_documents_and_lots() -> None:
    parsed = _parse_detail_page(DETAIL_HTML)

    detail_fields = parsed["detail_fields"]
    assert detail_fields["title_ru_detail"].startswith("Химическая обработка")
    assert detail_fields["organizer_name"].startswith(
        "ТОВАРИЩЕСТВО С ОГРАНИЧЕННОЙ"
    )
    assert detail_fields["organizer_url"].startswith(
        "https://eep.mitwork.kz/ru/publics/subject/"
    )
    assert detail_fields["status_text_detail"] == "Опубликовано"

    documents = parsed["_documents"]
    assert len(documents) == 1
    assert documents[0]["category"] == "Проекты договоров"
    assert documents[0]["name"] == "contract_project_s_2026_191748_v2.pdf"
    assert documents[0]["url"].startswith(
        "https://eep.mitwork.kz/ru/files/download/"
    )
    assert documents[0]["ext"] == "PDF"

    lots = parsed["_lots"]
    assert len(lots) == 1
    assert lots[0]["number"] == "694789-ОИ2"
    assert lots[0]["classification_code"] == "016110.510.000001"
    assert lots[0]["name_ru"].startswith("Услуги по обработке территорий")
    assert lots[0]["description_ru"].startswith("Химическая обработка")
    assert lots[0]["currency"] == "KZT"
    assert lots[0]["total_amount"] == Decimal("2000000.00")


# ---------------------------------------------------------------------------
# Fetch-pipeline tests — MockTransport, no real HTTP
# ---------------------------------------------------------------------------


async def test_fetch_latest_full_pipeline() -> None:
    html = _read_fixture("listing.html")
    expected_rows = len(HTMLParser(html).css("tr.item"))
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            # First page returns the fixture; subsequent pages are empty
            # so the connector terminates after walking the fixture once.
            page = request.url.params.get("page")
            if page in (None, "1"):
                return httpx.Response(200, html=html)
            return httpx.Response(200, html=EMPTY_LISTING_HTML)
        if _is_detail(request):
            return httpx.Response(200, html=DETAIL_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = MitworkConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    assert result.source_name == "mitwork"
    assert result.raw_item_count == expected_rows
    assert len(result.tenders) == expected_rows
    assert result.partial_errors == []
    assert result.fetched_at.tzinfo is UTC
    assert result.duration_ms > 0

    listing_calls = [r for r in captured if _is_listing(r)]
    assert len(listing_calls) >= 1
    detail_calls = [r for r in captured if _is_detail(r)]
    assert len(detail_calls) == expected_rows
    assert all(tender.raw_json.get("_documents") for tender in result.tenders)
    assert all(tender.raw_json.get("_lots") for tender in result.tenders)


async def test_fetch_latest_paginates_until_since() -> None:
    html = _read_fixture("listing.html")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, html=html)
        if _is_detail(request):
            return httpx.Response(200, html=DETAIL_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = MitworkConnector(http_client_factory=_client_factory(transport))

    # Pick a cutoff that falls inside the first listing page so at least
    # one row gets filtered out, which is what triggers the break.
    # Row data-key=192075 starts 2026-05-12 15:30 KZ = 10:30 UTC, and
    # rows around it cover 15:00–16:00 KZ. since=11:00 UTC = 16:00 KZ
    # filters out the earlier of the bunch and triggers the break.
    since = datetime(2026, 5, 12, 11, 0, 0, tzinfo=UTC)
    result = await connector.fetch_latest(since=since)

    listing_calls = [r for r in captured if _is_listing(r)]
    pages_called = [
        request.url.params.get("page") for request in listing_calls
    ]
    # Page 1 was fetched; page 2 must NOT have been.
    assert "2" not in pages_called
    # And we got at least some rows through (those after the cutoff).
    assert len(result.tenders) > 0


async def test_fetch_latest_stops_on_short_page() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, html=SHORT_LISTING_HTML)
        if _is_detail(request):
            return httpx.Response(200, html=DETAIL_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = MitworkConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_listing(r)]
    pages_called = [
        request.url.params.get("page") for request in listing_calls
    ]
    assert len(listing_calls) == 1
    assert "2" not in pages_called
    assert len(result.tenders) == 2


async def test_fetch_latest_500_raises_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = MitworkConnector(http_client_factory=_client_factory(transport))

    with pytest.raises(FetchError):
        await connector.fetch_latest()


async def test_fetch_latest_empty_listing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(200, html=EMPTY_LISTING_HTML)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = MitworkConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    assert result.raw_item_count == 0
    assert result.tenders == []
    assert result.partial_errors == []


async def test_fetch_latest_continues_when_detail_fetch_fails() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, html=SHORT_LISTING_HTML)
        if _is_detail(request):
            return httpx.Response(500, text="detail failed")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = MitworkConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    assert len(result.tenders) == 2
    assert result.partial_errors == []
    assert all(
        "_detail_fetch_error" in tender.raw_json for tender in result.tenders
    )


def test_zoneinfo_almaty_offset_is_plus_five() -> None:
    """Sanity-check: the host's tzdata understands Asia/Almaty as UTC+5."""
    moment = datetime(2026, 5, 12, 12, 0, 0, tzinfo=ZoneInfo("Asia/Almaty"))
    offset = moment.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 5 * 3600
