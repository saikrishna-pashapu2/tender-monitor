from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.http import make_client
from tender_monitor.connectors.zakup_unified import ZakupUnifiedConnector
from tender_monitor.core.enums import Country, Language, TenderStatus

LISTING_PATH = "/api/core/api/core/_lots/"
ANNOUNCEMENT_PATH_PREFIX = "/api/core/api/core/announcements/"


def _client_factory(
    transport: httpx.MockTransport,
) -> Callable[[], httpx.AsyncClient]:
    def _make() -> httpx.AsyncClient:
        return make_client(
            headers=ZakupUnifiedConnector.REQUIRED_HEADERS, transport=transport
        )

    return _make


def _is_listing(request: httpx.Request) -> bool:
    return request.url.path == LISTING_PATH


def _is_announcement(request: httpx.Request) -> bool:
    return request.url.path.startswith(ANNOUNCEMENT_PATH_PREFIX)


def _announcement_id_from_url(request: httpx.Request) -> int:
    parts = [p for p in request.url.path.split("/") if p]
    return int(parts[-1])


def _build_announcement(
    *,
    announcement_id: int,
    name: str = "Test announcement",
    publish_date: int = 1778370738,
    offer_end_date: int = 1779580800,
    total_price: float | None = 22700000.0,
    status_id: int = 6,
    organizer_name: str | None = "ГУ Test Buyer",
    organizer_iin: str | None = "123456789012",
) -> dict[str, Any]:
    return {
        "id": announcement_id,
        "external_id": 9876543,
        "announcement_number": str(announcement_id),
        "name": name,
        "publish_date": publish_date,
        "offer_start_date": publish_date,
        "offer_end_date": offer_end_date,
        "total_price": total_price,
        "status": {"id": status_id, "name": "Опубликован", "is_active": True},
        "purchase_method": {"id": 2, "name": "Открытый конкурс"},
        "purchase_subject": {"id": 1, "code": "goods", "name": "Товары"},
        "organizer": {
            "id": 1,
            "iin_bin": organizer_iin,
            "name": organizer_name,
        },
        "lot_count": 1,
    }


def _build_lot(
    *,
    announcement_id: int,
    lot_id: int,
    publish_iso: str = "2026-05-08T02:12:18Z",
    name_ru: str | None = None,
) -> dict[str, Any]:
    return {
        "id": lot_id,
        "announcement_id": announcement_id,
        "announcement_number": str(announcement_id),
        "lot_number": "1",
        "name_ru": name_ru,
        "name_kk": None,
        "description_ru": None,
        "description_kk": None,
        "total_price": 100000.0,
        "dumping_price": None,
        "quantity": 1.0,
        "organization_name": "Test Buyer",
        "delivery_addresses": [],
        "purchase_method_name": "Открытый конкурс",
        "purchase_method_id": 2,
        "offer_start_date": publish_iso,
        "offer_end_date": "2026-05-22T18:00:00Z",
        "announcement_publish_date": publish_iso,
        "status": {"id": 6, "name": "Опубликован", "is_active": True, "code": "published"},
        "system": {"id": 1, "name": "GoszakupRK"},
        "enstrus": [],
        "external_id": lot_id,
    }


# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------


def test_normalize_happy_path(load_json_fixture: Callable[[str], Any]) -> None:
    listing = load_json_fixture("zakup_unified/listing.json")
    announcement = load_json_fixture("zakup_unified/announcement_39385974.json")
    raw = copy.deepcopy(announcement)
    raw["_lots"] = [
        lot for lot in listing["results"] if lot["announcement_id"] == raw["id"]
    ]

    upsert = ZakupUnifiedConnector()._normalize(raw)

    assert upsert.source_name == "zakup_unified"
    assert upsert.external_id == "39385974"
    assert upsert.title == "Закуп строительных материалов для благоустройства территории"
    assert upsert.buyer_name == 'ГУ "Аппарат акима города Алматы"'
    assert upsert.buyer_external_id == "950140000123"
    assert upsert.country is Country.KZ
    assert upsert.value_amount == Decimal("22700000.0")
    assert upsert.value_currency == "KZT"
    assert upsert.published_at is not None
    assert upsert.published_at.tzinfo is UTC
    assert upsert.deadline_at is not None
    assert upsert.deadline_at.tzinfo is UTC
    assert upsert.status is TenderStatus.open
    assert upsert.source_url == "https://zakup.gov.kz/announcement/39385974"
    assert upsert.language is Language.ru
    assert "_lots" in upsert.raw_json
    assert len(upsert.raw_json["_lots"]) == 2


