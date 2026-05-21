"""Connector for zakup.gov.kz, Kazakhstan's UNIFIED procurement portal.

This is the JSON-API connector — historically misleadingly named
``goszakup`` because the URL host *looks* like the real Goszakup site.
It is not. ``zakup.gov.kz`` is the unified portal that aggregates a
narrower / lagged view across the underlying procurement systems.
The HTML-scraping connector that hits the real ``goszakup.gov.kz``
lives in ``goszakup.py`` and owns the ``goszakup`` source_name.

Important shape note: the listing endpoint (`/_lots/`) returns lots,
not tenders. Multiple lots roll up to one announcement, and the lot
rows typically have null ``name_ru``/``name_kk`` — the human-readable
title lives on the announcement. So the connector does a two-step
fetch:

1. Page through ``/_lots/`` (newest first) up to MAX_PAGES, with
   optional client-side ``since`` cutoff.
2. For each unique ``announcement_id``, GET ``/announcements/<id>/``.
3. Attach the constituent lots under ``_lots`` and emit one row per
   announcement. ONE TENDER ROW = ONE ANNOUNCEMENT, never one lot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import httpx

from tender_monitor.connectors.base import Connector
from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.http import make_client, with_retry
from tender_monitor.connectors.registry import register
from tender_monitor.core.enums import Country, Language, TenderStatus
from tender_monitor.core.logging import get_logger
from tender_monitor.core.schemas import TenderUpsert

logger = get_logger(__name__)


# Status id → our TenderStatus. Easy to extend; everything not listed
# falls through to TenderStatus.unknown.
STATUS_MAPPING: dict[int, TenderStatus] = {
    6: TenderStatus.open,  # "Опубликован"
    7: TenderStatus.open,  # "Опубликован (прием заявок)"
}


def _parse_iso(value: str) -> datetime:
    """Parse listing dates like '2026-05-08T02:12:18Z' to aware UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _unix_to_utc(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=UTC)


