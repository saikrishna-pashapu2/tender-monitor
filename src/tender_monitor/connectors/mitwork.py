"""Connector for eep.mitwork.kz — the Eurasian Electronic Procurement Portal.

Shape notes that differ from goszakup/samruk_kazyna:

- The portal is server-rendered HTML (Yii2/PHP), not a JSON API. We parse
  with ``selectolax`` and never touch a JSON endpoint here.
- The listing rows already carry every field we need for v1 (title,
  buyer name + BIN, dates, status, value). No detail-page fetch in v1.
- Dates in the HTML are KZ-local (Asia/Almaty, UTC+5) with no timezone
  suffix. We parse them as naive, localize, and convert to UTC before
  storing.
- ``data-key`` on each ``<tr class="item">`` is the stable internal id
  and is what we use as ``external_id``. The visible "Номер" column can
  differ (e.g. ``186288-6`` vs data-key ``192072``) because of
  sub-versions, so we ignore that column for identity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

import httpx
from selectolax.parser import HTMLParser, Node

# KZ_TZ is re-exported here because tests/connectors/test_mitwork.py
# still imports it from this module; the alias listing in __all__ at
# the bottom keeps ruff happy about an "unused" import.
from tender_monitor.connectors._html import (
    KZ_TZ,
    parse_kz_local_datetime,
    parse_kzt_amount,
)
from tender_monitor.connectors.base import Connector
from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.http import make_client, with_retry
from tender_monitor.connectors.registry import register
from tender_monitor.core.enums import Country, Language, TenderStatus
from tender_monitor.core.logging import get_logger
from tender_monitor.core.schemas import TenderUpsert

logger = get_logger(__name__)


# Status text on the listing → our TenderStatus. Expand as we observe new
# live values; everything not listed falls through to TenderStatus.unknown.
STATUS_MAPPING: dict[str, TenderStatus] = {
    "Опубликовано": TenderStatus.open,
}


def _td_text(td: Node) -> str:
    return td.text().strip()


def _parse_row(row: Node) -> dict[str, Any]:
    """Extract one MITWORK listing row into a dict.

    The row must be a ``<tr class="item" data-key="...">`` whose first 8
    ``<td>`` children carry the standard columns. Missing fields are
    returned as empty strings or None (depending on the field) and left
    for ``_normalize`` to reject — keeping parsing tolerant means a
    single off-shape row doesn't kill the whole page.
    """
    data_key = (row.attributes.get("data-key") or "").strip()
    tds = row.css("td")

    def _cell(idx: int) -> Node | None:
        return tds[idx] if idx < len(tds) else None

    # Column 0: announcement_number<br>Лотов: N
    announcement_number = ""
    lots_label: str | None = None
    cell_num = _cell(0)
    if cell_num is not None:
        announcement_number = (cell_num.text(deep=False) or "").strip()
        span = cell_num.css_first("span")
        if span is not None:
            lots_label = span.text().strip() or None

    # Column 1: title link
    title_ru = ""
    detail_url = ""
    cell_title = _cell(1)
    if cell_title is not None:
        link = cell_title.css_first("a")
        if link is not None:
            title_ru = link.text().strip()
            detail_url = (link.attributes.get("href") or "").strip()

    # Column 2: value text (e.g. "46 347,00 KZT", or "не указана")
    cell_value = _cell(2)
    value_text = _td_text(cell_value) if cell_value is not None else ""

    # Column 3: procurement_method
    cell_method = _cell(3)
    procurement_method = _td_text(cell_method) if cell_method is not None else ""

    # Column 4 & 5: start / end datetimes (KZ-local strings)
    cell_start = _cell(4)
    offer_start_text = _td_text(cell_start) if cell_start is not None else ""
    cell_end = _cell(5)
    offer_end_text = _td_text(cell_end) if cell_end is not None else ""

    # Column 6: buyer link (title= organization name, text= BIN)
    buyer_name = ""
    buyer_bin = ""
    subject_url = ""
    cell_buyer = _cell(6)
    if cell_buyer is not None:
        buyer_link = cell_buyer.css_first("a")
        if buyer_link is not None:
            buyer_name = (buyer_link.attributes.get("title") or "").strip()
            buyer_bin = buyer_link.text().strip()
            subject_url = (buyer_link.attributes.get("href") or "").strip()

    # Column 7: status text
    cell_status = _cell(7)
    status_text = _td_text(cell_status) if cell_status is not None else ""

    return {
        "data_key": data_key,
        "announcement_number": announcement_number,
        "lots_label": lots_label,
        "title_ru": title_ru,
        "detail_url": detail_url,
        "value_text": value_text,
        "procurement_method": procurement_method,
        "offer_start_text": offer_start_text,
        "offer_end_text": offer_end_text,
        "buyer_name": buyer_name or None,
        "buyer_bin": buyer_bin or None,
        "subject_url": subject_url,
        "status_text": status_text,
        "offer_start_local": parse_kz_local_datetime(offer_start_text),
    }


@register
class MitworkConnector(Connector):
    source_name: ClassVar[str] = "mitwork"

    LISTING_URL: ClassVar[str] = "https://eep.mitwork.kz/ru/publics/buys"
    DETAIL_URL_TEMPLATE: ClassVar[str] = (
        "https://eep.mitwork.kz/ru/publics/buy/{external_id}"
    )
    PAGE_SIZE: ClassVar[int] = 50  # server-fixed
    MAX_PAGES: ClassVar[int] = 20  # 50 * 20 = 1000 rows per run, ample

    REQUIRED_HEADERS: ClassVar[dict[str, str]] = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
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
        response = await client.get(
            self.LISTING_URL,
            params={"page": page},
        )
        response.raise_for_status()
        return response

    async def _fetch_listing_page(
        self, client: httpx.AsyncClient, page: int
    ) -> list[dict[str, Any]]:
        try:
            response = await self._do_listing_request(client, page)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            raise FetchError(
                f"mitwork listing page={page} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        tree = HTMLParser(response.text)
        rows = tree.css("tr.item")
        return [_parse_row(row) for row in rows]

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        accumulated: list[dict[str, Any]] = []
        crossed_since_boundary = False
        pages_walked = 0

        async with self._make_client() as client:
            for page_num in range(1, self.MAX_PAGES + 1):
                page_rows = await self._fetch_listing_page(client, page_num)
                pages_walked = page_num
                if not page_rows:
                    break

                if since is not None:
                    in_window: list[dict[str, Any]] = []
                    for row in page_rows:
                        start = row.get("offer_start_local")
                        if start is None:
                            in_window.append(row)
                            continue
                        if start >= since:
                            in_window.append(row)
                    accumulated.extend(in_window)
                    if len(in_window) < len(page_rows):
                        crossed_since_boundary = True
                        break
                else:
                    accumulated.extend(page_rows)

                if len(page_rows) < self.PAGE_SIZE:
                    break

        logger.info(
            "mitwork.listing_complete",
            pages_walked=pages_walked,
            items_collected=len(accumulated),
            crossed_since_boundary=crossed_since_boundary,
        )
        return accumulated

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        external_id = raw.get("data_key")
        if not external_id:
            raise ParseError("listing row is missing data-key")

        title = raw.get("title_ru")
        if not title:
            raise ParseError(f"listing row {external_id} has empty title")

        buyer_name = raw.get("buyer_name")
        buyer_external_id = raw.get("buyer_bin")

        value_amount = parse_kzt_amount(raw.get("value_text"))
        value_currency = "KZT" if value_amount is not None else None

        published_at = parse_kz_local_datetime(raw.get("offer_start_text"))
        deadline_at = parse_kz_local_datetime(raw.get("offer_end_text"))

        status_text = raw.get("status_text")
        status = (
            STATUS_MAPPING.get(status_text, TenderStatus.unknown)
            if isinstance(status_text, str)
            else TenderStatus.unknown
        )

        source_url = raw.get("detail_url") or self.DETAIL_URL_TEMPLATE.format(
            external_id=external_id
        )

        # Strip the parsed-datetime convenience field — it's there for the
        # since-filter step in _fetch_raw and would not be JSON-serializable
        # when this dict lands in the JSONB raw_json column.
        raw_json = {k: v for k, v in raw.items() if k != "offer_start_local"}

        return TenderUpsert(
            source_name=self.source_name,
            external_id=str(external_id),
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
            source_url=source_url,
            language=Language.ru,
            raw_json=raw_json,
        )


__all__ = [
    "KZ_TZ",
    "STATUS_MAPPING",
    "MitworkConnector",
    "parse_kz_local_datetime",
    "parse_kzt_amount",
]