def test_normalize_missing_title_raises() -> None:
    raw = _build_announcement(announcement_id=1, name="")
    raw["_lots"] = []
    with pytest.raises(ParseError, match="empty title"):
        ZakupUnifiedConnector()._normalize(raw)

    raw_none = _build_announcement(announcement_id=2)
    raw_none["name"] = None
    raw_none["_lots"] = []
    with pytest.raises(ParseError):
        ZakupUnifiedConnector()._normalize(raw_none)


def test_status_mapping_known_and_unknown() -> None:
    connector = ZakupUnifiedConnector()

    raw_open_6 = _build_announcement(announcement_id=10, status_id=6)
    raw_open_6["_lots"] = []
    assert connector._normalize(raw_open_6).status is TenderStatus.open

    raw_open_7 = _build_announcement(announcement_id=11, status_id=7)
    raw_open_7["_lots"] = []
    assert connector._normalize(raw_open_7).status is TenderStatus.open

    raw_unknown = _build_announcement(announcement_id=12, status_id=999)
    raw_unknown["_lots"] = []
    assert connector._normalize(raw_unknown).status is TenderStatus.unknown

    raw_no_status = _build_announcement(announcement_id=13)
    raw_no_status["status"] = None
    raw_no_status["_lots"] = []
    assert connector._normalize(raw_no_status).status is TenderStatus.unknown


# ---------------------------------------------------------------------------
# Fetch-pipeline tests (use MockTransport, no real HTTP)
# ---------------------------------------------------------------------------


async def test_fetch_latest_full_pipeline(
    load_json_fixture: Callable[[str], Any],
) -> None:
    listing = load_json_fixture("zakup_unified/listing.json")
    announcement = load_json_fixture("zakup_unified/announcement_39385974.json")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, json=listing)
        if _is_announcement(request):
            ann_id = _announcement_id_from_url(request)
            if ann_id == announcement["id"]:
                return httpx.Response(200, json=announcement)
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    connector = ZakupUnifiedConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    assert result.source_name == "zakup_unified"
    assert result.raw_item_count == 1  # one unique announcement_id
    assert len(result.tenders) == 1
    assert result.partial_errors == []
    assert result.duration_ms > 0
    assert result.fetched_at.tzinfo is UTC

    upsert = result.tenders[0]
    assert upsert.external_id == "39385974"
    assert "_lots" in upsert.raw_json
    assert len(upsert.raw_json["_lots"]) == 2

    listing_calls = [r for r in captured if _is_listing(r)]
    detail_calls = [r for r in captured if _is_announcement(r)]
    assert len(listing_calls) == 1
    assert len(detail_calls) == 1


async def test_fetch_latest_paginates_until_since() -> None:
    new_iso = "2026-05-08T02:12:18Z"
    old_iso = "2026-04-01T00:00:00Z"

    listing_page_1 = {
        "count": 4,
        "next": "https://zakup.gov.kz/api/core/api/core/_lots/?offset=50",
        "previous": None,
        "results": [
            _build_lot(announcement_id=1001, lot_id=1, publish_iso=new_iso),
            _build_lot(announcement_id=1001, lot_id=2, publish_iso=new_iso),
            _build_lot(announcement_id=1002, lot_id=3, publish_iso=old_iso),
            _build_lot(announcement_id=1003, lot_id=4, publish_iso=old_iso),
        ],
        "facets": {},
    }

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, json=listing_page_1)
        if _is_announcement(request):
            ann_id = _announcement_id_from_url(request)
            return httpx.Response(200, json=_build_announcement(announcement_id=ann_id))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = ZakupUnifiedConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest(
        since=datetime(2026, 5, 1, tzinfo=UTC)
    )

    listing_calls = [r for r in captured if _is_listing(r)]
    assert len(listing_calls) == 1
    offset_param = listing_calls[0].url.params.get("offset")
    assert offset_param in (None, "0")

    # Only the 'new' lots survive the since cutoff; both share announcement 1001.
    assert {t.external_id for t in result.tenders} == {"1001"}


