"""Tests for the UZEX e-Tender connector.

All offline; HTTP is exercised through ``httpx.MockTransport`` so the
suite is deterministic. The fixture under
``tests/fixtures/uzex_etender/listing.json`` carries 5 representative
items: bilingual ru/uz titles, one Uzbek-only title, mixed regions,
and UZS amounts.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.http import make_client
from tender_monitor.connectors.uzex_etender import (
    UzexEtenderConnector,
    parse_naive_tashkent,
)
from tender_monitor.core.enums import Country, Language, TenderStatus

LISTING_PATH = "/api/common/TradeList"
DEALS_PATH = "/api/common/DealsList"
NOT_DEALED_PATH = "/api/common/NotDealedList"
DETAIL_PATH_PREFIX = "/api/common/GetTrade/"
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "uzex_etender"


def _read_fixture() -> list[dict[str, Any]]:
    raw = (FIXTURES_DIR / "listing.json").read_text(encoding="utf-8")
    data: list[dict[str, Any]] = json.loads(raw)
    return data


def _read_detail_fixture(name: str) -> dict[str, Any]:
    raw = (FIXTURES_DIR / name).read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


def _client_factory(
    transport: httpx.MockTransport,
) -> Callable[[], httpx.AsyncClient]:
    def _make() -> httpx.AsyncClient:
        return make_client(
            headers=UzexEtenderConnector.REQUIRED_HEADERS, transport=transport
        )

    return _make


def _is_listing(request: httpx.Request) -> bool:
    return (
        request.url.path in {LISTING_PATH, DEALS_PATH, NOT_DEALED_PATH}
        and request.method == "POST"
    )


def _is_active_listing(request: httpx.Request) -> bool:
    return request.url.path == LISTING_PATH and request.method == "POST"


def _is_detail(request: httpx.Request) -> bool:
    return request.url.path.startswith(DETAIL_PATH_PREFIX) and request.method == "GET"


def _detail_lot_id(request: httpx.Request) -> int:
    return int(request.url.path[len(DETAIL_PATH_PREFIX) :].split("/", 1)[0])


def _body_json(request: httpx.Request) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(request.content.decode("utf-8"))
    return payload


# ---------------------------------------------------------------------------
# Date parser
# ---------------------------------------------------------------------------


def test_parse_naive_tashkent_converts_to_utc() -> None:
    result = parse_naive_tashkent("2026-05-10T12:14:02")
    assert result is not None
    # Asia/Tashkent is UTC+5, so 12:14 KZ-equivalent local -> 07:14 UTC.
    assert result == datetime(2026, 5, 10, 7, 14, 2, tzinfo=UTC)
    assert result.tzinfo is UTC


@pytest.mark.parametrize("text", [None, "", "   ", "not a date", "garbage-string"])
def test_parse_naive_tashkent_handles_empty(text: str | None) -> None:
    assert parse_naive_tashkent(text) is None


def test_parse_naive_tashkent_respects_offset_if_present() -> None:
    # Defensive: if the API ever starts sending offsets, trust them.
    result = parse_naive_tashkent("2026-05-10T12:14:02+00:00")
    assert result == datetime(2026, 5, 10, 12, 14, 2, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_happy_path() -> None:
    items = _read_fixture()
    upsert = UzexEtenderConnector()._normalize(items[0])

    assert upsert.source_name == "uzex_etender"
    assert upsert.external_id == "488134"
    assert upsert.title.startswith("Поставка металлопроката")
    assert " / " in upsert.title  # bilingual ru/uz separator
    assert upsert.buyer_name == 'АО "Узметкомбинат"'
    assert upsert.buyer_external_id == "200460222"
    assert upsert.country is Country.UZ
    assert upsert.value_amount == Decimal("196428571.25")
    assert upsert.value_currency == "UZS"
    assert upsert.published_at is not None
    assert upsert.published_at.tzinfo is UTC
    # 2026-05-10T12:14:02 Tashkent (UTC+5) -> 2026-05-10T07:14:02 UTC.
    assert upsert.published_at == datetime(2026, 5, 10, 7, 14, 2, tzinfo=UTC)
    assert upsert.deadline_at is not None
    assert upsert.deadline_at.tzinfo is UTC
    assert upsert.status is TenderStatus.open
    assert upsert.language is Language.ru
    assert upsert.source_url == "https://etender.uzex.uz/lot/488134"
    lots = upsert.raw_json["_lots"]
    assert len(lots) == 1
    assert lots[0]["name_ru"] == items[0]["name"]


def test_normalize_missing_name_raises() -> None:
    items = _read_fixture()
    bad = copy.deepcopy(items[0])
    bad["name"] = ""
    bad["category_name"] = ""
    with pytest.raises(ParseError, match="empty name"):
        UzexEtenderConnector()._normalize(bad)

    bad2 = copy.deepcopy(items[0])
    bad2["name"] = None
    bad2["category_name"] = None
    with pytest.raises(ParseError, match="empty name"):
        UzexEtenderConnector()._normalize(bad2)


def test_normalize_handles_uzbek_only_title() -> None:
    items = _read_fixture()
    # Item index 2 is the Uzbek-only title in the fixture.
    uzbek_only = items[2]
    assert "/" not in uzbek_only["name"]  # sanity
    upsert = UzexEtenderConnector()._normalize(uzbek_only)
    assert upsert.title == uzbek_only["name"]
    assert upsert.country is Country.UZ
    # Default language is still ru -- per-row detection is out of scope.
    assert upsert.language is Language.ru


def test_normalize_handles_null_cost() -> None:
    items = _read_fixture()
    bad = copy.deepcopy(items[0])
    bad["cost"] = None
    upsert = UzexEtenderConnector()._normalize(bad)
    assert upsert.value_amount is None
    assert upsert.value_currency is None


# ---------------------------------------------------------------------------
# Fetch pipeline -- MockTransport, no real HTTP
# ---------------------------------------------------------------------------


def _make_handler(
    *,
    pages: list[list[dict[str, Any]]],
    captured: list[httpx.Request],
    details_by_id: dict[int, dict[str, Any]] | None = None,
    pages_by_type: dict[int, list[list[dict[str, Any]]]] | None = None,
    deals_pages: list[list[dict[str, Any]]] | None = None,
    deals_pages_by_type: dict[int, list[list[dict[str, Any]]]] | None = None,
    not_dealed_pages: list[list[dict[str, Any]]] | None = None,
    not_dealed_pages_by_type: dict[
        int, list[list[dict[str, Any]]]
    ] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that serves successive listing pages in order.

    Pages are matched by ``From`` so the handler doesn't depend on the
    connector calling them sequentially -- only on the connector
    asking for the right offsets.
    """

    def _index_pages(
        stream_pages: list[list[dict[str, Any]]],
    ) -> dict[int, list[dict[str, Any]]]:
        pages_by_from: dict[int, list[dict[str, Any]]] = {}
        for idx, page in enumerate(stream_pages):
            pages_by_from[idx * UzexEtenderConnector.PAGE_SIZE + 1] = page
        return pages_by_from

    def _index_type_pages(
        typed_pages: dict[int, list[list[dict[str, Any]]]] | None,
        *,
        default_pages: list[list[dict[str, Any]]],
    ) -> dict[int, dict[int, list[dict[str, Any]]]]:
        indexed: dict[int, dict[int, list[dict[str, Any]]]] = {}
        if typed_pages:
            for type_id, stream_pages in typed_pages.items():
                indexed[type_id] = _index_pages(stream_pages)
        if 1 not in indexed:
            indexed[1] = _index_pages(default_pages)
        return indexed

    pages_by_path: dict[str, dict[int, dict[int, list[dict[str, Any]]]]] = {
        LISTING_PATH: _index_type_pages(
            pages_by_type,
            default_pages=pages,
        ),
        DEALS_PATH: _index_type_pages(
            deals_pages_by_type,
            default_pages=deals_pages or [[]],
        ),
        NOT_DEALED_PATH: _index_type_pages(
            not_dealed_pages_by_type,
            default_pages=not_dealed_pages or [[]],
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if not _is_listing(request):
            if _is_detail(request):
                lot_id = _detail_lot_id(request)
                detail = (details_by_id or {}).get(lot_id)
                if detail is None:
                    return httpx.Response(404)
                return httpx.Response(200, json=detail)
            return httpx.Response(404)
        body = _body_json(request)
        from_offset = int(body.get("From", 0))
        type_id = int(body.get("TypeId", 1))
        page = (
            pages_by_path.get(request.url.path, {})
            .get(type_id, {})
            .get(from_offset, [])
        )
        return httpx.Response(200, json=page)

    return handler


async def test_fetch_latest_full_pipeline() -> None:
    items = _read_fixture()
    captured: list[httpx.Request] = []
    handler = _make_handler(pages=[items, []], captured=captured)
    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    result = await connector.fetch_latest()

    assert result.source_name == "uzex_etender"
    assert result.raw_item_count == len(items)
    assert len(result.tenders) == len(items)
    assert result.partial_errors == []
    assert all(t.country is Country.UZ for t in result.tenders)
    assert all(t.value_currency == "UZS" for t in result.tenders)


async def test_fetch_latest_pagination() -> None:
    # Build 50 + 30 + 0 across three pages.
    item_template = _read_fixture()[0]

    def _clone(rn: int, item_id: int) -> dict[str, Any]:
        clone = copy.deepcopy(item_template)
        clone["rn"] = rn
        clone["id"] = item_id
        return clone

    page1 = [_clone(rn=i + 1, item_id=900000 + i) for i in range(50)]
    page2 = [_clone(rn=i + 51, item_id=901000 + i) for i in range(30)]

    captured: list[httpx.Request] = []
    handler = _make_handler(pages=[page1, page2, []], captured=captured)
    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    result = await connector.fetch_latest()

    listing_calls = [
        r
        for r in captured
        if _is_active_listing(r) and _body_json(r).get("TypeId") == 1
    ]
    # Page 2 was a short page (30 < 50), so the connector breaks
    # before requesting page 3. Two POSTs total.
    assert len(listing_calls) == 2
    assert result.raw_item_count == 80


async def test_fetch_latest_post_body_shape() -> None:
    items = _read_fixture()
    item_template = items[0]

    page1 = [copy.deepcopy(item_template) for _ in range(50)]
    for i, it in enumerate(page1):
        it["id"] = 800000 + i
    page2 = [copy.deepcopy(item_template) for _ in range(50)]
    for i, it in enumerate(page2):
        it["id"] = 801000 + i

    captured: list[httpx.Request] = []
    handler = _make_handler(pages=[page1, page2, []], captured=captured)
    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_active_listing(r)]
    assert len(listing_calls) >= 2
    body1 = _body_json(listing_calls[0])
    body2 = _body_json(listing_calls[1])
    assert body1 == {"TypeId": 1, "System_Id": 0, "From": 1, "To": 50}
    assert body2 == {"TypeId": 1, "System_Id": 0, "From": 51, "To": 100}


async def test_fetch_latest_no_validation_header() -> None:
    captured: list[httpx.Request] = []
    handler = _make_handler(pages=[[]], captured=captured)
    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_active_listing(r)]
    assert listing_calls, "expected at least one listing request"
    # The v1 contract: do NOT send the captured validation token.
    # If the live API rejects empty-validation we'll find out from the
    # live acceptance run, not by silently shipping the captured token.
    header_keys = [k.lower() for k in listing_calls[0].headers]
    assert "validation" not in header_keys


