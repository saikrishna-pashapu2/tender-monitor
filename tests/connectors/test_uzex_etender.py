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
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "uzex_etender"


def _read_fixture() -> list[dict[str, Any]]:
    raw = (FIXTURES_DIR / "listing.json").read_text(encoding="utf-8")
    data: list[dict[str, Any]] = json.loads(raw)
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
    return request.url.path == LISTING_PATH and request.method == "POST"


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
    with pytest.raises(ParseError, match="empty name"):
        UzexEtenderConnector()._normalize(bad)

    bad2 = copy.deepcopy(items[0])
    bad2["name"] = None
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
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that serves successive listing pages in order.

    Pages are matched by ``From`` so the handler doesn't depend on the
    connector calling them sequentially -- only on the connector
    asking for the right offsets.
    """

    pages_by_from: dict[int, list[dict[str, Any]]] = {}
    for idx, page in enumerate(pages):
        pages_by_from[idx * UzexEtenderConnector.PAGE_SIZE + 1] = page

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if not _is_listing(request):
            return httpx.Response(404)
        body = _body_json(request)
        from_offset = int(body.get("From", 0))
        page = pages_by_from.get(from_offset, [])
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

    listing_calls = [r for r in captured if _is_listing(r)]
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

    listing_calls = [r for r in captured if _is_listing(r)]
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

    listing_calls = [r for r in captured if _is_listing(r)]
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
    new_template["start_date"] = "2026-06-01T10:00:00"  # > since

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

    listing_calls = [r for r in captured if _is_listing(r)]
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


async def test_fetch_latest_500_raises_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = UzexEtenderConnector(http_client_factory=_client_factory(transport))

    with pytest.raises(FetchError):
        await connector.fetch_latest()