async def test_fetch_latest_soft_since_keeps_late_new_lot_on_same_page() -> None:
    old_iso = "2026-04-01T00:00:00Z"
    new_iso = "2026-05-08T02:12:18Z"

    listing_page_1 = {
        "count": 4,
        "next": None,
        "previous": None,
        "results": [
            _build_lot(announcement_id=1002, lot_id=3, publish_iso=old_iso),
            _build_lot(announcement_id=1003, lot_id=4, publish_iso=old_iso),
            _build_lot(announcement_id=1001, lot_id=1, publish_iso=new_iso),
            _build_lot(announcement_id=1001, lot_id=2, publish_iso=new_iso),
        ],
        "facets": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(200, json=listing_page_1)
        if _is_announcement(request):
            ann_id = _announcement_id_from_url(request)
            return httpx.Response(200, json=_build_announcement(announcement_id=ann_id))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = ZakupUnifiedConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest(
        since=datetime(2026, 5, 1, tzinfo=UTC)
    )

    assert {t.external_id for t in result.tenders} == {"1001"}


async def test_fetch_latest_handles_detail_404() -> None:
    listing = {
        "count": 2,
        "next": None,
        "previous": None,
        "results": [
            _build_lot(announcement_id=2001, lot_id=10),
            _build_lot(announcement_id=2002, lot_id=11),
        ],
        "facets": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(200, json=listing)
        if _is_announcement(request):
            ann_id = _announcement_id_from_url(request)
            if ann_id == 2002:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(
                200, json=_build_announcement(announcement_id=ann_id)
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = ZakupUnifiedConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    assert result.raw_item_count == 1  # one survivor
    assert len(result.tenders) == 1
    assert result.tenders[0].external_id == "2001"
    # 404s are not normalization failures, so they stay out of partial_errors.
    assert result.partial_errors == []


async def test_fetch_latest_listing_500_raises_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = ZakupUnifiedConnector(http_client_factory=_client_factory(transport))

    with pytest.raises(FetchError):
        await connector.fetch_latest()


async def test_referer_header_is_sent(
    load_json_fixture: Callable[[str], Any],
) -> None:
    listing = load_json_fixture("zakup_unified/listing.json")
    announcement = load_json_fixture("zakup_unified/announcement_39385974.json")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_listing(request):
            return httpx.Response(200, json=listing)
        if _is_announcement(request):
            return httpx.Response(200, json=announcement)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = ZakupUnifiedConnector(http_client_factory=_client_factory(transport))
    await connector.fetch_latest()

    assert captured, "expected the connector to make at least one request"
    expected_referer = (
        "https://zakup.gov.kz/home/lots?system_id__in=1__2__3"
    )
    for request in captured:
        assert request.headers.get("referer") == expected_referer
        assert request.headers.get("accept") == "application/json"


async def test_fetch_latest_stops_when_pagination_repeats_same_lots() -> None:
    listing_page = {
        "count": 50,
        "next": "https://zakup.gov.kz/api/core/api/core/_lots/?offset=50",
        "previous": None,
        "results": [
            _build_lot(announcement_id=5000 + idx, lot_id=6000 + idx)
            for idx in range(50)
        ],
        "facets": {},
    }
    pages_requested: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            pages_requested.append(request.url.params.get("offset"))
            return httpx.Response(200, json=listing_page)
        if _is_announcement(request):
            ann_id = _announcement_id_from_url(request)
            return httpx.Response(200, json=_build_announcement(announcement_id=ann_id))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = ZakupUnifiedConnector(http_client_factory=_client_factory(transport))
    result = await connector.fetch_latest()

    assert len(result.tenders) == 50
    assert pages_requested == ["0", "50"]
