"""Connector for etender.uzex.uz -- the UZEX e-Tender platform.

First non-Kazakhstan source. Shape notes that differ from the KZ
connectors:

- POST-JSON listing similar to zakup_unified. The listing still gives
  us stable row discovery, but we now enrich each in-window listing
  item with ``GET /api/common/GetTrade/{id}/0`` so matching can see
  the lot's technical description, product descriptions, criteria, and
  other detail-only fields.
- ONE TENDER ROW = ONE LISTING ITEM. ``external_id`` is
  ``str(item["id"] or item["trade_id"])``.
- Pagination uses ``From``/``To`` 1-indexed offsets in the body, not
  page numbers: page 1 -> From=1,To=50; page 2 -> From=51,To=100.
- Discovery now unions three streams: active tenders, failed tenders,
  and completed deals. That broadens coverage for lots that leave the
  active list before we ingest them.
- Order inside a stream is roughly id-descending, but date ordering is
  NOT strictly monotone. We therefore paginate to the per-stream page
  cap on steady-state runs, but allow deeper pagination on explicit
  backfills until several fully-stale pages are observed. We still
  apply the ``since`` filter post-pagination. Early termination on the
  first older-than-since hit would drop legitimately new but
  later-positioned items.
- Dates are naive ISO ``YYYY-MM-DDTHH:MM:SS`` with no timezone
  marker. Treat them as Asia/Tashkent local (UTC+5), localize, then
  convert to UTC for storage.
- Currency is UZS (Uzbek sum). The detail payload carries the ISO
  alpha code as ``currency_codeabc`` so we use that when available.
- ``TypeId=1`` is not the whole market. UZEX exposes multiple
  procurement lanes; we currently follow both the tender/best-offer
  lane and the competition lane so ended-but-recent competitions such
  as lot ``482604`` are still discoverable. Status is derived from the
  detail or ended-stream row rather than hardcoded open.
- The DevTools capture included a ``validation`` header that looks
  like a signed token. v1 deliberately omits it; if the live API
  rejects empty-validation, the live acceptance run catches that and
  we'll address it in a follow-up (likely by minting the token from
  a JS endpoint, similar to samruk_kazyna's ``tor``).
- Titles are often bilingual ``Russian / Uzbek`` separated by `` / ``;
  some are Uzbek-only. We default ``Language.ru`` because the
  Russian half is what the matcher's keywords are tuned for. Per-row
  language detection is explicitly out of scope for v1.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import quote

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


def parse_naive_tashkent(text: str | None) -> datetime | None:
    """Parse naive ISO ``YYYY-MM-DDTHH:MM:SS`` as Asia/Tashkent local,
    return an aware UTC datetime.

    The UZEX API emits timestamps without a timezone marker. We treat
    them as Tashkent-local (the platform's operational TZ) and convert
    to UTC for storage so cross-source queries Just Work.

    Returns ``None`` on empty / missing / unparseable input. Lives here
    rather than in ``_html.py`` because no second UZ source uses this
    format yet; promote it to the shared module if/when one does.
    """
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        # ``fromisoformat`` accepts both "T" and " " separators and is
        # strict about microseconds vs seconds, which is what we want.
        naive = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if naive.tzinfo is not None:
        # If the source ever starts sending offsets, trust them and
        # just convert. Defensive; the live API hasn't emitted these.
        return naive.astimezone(UTC)
    return naive.replace(tzinfo=TASHKENT_TZ).astimezone(UTC)


@register
class UzexEtenderConnector(Connector):
    source_name: ClassVar[str] = "uzex_etender"

    ACTIVE_LISTING_URL: ClassVar[str] = (
        "https://apietender.uzex.uz/api/common/TradeList"
    )
    DEALS_LISTING_URL: ClassVar[str] = (
        "https://apietender.uzex.uz/api/common/DealsList"
    )
    NOT_DEALED_LISTING_URL: ClassVar[str] = (
        "https://apietender.uzex.uz/api/common/NotDealedList"
    )
    DETAIL_URL_TEMPLATE: ClassVar[str] = (
        "https://apietender.uzex.uz/api/common/GetTrade/{lot_id}/0"
    )
    PAGE_SIZE: ClassVar[int] = 50
    ACTIVE_MAX_PAGES: ClassVar[int] = 15
    ENDED_MAX_PAGES: ClassVar[int] = 5
    BACKFILL_ACTIVE_MAX_PAGES: ClassVar[int] = 60
    BACKFILL_ENDED_MAX_PAGES: ClassVar[int] = 25
    STALE_PAGE_THRESHOLD: ClassVar[int] = 3
    LISTING_TYPE_IDS: ClassVar[tuple[int, ...]] = (
        1,  # tender / best-offer lane
        2,  # competition lane
    )
    LISTING_STREAMS: ClassVar[
        tuple[tuple[str, str, int, int, tuple[int, ...]], ...]
    ] = (
        (
            "active",
            ACTIVE_LISTING_URL,
            ACTIVE_MAX_PAGES,
            BACKFILL_ACTIVE_MAX_PAGES,
            LISTING_TYPE_IDS,
        ),
        (
            "not_dealed",
            NOT_DEALED_LISTING_URL,
            ENDED_MAX_PAGES,
            BACKFILL_ENDED_MAX_PAGES,
            LISTING_TYPE_IDS,
        ),
        (
            "deals",
            DEALS_LISTING_URL,
            ENDED_MAX_PAGES,
            BACKFILL_ENDED_MAX_PAGES,
            LISTING_TYPE_IDS,
        ),
    )
    EMBEDDED_JSON_FIELDS: ClassVar[tuple[str, ...]] = (
        "budget_products",
        "contacts",
        "fields",
        "languages",
        "qualification_fields",
    )
    FILE_SLOTS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("tech_file", "Technical attachment"),
        ("tech_doc_file", "Technical document"),
        ("add_file", "Additional attachment"),
        ("contract_proform_file", "Contract template"),
        ("contract_file", "Contract"),
        ("expertise_file", "Expertise file"),
        ("prolong_file", "Extension document"),
        ("protocol_file", "Protocol"),
        ("conclusion_file", "Conclusion"),
        ("additional_protocol_file", "Additional protocol"),
        ("additional_agreement_file", "Additional agreement"),
        ("deal_additional_file", "Deal attachment"),
        ("agreement_additional_file", "Agreement attachment"),
    )
    LISTING_BODY_TEMPLATE: ClassVar[dict[str, int]] = {
        "System_Id": 0,  # all systems
    }

    REQUIRED_HEADERS: ClassVar[dict[str, str]] = {
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Content-Type": "application/json; charset=UTF-8",
        "Origin": "https://etender.uzex.uz",
        "Referer": "https://etender.uzex.uz/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        # The portal's SPA sends ``language: ru``; the API may echo it
        # back into localized fields (region_name, currency_name).
        "language": "ru",
    }

    def _make_client(self) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return make_client(headers=self.REQUIRED_HEADERS)

    @with_retry(max_attempts=3)
    async def _do_listing_request(
        self,
        client: httpx.AsyncClient,
        *,
        url: str,
        type_id: int,
        from_offset: int,
        to_offset: int,
    ) -> httpx.Response:
        body: dict[str, int] = {
            **self.LISTING_BODY_TEMPLATE,
            "TypeId": type_id,
            "From": from_offset,
            "To": to_offset,
        }
        response = await client.post(url, json=body)
        response.raise_for_status()
        return response

    async def _fetch_listing_page(
        self,
        client: httpx.AsyncClient,
        *,
        url: str,
        stream_name: str,
        type_id: int,
        page_index: int,
    ) -> list[dict[str, Any]]:
        from_offset = page_index * self.PAGE_SIZE + 1
        to_offset = (page_index + 1) * self.PAGE_SIZE
        try:
            response = await self._do_listing_request(
                client,
                url=url,
                from_offset=from_offset,
                to_offset=to_offset,
                type_id=type_id,
            )
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            raise FetchError(
                f"uzex_etender {stream_name} listing From={from_offset} "
                f"To={to_offset} TypeId={type_id} "
                f"failed: {type(exc).__name__}: {exc}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, list):
            raise FetchError(
                f"uzex_etender {stream_name} listing From={from_offset} returned "
                f"non-list payload for TypeId={type_id}: "
                f"{type(payload).__name__}"
            )
        return payload

    @with_retry(max_attempts=3)
    async def _do_detail_request(
        self, client: httpx.AsyncClient, *, lot_id: str
    ) -> httpx.Response:
        response = await client.get(
            self.DETAIL_URL_TEMPLATE.format(lot_id=lot_id)
        )
        response.raise_for_status()
        return response

    async def _fetch_detail(
        self, client: httpx.AsyncClient, *, lot_id: str
    ) -> dict[str, Any]:
        try:
            response = await self._do_detail_request(client, lot_id=lot_id)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            raise FetchError(
                f"uzex_etender detail lot_id={lot_id} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise FetchError(
                f"uzex_etender detail lot_id={lot_id} returned non-dict "
                f"payload: {type(payload).__name__}"
            )
        return payload

    def _parse_embedded_json(self, value: Any) -> Any | None:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned or cleaned[0] not in "[{":
            return None
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None

    def _build_file_url(self, path: str) -> str:
        normalized = path.strip()
        return (
            "https://xarid.uzex.uz/x-cloud?file_path="
            f"{quote(normalized, safe='')}"
        )

    def _document_name_from_path(self, path: str) -> str:
        normalized = path.rstrip("/")
        if not normalized:
            return "Document"
        return normalized.rsplit("/", 1)[-1]

    def _extract_documents(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        seen_paths: set[str] = set()

        def _append_document(
            *,
            category: str,
            path: Any,
            name: Any = None,
            ext: Any = None,
            size_bytes: Any = None,
            source: str,
        ) -> None:
            if not isinstance(path, str) or not path.strip():
                return
            normalized_path = path.strip()
            if normalized_path in seen_paths:
                return
            seen_paths.add(normalized_path)
            if isinstance(size_bytes, int | float):
                normalized_size: int | float | None = size_bytes
            else:
                normalized_size = None
            documents.append(
                {
                    "category": category,
                    "name": (
                        name.strip()
                        if isinstance(name, str) and name.strip()
                        else self._document_name_from_path(normalized_path)
                    ),
                    "path": normalized_path,
                    "url": self._build_file_url(normalized_path),
                    "ext": (
                        ext.strip().upper()
                        if isinstance(ext, str) and ext.strip()
                        else None
                    ),
                    "size_bytes": normalized_size,
                    "source": source,
                }
            )

        for prefix, category in self.FILE_SLOTS:
            _append_document(
                category=category,
                path=raw.get(f"{prefix}_path"),
                name=raw.get(f"{prefix}_name"),
                ext=raw.get(f"{prefix}_ext"),
                size_bytes=raw.get(f"{prefix}_sizes"),
                source="detail_slot",
            )

        def _walk_embedded_documents(value: Any, *, category: str) -> None:
            if isinstance(value, dict):
                path = (
                    value.get("Form_File_Path")
                    or value.get("form_file_path")
                    or value.get("File_Path")
                    or value.get("file_path")
                )
                if path:
                    _append_document(
                        category=category,
                        path=path,
                        name=(
                            value.get("File_Name")
                            or value.get("file_name")
                            or value.get("Form_Name")
                            or value.get("form_name")
                            or value.get("Label")
                            or value.get("label")
                            or value.get("Name")
                            or value.get("name")
                        ),
                        ext=value.get("file_ext") or value.get("File_Ext"),
                        size_bytes=value.get("file_sizes")
                        or value.get("File_Sizes"),
                        source="detail_embedded",
                    )
                for nested in value.values():
                    if isinstance(nested, dict | list):
                        _walk_embedded_documents(nested, category=category)
            elif isinstance(value, list):
                for nested in value:
                    _walk_embedded_documents(nested, category=category)

        for field_name, category in (
            ("fields", "Criteria form"),
            ("qualification_fields", "Qualification document"),
        ):
            parsed_value = self._parse_embedded_json(raw.get(field_name))
            if parsed_value is not None:
                _walk_embedded_documents(parsed_value, category=category)

        return documents

    def _extract_detail_description(
        self, detail: dict[str, Any], *, fallback_title: str
    ) -> str | None:
        parts: list[str] = []
        for key in ("technical_description", "addon_description", "description"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())

        parsed_products = self._parse_embedded_json(detail.get("budget_products"))
        if isinstance(parsed_products, list):
            for product in parsed_products:
                if not isinstance(product, dict):
                    continue
                description = product.get("Description")
                if isinstance(description, str) and description.strip():
                    parts.append(description.strip())

        unique_parts: list[str] = []
        seen: set[str] = set()
        for part in parts:
            if part == fallback_title or part in seen:
                continue
            seen.add(part)
            unique_parts.append(part)
        if not unique_parts:
            return None
        return "\n\n".join(unique_parts)

    def _status_from_raw(self, raw: dict[str, Any]) -> TenderStatus:
        listing_stream = raw.get("_listing_stream")
        if listing_stream == "deals" or raw.get("_listing_deal_id") is not None:
            return TenderStatus.awarded
        if listing_stream == "not_dealed":
            return TenderStatus.cancelled

        status_id = raw.get("_listing_status_id")
        if status_id is None:
            status_id = raw.get("status_id")
        if status_id == 12:
            return TenderStatus.cancelled
        if status_id == 6:
            return TenderStatus.closed
        if status_id == 4:
            return TenderStatus.open

        status_name = raw.get("_listing_status_name")
        if status_name is None:
            status_name = raw.get("status_name")
        if isinstance(status_name, str):
            normalized = status_name.casefold()
            if "протокол" in normalized or "deal" in normalized:
                return TenderStatus.awarded
            if "не состоя" in normalized or "cancel" in normalized:
                return TenderStatus.cancelled
            if "тугат" in normalized or "closed" in normalized:
                return TenderStatus.closed
            if "эълон" in normalized or "open" in normalized:
                return TenderStatus.open

        return TenderStatus.open

    def _lot_id_from_raw(self, raw: dict[str, Any]) -> str | None:
        item_id = raw.get("id")
        if item_id is not None:
            return str(item_id)
        trade_id = raw.get("trade_id")
        if trade_id is not None:
            return str(trade_id)
        return None

    def _candidate_timestamp(self, raw: dict[str, Any]) -> datetime | None:
        candidates: list[datetime] = []
        for key in ("deal_date", "end_date", "start_date"):
            parsed = parse_naive_tashkent(raw.get(key))
            if parsed is not None:
                candidates.append(parsed)
        if not candidates:
            return None
        return max(candidates)

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        accumulated: list[dict[str, Any]] = []
        async with self._make_client() as client:
            for (
                stream_name,
                url,
                max_pages,
                backfill_max_pages,
                type_ids,
            ) in self.LISTING_STREAMS:
                for type_id in type_ids:
                    stream_items = await self._fetch_stream(
                        client,
                        stream_name=stream_name,
                        url=url,
                        max_pages=max_pages,
                        backfill_max_pages=backfill_max_pages,
                        type_id=type_id,
                        since=since,
                    )
                    accumulated.extend(stream_items)

            deduped = self._dedupe_raw_items(accumulated)

            logger.info(
                "uzex_etender.listing_complete",
                stream_count=len(self.LISTING_STREAMS),
                items_collected=len(accumulated),
                unique_items=len(deduped),
            )

            if since is None:
                return await self._enrich_with_details(client, deduped)

            # Post-pagination filter: see module docstring for why early
            # termination is unsafe (stream ordering is not strictly monotone
            # with respect to listing position).
            in_window: list[dict[str, Any]] = []
            for item in deduped:
                candidate_timestamp = self._candidate_timestamp(item)
                if candidate_timestamp is None or candidate_timestamp >= since:
                    in_window.append(item)
            logger.info(
                "uzex_etender.since_filter_applied",
                input_items=len(deduped),
                kept=len(in_window),
                since=since.isoformat(),
            )
            return await self._enrich_with_details(client, in_window)

    async def _fetch_stream(
        self,
        client: httpx.AsyncClient,
        *,
        stream_name: str,
        url: str,
        max_pages: int,
        backfill_max_pages: int,
        type_id: int,
        since: datetime | None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        pages_walked = 0
        stale_pages = 0
        page_limit = max_pages if since is None else backfill_max_pages
        for page_index in range(page_limit):
            page_items = await self._fetch_listing_page(
                client,
                url=url,
                stream_name=stream_name,
                type_id=type_id,
                page_index=page_index,
            )
            pages_walked = page_index + 1
            if not page_items:
                break

            page_is_stale = False
            if since is not None:
                page_is_stale = True
                for item in page_items:
                    if not isinstance(item, dict):
                        page_is_stale = False
                        break
                    candidate_timestamp = self._candidate_timestamp(item)
                    if candidate_timestamp is None or candidate_timestamp >= since:
                        page_is_stale = False
                        break
                if page_is_stale:
                    stale_pages += 1
                else:
                    stale_pages = 0

            for item in page_items:
                if isinstance(item, dict):
                    items.append(
                        {
                            **item,
                            "_listing_stream": stream_name,
                            "_listing_type_id": type_id,
                        }
                    )
            if len(page_items) < self.PAGE_SIZE:
                break
            if since is None and page_index + 1 >= max_pages:
                break
            if (
                since is not None
                and page_index + 1 > max_pages
                and stale_pages >= self.STALE_PAGE_THRESHOLD
            ):
                break

        logger.info(
            "uzex_etender.stream_complete",
            stream=stream_name,
            type_id=type_id,
            pages_walked=pages_walked,
            items_collected=len(items),
            stale_pages=stale_pages,
        )
        return items

    def _dedupe_raw_items(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for item in items:
            lot_id = self._lot_id_from_raw(item)
            if lot_id is None:
                continue
            unique.setdefault(lot_id, item)
        return list(unique.values())

    async def _enrich_with_details(
        self,
        client: httpx.AsyncClient,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for item in items:
            lot_id = self._lot_id_from_raw(item)
            if lot_id is None:
                enriched.append(item)
                continue

            try:
                detail = await self._fetch_detail(client, lot_id=lot_id)
            except FetchError as exc:
                logger.warning(
                    "uzex_etender.detail_fetch_failed",
                    lot_id=lot_id,
                    error=str(exc),
                )
                merged = dict(item)
                merged["_detail_fetch_error"] = str(exc)
                enriched.append(merged)
                continue

            merged = {**item, **detail}
            merged["_detail"] = detail
            merged["_listing_status_id"] = item.get("status_id")
            merged["_listing_status_name"] = item.get("status_name")
            merged["_listing_deal_id"] = item.get("deal_id")
            enriched.append(merged)

        logger.info(
            "uzex_etender.detail_enrichment_complete",
            items_requested=len(items),
            items_enriched=len(enriched),
        )
        return enriched

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        item_id = self._lot_id_from_raw(raw)
        if item_id is None:
            raise ParseError("uzex_etender item is missing id/trade_id")

        title = raw.get("name") or raw.get("category_name")
        if not title:
            raise ParseError(f"uzex_etender item {item_id} has empty name")

        detail = raw.get("_detail")
        detail_dict = detail if isinstance(detail, dict) else {}

        buyer_name = raw.get("customer_name") or raw.get("seller_name")
        buyer_external_id = (
            raw.get("customer_tin")
            or raw.get("customer_inn")
            or raw.get("seller_tin")
        )

        cost = raw.get("start_cost")
        if cost is None:
            cost = raw.get("cost")
        value_amount: Decimal | None = (
            Decimal(str(cost)) if cost is not None else None
        )
        value_currency = (
            raw.get("currency_codeabc") if value_amount is not None else None
        )

        published_at = parse_naive_tashkent(raw.get("start_date"))
        deadline_at = parse_naive_tashkent(raw.get("end_date"))

        source_url = f"https://etender.uzex.uz/lot/{item_id}"

        raw_json: dict[str, Any] = dict(raw)
        parsed_detail: dict[str, Any] = {}
        for field_name in self.EMBEDDED_JSON_FIELDS:
            parsed_value = self._parse_embedded_json(raw.get(field_name))
            if parsed_value is not None:
                parsed_detail[field_name] = parsed_value
        if parsed_detail:
            raw_json["_parsed_detail"] = parsed_detail
        documents = self._extract_documents(raw)
        if documents:
            raw_json["_documents"] = documents

        detail_description = self._extract_detail_description(
            detail_dict,
            fallback_title=title,
        )

        raw_json["_lots"] = [
            {
                "name_ru": raw.get("name"),
                "description_ru": detail_description,
            }
        ]

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
            status=self._status_from_raw(raw),
            source_url=source_url,
            language=Language.ru,
            raw_json=raw_json,
        )


__all__ = [
    "UzexEtenderConnector",
    "parse_naive_tashkent",
]
