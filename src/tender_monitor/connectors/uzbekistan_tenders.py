"""Connector for uzbekistantenders.com — an English-language UZ-only
commercial procurement aggregator.

Third HTML scraper (after mitwork and national_bank); second commercial
aggregator (after tendersinfo). It's the sparsest source we ingest so
far -- the homepage gives us title, ref number, deadline, and value;
buyer name and published_at are simply not surfaced in v1.

Shape notes that differ from the other connectors:

- Single-page scrape: only the homepage is fetched. The page is a
  "Latest Uzbekistan Tenders" teaser carrying ~10 cards; we don't
  paginate and we don't fetch detail pages. If we later need depth,
  follow-up probe.
- Country and language are hardcoded: ``Country.UZ`` /
  ``Language.en`` (the host is UZ-only by name and serves English).
- ``buyer_name`` and ``published_at`` are always ``None`` -- not in
  the listing. The matcher's haystack still works because
  ``tender.title`` is in it unconditionally.
- ``deadline_at`` is the only date we have, so it doubles as the
  ``since`` filter axis. We keep rows whose deadline didn't parse
  (don't drop data on parser hiccups).
- The page mixes "authority cards" (links to per-authority listings
  with no content row) and real tender cards. Real cards always
  carry a ``div.tender-card-content`` row -- that's the filter.
- ``Tender Value`` is sometimes a parseable number+currency
  (``"41200000 UZS"``, ``"208500 USD"``) and sometimes the placeholder
  ``"Refer Document"``. The credit-rating tender is one of the
  Refer-Document rows. ``value_amount`` is None whenever we can't
  pull a number out.
- Same upstream tender can appear on TendersInfo with a different
  ``external_id`` (the aggregators are independent of each other).
  We accept the duplication; cross-source dedup is a separate work
  item.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

import httpx
from selectolax.parser import HTMLParser, Node

from tender_monitor.connectors._html import parse_full_month_date
from tender_monitor.connectors.base import Connector
from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.http import make_client, with_retry
from tender_monitor.connectors.registry import register
from tender_monitor.core.enums import Country, Language, TenderStatus
from tender_monitor.core.logging import get_logger
from tender_monitor.core.schemas import TenderUpsert

logger = get_logger(__name__)


# NBSP escape so editor/Write-tool passes can't silently swap it
# for a regular space (we look it up by code point downstream).
_NBSP = "\u00a0"
_WS_RE = re.compile(r"\s+")


def _norm_text(node: Node | None) -> str:
    if node is None:
        return ""
    # Replace NBSP with a regular space, then collapse runs of
    # whitespace -- selectolax preserves both, and the page uses
    # them inconsistently.
    return _WS_RE.sub(" ", node.text().replace(_NBSP, " ")).strip()


def parse_value_text(text: str | None) -> tuple[Decimal | None, str | None]:
    """Split ``"41200000 UZS"`` / ``"Refer Document"`` / ``""`` into
    ``(amount, currency)``.

    The currency token is the last whitespace-delimited word IF it's
    alphabetic and 3 characters long (ISO-4217-ish). Anything else
    (``"Refer Document"``, empty, garbage) yields ``(None, None)``.
    """
    if not text:
        return None, None
    cleaned = _WS_RE.sub(" ", text.replace(_NBSP, " ")).strip()
    if not cleaned:
        return None, None
    parts = cleaned.split()
    if not parts:
        return None, None
    last = parts[-1]
    if last.isalpha() and len(last) == 3 and len(parts) >= 2:
        currency = last.upper()
        amount_str = "".join(parts[:-1])
        try:
            return Decimal(amount_str), currency
        except (InvalidOperation, ValueError):
            return None, currency
    return None, None


# Label substrings we look for inside each col-cell. Order doesn't
# matter on the page (the cells are in a fixed order but we don't
# rely on that).
_LABEL_REF = "UZT Ref No"
_LABEL_DEADLINE = "Deadline"
_LABEL_VALUE = "Tender Value"


def _strip_label(text: str, label: str) -> str:
    """Trim everything up to and including ``label`` plus the trailing
    punctuation and whitespace.

    The page renders cells as ``"<icon> <label>.: <value>"`` -- the
    ``"UZT Ref No"`` label is followed by a literal period before
    the colon, so we strip ``.``, ``:`` and whitespace until we hit
    the value. If the label isn't present, returns the original
    (stripped) text -- caller decides what to do with it.
    """
    idx = text.find(label)
    if idx < 0:
        return text.strip()
    remainder = text[idx + len(label) :].lstrip(".: \t")
    return remainder.strip()


def _parse_card(card: Node) -> dict[str, Any] | None:
    """Pull the four data points off one ``<div class="tender-card">``.

    Returns ``None`` for cards that are missing the title or the ref
    number -- the page mixes real tender cards with "authority cards"
    that share the same ``tender-card`` class but no content row,
    and we want the extractor to silently skip them.
    """
    heading = card.css_first("p.tender-card-heading")
    title = _norm_text(heading)
    if not title:
        return None

    content = card.css_first("div.tender-card-content")
    if content is None:
        # Authority card (link-only); no data row → not a real tender.
        return None

    # Prefer the heading's parent <a> for the detail URL; fall back to
    # the View Details button if the heading isn't wrapped.
    detail_url = ""
    if heading is not None and heading.parent is not None:
        parent = heading.parent
        if parent.tag == "a":
            detail_url = (parent.attributes.get("href") or "").strip()
    if not detail_url:
        view_btn = card.css_first("a.Viewbutton")
        if view_btn is not None:
            detail_url = (view_btn.attributes.get("href") or "").strip()

    external_id = ""
    deadline_text = ""
    value_text: str | None = None

    for col in content.css("div"):
        plain = _norm_text(col)
        if not plain:
            continue
        if _LABEL_REF in plain:
            external_id = _strip_label(plain, _LABEL_REF)
        elif _LABEL_DEADLINE in plain:
            deadline_text = _strip_label(plain, _LABEL_DEADLINE)
        elif _LABEL_VALUE in plain:
            value_text = _strip_label(plain, _LABEL_VALUE) or None

    if not external_id:
        return None

    return {
        "external_id": external_id,
        "title": title,
        "detail_url": detail_url,
        "deadline_text": deadline_text,
        "value_text": value_text,
    }


def _extract_cards(html: str) -> list[dict[str, Any]]:
    """Walk all ``<div class="tender-card">`` nodes; return only the
    well-formed ones (real tenders, not authority navigation cards).

    Cards we skip are logged at DEBUG -- a noisy WARNING on every
    authority card on every run wouldn't help anyone.
    """
    parser = HTMLParser(html)
    out: list[dict[str, Any]] = []
    for card in parser.css("div.tender-card"):
        parsed = _parse_card(card)
        if parsed is None:
            continue
        out.append(parsed)
    return out


@register
class UzbekistanTendersConnector(Connector):
    source_name: ClassVar[str] = "uzbekistan_tenders"

    LISTING_URL: ClassVar[str] = "https://www.uzbekistantenders.com/"

    REQUIRED_HEADERS: ClassVar[dict[str, str]] = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
    }

    def _make_client(self) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return make_client(headers=self.REQUIRED_HEADERS)

    @with_retry(max_attempts=3)
    async def _do_listing_request(
        self, client: httpx.AsyncClient
    ) -> httpx.Response:
        response = await client.get(self.LISTING_URL)
        response.raise_for_status()
        return response

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        async with self._make_client() as client:
            try:
                response = await self._do_listing_request(client)
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.HTTPStatusError,
            ) as exc:
                raise FetchError(
                    f"uzbekistan_tenders listing failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

        cards = _extract_cards(response.text)
        logger.info(
            "uzbekistan_tenders.listing_complete",
            cards_collected=len(cards),
        )

        if since is None:
            return cards

        in_window: list[dict[str, Any]] = []
        for card in cards:
            deadline = parse_full_month_date(card.get("deadline_text"))
            # Keep cards whose deadline didn't parse -- no published
            # date means deadline is the only filter axis, and a
            # parser hiccup shouldn't silently drop data.
            if deadline is None or deadline >= since:
                in_window.append(card)
        logger.info(
            "uzbekistan_tenders.since_filter_applied",
            input_items=len(cards),
            kept=len(in_window),
            since=since.isoformat(),
        )
        return in_window

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        external_id = raw.get("external_id")
        if not external_id:
            raise ParseError("uzbekistan_tenders card is missing external_id")

        title = raw.get("title")
        if not title:
            raise ParseError(
                f"uzbekistan_tenders tender {external_id} has empty title"
            )

        value_amount, value_currency = parse_value_text(raw.get("value_text"))
        deadline_at = parse_full_month_date(raw.get("deadline_text"))

        source_url = raw.get("detail_url") or self.LISTING_URL

        raw_json: dict[str, Any] = dict(raw)
        raw_json["_lots"] = [
            {
                "name_en": title,
                "description_en": None,
            }
        ]

        return TenderUpsert(
            source_name=self.source_name,
            external_id=str(external_id),
            title=title,
            buyer_name=None,
            buyer_external_id=None,
            country=Country.UZ,
            sector=None,
            value_amount=value_amount,
            value_currency=value_currency,
            published_at=None,
            deadline_at=deadline_at,
            status=TenderStatus.open,
            source_url=source_url,
            language=Language.en,
            raw_json=raw_json,
        )


__all__ = [
    "UzbekistanTendersConnector",
    "_extract_cards",
    "_parse_card",
    "parse_value_text",
]
