from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.samruk_kazyna import SamrukKazynaConnector
from tender_monitor.core.enums import Country, Language, TenderStatus


def _build_listing_item(
    *,
    advert_id: int,
    accept_begin: str,
    accept_end: str = "2026-05-22T18:00:00Z",
    name_ru: str = "Test advert",
    sum_value: float = 100000.0,
    advert_status: str = "PUBLISHED",
) -> dict[str, Any]:
    return {
        "id": advert_id,
        "number": str(advert_id),
        "nameRu": name_ru,
        "nameKk": None,
        "nameEn": None,
        "tenderType": "OTOU",
        "sumTruNoNds": sum_value,
        "acceptanceBeginDateTime": accept_begin,
        "acceptanceEndDateTime": accept_end,
        "advertStatus": advert_status,
        "flagApplicationFiled": False,
        "flagNegotiationOutside": None,
    }


def _build_advert_detail(
    *,
    advert_id: int,
    name_ru: str = "Test advert",
    sum_value: float | None = 100000.0,
    advert_status: str = "PUBLISHED",
    customer_bin: str = "000000000000",
) -> dict[str, Any]:
    return {
        "id": advert_id,
        "number": str(advert_id),
        "nameRu": name_ru,
        "nameKk": None,
        "nameEn": None,
        "tenderType": "OTOU",
        "sumTruNoNds": sum_value,
        "acceptanceBeginDateTime": "2026-05-08T10:00:00+05:00",
        "acceptanceEndDateTime": "2026-05-22T23:00:00+05:00",
        "advertStatus": advert_status,
        "simpleStatus": advert_status,
        "customer": {
            "id": 1,
            "identifier": str(customer_bin),
            "nameRu": "Test Customer",
            "bin": customer_bin,
        },
        "organizer": {
            "id": 1,
            "nameRu": "Test Customer",
            "bin": customer_bin,
        },
        "documents": [],
    }


