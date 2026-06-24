"""Connector for eep.mitwork.kz — the Eurasian Electronic Procurement Portal.

Shape notes that differ from goszakup/samruk_kazyna:

- The portal is server-rendered HTML (Yii2/PHP), not a JSON API. We parse
  with ``selectolax`` and never touch a JSON endpoint here.
- Listings provide discovery plus the main dates / buyer / status fields,
  but the detail page carries richer procurement metadata, lot
  descriptions, and downloadable files. We now fetch the detail page for
  each listing row and store parsed detail fields under ``raw_json`` so
  the matcher can see them and the UI can render documents/lots.
- Dates in the HTML are KZ-local (Asia/Almaty, UTC+5) with no timezone
  suffix. We parse them as naive, localize, and convert to UTC before
  storing.
- ``data-key`` on each ``<tr class="item">`` is the stable internal id
  and is what we use as ``external_id``. The visible "Номер" column can
  differ (e.g. ``186288-6`` vs data-key ``192072``) because of
  sub-versions, so we ignore that column for identity.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urljoin

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
    "Завершено": TenderStatus.closed,
    "Отменено": TenderStatus.cancelled,
}

DETAIL_FIELD_LABELS: dict[str, str] = {
    "Наименование на государственном языке": "title_kk",
    "Наименование на русском языке": "title_ru_detail",
    "Дата начала приема заявок": "offer_start_text_detail",
    "Дата окончания приема заявок": "offer_end_text_detail",
    "Организатор": "organizer_name",
    "Способ закупки": "procurement_method_detail",
    "Правила закупок": "rules_name",
    "Тип закупки": "purchase_type",
    "Статус": "status_text_detail",
}

_DETAIL_TOTAL_RE = re.compile(
    r"Общая\s+сумма\s+закупки\s*[-:]\s*([^,]+)",
    re.IGNORECASE,
)


def _td_text(td: Node) -> str:
    return td.text().strip()


def _extract_file_ext(name: str | None) -> str | None:
    if not name or "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1].strip().upper()
    return ext or None


def _detail_table_by_headers(
    tree: HTMLParser, expected_headers: tuple[str, ...]
) -> Node | None:
    for table in tree.css("table"):
        header_cells = table.css("thead th")
        if not header_cells:
            continue
        headers = tuple(_td_text(cell) for cell in header_cells)
        if headers[: len(expected_headers)] == expected_headers:
            return table
    return None


def _parse_detail_fields(tree: HTMLParser) -> dict[str, Any]:
    table = tree.css_first("table.detail-view")
    if table is None:
        return {}

    fields: dict[str, Any] = {}
    for row in table.css("tr"):
        key_cell = row.css_first("th")
        value_cell = row.css_first("td")
        if key_cell is None or value_cell is None:
            continue

        label = _td_text(key_cell)
        field_name = DETAIL_FIELD_LABELS.get(label)
        if field_name is None:
            continue

        fields[field_name] = _td_text(value_cell)

        link = value_cell.css_first("a")
        if link is None:
            continue

        href = (link.attributes.get("href") or "").strip()
        if not href:
            continue

        if field_name == "organizer_name":
            fields["organizer_url"] = urljoin(
                MitworkConnector.LISTING_URL, href
            )
        elif field_name == "rules_name":
            fields["rules_url"] = urljoin(MitworkConnector.LISTING_URL, href)

    return fields


def _parse_documents(tree: HTMLParser) -> list[dict[str, Any]]:
    table = _detail_table_by_headers(
        tree,
        (
            "Категория документа",
            "Наименование документа",
        ),
    )
    if table is None:
        return []

    documents: list[dict[str, Any]] = []
    for row in table.css("tbody tr"):
        cells = row.css("td")
        if len(cells) < 2:
            continue

        name_link = cells[1].css_first("a")
        action_link = cells[-1].css_first("a")
        name = _td_text(cells[1])
        show_url = (
            (name_link.attributes.get("href") or "").strip()
            if name_link is not None
            else ""
        )
        download_url = (
            (action_link.attributes.get("href") or "").strip()
            if action_link is not None
            else ""
        )

        documents.append(
            {
                "category": _td_text(cells[0]) or None,
                "name": name or "Document",
                "url": urljoin(
                    MitworkConnector.LISTING_URL, download_url or show_url
                )
                if download_url or show_url
                else None,
                "preview_url": urljoin(MitworkConnector.LISTING_URL, show_url)
                if show_url
                else None,
                "size_text": _td_text(cells[2]) if len(cells) > 2 else None,
                "uploaded_at_text": _td_text(cells[3])
                if len(cells) > 3
                else None,
                "hash": _td_text(cells[4]) if len(cells) > 4 else None,
                "ext": _extract_file_ext(name),
                "source": "detail_page",
            }
        )

    return documents


def _parse_lots(tree: HTMLParser) -> list[dict[str, Any]]:
    table = _detail_table_by_headers(
        tree,
        (
            "Номер",
            "Наименование",
        ),
    )
    if table is None:
        return []

    lots: list[dict[str, Any]] = []
    for row in table.css("tbody tr"):
        cells = row.css("td")
        if len(cells) < 7:
            continue

        title_cell = cells[1]
        title_link = title_cell.css_first("a")
        title = title_link.text().strip() if title_link is not None else _td_text(title_cell)
        lot_url = (
            urljoin(
                MitworkConnector.LISTING_URL,
                (title_link.attributes.get("href") or "").strip(),
            )
            if title_link is not None
            else None
        )
        code_badge = title_cell.css_first("span.label")

        unit_price = parse_kzt_amount(_td_text(cells[4]))
        total_amount = parse_kzt_amount(_td_text(cells[5]))

        lots.append(
            {
                "number": _td_text(cells[0]) or None,
                "name_ru": title or None,
                "description_ru": _td_text(cells[2]) or None,
                "quantity_text": _td_text(cells[3]) or None,
                "unit_price_text": _td_text(cells[4]) or None,
                "unit_price_amount": (
                    str(unit_price) if unit_price is not None else None
                ),
                "total_amount_text": _td_text(cells[5]) or None,
                "total_amount": (
                    str(total_amount) if total_amount is not None else None
                ),
                "currency": "KZT"
                if unit_price is not None or total_amount is not None
                else None,
                "submitted_bids_text": _td_text(cells[6]) or None,
                "classification_code": code_badge.text().strip()
                if code_badge is not None
                else None,
                "lot_url": lot_url,
            }
        )

    return lots


def _parse_detail_total_amount_text(tree: HTMLParser) -> str | None:
    meta = tree.css_first('meta[property="og:description"]')
    if meta is None:
        return None

    content = (meta.attributes.get("content") or "").strip()
    if not content:
        return None

    match = _DETAIL_TOTAL_RE.search(content)
    if match is None:
        return content if parse_kzt_amount(content) is not None else None
    return match.group(1).strip() or None


def _parse_detail_page(html: str) -> dict[str, Any]:
    tree = HTMLParser(html)
    detail_fields = _parse_detail_fields(tree)
    documents = _parse_documents(tree)
    lots = _parse_lots(tree)
    detail_total_amount_text = _parse_detail_total_amount_text(tree)

    parsed: dict[str, Any] = {}
    if detail_fields:
        parsed["detail_fields"] = detail_fields
    if documents:
        parsed["_documents"] = documents
    if lots:
        parsed["_lots"] = lots
    if detail_total_amount_text is not None:
        parsed["detail_total_amount_text"] = detail_total_amount_text
    return parsed


def _coerce_kzt_amount(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return parse_kzt_amount(value)
    return None


def _sum_detail_lot_totals(raw: dict[str, Any]) -> Decimal | None:
    lots = raw.get("_lots")
    if not isinstance(lots, list) or not lots:
        return None

    total = Decimal("0")
    for lot in lots:
        if not isinstance(lot, dict):
            return None

        amount = _coerce_kzt_amount(lot.get("total_amount"))
        if amount is None:
            amount = _coerce_kzt_amount(lot.get("total_amount_text"))
        if amount is None:
            return None
        total += amount

    return total


def _resolve_value_amount(raw: dict[str, Any]) -> Decimal | None:
    listing_value = parse_kzt_amount(raw.get("value_text"))
    if listing_value is not None:
        return listing_value

    lot_total = _sum_detail_lot_totals(raw)
    if lot_total is not None:
        return lot_total

    return parse_kzt_amount(raw.get("detail_total_amount_text"))


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

    @with_retry(max_attempts=3)
    async def _do_detail_request(
        self, client: httpx.AsyncClient, external_id: str
    ) -> httpx.Response:
        response = await client.get(
            self.DETAIL_URL_TEMPLATE.format(external_id=external_id)
        )
        response.raise_for_status()
        return response

    async def _fetch_detail_page(
        self, client: httpx.AsyncClient, external_id: str
    ) -> dict[str, Any]:
        try:
            response = await self._do_detail_request(client, external_id)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            raise FetchError(
                f"mitwork detail external_id={external_id} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return _parse_detail_page(response.text)

    async def _enrich_with_details(
        self,
        client: httpx.AsyncClient,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for row in rows:
            external_id = row.get("data_key")
            if not isinstance(external_id, str) or not external_id:
                enriched.append(row)
                continue

            try:
                detail = await self._fetch_detail_page(client, external_id)
            except FetchError as exc:
                logger.warning(
                    "mitwork.detail_fetch_failed",
                    external_id=external_id,
                    error=str(exc),
                )
                merged = dict(row)
                merged["_detail_fetch_error"] = str(exc)
                enriched.append(merged)
                continue

            enriched.append({**row, **detail})

        logger.info(
            "mitwork.detail_enrichment_complete",
            items_requested=len(rows),
            items_enriched=len(enriched),
        )
        return enriched

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

            accumulated = await self._enrich_with_details(client, accumulated)

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

        value_amount = _resolve_value_amount(raw)
        value_currency = "KZT" if value_amount is not None else None

        detail_fields = raw.get("detail_fields")
        detail_fields_dict = (
            detail_fields if isinstance(detail_fields, dict) else {}
        )

        published_at = parse_kz_local_datetime(
            detail_fields_dict.get("offer_start_text_detail")
            or raw.get("offer_start_text")
        )
        deadline_at = parse_kz_local_datetime(
            detail_fields_dict.get("offer_end_text_detail")
            or raw.get("offer_end_text")
        )

        status_text = (
            detail_fields_dict.get("status_text_detail") or raw.get("status_text")
        )
        status = (
            STATUS_MAPPING.get(status_text, TenderStatus.unknown)
            if isinstance(status_text, str)
            else TenderStatus.unknown
        )

        source_url = raw.get("detail_url") or self.DETAIL_URL_TEMPLATE.format(
            external_id=external_id
        )

        title = detail_fields_dict.get("title_ru_detail") or title
        buyer_name = detail_fields_dict.get("organizer_name") or buyer_name

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
