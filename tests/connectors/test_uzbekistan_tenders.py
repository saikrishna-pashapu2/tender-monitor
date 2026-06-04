"""Tests for the UzbekistanTenders.com connector.

All offline; HTTP is exercised through ``httpx.MockTransport``. The
fixture under ``tests/fixtures/uzbekistan_tenders/listing.html`` is
a verbatim capture of the live listing page including the credit-rating
tender we use for the end-to-end matcher check.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from selectolax.parser import HTMLParser

from tender_monitor.connectors._html import parse_full_month_date
from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.http import make_client
from tender_monitor.connectors.uzbekistan_tenders import (
    UzbekistanTendersConnector,
    _extract_cards,
    _has_next_page,
    _parse_card,
    parse_value_text,
)
from tender_monitor.core.enums import Country, Language, TenderStatus
from tender_monitor.matching import KeywordsConfig, match_tender

LISTING_PATH = "/tenders.php"
FIXTURES_DIR = (
    Path(__file__).parent.parent / "fixtures" / "uzbekistan_tenders"
)
KEYWORDS_PATH = (
    Path(__file__).parent.parent.parent / "config" / "keywords.yaml"
)
NBSP = "\u00a0"


def _read_fixture() -> str:
    return (FIXTURES_DIR / "listing.html").read_text(encoding="utf-8")


def _client_factory(
    transport: httpx.MockTransport,
) -> Callable[[], httpx.AsyncClient]:
    def _make() -> httpx.AsyncClient:
        return make_client(
            headers=UzbekistanTendersConnector.REQUIRED_HEADERS, transport=transport
        )

    return _make


def _is_listing(request: httpx.Request) -> bool:
    return request.url.path.startswith(LISTING_PATH) and request.method == "GET"


def _credit_rating_card() -> dict[str, str]:
    """Find and return the credit-rating card dict in the fixture."""
    cards = _extract_cards(_read_fixture())
    for c in cards:
        title = c.get("title") or ""
        if "credit rating" in title.lower():
            return c  # type: ignore[return-value]
    raise AssertionError(
        "fixture is expected to carry the credit-rating tender; got "
        f"{len(cards)} cards with no matching title"
    )


# ---------------------------------------------------------------------------
# Date parser
# ---------------------------------------------------------------------------


def test_parse_full_month_date_happy_path() -> None:
    result = parse_full_month_date("30 May 2026")
    assert result == datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC)


def test_parse_full_month_date_handles_abbreviated_month() -> None:
    # The aggregator emits "02 Jun 2026" -- three-letter abbreviation.
    # We accept both full-name and abbreviated forms.
    assert parse_full_month_date("02 Jun 2026") == datetime(
        2026, 6, 2, 0, 0, 0, tzinfo=UTC
    )


@pytest.mark.parametrize("text", [None, "", "   "])
def test_parse_full_month_date_handles_empty(text: str | None) -> None:
    assert parse_full_month_date(text) is None


@pytest.mark.parametrize("text", ["not-a-date", "30-May-2026", "2026-05-30"])
def test_parse_full_month_date_handles_garbage(text: str) -> None:
    assert parse_full_month_date(text) is None


# ---------------------------------------------------------------------------
# Value parser
# ---------------------------------------------------------------------------


def test_parse_value_text_with_currency() -> None:
    assert parse_value_text("41200000 UZS") == (
        Decimal("41200000"),
        "UZS",
    )
    assert parse_value_text("208500 USD") == (Decimal("208500"), "USD")


def test_parse_value_text_refer_document() -> None:
    # Aggregator placeholder; no parseable amount.
    assert parse_value_text("Refer Document") == (None, None)


def test_parse_value_text_handles_nbsp() -> None:
    assert parse_value_text(f"41200000{NBSP}UZS") == (
        Decimal("41200000"),
        "UZS",
    )
    # Multiple NBSPs and a regular space mixed.
    assert parse_value_text(f"41200000{NBSP}{NBSP} UZS") == (
        Decimal("41200000"),
        "UZS",
    )


def test_parse_value_text_empty() -> None:
    assert parse_value_text(None) == (None, None)
    assert parse_value_text("") == (None, None)
    assert parse_value_text("   ") == (None, None)


# ---------------------------------------------------------------------------
# Card parser
# ---------------------------------------------------------------------------


def test_parse_card_extracts_all_fields() -> None:
    """Pick a real-tender card (one with a content row) from the
    fixture and assert every field is populated."""
    parser = HTMLParser(_read_fixture())
    real_cards = [
        c
        for c in parser.css("div.tender-card")
        if c.css_first("div.tender-card-content") is not None
    ]
    assert real_cards, "fixture must have at least one real tender card"
    parsed = _parse_card(real_cards[0])
    assert parsed is not None
    assert parsed["external_id"].isdigit()
    assert parsed["title"]
    assert parsed["detail_url"].startswith(
        "https://www.uzbekistantenders.com/tender/"
    )
    # Deadline strings the live page emits look like "DD Mon YYYY".
    assert parsed["deadline_text"]
    # value_text can be a parseable string or "Refer Document"; both
    # are non-None for cards that carry the Tender Value cell.
    assert parsed["value_text"]


def test_parse_card_missing_heading_returns_none() -> None:
    html = (
        '<div class="tender-card">'
        '<div class="row tender-card-content">'
        '<div class="col-md-3"><p>UZT Ref No.: 999</p></div>'
        "</div></div>"
    )
    card = HTMLParser(html).css_first("div.tender-card")
    assert card is not None
    assert _parse_card(card) is None


def test_parse_card_authority_card_returns_none() -> None:
    """Authority links share the ``tender-card`` class but have no
    content row; the extractor must skip them silently."""
    html = (
        '<div class="tender-card">'
        '<a href="/authority/foo/">'
        '<p class="tender-card-heading">Foo Authority Tenders</p>'
        "</a></div>"
    )
    card = HTMLParser(html).css_first("div.tender-card")
    assert card is not None
    assert _parse_card(card) is None


def test_extract_cards_filters_to_real_tenders() -> None:
    """Spot-check: the fixture mixes authority + real cards; the
    extractor returns only the latter."""
    cards = _extract_cards(_read_fixture())
    # Every returned card has a non-empty external_id and a title.
    assert cards, "fixture must yield at least one real tender card"
    for c in cards:
        assert c["external_id"]
        assert c["title"]


def test_has_next_page_detects_pagination() -> None:
    assert _has_next_page(_read_fixture()) is True


def test_has_next_page_false_when_next_missing() -> None:
    html = """
    <html><body>
      <ul class="pagination">
        <li class="active"><a href="/tenders.php">1</a></li>
        <li><a href="/tenders.php/2">2</a></li>
      </ul>
    </body></html>
    """
    assert _has_next_page(html) is False


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_happy_path() -> None:
    card = _credit_rating_card()
    upsert = UzbekistanTendersConnector()._normalize(card)

    assert upsert.source_name == "uzbekistan_tenders"
    assert upsert.external_id == card["external_id"]
    assert (
        upsert.title
        == "Provision of services for assigning an international credit rating"
    )
    assert upsert.country is Country.UZ
    assert upsert.language is Language.en
    assert upsert.status is TenderStatus.open
    # Credit-rating card carries "Refer Document" → no amount.
    assert upsert.value_amount is None
    assert upsert.value_currency is None
    assert upsert.buyer_name is None
    assert upsert.published_at is None
    assert upsert.deadline_at == datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC)
    assert "/tender/" in upsert.source_url


def test_normalize_credit_rating_card_matches_keyword_filter() -> None:
    """End-to-end: the credit-rating tender from this source fires
    the same matcher group as the TendersInfo equivalent. Pins the
    English-keyword pipeline through this connector."""
    upsert = UzbekistanTendersConnector()._normalize(_credit_rating_card())
    config = KeywordsConfig.load(KEYWORDS_PATH)
    result = match_tender(upsert, config)
    assert result.is_match
    assert "credit_rating" in result.matched_groups
    cr = result.match_details["credit_rating"]
    assert any(
        phrase.lower() == "credit rating" for phrase in cr["matched_phrases"]
    )


def test_normalize_empty_title_raises() -> None:
    with pytest.raises(ParseError, match="empty title"):
        UzbekistanTendersConnector()._normalize(
            {
                "external_id": "999",
                "title": "",
                "detail_url": "https://example.com/x",
                "deadline_text": "30 May 2026",
                "value_text": None,
            }
        )


def test_normalize_missing_external_id_raises() -> None:
    with pytest.raises(ParseError, match="missing external_id"):
        UzbekistanTendersConnector()._normalize(
            {
                "external_id": "",
                "title": "T",
                "detail_url": "https://example.com/x",
                "deadline_text": "30 May 2026",
                "value_text": None,
            }
        )


# ---------------------------------------------------------------------------
# Fetch pipeline -- MockTransport
# ---------------------------------------------------------------------------


async def test_fetch_latest_full_pipeline() -> None:
    html = _read_fixture()
    expected = len(_extract_cards(html))
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == LISTING_PATH:
            return httpx.Response(200, html=html)
        if request.url.path == f"{LISTING_PATH}/2":
            return httpx.Response(200, html="<html><body></body></html>")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = UzbekistanTendersConnector(
        http_client_factory=_client_factory(transport)
    )
    result = await connector.fetch_latest()

    assert result.source_name == "uzbekistan_tenders"
    assert result.raw_item_count == expected
    assert len(result.tenders) == expected
    assert result.partial_errors == []
    assert all(t.country is Country.UZ for t in result.tenders)
    assert all(t.language is Language.en for t in result.tenders)
    # The fixture advertises a next page, so we probe page 2 before stopping.
    listing_calls = [r for r in captured if _is_listing(r)]
    assert [r.url.path for r in listing_calls] == [LISTING_PATH, f"{LISTING_PATH}/2"]


async def test_fetch_latest_500_raises_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LISTING_PATH:
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = UzbekistanTendersConnector(
        http_client_factory=_client_factory(transport)
    )
    with pytest.raises(FetchError):
        await connector.fetch_latest()


async def test_fetch_latest_since_keeps_unparseable_deadlines() -> None:
    """Synthetic page with one valid card (deadline 30 May 2026)
    and one card with a garbage deadline. With ``since`` set
    AFTER both real dates, the connector keeps the garbage-
    deadline card (deadline parse failed → keep) and drops the
    parseable-but-too-old card."""
    synthetic_html = """
    <html><body><div id="tenderlisting">
      <div class="tender-card">
        <a href="https://www.uzbekistantenders.com/tender/aaa.php">
          <p class="tender-card-heading">Old But Parseable</p>
        </a>
        <div class="row tender-card-content">
          <div><p><i></i> UZT Ref No.:&nbsp;&nbsp;111</p></div>
          <div><p><i></i> Deadline:&nbsp;&nbsp;01 Jan 2020</p></div>
          <div><i></i> Tender Value:&nbsp;&nbsp;1 UZS&nbsp;&nbsp;</div>
          <div><a class="Viewbutton" href="x">View Details</a></div>
        </div>
      </div>
      <div class="tender-card">
        <a href="https://www.uzbekistantenders.com/tender/bbb.php">
          <p class="tender-card-heading">Unparseable Deadline</p>
        </a>
        <div class="row tender-card-content">
          <div><p><i></i> UZT Ref No.:&nbsp;&nbsp;222</p></div>
          <div><p><i></i> Deadline:&nbsp;&nbsp;TBD</p></div>
          <div><i></i> Tender Value:&nbsp;&nbsp;Refer Document&nbsp;&nbsp;</div>
          <div><a class="Viewbutton" href="x">View Details</a></div>
        </div>
      </div>
    </div></body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LISTING_PATH:
            return httpx.Response(200, html=synthetic_html)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = UzbekistanTendersConnector(
        http_client_factory=_client_factory(transport)
    )

    since = datetime(2026, 5, 15, 0, 0, tzinfo=UTC)
    result = await connector.fetch_latest(since=since)

    ids = {t.external_id for t in result.tenders}
    # 111 was 01 Jan 2020 (parseable, before since) → dropped.
    # 222 had "TBD" (unparseable) → kept.
    assert ids == {"222"}