async def test_fetch_latest_since_filter_after_pagination() -> None:
    # Build two pages where every item on page 1 is OLDER than `since`
    # and every item on page 2 is NEWER. Early-stop-on-old would drop
    # the page-2 items; we want to assert that does NOT happen.
    old_template = copy.deepcopy(_read_fixture()[0])
    new_template = copy.deepcopy(_read_fixture()[0])
    old_template["start_date"] = "2026-04-01T10:00:00"  # < since
    old_template["end_date"] = "2026-04-02T10:00:00"  # < since
    new_template["start_date"] = "2026-06-01T10:00:00"  # > since
    new_template["end_date"] = "2026-06-02T10:00:00"  # > since

    page1 = []
    for i in range(50):
        clone = copy.deepcopy(old_template)
        clone["id"] = 700000 + i
        clone["rn"] = i + 1
        page1.append(clone)

    page2 = []
    for i in range(20):
        clone = copy.deepcopy(new_template)
        clone["id"] = 701000 + i
        clone["rn"] = i + 51
        page2.append(clone)

    captured: list[httpx.Request] = []
    handler = _make_handler(pages=[page1, page2, []], captured=captured)
    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    since = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    result = await connector.fetch_latest(since=since)

    listing_calls = [r for r in captured if _is_active_listing(r)]
    # CRITICAL: page 2 was requested even though every item on page 1
    # was older than `since`. This is the non-monotone-since pin.
    pages_requested = [_body_json(r).get("From") for r in listing_calls]
    assert 1 in pages_requested
    assert 51 in pages_requested
    # Only the new-page items survive the filter.
    assert len(result.tenders) == 20
    assert all(
        t.published_at is not None and t.published_at >= since
        for t in result.tenders
    )


