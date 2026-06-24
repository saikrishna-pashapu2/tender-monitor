"""Connector for xt-xarid.uz — Uzbek state procurement portal.

Second UZ source. Shape notes that differ from uzex_etender (the
first UZ source):

- **JSON-RPC 2.0** envelope, not plain JSON POST. The request body
  wraps ``{"id": 1, "jsonrpc": "2.0", "method": "ref", "params":
  {...}}``; the response is ``{"result": [...], "error": ...}``. We
  treat a non-null/non-empty ``error`` as a hard ``FetchError`` so
  the scheduler marks the source failed; the API never returns 200
  with an error body for valid auth, so any error here is a real
  problem.

- **Title comes from ``meta.good_maps[].name``**, NOT the top-level
  ``name`` field which is a generic ``"Тендер"`` placeholder. The
  ``good_maps`` array is row-per-line-item and frequently carries
  the same name dozens of times (one row per delivery batch). We
  dedupe by case-insensitive stripped name, preserve first-seen
  order, and join distinct names with `` | ``. A tender with zero
  usable names is a ``ParseError`` — the title is the only signal
  the keyword matcher has, so a blank title is a row we shouldn't
  store.

- **``publicated_at`` and ``close_at`` are nullable.** Pre-publication
  states like ``"docs_objections"`` (objection window) have not
  reached the visible-to-suppliers stage yet and the API omits the
  publish timestamp. We store nulls; the scheduler's TRACKED_FIELDS
  comparator already handles null → value transitions, so the
  "moved into open status" event will fall out naturally on a later
  run.

- **No signed-token headers required.** The DevTools capture has
  ``x-idempotency-key`` and ``x-url-on``, but those are client-side
  dedup/tracing metadata — the server doesn't check them. Filing
  the headers we DO send under "minimal-cookie-style hygiene": SPA
  identity (Origin + Referer + Accept-Language) plus the language
  marker so the API echoes Russian-localized region names where
  available.

- **``filters: {}``** in v1 — we ingest everything including
  pre-publication states. The status taxonomy beyond
  ``"docs_objections"`` is unknown; ``STATUS_MAPPING`` returns
  ``unknown`` for everything else and we'll expand as live data
  arrives.

- **No early-stop on ``since``.** The listing's default sort order
  isn't documented; assuming "newest first" and breaking on the
  first older hit would silently drop legitimately newer tenders
  that happen to sit later in the page. We paginate to
  a generous ``MAX_PAGES`` then filter by ``publicated_at >= since``
  after the fact, keeping items whose ``publicated_at`` is null (we
  have no basis to exclude — they may have changed state since the
  last run).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import httpx

from tender_monitor.connectors._html import TASHKENT_TZ
from tender_monitor.connectors.base import Connector
from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.http import make_client, with_retry
from tender_monitor.connectors.registry import register
from tender_monitor.core.enums import Country, Language, TenderStatus
from tender_monitor.core.logging import get_logger
from tender_monitor.core.schemas import TenderUpsert

logger = get_logger(__name__)


# Status string from the API → our TenderStatus. Everything not listed
# falls through to TenderStatus.unknown so a genuinely new upstream
# state never silently looks open.
STATUS_MAPPING: dict[str, TenderStatus] = {
    "cancel": TenderStatus.cancelled,
    "check_affilation_and_debts": TenderStatus.closed,
    "check_docs": TenderStatus.closed,
    "close": TenderStatus.closed,
    "docs_objections": TenderStatus.announced,
    "not_realized": TenderStatus.cancelled,
    "open": TenderStatus.open,
}


def map_status(value: str | None) -> TenderStatus:
    if not isinstance(value, str) or not value:
        return TenderStatus.unknown
    return STATUS_MAPPING.get(value, TenderStatus.unknown)


def _parse_iso_maybe(text: str | None) -> datetime | None:
    """Parse an ISO-ish timestamp.

    The xt-xarid API has been observed to emit both naive and
    offset-aware strings (we have not yet seen an offset variant
    in the captures but the API is inconsistent across sibling
    Uzbek portals, so we hedge). Naive values are treated as
    Asia/Tashkent local — same convention as uzex_etender — and
    converted to UTC. Offset-aware values are respected and
    converted to UTC. ``None`` / empty / unparseable → ``None``.
    """
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return parsed.replace(tzinfo=TASHKENT_TZ).astimezone(UTC)


def _build_title(raw: dict[str, Any]) -> str:
    """Build a tender title from ``meta.good_maps[].name``.

    Dedupe case-insensitively on the stripped value, preserve
    first-seen order, join with `` | ``. Raises ``ParseError`` if
    there are no usable names — the title is the only signal the
    keyword matcher has against this source, so blank titles can't
    be allowed through.
    """
    meta = raw.get("meta") or {}
    good_maps = meta.get("good_maps") or []
    seen: set[str] = set()
    titles: list[str] = []
    for gm in good_maps:
        if not isinstance(gm, dict):
            continue
        name = (gm.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        titles.append(name)
    if not titles:
        raise ParseError(
            f"no good_maps names for tender {raw.get('id')}"
        )
    return " | ".join(titles)


def _string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _build_lots(raw: dict[str, Any], title: str) -> list[dict[str, Any]]:
    """Expose match-worthy ``good_maps`` text through ``raw_json["_lots"]``.

    The source's full payload is still stored unchanged in ``raw_json``.
    This projection exists so the generic matcher and detail UI can
    inspect each distinct line-item name/description without knowing
    XT-Xarid's nested ``meta.good_maps`` shape.
    """
    raw_meta = raw.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    good_maps = meta.get("good_maps")
    if not isinstance(good_maps, list):
        good_maps = []

    lots: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for gm in good_maps:
        if not isinstance(gm, dict):
            continue
        name = _string_or_none(gm.get("name"))
        description = (
            _string_or_none(gm.get("description"))
            or _string_or_none(gm.get("description_ru"))
            or _string_or_none(gm.get("technical_description"))
            or _string_or_none(gm.get("characteristic"))
        )
        if name is None:
            continue
        key = (name.casefold(), description.casefold() if description else None)
        if key in seen:
            continue
        seen.add(key)

        lot: dict[str, Any] = {
            "name_ru": name,
            "description_ru": description,
        }
        optional_fields = {
            "lot_id": "lot_id",
            "id": "classification_code",
            "amount": "quantity",
            "unit": "unit",
            "price": "unit_price",
            "totalcost_item": "total_amount",
        }
        for source_key, target_key in optional_fields.items():
            value = gm.get(source_key)
            if value not in (None, ""):
                lot[target_key] = value
        lots.append(lot)

    if lots:
        return lots
    return [{"name_ru": title, "description_ru": None}]


@register
class XtXaridConnector(Connector):
    source_name: ClassVar[str] = "xt_xarid"

    LISTING_URL: ClassVar[str] = "https://api.xt-xarid.uz/rpc"
    PAGE_SIZE: ClassVar[int] = 50
    MAX_PAGES: ClassVar[int] = 20
    LISTING_FIELDS: ClassVar[list[str]] = [
        "green",
        "id",
        "publicated_at",
        "status",
        "name",
        "good_count",
        "close_at",
        "totalcost",
        "currency",
        "lang",
        "part_count",
        "meta",
        "remain_time",
        "lot_count",
        "docs_objections_remain_time",
        "close_docs_objections_at",
    ]

    REQUIRED_HEADERS: ClassVar[dict[str, str]] = {
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Content-Type": "application/json;charset=utf-8",
        "Origin": "https://xt-xarid.uz",
        "Referer": "https://xt-xarid.uz/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        # The portal's SPA sends this; the API echoes Cyrillic-Uzbek
        # localized strings (region names, etc.) when it's set.
        "x-dbrpc-language": "uz_UZ@cyrillic",
    }

    def _make_client(self) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return make_client(headers=self.REQUIRED_HEADERS)

    def _build_rpc_body(self, offset: int) -> dict[str, Any]:
        return {
            "id": 1,
            "jsonrpc": "2.0",
            "method": "ref",
            "params": {
                "ref": "ref_tender_public",
                "op": "read",
                "limit": self.PAGE_SIZE,
                "offset": offset,
                "filters": {},
                "fields": self.LISTING_FIELDS,
            },
        }

    @with_retry(max_attempts=3)
    async def _do_listing_request(
        self, client: httpx.AsyncClient, *, offset: int
    ) -> httpx.Response:
        response = await client.post(
            self.LISTING_URL, json=self._build_rpc_body(offset)
        )
        response.raise_for_status()
        return response

    async def _fetch_listing_page(
        self, client: httpx.AsyncClient, *, offset: int
    ) -> list[dict[str, Any]]:
        try:
            response = await self._do_listing_request(client, offset=offset)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            raise FetchError(
                f"xt_xarid listing offset={offset} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise FetchError(
                f"xt_xarid listing offset={offset} returned non-dict "
                f"payload: {type(payload).__name__}"
            )
        error = payload.get("error")
        if error:
            raise FetchError(f"xt_xarid RPC error: {error}")
        result = payload.get("result") or []
        if not isinstance(result, list):
            raise FetchError(
                f"xt_xarid listing offset={offset} 'result' is not a "
                f"list: {type(result).__name__}"
            )
        return result

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        accumulated: list[dict[str, Any]] = []
        seen_external_ids: set[str] = set()
        pages_walked = 0
        async with self._make_client() as client:
            for page_index in range(self.MAX_PAGES):
                offset = page_index * self.PAGE_SIZE
                page_items = await self._fetch_listing_page(client, offset=offset)
                pages_walked = page_index + 1
                if not page_items:
                    break
                new_items = 0
                for item in page_items:
                    item_id = item.get("id")
                    external_id = str(item_id) if item_id is not None else ""
                    if external_id and external_id in seen_external_ids:
                        continue
                    if external_id:
                        seen_external_ids.add(external_id)
                    accumulated.append(item)
                    new_items += 1
                if new_items == 0:
                    logger.info(
                        "xt_xarid.pagination_stalled",
                        offset=offset,
                        page=page_index + 1,
                    )
                    break
                if len(page_items) < self.PAGE_SIZE:
                    break

        logger.info(
            "xt_xarid.listing_complete",
            pages_walked=pages_walked,
            items_collected=len(accumulated),
        )

        if since is None:
            return accumulated

        in_window: list[dict[str, Any]] = []
        for item in accumulated:
            published = _parse_iso_maybe(item.get("publicated_at"))
            # Keep items with no publish timestamp — pre-publication
            # rows whose state may have changed since the last run.
            if published is None or published >= since:
                in_window.append(item)
        logger.info(
            "xt_xarid.since_filter_applied",
            input_items=len(accumulated),
            kept=len(in_window),
            since=since.isoformat(),
        )
        return in_window

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        item_id = raw.get("id")
        if item_id is None:
            raise ParseError("xt_xarid item is missing 'id'")

        title = _build_title(raw)

        meta = raw.get("meta") or {}
        buyer_name = meta.get("company_name")
        buyer_external_id = meta.get("company_inn")

        cost = raw.get("totalcost")
        value_amount: Decimal | None
        if cost is not None:
            try:
                value_amount = Decimal(str(cost))
            except (TypeError, ValueError):
                value_amount = None
        else:
            value_amount = None
        value_currency = raw.get("currency") if value_amount is not None else None

        published_at = _parse_iso_maybe(raw.get("publicated_at"))
        deadline_at = _parse_iso_maybe(raw.get("close_at"))

        status = map_status(raw.get("status"))

        source_url = f"https://xt-xarid.uz/procedure/tender/{item_id}"

        lang_value = str(raw.get("lang") or "")
        language = Language.uz if lang_value.startswith("uz") else Language.ru

        raw_json: dict[str, Any] = dict(raw)
        raw_json["_lots"] = _build_lots(raw, title)

        return TenderUpsert(
            source_name=self.source_name,
            external_id=str(item_id),
            title=title,
            buyer_name=buyer_name,
            buyer_external_id=buyer_external_id,
            country=Country.UZ,
            sector=None,
            value_amount=value_amount,
            value_currency=value_currency,
            published_at=published_at,
            deadline_at=deadline_at,
            status=status,
            source_url=source_url,
            language=language,
            raw_json=raw_json,
        )


__all__ = [
    "STATUS_MAPPING",
    "XtXaridConnector",
    "_build_lots",
    "_build_title",
    "_parse_iso_maybe",
    "map_status",
]