async def test_fetch_latest_paginates_and_dedupes_ids() -> None:
    page1 = """
    <html><body>
      <div class="tender-card">
        <a href="https://www.uzbekistantenders.com/tender/a.php">
          <p class="tender-card-heading">Tender A</p>
        </a>
        <div class="row tender-card-content">
          <div><p>UZT Ref No.: 111</p></div>
          <div><p>Deadline: 10 Jun 2026</p></div>
          <div>Tender Value: 100 UZS</div>
          <div><a class="Viewbutton" href="https://www.uzbekistantenders.com/tender/a.php">View Details</a></div>
        </div>
      </div>
      <ul class="pagination"><li><a href="/tenders.php/2">Next</a></li></ul>
    </body></html>
    """
    page2 = """
    <html><body>
      <div class="tender-card">
        <a href="https://www.uzbekistantenders.com/tender/b.php">
          <p class="tender-card-heading">Tender B</p>
        </a>
        <div class="row tender-card-content">
          <div><p>UZT Ref No.: 222</p></div>
          <div><p>Deadline: 11 Jun 2026</p></div>
          <div>Tender Value: 200 UZS</div>
          <div><a class="Viewbutton" href="https://www.uzbekistantenders.com/tender/b.php">View Details</a></div>
        </div>
      </div>
      <div class="tender-card">
        <a href="https://www.uzbekistantenders.com/tender/a.php">
          <p class="tender-card-heading">Tender A duplicate</p>
        </a>
        <div class="row tender-card-content">
          <div><p>UZT Ref No.: 111</p></div>
          <div><p>Deadline: 10 Jun 2026</p></div>
          <div>Tender Value: 100 UZS</div>
          <div><a class="Viewbutton" href="https://www.uzbekistantenders.com/tender/a.php">View Details</a></div>
        </div>
      </div>
    </body></html>
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == LISTING_PATH:
            return httpx.Response(200, html=page1)
        if request.url.path == f"{LISTING_PATH}/2":
            return httpx.Response(200, html=page2)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    connector = UzbekistanTendersConnector(
        http_client_factory=_client_factory(transport)
    )

    result = await connector.fetch_latest()

    assert {t.external_id for t in result.tenders} == {"111", "222"}
    assert [r.url.path for r in captured] == [LISTING_PATH, f"{LISTING_PATH}/2"]