async def test_fetch_latest_enriches_with_detail_payload() -> None:
    item = copy.deepcopy(_read_fixture()[0])
    item["id"] = 482604
    item["name"] = "Закупка консалтинговых услуг"
    item["seller_name"] = "Listing Seller"
    item["seller_tin"] = "123456789"
    item["start_date"] = "2026-05-15T12:36:31"
    item["end_date"] = "2026-05-22T12:36:31"
    item["cost"] = 100.0
    item["currency_codeabc"] = "UZS"

    detail = _read_detail_fixture("detail_482604.json")

    captured: list[httpx.Request] = []
    handler = _make_handler(
        pages=[[item]],
        captured=captured,
        details_by_id={482604: detail},
    )
    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    result = await connector.fetch_latest()

    assert result.raw_item_count == 1
    assert len(result.tenders) == 1
    tender = result.tenders[0]
    assert tender.buyer_name == "АО O`ZBEKTELEKOM"
    assert tender.buyer_external_id == "203366731"
    assert tender.value_amount == Decimal("2476000000.0")
    assert tender.status is TenderStatus.closed
    assert tender.raw_json["_detail"]["id"] == 482604
    assert len(tender.raw_json["_documents"]) == 5
    assert tender.raw_json["_documents"][0]["url"].startswith(
        "https://xarid.uzex.uz/x-cloud?file_path="
    )
    assert any(
        doc["name"] == "202604233530012202.pdf"
        and doc["ext"] == "PDF"
        for doc in tender.raw_json["_documents"]
    )
    assert (
        tender.raw_json["_parsed_detail"]["budget_products"][0]["Description"]
        .lower()
        .find("dekarbonizatsiya")
        >= 0
    )
    assert "iqlim strategiyasini" in (
        tender.raw_json["_lots"][0]["description_ru"] or ""
    ).lower()


