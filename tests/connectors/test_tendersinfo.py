"""Tests for the TendersInfo connector.

All offline; HTTP is exercised through ``httpx.MockTransport``. The
two fixtures (``listing_uz.json``, ``listing_kz.json``) sit under
``tests/fixtures/tendersinfo/`` and are wired into the mock handler
by parsing the URL-encoded ``country_code`` form field.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from tender_monitor.connectors._html import parse_dmy_month_name
from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.http import make_client
from tender_monitor.connectors.tendersinfo import (
    TendersinfoConnector,
    clean_title,
    extract_authority,
)
from tender_monitor.core.enums import Country, Language, TenderStatus
from tender_monitor.core.schemas import TenderUpsert
from tender_monitor.matching import KeywordsConfig, match_tender

LISTING_PATH = "/esearch/tender_sector_test"
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "tendersinfo"
KEYWORDS_PATH = Path(__file__).parent.parent.parent / "config" / "keywords.yaml"


def _read_fixture(name: str) -> dict[str, Any]:
    raw = (FIXTURES_DIR / name).read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


def _client_factory(
    transport: httpx.MockTransport,
) -> Callable[[], httpx.AsyncClient]:
    def _make() -> httpx.AsyncClient:
        return make_client(
            headers=TendersinfoConnector.REQUIRED_HEADERS, transport=transport
        )

    return _make


def _is_listing(request: httpx.Request) -> bool:
    return request.url.path == LISTING_PATH and request.method == "POST"


def _body_form(request: httpx.Request) -> dict[str, str]:
    """Parse the URL-encoded body into a flat dict (last value wins)."""
    raw = request.content.decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


# ---------------------------------------------------------------------------
# Date parser
# ---------------------------------------------------------------------------


def test_parse_dmy_month_name_happy_path() -> None:
    result = parse_dmy_month_name("16-May-2026")
    assert result == datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC)
    assert result is not None and result.tzinfo is UTC


@pytest.mark.parametrize("text", [None, "", "   "])
def test_parse_dmy_month_name_handles_empty(text: str | None) -> None:
    assert parse_dmy_month_name(text) is None


@pytest.mark.parametrize("text", ["not-a-date", "16-Maaay-2026", "2026-05-16"])
def test_parse_dmy_month_name_handles_garbage(text: str) -> None:
    assert parse_dmy_month_name(text) is None


# ---------------------------------------------------------------------------
# Title / authority helpers
# ---------------------------------------------------------------------------


def test_clean_title_strips_suffix() -> None:
    assert (
        clean_title("Conducting X\nopen In A New Window")
        == "Conducting X"
    )
    # Case-insensitive suffix.
    assert clean_title("Title XYZ OPEN IN A NEW WINDOW") == "Title XYZ"


def test_clean_title_idempotent_when_no_suffix() -> None:
    assert clean_title("Conducting X") == "Conducting X"
    assert clean_title("  spaced  ") == "spaced"


def test_clean_title_handles_none() -> None:
    assert clean_title(None) == ""
    assert clean_title("") == ""


def test_extract_authority_parses_html() -> None:
    assert (
        extract_authority("<br><b>Tender Authority: </b>UNDP") == "UNDP"
    )
    # Trailing whitespace and other tag flavours.
    assert (
        extract_authority(
            "<div><br><b>Tender Authority:</b> Ministry of Finance</div>"
        )
        == "Ministry of Finance"
    )


def test_extract_authority_handles_no_label() -> None:
    assert extract_authority("<br>Some random text") == "Some random text"


def test_extract_authority_none_input() -> None:
    assert extract_authority(None) is None
    assert extract_authority("") is None
    assert extract_authority("   <br>   ") is None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_happy_path_uz() -> None:
    items = _read_fixture("listing_uz.json")["data"]
    upsert = TendersinfoConnector()._normalize(items[0])

    assert upsert.source_name == "tendersinfo"
    assert upsert.external_id == "532912293"
    assert upsert.title == (
        "Conducting Inclusivity Assessment For Sustainable Urban Planning"
    )
    assert upsert.buyer_name == "United Nations Development Programme"
    assert upsert.country is Country.UZ
    assert upsert.sector == "Environment And Pollution"
    assert upsert.published_at == datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC)
    assert upsert.deadline_at == datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    assert upsert.source_url.startswith(
        "https://www.tendersinfo.com/tenders_details/"
    )
    assert upsert.language is Language.en
    assert upsert.value_amount is None
    assert upsert.value_currency is None
    assert upsert.status is TenderStatus.open
    # raw_json carries the English title on the synthetic _lots wrap.
    lots = upsert.raw_json["_lots"]
    assert len(lots) == 1
    assert lots[0]["name_en"] == upsert.title


def test_normalize_happy_path_kz() -> None:
    items = _read_fixture("listing_kz.json")["data"]
    upsert = TendersinfoConnector()._normalize(items[0])

    assert upsert.country is Country.KZ
    assert upsert.title == "Procurement Of Industrial Boilers For Power Generation"
    assert upsert.buyer_name == "Samruk-Kazyna JSC"
    assert upsert.language is Language.en


def test_normalize_empty_title_raises() -> None:
    items = _read_fixture("listing_uz.json")["data"]
    bad = copy.deepcopy(items[0])
    bad["short_desc"] = ""
    with pytest.raises(ParseError, match="empty title"):
        TendersinfoConnector()._normalize(bad)

    # After cleaning, just the suffix is empty too.
    bad2 = copy.deepcopy(items[0])
    bad2["short_desc"] = "\nopen In A New Window"
    with pytest.raises(ParseError, match="empty title"):
        TendersinfoConnector()._normalize(bad2)


def test_normalize_missing_country_raises() -> None:
    items = _read_fixture("listing_uz.json")["data"]
    bad = copy.deepcopy(items[0])
    bad["country"] = ""
    with pytest.raises(ParseError, match="missing 'country'"):
        TendersinfoConnector()._normalize(bad)


def test_normalize_third_country_propagates_valueerror() -> None:
    items = _read_fixture("listing_uz.json")["data"]
    bad = copy.deepcopy(items[0])
    bad["country"] = "RU"  # not in Country enum
    with pytest.raises(ValueError):
        TendersinfoConnector()._normalize(bad)


# ---------------------------------------------------------------------------
# Fetch pipeline -- MockTransport
# ---------------------------------------------------------------------------


def _make_handler(
    *,
    captured: list[httpx.Request],
    payloads: dict[str, dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler keyed on country_code in the URL-encoded body."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if not _is_listing(request):
            return httpx.Response(404)
        body = _body_form(request)
        country_code = body.get("country_code", "")
        payload = payloads.get(country_code) or {
            "draw": 1,
            "recordsTotal": 0,
            "recordsFiltered": 0,
            "data": [],
        }
        return httpx.Response(200, json=payload)

    return handler


async def test_fetch_latest_calls_both_countries() -> None:
    captured: list[httpx.Request] = []
    handler = _make_handler(
        captured=captured,
        payloads={
            "KZ": _read_fixture("listing_kz.json"),
            "UZ": _read_fixture("listing_uz.json"),
        },
    )
    transport = httpx.MockTransport(handler)
    connector = TendersinfoConnector(http_client_factory=_client_factory(transport))

    result = await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_listing(r)]
    # Exactly two POSTs (1 per country) when both single-page totals
    # fit on the first page (recordsTotal=3 each, PAGE_SIZE=100).
    assert len(listing_calls) == 2
    countries_sent = sorted(
        _body_form(r)["country_code"] for r in listing_calls
    )
    assert countries_sent == ["KZ", "UZ"]

    # 3 KZ + 3 UZ = 6 normalized tenders.
    assert len(result.tenders) == 6
    by_country = sorted(t.country.value for t in result.tenders)
    assert by_country == ["KZ", "KZ", "KZ", "UZ", "UZ", "UZ"]
    assert result.partial_errors == []


async def test_fetch_latest_handles_kz_empty_uz_populated() -> None:
    captured: list[httpx.Request] = []
    template_item = _read_fixture("listing_uz.json")["data"][0]
    uz_items: list[dict[str, Any]] = []
    for i in range(95):
        clone = copy.deepcopy(template_item)
        clone["site_tender_id"] = f"60000{i:03d}"
        uz_items.append(clone)
    handler = _make_handler(
        captured=captured,
        payloads={
            "KZ": {"draw": 1, "recordsTotal": 0, "recordsFiltered": 0, "data": []},
            "UZ": {
                "draw": 1,
                "recordsTotal": 95,
                "recordsFiltered": 95,
                "data": uz_items,
            },
        },
    )
    transport = httpx.MockTransport(handler)
    connector = TendersinfoConnector(http_client_factory=_client_factory(transport))

    result = await connector.fetch_latest()

    assert len(result.tenders) == 95
    assert all(t.country is Country.UZ for t in result.tenders)


async def test_fetch_latest_pagination_within_country() -> None:
    template_item = _read_fixture("listing_uz.json")["data"][0]

    def _clone(idx: int) -> dict[str, Any]:
        clone = copy.deepcopy(template_item)
        clone["site_tender_id"] = f"700000{idx:04d}"
        return clone

    page1 = [_clone(i) for i in range(100)]
    page2 = [_clone(i + 100) for i in range(50)]

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if not _is_listing(request):
            return httpx.Response(404)
        body = _body_form(request)
        country_code = body.get("country_code", "")
        start = int(body.get("start", "0"))
        if country_code != "UZ":
            return httpx.Response(
                200,
                json={"draw": 1, "recordsTotal": 0, "recordsFiltered": 0, "data": []},
            )
        items = page1 if start == 0 else page2 if start == 100 else []
        return httpx.Response(
            200,
            json={
                "draw": 1,
                "recordsTotal": 150,
                "recordsFiltered": 150,
                "data": items,
            },
        )

    transport = httpx.MockTransport(handler)
    connector = TendersinfoConnector(http_client_factory=_client_factory(transport))

    result = await connector.fetch_latest()

    uz_calls = [
        r
        for r in captured
        if _is_listing(r) and _body_form(r).get("country_code") == "UZ"
    ]
    starts = sorted(int(_body_form(r).get("start", "0")) for r in uz_calls)
    # We requested page 1 (start=0) and page 2 (start=100), then
    # stopped because (page+1)*100 >= 150 after page 2.
    assert starts == [0, 100]
    assert len(result.tenders) == 150


async def test_fetch_latest_dedupes_repeated_items_across_pages() -> None:
    template_item = _read_fixture("listing_uz.json")["data"][0]

    first_page = []
    for i in range(100):
        clone = copy.deepcopy(template_item)
        clone["site_tender_id"] = f"710000{i:04d}"
        clone["url"] = f"https://www.tendersinfo.com/tenders_details/{710000 + i}.php"
        first_page.append(clone)

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if not _is_listing(request):
            return httpx.Response(404)
        body = _body_form(request)
        country_code = body.get("country_code", "")
        start = int(body.get("start", "0"))
        if country_code != "UZ":
            return httpx.Response(
                200,
                json={"draw": 1, "recordsTotal": 0, "recordsFiltered": 0, "data": []},
            )
        items = first_page if start in (0, 100) else []
        return httpx.Response(
            200,
            json={
                "draw": 1,
                "recordsTotal": 200,
                "recordsFiltered": 200,
                "data": items,
            },
        )

    transport = httpx.MockTransport(handler)
    connector = TendersinfoConnector(http_client_factory=_client_factory(transport))

    result = await connector.fetch_latest()

    uz_calls = [
        r
        for r in captured
        if _is_listing(r) and _body_form(r).get("country_code") == "UZ"
    ]
    starts = sorted(int(_body_form(r).get("start", "0")) for r in uz_calls)
    assert starts == [0, 100]
    assert len(result.tenders) == 100


async def test_fetch_latest_since_filter_keeps_unparseable_dates() -> None:
    items = _read_fixture("listing_uz.json")["data"]
    # The fixture's third row has date_c="" -- that's our unparseable
    # canary. With since set to a date that drops the May-12 row but
    # keeps the May-16 one, we expect the blank-date row to survive.
    captured: list[httpx.Request] = []
    handler = _make_handler(
        captured=captured,
        payloads={
            "KZ": {"draw": 1, "recordsTotal": 0, "recordsFiltered": 0, "data": []},
            "UZ": {
                "draw": 1,
                "recordsTotal": len(items),
                "recordsFiltered": len(items),
                "data": items,
            },
        },
    )
    transport = httpx.MockTransport(handler)
    connector = TendersinfoConnector(http_client_factory=_client_factory(transport))

    since = datetime(2026, 5, 15, 0, 0, tzinfo=UTC)
    result = await connector.fetch_latest(since=since)

    ids = {t.external_id for t in result.tenders}
    # 532912293 (16-May, kept); 532912295 (blank date, kept).
    # 532912294 (12-May, dropped).
    assert ids == {"532912293", "532912295"}


async def test_fetch_latest_since_filter_keeps_same_day_rows() -> None:
    items = _read_fixture("listing_uz.json")["data"]
    # The upstream date_c value only has day precision. If the scheduler
    # last ran at noon on May 16, a row dated 16-May-2026 must still
    # survive the filter even though parse_dmy_month_name returns midnight.
    captured: list[httpx.Request] = []
    handler = _make_handler(
        captured=captured,
        payloads={
            "KZ": {"draw": 1, "recordsTotal": 0, "recordsFiltered": 0, "data": []},
            "UZ": {
                "draw": 1,
                "recordsTotal": len(items),
                "recordsFiltered": len(items),
                "data": items,
            },
        },
    )
    transport = httpx.MockTransport(handler)
    connector = TendersinfoConnector(http_client_factory=_client_factory(transport))

    since = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    result = await connector.fetch_latest(since=since)

    ids = {t.external_id for t in result.tenders}
    assert "532912293" in ids


async def test_fetch_latest_500_raises_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _is_listing(request):
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = TendersinfoConnector(http_client_factory=_client_factory(transport))

    with pytest.raises(FetchError):
        await connector.fetch_latest()


async def test_fetch_latest_url_encoded_body_carries_required_fields() -> None:
    captured: list[httpx.Request] = []
    handler = _make_handler(
        captured=captured,
        payloads={
            "KZ": {"draw": 1, "recordsTotal": 0, "recordsFiltered": 0, "data": []},
            "UZ": {"draw": 1, "recordsTotal": 0, "recordsFiltered": 0, "data": []},
        },
    )
    transport = httpx.MockTransport(handler)
    connector = TendersinfoConnector(http_client_factory=_client_factory(transport))

    await connector.fetch_latest()

    listing_calls = [r for r in captured if _is_listing(r)]
    assert listing_calls
    # Content-Type is the DataTables-required form encoding.
    ct = listing_calls[0].headers.get("Content-Type", "").lower()
    assert "application/x-www-form-urlencoded" in ct
    body = _body_form(listing_calls[0])
    # The columns boilerplate is positional; verify a couple of slots.
    assert body.get("columns[0][data]") == "site_tender_id"
    assert body.get("columns[4][data]") == "short_desc"
    assert body.get("length") == "100"
    assert body.get("notice_type") == "1, 3, 8"


# ---------------------------------------------------------------------------
# End-to-end matcher proof
# ---------------------------------------------------------------------------


def test_matcher_fires_on_english_title() -> None:
    """Build a TenderUpsert via the real ``_normalize`` path on a row
    whose title contains "ESG audit", then run the matcher against
    the live ``config/keywords.yaml``. The ``esg`` group MUST fire
    -- this is the first end-to-end proof that the English keywords
    in the YAML are reachable through our pipeline.
    """
    items = _read_fixture("listing_kz.json")["data"]
    esg_row = items[1]  # "ESG Audit And Sustainability Reporting Services..."
    assert "ESG" in esg_row["short_desc"]

    upsert = TendersinfoConnector()._normalize(esg_row)
    config = KeywordsConfig.load(KEYWORDS_PATH)
    result = match_tender(upsert, config)

    assert result.is_match
    assert "esg" in result.matched_groups
    # The "ESG audit" phrase is the explicit trigger here; the bare
    # "ESG" token also fires.
    esg_details = result.match_details["esg"]
    assert any(
        phrase.lower() == "esg audit" for phrase in esg_details["matched_phrases"]
    )
    assert "ESG" in esg_details["matched_tokens"]


def test_matcher_fires_on_credit_rating_title() -> None:
    """The UZ fixture's row 2 is a credit-rating tender. Same
    matcher pipeline; this time it's the ``credit_rating`` group.
    """
    items = _read_fixture("listing_uz.json")["data"]
    cr_row = items[1]
    assert "Credit Rating" in cr_row["short_desc"]

    upsert = TendersinfoConnector()._normalize(cr_row)
    config = KeywordsConfig.load(KEYWORDS_PATH)
    result = match_tender(upsert, config)

    assert result.is_match
    assert "credit_rating" in result.matched_groups


def test_synthetic_upsert_carries_english_title_to_haystack() -> None:
    """Cheap sanity: a TenderUpsert with an ESG-bearing title is
    visible to the matcher even without going through the connector
    -- proves the title-only haystack path, independent of the
    _lots-walk that only picks up _ru/name/description keys.
    """
    upsert = TenderUpsert(
        source_name="tendersinfo",
        external_id="x",
        title="ESG audit services",
        country=Country.UZ,
        status=TenderStatus.open,
        source_url="https://example.com/",
        language=Language.en,
        raw_json={},
    )
    config = KeywordsConfig.load(KEYWORDS_PATH)
    result = match_tender(upsert, config)
    assert "esg" in result.matched_groups
