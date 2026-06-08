"""Connector for zakup.nationalbank.kz — the National Bank of Kazakhstan
procurement portal.

Shape notes that differ from MITWORK:

- We use ``/ru/publics/lots`` (NOT ``/publics/buys``). One LOT becomes
  one tender row. The lot's name + characteristic is specific and
  match-worthy; the announcement-level name tends to be generic
  ("Услуги по продлению лицензий", etc.).
- The LISTING has no date columns — start / deadline live only on the
  detail page. The connector therefore ALWAYS fetches the detail
  page for every listing row. With ~10 new lots/day and MAX_PAGES=3
  the per-run cost is ~150 detail fetches.
- The detail page embeds the announcement info (start/end, organizer,
  procurement method, status) plus the document table, so one fetch
  per lot is enough — no separate announcement hop.

`since` filtering is "soft": because newer lot ids can be attached to
older announcements, we don't stop at the first lot whose announcement
is older than ``since``. We tolerate a run of older hits, and only
stop after seeing ``SINCE_OLD_THRESHOLD`` consecutive ones.

The matcher walks ``raw_json["_lots"]``; National Bank doesn't have a
natural lots array (one lot per row already). We synthesize a single-
element ``_lots`` entry on the way out so the matcher sees both the
name and the characteristic text in the haystack.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

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


# Status text on the LISTING row → our TenderStatus. The listing
# column carries the lot status (masculine "Опубликован" because "лот"
# is masculine in Russian). The announcement-level status on the
# detail page is "Опубликовано" (neuter, because "объявление" is
# neuter) — that one lands in raw_json["announcement_status"].
STATUS_MAPPING: dict[str, TenderStatus] = {
    "Опубликован": TenderStatus.open,
    "Итоги. Закупка состоялась": TenderStatus.awarded,
}


def _td_text(td: Node) -> str:
    return td.text().strip()


def _parse_listing_row(row: Node) -> dict[str, Any]:
    """Extract one National Bank listing row into a dict.

    Columns: Number | Name (with ЕНСТРУ <span class="label">) |
    Characteristic | Sum | Customer | Status. Missing fields fall
    through as empty strings or None — ``_normalize`` is responsible
    for rejecting incomplete rows.
    """
    data_key = (row.attributes.get("data-key") or "").strip()
    tds = row.css("td")

    def _cell(idx: int) -> Node | None:
        return tds[idx] if idx < len(tds) else None

    # Column 0: announcement_number (plain text)
    cell_num = _cell(0)
    announcement_number = _td_text(cell_num) if cell_num is not None else ""

    # Column 1: title link + ЕНСТРУ label
    title_ru = ""
    detail_url = ""
    enstru_code: str | None = None
    cell_name = _cell(1)
    if cell_name is not None:
        link = cell_name.css_first("a")
        if link is not None:
            title_ru = link.text().strip()
            detail_url = (link.attributes.get("href") or "").strip()
        label = cell_name.css_first("span.label")
        if label is not None:
            enstru_code = label.text().strip() or None

    # Column 2: characteristic
    cell_char = _cell(2)
    characteristic_ru = (
        (_td_text(cell_char) or None) if cell_char is not None else None
    )

    # Column 3: value text
    cell_value = _cell(3)
    value_text = _td_text(cell_value) if cell_value is not None else ""

    # Column 4: customer link (title= organization name, text= BIN)
    buyer_name = ""
    buyer_bin = ""
    subject_url = ""
    cell_buyer = _cell(4)
    if cell_buyer is not None:
        buyer_link = cell_buyer.css_first("a")
        if buyer_link is not None:
            buyer_name = (buyer_link.attributes.get("title") or "").strip()
            buyer_bin = buyer_link.text().strip()
            subject_url = (buyer_link.attributes.get("href") or "").strip()

    # Column 5: status text
    cell_status = _cell(5)
    status_text = _td_text(cell_status) if cell_status is not None else ""

    return {
        "data_key": data_key,
        "announcement_number": announcement_number,
        "title_ru": title_ru,
        "enstru_code": enstru_code,
        "detail_url": detail_url,
        "characteristic_ru": characteristic_ru,
        "value_text": value_text,
        "buyer_name": buyer_name or None,
        "buyer_bin": buyer_bin or None,
        "subject_url": subject_url,
        "status_text": status_text,
    }


# Label-text → key in the detail dict, for the lot's own detail-view
# (first table on the page). Labels are the exact strings used by the
# portal; the lookup is intentionally exact-match to surface schema
# changes loudly instead of silently dropping fields.
_LOT_LABEL_KEYS: dict[str, str] = {
    "Наименование закупаемых товаров, работ, услуг на русском языке": "name_ru",
    "Наименование закупаемых товаров, работ, услуг на государственном языке": "name_kk",
    "Код ЕНСТРУ": "enstru_code",
    "Характеристика закупаемых товаров, работ, услуг на русском языке": "characteristic_ru",
    "Характеристика закупаемых товаров, работ, услуг на государственном языке": "characteristic_kk",
    "Тип пункта плана": "plan_type",
    "Год": "year",
    "Срок проведения закупки": "period",
}

# Label-text → key for the announcement detail-view (second table).
_ANNOUNCEMENT_LABEL_KEYS: dict[str, str] = {
    "Наименование объявления на русском языке": "announcement_name_ru",
    "Наименование объявления на государственном языке": "announcement_name_kk",
    "Дата и время начала приема заявок": "announcement_start_text",
    "Дата и время вскрытия и завершения приема заявок": "announcement_end_text",
    "Организатор": "organizer_name",
    "Электронный адрес организатора закупки": "organizer_email",
    "Способ закупки": "procurement_method",
    "Статус": "announcement_status",
}


def _extract_detail_view_kv(
    table: Node, label_keys: dict[str, str]
) -> dict[str, str | None]:
    """Walk a Yii2 ``detail-view`` table, mapping known TH labels to TD text.

    Unknown labels are silently ignored (the portal occasionally
    sprouts new rows we have no slot for; they survive in raw_json
    anyway). Empty strings are normalized to None.
    """
    extracted: dict[str, str | None] = {}
    for tr in table.css("tr"):
        th = tr.css_first("th")
        td = tr.css_first("td")
        if th is None or td is None:
            continue
        label = th.text().strip()
        key = label_keys.get(label)
        if key is None:
            continue
        # Strip transient labels (e.g. "через 7 дней") and inline
        # scripts/br before reading the TD text.
        for stripme in td.css("span.label, script, br"):
            stripme.decompose()
        value = td.text().strip() or None
        extracted[key] = value
    return extracted


def _extract_organizer_url(table: Node) -> str | None:
    for tr in table.css("tr"):
        th = tr.css_first("th")
        td = tr.css_first("td")
        if th is None or td is None:
            continue
        if th.text().strip() != "Организатор":
            continue
        link = td.css_first("a")
        if link is None:
            return None
        href = (link.attributes.get("href") or "").strip()
        return href or None
    return None


def _extract_announcement_id(tree: HTMLParser) -> tuple[str | None, str | None]:
    """Pull ``(announcement_id, announcement_url)`` from the H3 heading.

    The heading reads ``Информация об объявлении <a href=...>NNNN</a>``;
    we return the link's text + href. None for either if the section is
    absent (very early-stage or unusual lot pages).
    """
    for h in tree.css("h3"):
        text = h.text().strip()
        if not text.startswith("Информация об объявлении"):
            continue
        link = h.css_first("a")
        if link is None:
            return None, None
        href = (link.attributes.get("href") or "").strip() or None
        ann_id = link.text().strip() or None
        return ann_id, href
    return None, None


def _extract_delivery_places(tree: HTMLParser) -> list[dict[str, str]]:
    """Pull the "Место поставки" rows. Best-effort; empty list on miss.

    The page has two ``div.grid-view`` blocks (places, then docs); the
    places block sits between the lot-detail table and the announcement
    H3. We scope by H3 anchor to keep this robust against extra grids.
    """
    places: list[dict[str, str]] = []
    # Find the "Место поставки" heading and walk forward to its table.
    target_h3: Node | None = None
    for h in tree.css("h3"):
        if h.text().strip() == "Место поставки":
            target_h3 = h
            break
    if target_h3 is None:
        return places
    sibling = target_h3.next
    while sibling is not None:
        if sibling.tag == "div":
            table = sibling.css_first("table")
            if table is not None:
                for tr in table.css("tbody tr"):
                    cells = [_td_text(td) for td in tr.css("td")]
                    if len(cells) >= 3:
                        places.append(
                            {
                                "country": cells[0],
                                "place": cells[1],
                                "quantity": cells[2],
                            }
                        )
                break
        sibling = sibling.next
    return places


def _extract_documents(tree: HTMLParser) -> list[dict[str, str | None]]:
    """Pull the document table rows in the shared UI-friendly shape."""
    docs: list[dict[str, str | None]] = []
    target_h3: Node | None = None
    for h in tree.css("h3"):
        if "Документы" in h.text():
            target_h3 = h
            break
    if target_h3 is None:
        return docs
    sibling = target_h3.next
    while sibling is not None:
        if sibling.tag == "div":
            table = sibling.css_first("table")
            if table is not None:
                for tr in table.css("tbody tr"):
                    td_nodes = tr.css("td")
                    cells = [_td_text(td) for td in td_nodes]
                    if len(cells) >= 5:
                        download_link = (
                            td_nodes[5].css_first("a")
                            if len(td_nodes) > 5
                            else None
                        )
                        download_url = None
                        if download_link is not None:
                            href = (download_link.attributes.get("href") or "").strip()
                            download_url = href or None
                        ext = None
                        if "." in cells[1]:
                            ext = cells[1].rsplit(".", 1)[-1].strip().upper() or None
                        docs.append(
                            {
                                "category": cells[0],
                                "name": cells[1],
                                "size_text": cells[2],
                                "uploaded_at_text": cells[3],
                                "hash": cells[4],
                                "url": download_url,
                                "ext": ext,
                                "source": "detail_page",
                            }
                        )
                break
        sibling = sibling.next
    return docs


def _parse_detail(html: str) -> dict[str, Any]:
    """Extract a National Bank lot detail page into a flat dict.

    All keys are always present (None on miss). The ``announcement_*``
    keys come from the lower table; the lot fields come from the upper
    one. Unknown labels are tolerated.
    """
    tree = HTMLParser(html)

    detail_tables = tree.css("table.detail-view")
    lot_table = detail_tables[0] if len(detail_tables) > 0 else None
    ann_table = detail_tables[1] if len(detail_tables) > 1 else None

    lot_fields: dict[str, str | None] = dict.fromkeys(_LOT_LABEL_KEYS.values())
    if lot_table is not None:
        lot_fields.update(_extract_detail_view_kv(lot_table, _LOT_LABEL_KEYS))

    # The "Количество (объем), сумма выделенная на закупку" row is the
    # only one with an <h4> inside the td. We pull that summary separately
    # because it carries the unit + per-unit + total price in one string.
    amount_summary: str | None = None
    if lot_table is not None:
        for tr in lot_table.css("tr"):
            th = tr.css_first("th")
            if th is None:
                continue
            if "Количество" not in th.text():
                continue
            td = tr.css_first("td")
            if td is None:
                continue
            h4 = td.css_first("h4")
            amount_summary = (h4.text() if h4 is not None else td.text()).strip()
            amount_summary = amount_summary or None
            break

    ann_fields: dict[str, str | None] = dict.fromkeys(_ANNOUNCEMENT_LABEL_KEYS.values())
    organizer_url: str | None = None
    if ann_table is not None:
        ann_fields.update(_extract_detail_view_kv(ann_table, _ANNOUNCEMENT_LABEL_KEYS))
        organizer_url = _extract_organizer_url(ann_table)

    announcement_id, announcement_url = _extract_announcement_id(tree)

    return {
        "name_ru": lot_fields.get("name_ru"),
        "name_kk": lot_fields.get("name_kk"),
        "enstru_code": lot_fields.get("enstru_code"),
        "characteristic_ru": lot_fields.get("characteristic_ru"),
        "characteristic_kk": lot_fields.get("characteristic_kk"),
        "plan_type": lot_fields.get("plan_type"),
        "year": lot_fields.get("year"),
        "period": lot_fields.get("period"),
        "amount_summary": amount_summary,
        "delivery_places": _extract_delivery_places(tree),
        "announcement_id": announcement_id,
        "announcement_url": announcement_url,
        "announcement_name_ru": ann_fields.get("announcement_name_ru"),
        "announcement_name_kk": ann_fields.get("announcement_name_kk"),
        "announcement_start_text": ann_fields.get("announcement_start_text"),
        "announcement_end_text": ann_fields.get("announcement_end_text"),
        "organizer_name": ann_fields.get("organizer_name"),
        "organizer_url": organizer_url,
        "organizer_email": ann_fields.get("organizer_email"),
        "procurement_method": ann_fields.get("procurement_method"),
        "announcement_status": ann_fields.get("announcement_status"),
        "_documents": _extract_documents(tree),
    }


def _merge_listing_and_detail(
    listing: dict[str, Any], detail: dict[str, Any]
) -> dict[str, Any]:
    """Merge listing row and detail dict; prefix detail collisions with ``detail_``."""
    merged: dict[str, Any] = dict(listing)
    for key, value in detail.items():
        if key in merged:
            merged[f"detail_{key}"] = value
        else:
            merged[key] = value
    return merged


@register
class NationalBankConnector(Connector):
    source_name: ClassVar[str] = "national_bank"

    LISTING_URL: ClassVar[str] = "https://zakup.nationalbank.kz/ru/publics/lots"
    DETAIL_URL_TEMPLATE: ClassVar[str] = (
        "https://zakup.nationalbank.kz/ru/publics/lot/{external_id}"
    )
    MAX_PAGES: ClassVar[int] = 10  # 50 × 10 = 500 lots/run
    PAGE_SIZE: ClassVar[int] = 50  # server-fixed
    # See `_fetch_raw` for the rationale: newer lot ids can sit on older
    # announcements, so we don't stop at the first old hit — only after
    # a run of this many in a row.
    SINCE_OLD_THRESHOLD: ClassVar[int] = 5

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

    DETAIL_REFERER: ClassVar[str] = "https://zakup.nationalbank.kz/"

    def _make_client(self) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return make_client(headers=self.REQUIRED_HEADERS)

    @with_retry(max_attempts=3)
    async def _do_listing_request(
        self, client: httpx.AsyncClient, page: int
    ) -> httpx.Response:
        response = await client.get(self.LISTING_URL, params={"page": page})
        response.raise_for_status()
        return response

    @with_retry(max_attempts=3)
    async def _do_detail_request(
        self, client: httpx.AsyncClient, external_id: str
    ) -> httpx.Response:
        response = await client.get(
            self.DETAIL_URL_TEMPLATE.format(external_id=external_id),
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
                f"national_bank listing page={page} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        tree = HTMLParser(response.text)
        rows = tree.css("tr.item")
        return [_parse_listing_row(row) for row in rows]

    async def _fetch_lot_detail(
        self, client: httpx.AsyncClient, external_id: str
    ) -> dict[str, Any] | None:
        try:
            response = await self._do_detail_request(client, external_id)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            logger.warning(
                "national_bank.detail_fetch_failed",
                external_id=external_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None
        return _parse_detail(response.text)

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        listing_rows: list[dict[str, Any]] = []
        pages_walked = 0
        async with self._make_client() as client:
            seen_listing_ids: set[str] = set()
            for page in range(1, self.MAX_PAGES + 1):
                page_rows = await self._fetch_listing_page(client, page)
                pages_walked = page
                if not page_rows:
                    break
                fresh_rows = 0
                for row in page_rows:
                    external_id = row.get("data_key")
                    if not isinstance(external_id, str) or not external_id:
                        continue
                    if external_id in seen_listing_ids:
                        continue
                    seen_listing_ids.add(external_id)
                    listing_rows.append(row)
                    fresh_rows += 1
                if page_rows and fresh_rows == 0:
                    logger.info(
                        "national_bank.pagination_stalled",
                        page=page,
                    )
                    break
                if len(page_rows) < self.PAGE_SIZE:
                    break

            logger.info(
                "national_bank.listing_complete",
                pages_walked=pages_walked,
                rows_collected=len(listing_rows),
            )

            result: list[dict[str, Any]] = []
            consecutive_old = 0
            stopped_on_since = False
            for row in listing_rows:
                external_id = row.get("data_key") or ""
                if not external_id:
                    logger.warning(
                        "national_bank.listing_row_missing_data_key", row=row
                    )
                    continue
                detail = await self._fetch_lot_detail(client, external_id)
                if detail is None:
                    continue
                merged = _merge_listing_and_detail(row, detail)
                start_at = parse_kz_local_datetime(
                    merged.get("announcement_start_text")
                )
                if since is not None and start_at is not None and start_at < since:
                    consecutive_old += 1
                    if consecutive_old >= self.SINCE_OLD_THRESHOLD:
                        logger.info(
                            "national_bank.soft_since_break",
                            consecutive_old=consecutive_old,
                            since=since.isoformat(),
                        )
                        stopped_on_since = True
                        break
                    continue
                consecutive_old = 0
                result.append(merged)

            logger.info(
                "national_bank.fetch_complete",
                rows_collected=len(result),
                stopped_on_since=stopped_on_since,
            )
        return result

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        external_id = raw.get("data_key")
        if not external_id:
            raise ParseError("national_bank row is missing data-key")

        title = raw.get("title_ru")
        if not title:
            raise ParseError(f"national_bank lot {external_id} has empty title")

        buyer_name = raw.get("buyer_name")
        buyer_external_id = raw.get("buyer_bin")

        value_amount: Decimal | None = parse_kzt_amount(raw.get("value_text"))
        value_currency = "KZT" if value_amount is not None else None

        published_at = parse_kz_local_datetime(raw.get("announcement_start_text"))
        deadline_at = parse_kz_local_datetime(raw.get("announcement_end_text"))

        status_text = raw.get("status_text")
        status = (
            STATUS_MAPPING.get(status_text, TenderStatus.unknown)
            if isinstance(status_text, str)
            else TenderStatus.unknown
        )

        source_url = raw.get("detail_url") or self.DETAIL_URL_TEMPLATE.format(
            external_id=external_id
        )

        # Synthetic single-element _lots so the matcher's haystack walk
        # picks up the lot's characteristic alongside its name. See the
        # module docstring for context.
        raw_json: dict[str, Any] = dict(raw)
        raw_json["_lots"] = [
            {
                "name_ru": raw.get("title_ru"),
                "description_ru": raw.get("characteristic_ru"),
            }
        ]
        documents = raw.get("_documents")
        if isinstance(documents, list) and documents:
            raw_json["_documents"] = documents

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
    "STATUS_MAPPING",
    "NationalBankConnector",
    "_parse_detail",
    "_parse_listing_row",
]