class _FakeBrowser:
    """Stand-in for SamrukKazynaBrowser. Used to drive the connector in
    tests without spinning up Playwright/Chromium.

    Owners populate ``listing`` and the ``adverts`` map; the connector
    consumes them as if they came from a real browser session.
    """

    def __init__(
        self,
        *,
        listing: list[dict[str, Any]],
        adverts: dict[int, tuple[dict[str, Any], list[dict[str, Any]]] | None],
        listing_error: BaseException | None = None,
    ) -> None:
        self.listing = listing
        self.adverts = adverts
        self.listing_error = listing_error
        self.fetched_listing_count = 0
        self.fetched_advert_ids: list[int] = []

    async def fetch_listing(self) -> list[dict[str, Any]]:
        self.fetched_listing_count += 1
        if self.listing_error is not None:
            raise self.listing_error
        return list(self.listing)

    async def fetch_listing_pages(
        self, *, max_pages: int
    ) -> list[dict[str, Any]]:
        self.fetched_listing_count += 1
        if self.listing_error is not None:
            raise self.listing_error
        return list(self.listing)

    async def fetch_advert(
        self, advert_id: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        self.fetched_advert_ids.append(advert_id)
        return self.adverts.get(advert_id)


def _browser_factory(browser: _FakeBrowser) -> Callable[[], Any]:
    @asynccontextmanager
    async def _cm() -> AsyncIterator[_FakeBrowser]:
        yield browser

    def factory() -> Any:
        return _cm()

    return factory


# ---------------------------------------------------------------------------
# Normalization tests
# ---------------------------------------------------------------------------


def test_normalize_happy_path(load_json_fixture: Callable[[str], Any]) -> None:
    advert = load_json_fixture("samruk_kazyna/advert_1220290.json")
    lots = load_json_fixture("samruk_kazyna/lots_1220290.json")
    raw = copy.deepcopy(advert)
    raw["_lots"] = lots

    upsert = SamrukKazynaConnector()._normalize(raw)

    assert upsert.source_name == "samruk_kazyna"
    assert upsert.external_id == "1220290"
    assert upsert.title.startswith("Работы по капитальному ремонту")
    assert upsert.buyer_name == 'Акционерное общество "Алатау Жарық Компаниясы"'
    assert upsert.buyer_external_id == "960840000483"
    assert upsert.country is Country.KZ
    assert upsert.value_amount == Decimal("34191869.47")
    assert upsert.value_currency == "KZT"
    assert upsert.published_at is not None
    assert upsert.published_at.tzinfo is not None
    assert upsert.deadline_at is not None
    assert upsert.deadline_at.tzinfo is not None
    assert upsert.status is TenderStatus.open
    assert upsert.source_url.startswith("https://zakup.sk.kz/")
    assert "1220290" in upsert.source_url
    assert upsert.language is Language.ru
    assert "_lots" in upsert.raw_json
    assert len(upsert.raw_json["_lots"]) == 2


def test_normalize_missing_title_raises() -> None:
    raw_empty = _build_advert_detail(advert_id=1, name_ru="")
    raw_empty["_lots"] = []
    with pytest.raises(ParseError, match="empty nameRu"):
        SamrukKazynaConnector()._normalize(raw_empty)

    raw_none = _build_advert_detail(advert_id=2)
    raw_none["nameRu"] = None
    raw_none["_lots"] = []
    with pytest.raises(ParseError):
        SamrukKazynaConnector()._normalize(raw_none)


def test_status_mapping_published_and_unknown() -> None:
    connector = SamrukKazynaConnector()

    pub = _build_advert_detail(advert_id=10, advert_status="PUBLISHED")
    pub["_lots"] = []
    assert connector._normalize(pub).status is TenderStatus.open

    cancelled = _build_advert_detail(advert_id=11, advert_status="CANCELLED")
    cancelled["_lots"] = []
    assert connector._normalize(cancelled).status is TenderStatus.unknown

    missing = _build_advert_detail(advert_id=12)
    missing["advertStatus"] = None
    missing["_lots"] = []
    assert connector._normalize(missing).status is TenderStatus.unknown


# ---------------------------------------------------------------------------
# Fetch-pipeline tests — use the fake browser injection
# ---------------------------------------------------------------------------


async def test_fetch_latest_full_pipeline(
    load_json_fixture: Callable[[str], Any],
) -> None:
    listing = load_json_fixture("samruk_kazyna/listing.json")
    advert_a = load_json_fixture("samruk_kazyna/advert_1220290.json")
    advert_b = load_json_fixture("samruk_kazyna/advert_1220395.json")
    lots_a = load_json_fixture("samruk_kazyna/lots_1220290.json")
    lots_b = load_json_fixture("samruk_kazyna/lots_1220395.json")

    browser = _FakeBrowser(
        listing=listing,
        adverts={
            1220290: (advert_a, lots_a),
            1220395: (advert_b, lots_b),
        },
    )
    connector = SamrukKazynaConnector(browser_factory=_browser_factory(browser))
    result = await connector.fetch_latest()

    assert result.source_name == "samruk_kazyna"
    assert result.raw_item_count == 2
    assert len(result.tenders) == 2
    assert result.partial_errors == []
    assert result.duration_ms > 0
    assert result.fetched_at.tzinfo is UTC
    assert {t.external_id for t in result.tenders} == {"1220290", "1220395"}

    assert browser.fetched_listing_count == 1
    assert browser.fetched_advert_ids == [1220290, 1220395]


async def test_fetch_latest_filters_by_since() -> None:
    new_iso = "2026-05-08T10:00:00Z"
    old_iso = "2026-04-01T00:00:00Z"
    listing = [
        _build_listing_item(advert_id=101, accept_begin=new_iso),
        _build_listing_item(advert_id=102, accept_begin=new_iso),
        _build_listing_item(advert_id=103, accept_begin=old_iso),
    ]
    adverts: dict[int, Any] = {
        101: (_build_advert_detail(advert_id=101), []),
        102: (_build_advert_detail(advert_id=102), []),
        103: (_build_advert_detail(advert_id=103), []),
    }
    browser = _FakeBrowser(listing=listing, adverts=adverts)
    connector = SamrukKazynaConnector(browser_factory=_browser_factory(browser))

    result = await connector.fetch_latest(since=datetime(2026, 5, 1, tzinfo=UTC))

    assert {t.external_id for t in result.tenders} == {"101", "102"}
    # 103 is out of window, so we never even ask the browser for it.
    assert browser.fetched_advert_ids == [101, 102]


async def test_fetch_latest_handles_advert_failure() -> None:
    listing = [
        _build_listing_item(advert_id=201, accept_begin="2026-05-08T10:00:00Z"),
        _build_listing_item(advert_id=202, accept_begin="2026-05-08T10:00:00Z"),
    ]
    adverts: dict[int, Any] = {
        201: None,  # browser couldn't fetch (e.g., card not found)
        202: (_build_advert_detail(advert_id=202), []),
    }
    browser = _FakeBrowser(listing=listing, adverts=adverts)
    connector = SamrukKazynaConnector(browser_factory=_browser_factory(browser))

    result = await connector.fetch_latest()

    # 201 dropped silently (per-item failure logged inside the browser
    # helper, not promoted to partial_errors).
    assert result.raw_item_count == 1
    assert len(result.tenders) == 1
    assert result.tenders[0].external_id == "202"
    assert result.partial_errors == []


async def test_fetch_latest_advert_exception_is_skipped() -> None:
    listing = [
        _build_listing_item(advert_id=301, accept_begin="2026-05-08T10:00:00Z"),
        _build_listing_item(advert_id=302, accept_begin="2026-05-08T10:00:00Z"),
    ]

    class _RaisingBrowser(_FakeBrowser):
        async def fetch_advert(self, advert_id: int) -> Any:
            self.fetched_advert_ids.append(advert_id)
            if advert_id == 301:
                raise RuntimeError("browser flaked out on this one")
            return _build_advert_detail(advert_id=advert_id), []

    browser = _RaisingBrowser(listing=listing, adverts={})
    connector = SamrukKazynaConnector(browser_factory=_browser_factory(browser))

    result = await connector.fetch_latest()

    assert result.raw_item_count == 1
    assert result.tenders[0].external_id == "302"


async def test_fetch_latest_listing_failure_raises_fetch_error() -> None:
    browser = _FakeBrowser(
        listing=[], adverts={}, listing_error=RuntimeError("network down")
    )
    connector = SamrukKazynaConnector(browser_factory=_browser_factory(browser))

    with pytest.raises(FetchError, match="listing failed"):
        await connector.fetch_latest()


async def test_fetch_latest_dedupes_repeated_ids() -> None:
    # Defensive: the gateway shouldn't return dups, but if it ever did,
    # we should not double-fetch.
    listing = [
        _build_listing_item(advert_id=401, accept_begin="2026-05-08T10:00:00Z"),
        _build_listing_item(advert_id=401, accept_begin="2026-05-08T10:00:00Z"),
        _build_listing_item(advert_id=402, accept_begin="2026-05-08T10:00:00Z"),
    ]
    adverts: dict[int, Any] = {
        401: (_build_advert_detail(advert_id=401), []),
        402: (_build_advert_detail(advert_id=402), []),
    }
    browser = _FakeBrowser(listing=listing, adverts=adverts)
    connector = SamrukKazynaConnector(browser_factory=_browser_factory(browser))

    await connector.fetch_latest()

    # 401 only requested once despite appearing twice in the listing.
    assert browser.fetched_advert_ids == [401, 402]


async def test_fetch_latest_uses_paginated_listing_results() -> None:
    listing = [
        _build_listing_item(advert_id=501, accept_begin="2026-05-08T10:00:00Z"),
        _build_listing_item(advert_id=502, accept_begin="2026-05-08T10:00:00Z"),
        _build_listing_item(advert_id=503, accept_begin="2026-05-08T10:00:00Z"),
    ]
    adverts: dict[int, Any] = {
        501: (_build_advert_detail(advert_id=501), []),
        502: (_build_advert_detail(advert_id=502), []),
        503: (_build_advert_detail(advert_id=503), []),
    }

    class _PagedBrowser(_FakeBrowser):
        def __init__(self) -> None:
            super().__init__(listing=[], adverts=adverts)
            self.max_pages_requested: int | None = None

        async def fetch_listing(self) -> list[dict[str, Any]]:
            raise AssertionError("connector should use fetch_listing_pages")

        async def fetch_listing_pages(
            self, *, max_pages: int
        ) -> list[dict[str, Any]]:
            self.fetched_listing_count += 1
            self.max_pages_requested = max_pages
            return list(listing)

    browser = _PagedBrowser()
    connector = SamrukKazynaConnector(browser_factory=_browser_factory(browser))

    result = await connector.fetch_latest()

    assert {t.external_id for t in result.tenders} == {"501", "502", "503"}
    assert browser.max_pages_requested == connector.LISTING_MAX_PAGES
    assert browser.fetched_advert_ids == [501, 502, 503]
