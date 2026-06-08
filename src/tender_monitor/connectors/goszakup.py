"""Connector for goszakup.gov.kz — Kazakhstan's main procurement portal.

Shape notes that differ from zakup_unified (the JSON-API connector
that hits zakup.gov.kz):

- HTML scraping (Yii2 server-rendered), not a JSON API. We parse with
  ``selectolax`` and never touch a JSON endpoint here.
- ONE TENDER ROW = ONE LOT, not one announcement. The listing at
  ``/ru/search/lots`` is naturally lot-level; lot-level descriptions
  give stronger keyword-match signal than the announcement-level
  title (which tends to be generic — "Закуп моющих средств" — while
  lots are specific — "Средство моющее для посуды").
- Multiple lots share an announcement. We fetch each announcement's
  main page exactly once and also pull the announcement ``lots`` and
  ``documents`` tabs exactly once, then stitch them onto every lot
  that points at that announcement, so we never N+1 the same
  announcement URL family.
- Announcement detail is HTML too. It carries the publish date,
  offer start/end, organizer BIN, etc. — fields the listing doesn't
  surface inline.

``since`` filtering is "soft": newer lots can attach to older
announcements (re-publication of a specific lot), so we don't stop at
the first old hit. We tolerate a run of older lots and only break
after ``SINCE_OLD_THRESHOLD`` consecutive ones (same approach as
National Bank).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser, Node

from tender_monitor.connectors._html import (
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


STATUS_MAPPING: dict[str, TenderStatus] = {
    "Опубликован": TenderStatus.open,
    "Опубликован (прием ценовых предложений)": TenderStatus.open,
    "Опубликован (прием заявок)": TenderStatus.open,
}

# Field labels on the announcement detail page. The labels were
# updated to match what the live portal actually renders (we verified
# against a captured page at tests/fixtures/goszakup/announce_17013627.html
# in Prompt 12). The page has two structures we read from:
#
#   * a "form" panel at the top — six labelled <input value="..."/>
#     pairs inside <div class="form-group"> (publish/offer dates,
#     status, etc.)
#   * an "Общие сведения" panel below — <th>/<td> table rows
#     (procurement method, organizer info, total amount, …)
#
# A single map covers both; the parser does two passes and either
# source can populate the same key.
_ANNOUNCEMENT_FIELD_MAP: dict[str, str] = {
    # form-control panel
    "Номер объявления": "announcement_number",
    "Наименование объявления": "announcement_title_ru",
    "Статус объявления": "announcement_status",
    "Дата публикации объявления": "publish_date_text",
    "Срок начала приема заявок": "offer_start_text",
    "Срок окончания приема заявок": "offer_end_text",
    # Общие сведения panel (table)
    "Способ проведения закупки": "procurement_method",
    "Тип закупки": "purchase_type",
    "Способ несостоявшейся закупки": "failed_procurement_method",
    "Вид предмета закупок": "subject_type",
    "Организатор": "organizer_text",
    "Юр. адрес организатора": "organizer_legal_address",
    "Кол-во лотов в объявлении": "lot_count_text",
    "Сумма закупки": "total_amount_text",
    "Признаки": "signs",
    "Приглашенный поставщик": "invited_supplier",
    "ФИО представителя": "organizer_representative",
    "Должность": "organizer_position",
    "E-Mail": "organizer_email",
    "Создатель объявления": "announcement_creator",
}

_ANNOUNCEMENT_LINK_RE = re.compile(r"/ru/announce/index/(\d+)")
_LOT_LINK_RE = re.compile(r"/ru/subpriceoffer/index/(\d+)/(\d+)")
_BIN_PREFIX_RE = re.compile(r"^(\d{12})\s*(.*)$")


def _text(node: Node | None) -> str:
    return node.text().strip() if node is not None else ""


def _normalize_header_cells(table: Node) -> tuple[str, ...]:
    header_cells = table.css("thead th")
    if header_cells:
        return tuple(" ".join(cell.text().split()) for cell in header_cells)
    first_row = table.css_first("tr")
    if first_row is None:
        return ()
    th_cells = first_row.css("th")
    return tuple(" ".join(cell.text().split()) for cell in th_cells)


def _find_table_by_header_prefix(
    parser: HTMLParser, expected_headers: tuple[str, ...]
) -> Node | None:
    for table in parser.css("table"):
        headers = _normalize_header_cells(table)
        if headers[: len(expected_headers)] == expected_headers:
            return table
    return None


def _parse_listing_row(tr: Node) -> dict[str, Any]:
    """Extract one lot row from the goszakup listing table.

    Layout (1-indexed in the spec, 0-indexed here):
      0: lot reference number      (cell text in <strong>)
      1: announcement link + buyer (<a> + <small><b>Заказчик:</b>)
      2: lot link + История link
      3: quantity (plain text)
      4: amount ("17 241.37")
      5: procurement method
      6: status
    """
    tds = tr.css("td")

    def _cell(idx: int) -> Node | None:
        return tds[idx] if idx < len(tds) else None

    # Cell 0 — lot reference.
    lot_ref_cell = _cell(0)
    lot_reference_number = _text(lot_ref_cell)

    # Cell 1 — announcement link + buyer.
    announcement_id = ""
    announcement_number = ""
    announcement_title = ""
    buyer_name = ""
    cell_ann = _cell(1)
    if cell_ann is not None:
        link = cell_ann.css_first("a")
        if link is not None:
            href = (link.attributes.get("href") or "").strip()
            match = _ANNOUNCEMENT_LINK_RE.search(href)
            if match is not None:
                announcement_id = match.group(1)
            link_text = link.text().strip()
            # "16994597-1 Закуп моющих средств..." — first whitespace-
            # separated token is the announcement_number, rest is the
            # title. The number itself can carry suffixes ("-1") so we
            # don't try to parse it numerically here.
            parts = link_text.split(maxsplit=1)
            if parts:
                announcement_number = parts[0]
            if len(parts) > 1:
                announcement_title = parts[1]
        # <small><b>Заказчик:</b> Buyer Name</small>
        small = cell_ann.css_first("small")
        if small is not None:
            small_text = small.text().strip()
            if "Заказчик:" in small_text:
                buyer_name = small_text.split("Заказчик:", 1)[1].strip()
            else:
                buyer_name = small_text

    # Cell 2 — lot link + История link. The first <a> (without a
    # "history" class) is the lot title link.
    lot_id = ""
    lot_title = ""
    lot_detail_url = ""
    cell_lot = _cell(2)
    if cell_lot is not None:
        for link in cell_lot.css("a"):
            classes = (link.attributes.get("class") or "").split()
            if "history" in classes:
                continue
            href = (link.attributes.get("href") or "").strip()
            match = _LOT_LINK_RE.search(href)
            if match is not None:
                # The lot URL also encodes the announcement_id; we
                # don't re-overwrite if it was already pulled from
                # the announcement link in cell 1.
                if not announcement_id:
                    announcement_id = match.group(1)
                lot_id = match.group(2)
            lot_title = link.text().strip()
            lot_detail_url = (
                "https://goszakup.gov.kz" + href if href.startswith("/") else href
            )
            break

    return {
        "lot_reference_number": lot_reference_number,
        "announcement_id": announcement_id,
        "announcement_number": announcement_number,
        "announcement_title": announcement_title,
        "buyer_name": buyer_name,
        "lot_id": lot_id,
        "lot_title": lot_title,
        "lot_detail_url": lot_detail_url,
        "quantity_text": _text(_cell(3)),
        "amount_text": _text(_cell(4)),
        "procurement_method": _text(_cell(5)),
        "status_text": _text(_cell(6)),
    }


def _parse_announcement(html: str) -> dict[str, Any]:
    """Pull labelled fields off the announcement detail page.

    The page renders metadata in two different structures:

    * ``<div class="form-group">`` with a ``<label>`` + ``<input
      value="…"/>`` (announcement number, dates, status).
    * ``<table>`` rows with ``<th>label</th><td>value</td>`` ("Общие
      сведения" — procurement method, organizer info, totals).

    We walk both and merge into one flat dict keyed by
    ``_ANNOUNCEMENT_FIELD_MAP`` values. Labels are whitespace-
    normalized before lookup so stray ``&nbsp;`` and soft line-breaks
    don't cause silent misses.

    Finally we split the ``Организатор`` cell ("BIN Name") into
    ``organizer_bin`` + ``organizer_name`` since downstream consumers
    want them separately.
    """
    parser = HTMLParser(html)
    out: dict[str, Any] = dict.fromkeys(_ANNOUNCEMENT_FIELD_MAP.values())
    out["organizer_bin"] = None
    out["organizer_name"] = None

    # Pass 1: <div class="form-group"> with <label>...<input value="..."/>.
    for fg in parser.css("div.form-group"):
        label_node = fg.css_first("label")
        input_node = fg.css_first("input")
        if label_node is None or input_node is None:
            continue
        label = " ".join(label_node.text().split())
        value = (input_node.attributes.get("value") or "").strip()
        if not label or not value:
            continue
        field = _ANNOUNCEMENT_FIELD_MAP.get(label)
        if field is not None:
            out[field] = value

    # Pass 2: <table><tr><th>label</th><td>value</td></tr></table>.
    for tr in parser.css("table tr"):
        th = tr.css_first("th")
        td = tr.css_first("td")
        if th is None or td is None:
            continue
        label = " ".join(th.text().split())
        value = " ".join(td.text().split())
        if not label or not value:
            continue
        field = _ANNOUNCEMENT_FIELD_MAP.get(label)
        if field is not None:
            out[field] = value

    organizer_text = out.get("organizer_text")
    if isinstance(organizer_text, str):
        match = _BIN_PREFIX_RE.match(organizer_text)
        if match is not None:
            out["organizer_bin"] = match.group(1)
            remainder = match.group(2).strip()
            out["organizer_name"] = remainder or None
        else:
            out["organizer_name"] = organizer_text

    return out


def _parse_documents_tab(html: str) -> list[dict[str, Any]]:
    parser = HTMLParser(html)
    table = _find_table_by_header_prefix(
        parser,
        (
            "Наименование документа",
            "Признак",
        ),
    )
    if table is None:
        return []

    documents: list[dict[str, Any]] = []
    for row in table.css("tbody tr"):
        cells = row.css("td")
        if not cells:
            continue
        name = _text(cells[0])
        if not name:
            continue
        link = row.css_first("a")
        href = (link.attributes.get("href") or "").strip() if link is not None else ""
        documents.append(
            {
                "category": None,
                "name": name,
                "signed_text": _text(cells[1]) if len(cells) > 1 else None,
                "url": urljoin("https://goszakup.gov.kz", href) if href else None,
                "source": "announcement_documents_tab",
            }
        )
    return documents


def _parse_lots_tab(html: str) -> list[dict[str, Any]]:
    parser = HTMLParser(html)
    table = _find_table_by_header_prefix(
        parser,
        (
            "№",
            "Наименование",
        ),
    )
    if table is None:
        return []

    lots: list[dict[str, Any]] = []
    for row in table.css("tbody tr"):
        cells = row.css("td")
        if len(cells) < 2:
            continue

        title_cell = cells[1]
        title_link = title_cell.css_first("a")
        title = _text(title_cell)
        href = (
            (title_link.attributes.get("href") or "").strip()
            if title_link is not None
            else ""
        )
        lot_record: dict[str, Any] = {
            "sequence_text": _text(cells[0]) or None,
            "name_ru": title or None,
            "lot_url": urljoin("https://goszakup.gov.kz", href) if href else None,
        }

        if len(cells) > 2:
            lot_record["description_ru"] = _text(cells[2]) or None
        if len(cells) > 3:
            lot_record["quantity_text"] = _text(cells[3]) or None
        if len(cells) > 4:
            amount_text = _text(cells[4]) or None
            lot_record["amount_text"] = amount_text
            amount = parse_kzt_amount(amount_text)
            lot_record["amount"] = str(amount) if amount is not None else None
            lot_record["currency"] = (
                "KZT" if amount is not None else None
            )

        lots.append(lot_record)
    return lots


@register
class GoszakupConnector(Connector):
    source_name: ClassVar[str] = "goszakup"

    LISTING_URL: ClassVar[str] = "https://goszakup.gov.kz/ru/search/lots"
    DETAIL_URL_TEMPLATE: ClassVar[str] = (
        "https://goszakup.gov.kz/ru/announce/index/{announcement_id}"
    )
    PAGE_SIZE: ClassVar[int] = 50  # server-fixed
    MAX_PAGES: ClassVar[int] = 20
    SINCE_OLD_THRESHOLD: ClassVar[int] = 10
    ANNOUNCEMENT_TABS: ClassVar[tuple[str, ...]] = ("documents", "lots")

    REQUIRED_HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }

    def _make_client(self) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return make_client(headers=self.REQUIRED_HEADERS)

    @with_retry(max_attempts=3)
    async def _do_listing_request(
        self, client: httpx.AsyncClient, page: int
    ) -> httpx.Response:
        params: dict[str, str | int] = {"page": page}
        response = await client.get(self.LISTING_URL, params=params)
        response.raise_for_status()
        return response

    @with_retry(max_attempts=3)
    async def _do_detail_request(
        self,
        client: httpx.AsyncClient,
        announcement_id: str,
        *,
        tab: str | None = None,
    ) -> httpx.Response:
        params = {"tab": tab} if tab is not None else None
        response = await client.get(
            self.DETAIL_URL_TEMPLATE.format(announcement_id=announcement_id),
            params=params,
            headers={"Referer": self.LISTING_URL},
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
                f"goszakup listing page={page} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        parser = HTMLParser(response.text)
        table = parser.css_first("table#search-result")
        if table is None:
            return []
        rows: list[dict[str, Any]] = []
        for tr in table.css("tbody tr"):
            row = _parse_listing_row(tr)
            if row.get("lot_id"):
                rows.append(row)
        return rows

    async def _fetch_announcement(
        self, client: httpx.AsyncClient, announcement_id: str
    ) -> dict[str, Any] | None:
        try:
            response = await self._do_detail_request(client, announcement_id)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            logger.warning(
                "goszakup.announcement_fetch_failed",
                announcement_id=announcement_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None
        try:
            parsed = _parse_announcement(response.text)
        except Exception as exc:  # defensive: bad HTML shouldn't sink the run
            logger.warning(
                "goszakup.announcement_parse_failed",
                announcement_id=announcement_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None
        for tab in self.ANNOUNCEMENT_TABS:
            try:
                tab_response = await self._do_detail_request(
                    client,
                    announcement_id,
                    tab=tab,
                )
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.HTTPStatusError,
            ) as exc:
                logger.warning(
                    "goszakup.announcement_tab_fetch_failed",
                    announcement_id=announcement_id,
                    tab=tab,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                continue

            try:
                if tab == "documents":
                    documents = _parse_documents_tab(tab_response.text)
                    if documents:
                        parsed["_documents"] = documents
                elif tab == "lots":
                    announcement_lots = _parse_lots_tab(tab_response.text)
                    if announcement_lots:
                        parsed["_announcement_lots"] = announcement_lots
            except Exception as exc:  # defensive
                logger.warning(
                    "goszakup.announcement_tab_parse_failed",
                    announcement_id=announcement_id,
                    tab=tab,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        return parsed

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        listing_rows: list[dict[str, Any]] = []
        async with self._make_client() as client:
            seen_lot_ids: set[str] = set()
            for page in range(1, self.MAX_PAGES + 1):
                page_rows = await self._fetch_listing_page(client, page)
                fresh_rows = 0
                page_duplicate_rows = 0
                for row in page_rows:
                    lot_id = row.get("lot_id")
                    if not isinstance(lot_id, str) or not lot_id:
                        continue
                    if lot_id in seen_lot_ids:
                        page_duplicate_rows += 1
                        continue
                    seen_lot_ids.add(lot_id)
                    listing_rows.append(row)
                    fresh_rows += 1
                if page_rows and fresh_rows == 0:
                    logger.info(
                        "goszakup.pagination_stalled",
                        page=page,
                        skipped_duplicates=page_duplicate_rows,
                    )
                    break
                if len(page_rows) < self.PAGE_SIZE:
                    break

            logger.info(
                "goszakup.listing_complete",
                rows_collected=len(listing_rows),
            )

            announcement_ids: list[str] = []
            seen: set[str] = set()
            for row in listing_rows:
                ann_id = row.get("announcement_id")
                if not ann_id or ann_id in seen:
                    continue
                seen.add(ann_id)
                announcement_ids.append(ann_id)

            details: dict[str, dict[str, Any]] = {}
            for ann_id in announcement_ids:
                detail = await self._fetch_announcement(client, ann_id)
                if detail is not None:
                    details[ann_id] = detail

        combined: list[dict[str, Any]] = []
        consecutive_olds = 0
        for row in listing_rows:
            ann_id = row.get("announcement_id") or ""
            detail = details.get(ann_id)
            if detail is None:
                # Announcement detail failed — drop all this announcement's lots.
                continue
            merged = {**row, **detail}
            if since is not None:
                published = parse_kz_local_datetime(
                    merged.get("publish_date_text")
                )
                if published is not None and published < since:
                    consecutive_olds += 1
                    if consecutive_olds >= self.SINCE_OLD_THRESHOLD:
                        logger.info(
                            "goszakup.soft_since_break",
                            consecutive_olds=consecutive_olds,
                            since=since.isoformat(),
                        )
                        break
                    continue
                consecutive_olds = 0
            combined.append(merged)
        return combined

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        lot_id = raw.get("lot_id")
        if not lot_id:
            raise ParseError("lot row is missing 'lot_id'")

        title = raw.get("lot_title")
        if not title:
            raise ParseError(f"lot {lot_id} has empty lot_title")

        buyer_name = raw.get("buyer_name") or raw.get("organizer_name")
        buyer_external_id = raw.get("organizer_bin")

        value_amount = parse_kzt_amount(raw.get("amount_text"))
        value_currency = "KZT" if value_amount is not None else None

        published_at = parse_kz_local_datetime(raw.get("publish_date_text"))
        deadline_at = parse_kz_local_datetime(raw.get("offer_end_text"))

        status_text = raw.get("status_text") or raw.get("announcement_status")
        status = STATUS_MAPPING.get(
            status_text or "", TenderStatus.unknown
        ) if isinstance(status_text, str) else TenderStatus.unknown

        source_url = raw.get("lot_detail_url") or (
            f"https://goszakup.gov.kz/ru/subpriceoffer/index/"
            f"{raw.get('announcement_id', '')}/{lot_id}"
        )

        # Synthetic _lots wrap so the keyword matcher walks the lot
        # title alongside the announcement title.
        raw_json = dict(raw)
        announcement_lots = raw.get("_announcement_lots")
        if isinstance(announcement_lots, list) and announcement_lots:
            raw_json["announcement_lots"] = announcement_lots
        raw_json["_lots"] = [{
            "name_ru": raw.get("lot_title"),
            "description_ru": raw.get("announcement_title"),
            "quantity_text": raw.get("quantity_text"),
            "amount_text": raw.get("amount_text"),
        }]

        return TenderUpsert(
            source_name=self.source_name,
            external_id=str(lot_id),
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
    "STATUS_MAPPING",
    "GoszakupConnector",
    "_parse_announcement",
    "_parse_documents_tab",
    "_parse_listing_row",
    "_parse_lots_tab",
]