@register
class ZakupUnifiedConnector(Connector):
    source_name: ClassVar[str] = "zakup_unified"

    LISTING_URL: ClassVar[str] = "https://zakup.gov.kz/api/core/api/core/_lots/"
    DETAIL_URL_TEMPLATE: ClassVar[str] = (
        "https://zakup.gov.kz/api/core/api/core/announcements/{id}/"
    )
    PAGE_SIZE: ClassVar[int] = 50
    MAX_PAGES: ClassVar[int] = 20
    SYSTEM_FILTER: ClassVar[str] = "1__2__3"

    REQUIRED_HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": "https://zakup.gov.kz/home/lots?system_id__in=1__2__3",
    }

    def _make_client(self) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return make_client(headers=self.REQUIRED_HEADERS)

    @with_retry(max_attempts=3)
    async def _do_listing_request(
        self, client: httpx.AsyncClient, offset: int
    ) -> httpx.Response:
        response = await client.get(
            self.LISTING_URL,
            params={
                "system_id__in": self.SYSTEM_FILTER,
                "limit": self.PAGE_SIZE,
                "offset": offset,
            },
        )
        response.raise_for_status()
        return response

    @with_retry(max_attempts=3)
    async def _do_detail_request(
        self, client: httpx.AsyncClient, announcement_id: int
    ) -> httpx.Response:
        response = await client.get(
            self.DETAIL_URL_TEMPLATE.format(id=announcement_id)
        )
        response.raise_for_status()
        return response

    async def _fetch_announcement(
        self, client: httpx.AsyncClient, announcement_id: int
    ) -> dict[str, Any] | None:
        """Return the announcement detail or None on any HTTP failure.

        Per spec: a single bad announcement must not sink the whole run.
        We log at WARNING and skip; the base class' partial_errors path
        only covers normalization, so these never appear there.
        """
        try:
            response = await self._do_detail_request(client, announcement_id)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            logger.warning(
                "zakup_unified.detail_fetch_failed",
                announcement_id=announcement_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None
        payload: dict[str, Any] = response.json()
        return payload

    async def _fetch_listing_page(
        self, client: httpx.AsyncClient, offset: int
    ) -> dict[str, Any]:
        try:
            response = await self._do_listing_request(client, offset)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            raise FetchError(
                f"zakup_unified listing offset={offset} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        payload: dict[str, Any] = response.json()
        return payload

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        accumulated_lots: list[dict[str, Any]] = []
        crossed_since_boundary = False
        pages_walked = 0

        async with self._make_client() as client:
            for page_index in range(self.MAX_PAGES):
                offset = page_index * self.PAGE_SIZE
                data = await self._fetch_listing_page(client, offset)
                pages_walked = page_index + 1
                page_results = data.get("results", []) or []
                if not page_results:
                    break

                if since is not None:
                    in_window: list[dict[str, Any]] = []
                    for item in page_results:
                        publish_raw = item.get("announcement_publish_date")
                        if publish_raw is None:
                            in_window.append(item)
                            continue
                        if _parse_iso(publish_raw) >= since:
                            in_window.append(item)
                    accumulated_lots.extend(in_window)
                    if len(in_window) < len(page_results):
                        crossed_since_boundary = True
                        break
                else:
                    accumulated_lots.extend(page_results)

                if data.get("next") is None:
                    break

            logger.info(
                "zakup_unified.listing_complete",
                pages_walked=pages_walked,
                lots_collected=len(accumulated_lots),
                crossed_since_boundary=crossed_since_boundary,
            )

            grouped: dict[int, list[dict[str, Any]]] = {}
            for lot in accumulated_lots:
                ann_id = lot.get("announcement_id")
                if ann_id is None:
                    logger.warning(
                        "zakup_unified.lot_missing_announcement_id",
                        lot_id=lot.get("id"),
                    )
                    continue
                grouped.setdefault(ann_id, []).append(lot)

            announcements: list[dict[str, Any]] = []
            for ann_id, lots in grouped.items():
                detail = await self._fetch_announcement(client, ann_id)
                if detail is None:
                    continue
                detail["_lots"] = lots
                announcements.append(detail)

        return announcements

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        announcement_id = raw.get("id")
        if announcement_id is None:
            raise ParseError("announcement is missing 'id'")

        title = raw.get("name")
        if not title:
            raise ParseError(f"announcement {announcement_id} has empty title")

        organizer = raw.get("organizer") or {}
        buyer_name = organizer.get("name")
        buyer_external_id = organizer.get("iin_bin")

        total_price = raw.get("total_price")
        value_amount: Decimal | None
        value_currency: str | None
        if total_price is not None:
            value_amount = Decimal(str(total_price))
            value_currency = "KZT"
        else:
            value_amount = None
            value_currency = None

        publish_ts = raw.get("publish_date")
        published_at = _unix_to_utc(publish_ts) if publish_ts else None

        deadline_ts = raw.get("offer_end_date")
        deadline_at = _unix_to_utc(deadline_ts) if deadline_ts else None

        status_id = (raw.get("status") or {}).get("id")
        status = (
            STATUS_MAPPING.get(status_id, TenderStatus.unknown)
            if isinstance(status_id, int)
            else TenderStatus.unknown
        )

        return TenderUpsert(
            source_name=self.source_name,
            external_id=str(announcement_id),
            title=title,
            buyer_name=buyer_name,
            buyer_external_id=buyer_external_id,
            country=Country.KZ,
            sector=None,
            value_amount=value_amount,
            value_currency=value_currency,
            published_at=published_at,
            deadline_at=deadline_at,
            status=status,
            source_url=f"https://zakup.gov.kz/announcement/{announcement_id}",
            language=Language.ru,
            raw_json=raw,
        )


__all__ = ["ZakupUnifiedConnector"]