async def test_fetch_latest_fetches_detail_only_for_in_window_items() -> None:
    template = _read_fixture()[0]

    old_item = copy.deepcopy(template)
    old_item["id"] = 600001
    old_item["start_date"] = "2026-04-01T10:00:00"
    old_item["end_date"] = "2026-04-02T10:00:00"

    new_item = copy.deepcopy(template)
    new_item["id"] = 600002
    new_item["start_date"] = "2026-06-01T10:00:00"
    new_item["end_date"] = "2026-06-02T10:00:00"

    detail = _read_detail_fixture("detail_484751.json")
    detail["id"] = 600002
    captured: list[httpx.Request] = []
    handler = _make_handler(
        pages=[[old_item, new_item]],
        captured=captured,
        details_by_id={600001: detail, 600002: detail},
    )
    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    since = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    result = await connector.fetch_latest(since=since)

    detail_calls = [r for r in captured if _is_detail(r)]
    assert result.raw_item_count == 1
    assert len(detail_calls) == 1
    assert _detail_lot_id(detail_calls[0]) == 600002


async def test_fetch_latest_includes_recent_not_dealed_items() -> None:
    active_item = copy.deepcopy(_read_fixture()[0])
    active_item["id"] = 700001
    active_item["start_date"] = "2026-05-20T10:00:00"
    active_item["end_date"] = "2026-05-22T10:00:00"

    failed_item = {
        "trade_id": 700002,
        "display_no": "26110012700002",
        "start_date": "2026-05-01T08:00:00",
        "end_date": "2026-05-22T17:44:38",
        "category_name": "Iqlim strategiyasi bo'yicha xizmatlar",
        "start_cost": 1000000.0,
        "customer_name": "Test Customer",
        "customer_inn": "123456789",
        "status_id": 12,
        "status_name": "Торг не состоялся",
    }

    active_detail = _read_detail_fixture("detail_482604.json")
    active_detail["id"] = 700001
    failed_detail = copy.deepcopy(active_detail)
    failed_detail["id"] = 700002
    captured: list[httpx.Request] = []
    handler = _make_handler(
        pages=[[active_item]],
        not_dealed_pages=[[failed_item]],
        captured=captured,
        details_by_id={700001: active_detail, 700002: failed_detail},
    )
    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    since = datetime(2026, 5, 21, 0, 0, tzinfo=UTC)
    result = await connector.fetch_latest(since=since)

    assert result.raw_item_count == 2
    assert {t.external_id for t in result.tenders} == {"700001", "700002"}
    failed_tender = next(t for t in result.tenders if t.external_id == "700002")
    assert failed_tender.status is TenderStatus.cancelled


