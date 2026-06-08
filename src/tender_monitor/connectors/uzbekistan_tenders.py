"""Connector for uzbekistantenders.com — an English-language UZ-only
commercial procurement aggregator.

Third HTML scraper (after mitwork and national_bank); second commercial
aggregator (after tendersinfo). We crawl the real paginated tenders
index at ``/tenders.php`` and enrich each listing card from its detail
page metadata, which is where the aggregator exposes buyer, publish
date, and longer descriptions.

Shape notes that differ from the other connectors:

- Multi-page scrape: we fetch ``/tenders.php`` and then follow the
  page-number pattern ``/tenders.php/<n>`` up to ``MAX_PAGES`` or
  until the pagination block says there is no next page.
- Detail enrichment is best-effort: listing fetch failures still fail
  the whole run, but a broken/missing detail page only logs a warning
  and keeps the listing-only card.
- Country and language are hardcoded: ``Country.UZ`` /
  ``Language.en`` (the host is UZ-only by name and serves English).
- Detail descriptions are copied into ``raw_json["_lots"]`` so the
  shared matcher can see ESG / credit terms that are absent from the
  listing title.
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

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urljoin

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


def _has_next_page(html: str) -> bool:
    parser = HTMLParser(html)
    for link in parser.css("ul.pagination a"):
        label = _norm_text(link)
        if label.casefold().startswith("next"):
            return True
    return False


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


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _WS_RE.sub(" ", value.replace(_NBSP, " ")).strip()
    return cleaned or None


def _parse_detail_date(text: str | None) -> datetime | None:
    parsed = parse_full_month_date(text)
    if parsed is not None:
        return parsed
    cleaned = _clean_string(text)
    if cleaned is None:
        return None
    try:
        iso = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None
    if iso.tzinfo is None:
        return iso.replace(tzinfo=UTC)
    return iso.astimezone(UTC)


def _iter_jsonld_objects(payload: Any) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        objects.append(payload)
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    objects.append(item)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                objects.extend(_iter_jsonld_objects(item))
    return objects


def _is_offer_jsonld(obj: dict[str, Any]) -> bool:
    type_value = obj.get("@type")
    if isinstance(type_value, str):
        return type_value.casefold() == "offer"
    if isinstance(type_value, list):
        return any(
            isinstance(item, str) and item.casefold() == "offer"
            for item in type_value
        )
    return False


def _parse_detail_page(html: str) -> dict[str, Any]:
    """Extract match-worthy metadata from one tender detail page.

    UzbekistanTenders exposes its useful detail data through JSON-LD.
    We keep the parser tolerant because detail enrichment should not
    sink an otherwise valid listing row.
    """
    parser = HTMLParser(html)
    parsed: dict[str, Any] = {}

    meta_description = parser.css_first('meta[name="description"]')
    if meta_description is not None:
        description = _clean_string(meta_description.attributes.get("content"))
        if description is not None:
            parsed["detail_meta_description"] = description

    offer: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None
    for script in parser.css('script[type="application/ld+json"]'):
        script_text = script.text().strip()
        if not script_text:
            continue
        try:
            payload = json.loads(script_text)
        except json.JSONDecodeError:
            continue
        for obj in _iter_jsonld_objects(payload):
            if _is_offer_jsonld(obj):
                offer = obj
                break
            if fallback is None and (
                "description" in obj or "identifier" in obj
            ):
                fallback = obj
        if offer is not None:
            break

    detail = offer or fallback
    if detail is None:
        return parsed

    description = _clean_string(detail.get("description"))
    if description is not None:
        parsed["detail_description"] = description
    identifier = _clean_string(detail.get("identifier"))
    if identifier is not None:
        parsed["detail_identifier"] = identifier
    starts = _clean_string(detail.get("availabilityStarts"))
    if starts is not None:
        parsed["published_text_detail"] = starts
    ends = _clean_string(detail.get("availabilityEnds"))
    if ends is not None:
        parsed["deadline_text_detail"] = ends
    category = _clean_string(detail.get("category"))
    if category is not None:
        parsed["detail_category"] = category
    price = _clean_string(detail.get("price"))
    if price is not None:
        parsed["detail_price"] = price
    currency = _clean_string(detail.get("priceCurrency"))
    if currency is not None:
        parsed["detail_price_currency"] = currency

    offered_by = detail.get("offeredBy")
    if isinstance(offered_by, dict):
        buyer_name = _clean_string(offered_by.get("name"))
        if buyer_name is not None:
            parsed["buyer_name_detail"] = buyer_name

    parsed["_detail_jsonld"] = detail
    return parsed


@register
class UzbekistanTendersConnector(Connector):
    source_name: ClassVar[str] = "uzbekistan_tenders"

    LISTING_URL: ClassVar[str] = "https://www.uzbekistantenders.com/tenders.php"
    MAX_PAGES: ClassVar[int] = 20

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
        self, client: httpx.AsyncClient, page: int
    ) -> httpx.Response:
        url = self.LISTING_URL if page <= 1 else f"{self.LISTING_URL}/{page}"
        response = await client.get(url)
        response.raise_for_status()
        return response

    @with_retry(max_attempts=3)
    async def _do_detail_request(
        self, client: httpx.AsyncClient, detail_url: str
    ) -> httpx.Response:
        url = urljoin(self.LISTING_URL, detail_url)
        response = await client.get(url, headers={"Referer": self.LISTING_URL})
        response.raise_for_status()
        return response

    async def _fetch_listing_page(
        self, client: httpx.AsyncClient, page: int
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            response = await self._do_listing_request(client, page)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            raise FetchError(
                f"uzbekistan_tenders listing page={page} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        html = response.text
        return _extract_cards(html), _has_next_page(html)

    async def _fetch_detail_page(
        self, client: httpx.AsyncClient, card: dict[str, Any]
    ) -> dict[str, Any]:
        detail_url = card.get("detail_url")
        if not isinstance(detail_url, str) or not detail_url:
            return {}
        external_id = card.get("external_id")
        try:
            response = await self._do_detail_request(client, detail_url)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            logger.warning(
                "uzbekistan_tenders.detail_fetch_failed",
                external_id=external_id,
                detail_url=detail_url,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return {}
        return _parse_detail_page(response.text)

    async def _enrich_cards_with_details(
        self, client: httpx.AsyncClient, cards: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for card in cards:
            detail = await self._fetch_detail_page(client, card)
            if detail:
                enriched.append({**card, **detail})
            else:
                enriched.append(card)
        return enriched

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        pages_walked = 0
        accumulated: list[dict[str, Any]] = []
        seen_external_ids: set[str] = set()

        async with self._make_client() as client:
            for page in range(1, self.MAX_PAGES + 1):
                cards, has_next = await self._fetch_listing_page(client, page)
                pages_walked = page
                if not cards:
                    break
                for card in cards:
                    external_id = str(card.get("external_id") or "")
                    if not external_id or external_id in seen_external_ids:
                        continue
                    seen_external_ids.add(external_id)
                    accumulated.append(card)
                if not has_next:
                    break

            logger.info(
                "uzbekistan_tenders.listing_complete",
                pages_walked=pages_walked,
                cards_collected=len(accumulated),
            )

            if since is None:
                return await self._enrich_cards_with_details(client, accumulated)

            in_window: list[dict[str, Any]] = []
            for card in accumulated:
                deadline = parse_full_month_date(card.get("deadline_text"))
                # Keep cards whose deadline didn't parse -- no published
                # date means deadline is the only filter axis, and a
                # parser hiccup shouldn't silently drop data.
                if deadline is None or deadline >= since:
                    in_window.append(card)
            logger.info(
                "uzbekistan_tenders.since_filter_applied",
                input_items=len(accumulated),
                kept=len(in_window),
                since=since.isoformat(),
            )
            return await self._enrich_cards_with_details(client, in_window)

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
        if value_amount is None:
            value_amount, value_currency = parse_value_text(raw.get("detail_price"))
            if value_currency is None:
                detail_currency = raw.get("detail_price_currency")
                value_currency = (
                    detail_currency if isinstance(detail_currency, str) else None
                )
        deadline_at = _parse_detail_date(
            raw.get("deadline_text_detail")
        ) or parse_full_month_date(raw.get("deadline_text"))
        published_at = _parse_detail_date(raw.get("published_text_detail"))
        raw_buyer_name = raw.get("buyer_name_detail")
        buyer_name = raw_buyer_name if isinstance(raw_buyer_name, str) else None
        raw_description = raw.get("detail_description") or raw.get(
            "detail_meta_description"
        )
        description = (
            raw_description if isinstance(raw_description, str) else None
        )

        source_url = raw.get("detail_url") or self.LISTING_URL

        raw_json: dict[str, Any] = dict(raw)
        raw_json["_lots"] = [
            {
                "name_en": title,
                "description_en": description,
            }
        ]

        return TenderUpsert(
            source_name=self.source_name,
            external_id=str(external_id),
            title=title,
            buyer_name=buyer_name,
            buyer_external_id=None,
            country=Country.UZ,
            sector=None,
            value_amount=value_amount,
            value_currency=value_currency,
            published_at=published_at,
            deadline_at=deadline_at,
            status=TenderStatus.open,
            source_url=source_url,
            language=Language.en,
            raw_json=raw_json,
        )


__all__ = [
    "UzbekistanTendersConnector",
    "_extract_cards",
    "_has_next_page",
    "_parse_card",
    "_parse_detail_date",
    "_parse_detail_page",
    "parse_value_text",
]
