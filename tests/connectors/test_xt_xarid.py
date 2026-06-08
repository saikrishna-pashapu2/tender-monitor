"""Tests for the XT-Xarid connector (xt-xarid.uz, Uzbek state procurement).

All offline; HTTP is exercised through ``httpx.MockTransport``. The
fixture under ``tests/fixtures/xt_xarid/listing.json`` carries the
full JSON-RPC envelope including the three trimmed result items: a
12-good_maps food-services tender (the live #1), a 2-good_maps
office-furniture tender, and a 3-good_maps medical-equipment +
lab-supplies multi-lot tender.
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
from tender_monitor.connectors.xt_xarid import (
    STATUS_MAPPING,
    XtXaridConnector,
    _build_lots,
    _build_title,
    _parse_iso_maybe,
    map_status,
)
from tender_monitor.core.enums import Country, Language, TenderStatus
from tender_monitor.matching import KeywordsConfig, match_tender

RPC_PATH = "/rpc"
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "xt_xarid"
KEYWORDS_PATH = (
    Path(__file__).parent.parent.parent / "config" / "keywords.yaml"
)


def _read_fixture() -> dict[str, Any]:
    raw = (FIXTURES_DIR / "listing.json").read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


def _fixture_items() -> list[dict[str, Any]]:
    items = _read_fixture()["result"]
    assert isinstance(items, list)
    return items


def _client_factory(
    transport: httpx.MockTransport,
) -> Callable[[], httpx.AsyncClient]:
    def _make() -> httpx.AsyncClient:
        return make_client(
            headers=XtXaridConnector.REQUIRED_HEADERS, transport=transport
        )

    return _make


def _is_listing(request: httpx.Request) -> bool:
    return request.url.path == RPC_PATH and request.method == "POST"


def _body_json(request: httpx.Request) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(request.content.decode("utf-8"))
    return payload


# ---------------------------------------------------------------------------
# _build_title
# ---------------------------------------------------------------------------


def test_build_title_dedupes_good_maps() -> None:
    raw = _fixture_items()[0]
    # The fixture has 12 identical good_maps names; dedup should
    # collapse to one entry, no " | " in the result.
    assert raw["good_count"] == 12
    title = _build_title(raw)
    assert title == "Услуги по аутсорсингу приготовления еды"
    assert " | " not in title


def test_build_title_joins_distinct_names() -> None:
    raw = {
        "id": 1,
        "meta": {
            "good_maps": [
                {"name": "Alpha"},
                {"name": "Beta"},
                {"name": "Alpha"},  # dup
                {"name": "Gamma"},
                {"name": "  alpha  "},  # case-insensitive dup, stripped
            ]
        },
    }
    title = _build_title(raw)
    # First-seen order, deduped case-insensitively.
    assert title == "Alpha | Beta | Gamma"


def test_build_title_no_good_maps_raises() -> None:
    with pytest.raises(ParseError, match="no good_maps names"):
        _build_title({"id": 999, "meta": {"good_maps": []}})

    with pytest.raises(ParseError, match="no good_maps names"):
        _build_title({"id": 999, "meta": {}})

    with pytest.raises(ParseError, match="no good_maps names"):
        _build_title(
            {"id": 999, "meta": {"good_maps": [{"name": ""}, {"name": "   "}]}}
        )


def test_build_lots_dedupes_good_maps_and_keeps_fields() -> None:
    raw = _fixture_items()[0]
    lots = _build_lots(raw, _build_title(raw))

    assert len(lots) == 1
    assert lots[0]["name_ru"] == "Услуги по аутсорсингу приготовления еды"
    assert "lot_id" in lots[0]
    assert "quantity" in lots[0]
    assert "unit_price" in lots[0]
    assert "total_amount" in lots[0]


def test_build_lots_keeps_distinct_line_items() -> None:
    raw = _fixture_items()[2]
    lots = _build_lots(raw, _build_title(raw))

    names = {lot["name_ru"] for lot in lots}
    assert names == {
        "Поставка медицинского оборудования",
        "Расходные материалы для лабораторий",
    }


# ---------------------------------------------------------------------------
# _parse_iso_maybe / map_status
# ---------------------------------------------------------------------------


def test_parse_iso_maybe_naive_is_tashkent() -> None:
    result = _parse_iso_maybe("2026-05-10T12:14:02")
    assert result is not None
    # Tashkent UTC+5 → 12:14 local = 07:14 UTC.
    assert result == datetime(2026, 5, 10, 7, 14, 2, tzinfo=UTC)
    assert result.tzinfo is UTC


def test_parse_iso_maybe_offset_aware_respected() -> None:
    result = _parse_iso_maybe("2026-05-10T12:14:02+00:00")
    assert result == datetime(2026, 5, 10, 12, 14, 2, tzinfo=UTC)


@pytest.mark.parametrize("text", [None, "", "   ", "not a date"])
def test_parse_iso_maybe_returns_none_on_garbage(text: str | None) -> None:
    assert _parse_iso_maybe(text) is None


def test_map_status_known_and_unknown() -> None:
    assert STATUS_MAPPING["docs_objections"] is TenderStatus.announced
    assert map_status("docs_objections") is TenderStatus.announced
    assert map_status("brand_new_value") is TenderStatus.unknown
    assert map_status("") is TenderStatus.unknown
    assert map_status(None) is TenderStatus.unknown


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_happy_path() -> None:
    raw = _fixture_items()[0]
    upsert = XtXaridConnector()._normalize(raw)

    assert upsert.source_name == "xt_xarid"
    assert upsert.external_id == "7393512"
    assert upsert.title == "Услуги по аутсорсингу приготовления еды"
    assert upsert.buyer_name is not None
    assert "фтизиатрии" in upsert.buyer_name
    assert upsert.buyer_external_id == "200935928"
    assert upsert.country is Country.UZ
    assert upsert.value_amount == Decimal("5099982101.04")
    assert upsert.value_currency == "UZS"
    assert upsert.published_at is None
    assert upsert.deadline_at is None
    assert upsert.status is TenderStatus.announced
    assert upsert.source_url == "https://xt-xarid.uz/procedure/tender/7393512"
    assert upsert.language is Language.uz
    # Distinct good_maps rows are projected into _lots for the matcher.
    lots = upsert.raw_json["_lots"]
    assert len(lots) == 1
    assert lots[0]["name_ru"] == upsert.title
    assert "quantity" in lots[0]


def test_normalize_missing_title_raises() -> None:
    raw = copy.deepcopy(_fixture_items()[0])
    raw["meta"]["good_maps"] = []
    with pytest.raises(ParseError, match="no good_maps names"):
        XtXaridConnector()._normalize(raw)


def test_normalize_picks_ru_language_for_ru_lang() -> None:
    raw = _fixture_items()[1]
    assert raw["lang"] == "ru-RU"
    upsert = XtXaridConnector()._normalize(raw)
    assert upsert.language is Language.ru


def test_normalize_multi_lot_title_joins_distinct_names() -> None:
    raw = _fixture_items()[2]
    upsert = XtXaridConnector()._normalize(raw)
    assert " | " in upsert.title
    assert "Поставка медицинского оборудования" in upsert.title
    assert "Расходные материалы для лабораторий" in upsert.title
    assert len(upsert.raw_json["_lots"]) == 2


def test_normalize_good_maps_description_matches_keyword_filter() -> None:
    raw = copy.deepcopy(_fixture_items()[0])
    raw["id"] = 7777777
    raw["meta"]["good_maps"] = [
        {
            "lot_id": 1,
            "id": 100,
            "name": "General consulting services",
            "description": "ESG reporting and sustainability report preparation",
            "amount": 1,
            "unit": "service",
            "price": 1000,
            "totalcost_item": 1000,
        }
    ]
    upsert = XtXaridConnector()._normalize(raw)

    assert upsert.title == "General consulting services"
    assert (
        upsert.raw_json["_lots"][0]["description_ru"]
        == "ESG reporting and sustainability report preparation"
    )

    config = KeywordsConfig.load(KEYWORDS_PATH)
    result = match_tender(upsert, config)
    assert result.is_match
    assert "esg" in result.matched_groups


def test_normalize_handles_null_cost() -> None:
    raw = copy.deepcopy(_fixture_items()[0])
    raw["totalcost"] = None
    upsert = XtXaridConnector()._normalize(raw)
    assert upsert.value_amount is None
    assert upsert.value_currency is None


# ---------------------------------------------------------------------------
# Fetch pipeline — MockTransport, no real HTTP
# ---------------------------------------------------------------------------


def _make_handler(
    *,
    pages_by_offset: dict[int, list[dict[str, Any]]],
    captured: list[httpx.Request],
    error: dict[str, Any] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if not _is_listing(request):
            return httpx.Response(404)
        body = _body_json(request)
        offset = int(body.get("params", {}).get("offset", 0))
        result = pages_by_offset.get(offset, [])
        payload: dict[str, Any] = {
            "id": 1,
            "jsonrpc": "2.0",
            "result": result,
        }
        if error is not None:
            payload["error"] = error
        return httpx.Response(200, json=payload)

    return handler


async def test_fetch_latest_full_pipeline() -> None:
    items = _fixture_items()
    captured: list[httpx.Request] = []
    handler = _make_handler(pages_by_offset={0: items}, captured=captured)
    transport = httpx.MockTransport(handler)
    connector = XtXaridConnector(http_client_factory=_client_factory(transport))

    result = await connector.fetch_latest()

    assert result.source_name == "xt_xarid"
    assert result.raw_item_count == len(items)
    assert len(result.tenders) == len(items)
    assert result.partial_errors == []
    assert all(t.country is Country.UZ for t in result.tenders)


async def test_fetch_latest_json_rpc_body_shape() -> None:
    captured: list[httpx.Request] = []
    handler = _make_handler(
        pages_by_offset={0: _fixture_items()}, captured=captured
    )
    transport = httpx.MockTransport(handler)
    connector = XtXaridConnector(http_client_factory=_client_factory(transport))

    await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_listing(r)]
    assert listing_calls, "expected at least one listing request"
    body = _body_json(listing_calls[0])
    assert body["id"] == 1
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "ref"
    params = body["params"]
    assert params["ref"] == "ref_tender_public"
    assert params["op"] == "read"
    assert params["offset"] == 0
    assert params["limit"] == 50
    assert params["filters"] == {}
    assert "meta" in params["fields"]


async def test_fetch_latest_pagination_offset_increments() -> None:
    template = _fixture_items()[0]

    def _clone(item_id: int) -> dict[str, Any]:
        clone = copy.deepcopy(template)
        clone["id"] = item_id
        return clone

    page1 = [_clone(900000 + i) for i in range(50)]
    page2 = [_clone(901000 + i) for i in range(50)]
    page3 = [_clone(902000 + i) for i in range(10)]

    captured: list[httpx.Request] = []
    handler = _make_handler(
        pages_by_offset={0: page1, 50: page2, 100: page3},
        captured=captured,
    )
    transport = httpx.MockTransport(handler)
    connector = XtXaridConnector(http_client_factory=_client_factory(transport))

    result = await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_listing(r)]
    offsets = [_body_json(r)["params"]["offset"] for r in listing_calls]
    # Three pages: short page 3 (10 < 50) stops pagination, so no
    # offset=150 request.
    assert offsets == [0, 50, 100]
    assert result.raw_item_count == 110


async def test_fetch_latest_dedupes_repeated_ids_across_pages() -> None:
    template = _fixture_items()[0]

    def _clone(item_id: int) -> dict[str, Any]:
        clone = copy.deepcopy(template)
        clone["id"] = item_id
        return clone

    page1 = [_clone(910000 + i) for i in range(50)]
    page2 = [_clone(910000 + i) for i in range(50)]

    captured: list[httpx.Request] = []
    handler = _make_handler(
        pages_by_offset={0: page1, 50: page2},
        captured=captured,
    )
    transport = httpx.MockTransport(handler)
    connector = XtXaridConnector(http_client_factory=_client_factory(transport))

    result = await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_listing(r)]
    offsets = [_body_json(r)["params"]["offset"] for r in listing_calls]
    assert offsets == [0, 50]
    assert result.raw_item_count == 50
    assert len(result.tenders) == 50


async def test_fetch_latest_rpc_error_raises_fetch_error() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": 1,
                "jsonrpc": "2.0",
                "result": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            },
        )

    transport = httpx.MockTransport(handler)
    connector = XtXaridConnector(http_client_factory=_client_factory(transport))

    with pytest.raises(FetchError, match="Invalid Request"):
        await connector.fetch_latest()


async def test_fetch_latest_since_keeps_nulls_filters_past() -> None:
    template = _fixture_items()[0]
    # null publicated_at → KEEP
    null_item = copy.deepcopy(template)
    null_item["id"] = 100
    null_item["publicated_at"] = None
    # past publicated_at → DROP
    past_item = copy.deepcopy(template)
    past_item["id"] = 200
    past_item["publicated_at"] = "2026-04-01T10:00:00"
    # future publicated_at → KEEP
    future_item = copy.deepcopy(template)
    future_item["id"] = 300
    future_item["publicated_at"] = "2026-06-01T10:00:00"

    captured: list[httpx.Request] = []
    handler = _make_handler(
        pages_by_offset={0: [null_item, past_item, future_item]},
        captured=captured,
    )
    transport = httpx.MockTransport(handler)
    connector = XtXaridConnector(http_client_factory=_client_factory(transport))

    since = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    result = await connector.fetch_latest(since=since)

    ids = {t.external_id for t in result.tenders}
    assert ids == {"100", "300"}
    # past dropped, null kept, future kept.
    assert "200" not in ids


async def test_fetch_latest_listing_500_raises_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = XtXaridConnector(http_client_factory=_client_factory(transport))

    with pytest.raises(FetchError):
        await connector.fetch_latest()


async def test_fetch_latest_no_signed_token_headers() -> None:
    captured: list[httpx.Request] = []
    handler = _make_handler(
        pages_by_offset={0: _fixture_items()}, captured=captured
    )
    transport = httpx.MockTransport(handler)
    connector = XtXaridConnector(http_client_factory=_client_factory(transport))

    await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_listing(r)]
    headers = {k.lower() for k in listing_calls[0].headers}
    assert "x-idempotency-key" not in headers
    assert "x-url-on" not in headers