async def test_fetch_latest_discovers_competition_lane_items() -> None:
    competition_item = {
        "id": 482604,
        "display_no": "26120012482604",
        "name": "Услуга по разработке климатической стратегии",
        "seller_name": "АО O`ZBEKTELEKOM",
        "seller_tin": "203366731",
        "start_date": "2026-05-15T12:36:00",
        "end_date": "2026-05-22T12:36:00",
        "cost": 2476000000.0,
        "currency_codeabc": "UZS",
        "status_id": 6,
        "status_name": "Application deadline has ended",
    }
    detail = _read_detail_fixture("detail_482604.json")

    captured: list[httpx.Request] = []
    handler = _make_handler(
        pages=[[]],
        pages_by_type={2: [[competition_item]]},
        captured=captured,
        details_by_id={482604: detail},
    )
    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    result = await connector.fetch_latest()

    assert result.raw_item_count == 1
    assert [t.external_id for t in result.tenders] == ["482604"]
    assert result.tenders[0].status is TenderStatus.closed
    listing_bodies = [_body_json(request) for request in captured if _is_active_listing(request)]
    assert {"TypeId": 1, "System_Id": 0, "From": 1, "To": 50} in listing_bodies
    assert {"TypeId": 2, "System_Id": 0, "From": 1, "To": 50} in listing_bodies


async def test_fetch_latest_backfills_beyond_default_active_page_cap() -> None:
    template = copy.deepcopy(_read_fixture()[0])
    old_pages: list[list[dict[str, Any]]] = []
    for page_index in range(UzexEtenderConnector.ACTIVE_MAX_PAGES):
        page: list[dict[str, Any]] = []
        for item_index in range(UzexEtenderConnector.PAGE_SIZE):
            clone = copy.deepcopy(template)
            clone["id"] = 800000 + page_index * 100 + item_index
            clone["start_date"] = "2026-04-01T10:00:00"
            page.append(clone)
        old_pages.append(page)

    target_item = copy.deepcopy(template)
    target_item["id"] = 482604
    target_item["display_no"] = "26120012482604"
    target_item["start_date"] = "2026-05-15T12:36:31"
    target_item["end_date"] = "2026-05-22T12:36:31"
    target_item["cost"] = 2476000000.0
    target_item["currency_codeabc"] = "UZS"

    page_16 = [target_item]
    detail = _read_detail_fixture("detail_482604.json")

    captured: list[httpx.Request] = []
    handler = _make_handler(
        pages=[*old_pages, page_16],
        captured=captured,
        details_by_id={482604: detail},
    )
    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    since = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    result = await connector.fetch_latest(since=since)

    assert "482604" in {t.external_id for t in result.tenders}
    active_listing_bodies = [
        _body_json(request) for request in captured if _is_active_listing(request)
    ]
    assert {"TypeId": 1, "System_Id": 0, "From": 751, "To": 800} in active_listing_bodies


async def test_fetch_latest_500_raises_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    with pytest.raises(FetchError):
        await connector.fetch_latest()
