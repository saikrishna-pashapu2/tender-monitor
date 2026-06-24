"""Connector for ets-tender.kz — a commercial-procurement portal where
private buyers (banks, mining companies, manufacturers, …) publish RFQ-
style tenders open to outside suppliers.

Shape notes that differ from the other HTML scrapers:

- ``?show=actual`` is the only listing slice we touch. Every tender we
  ingest is implicitly ``TenderStatus.open``; archived tenders live at
  ``?show=archive`` and are out of scope.
- The listing carries dates inline (``Опубликовано`` / ``Актуально до``
  columns) so the ``since`` window can be enforced at listing level. We
  still keep the filter soft rather than stopping on the first old row:
  ETS-Tender's sort is usually newest-first, but a strict monotonicity
  assumption is too brittle if pinned/reopened tenders appear out of
  order.
- The listing does NOT carry amount, ENSTRU code, or the full
  description. Those live on the detail page, so we always fetch
  ``/market/<slug>/tender-<id>/`` per listing row.
- ``external_id`` is the digits after ``tender-`` in the detail URL
  (e.g. ``2085996``). The trailing ``#btid=…`` URL fragment is client-
  side state and gets stripped before the request goes out.
- Closed/private procedures ("Закрытый запрос цен …") render dates as
  ``Скрыто`` ("Hidden") on the listing and may 403 / 404 on detail. We
  store what we have from the listing and log a warning on detail
  failure — the run still produces a row.
- Amounts use a mixed Cyrillic format: NBSP / regular space thousands,
  comma decimal, ``тенге`` suffix, often with a parenthetical VAT note
  like ``(цена с НДС, НДС: 16%)``. ``parse_kzt_amount`` (now refactored
  to be format-agnostic) does the heavy lifting.
- Dates are KZ-local in the European ``DD.MM.YYYY HH:MM`` format, not
  the ``YYYY-MM-DD HH:MM:SS`` MITWORK and National Bank use, hence the
  ``parse_kz_local_datetime_dmy`` sibling parser.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, ClassVar

import httpx
from selectolax.parser import HTMLParser, Node

from tender_monitor.connectors._html import (
    parse_kz_local_datetime_dmy,
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


_TENDER_ID_RE = re.compile(r"/tender-(\d+)/")
_FILE_EXT_RE = re.compile(
    r"\.(?:pdf|doc|docx|xls|xlsx|zip|rar|7z|rtf|txt|jpg|jpeg|png)$",
    re.IGNORECASE,
)

# Labels on the detail-page tables that we project into typed keys.
# Anything not in this map still survives in raw_json (we keep the raw
# row + detail dict), so adding a column later is a parser tweak, not
# a schema change.
_DETAIL_LABEL_KEYS: dict[str, str] = {
    "Категория ЕНС ТРУ": "enstru_text",
    "Количество": "quantity_text",
    "Цена за единицу": "unit_price_text",
    "Общая стоимость": "total_price_text",
    "Общая стоимость закупки": "total_price_text",
    "Опубликовано": "published_text",
    "Актуально до": "deadline_text",
    "Последнее изменение": "last_edited_text",
    "Место поставки": "delivery_address",
    "Условия оплаты": "payment_terms",
}


def _strip_fragment(href: str) -> str:
    """Drop the ``#…`` fragment from a URL — ETS-Tender stuffs client-
    side state (``btid``, ``sqh``, ``tsid``) into the fragment.
    """
    idx = href.find("#")
    return href[:idx] if idx >= 0 else href


def _norm_text(node: Node | None) -> str:
    return " ".join(node.text().split()) if node is not None else ""


def _norm_label(node: Node | None) -> str:
    return _norm_text(node).rstrip(":").strip()


def _detail_value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict | tuple | set):
        return bool(value)
    return True


def _merge_listing_and_detail(
    listing: dict[str, Any], detail: dict[str, Any]
) -> dict[str, Any]:
    """Merge detail fields without letting detail misses erase listing data."""
    merged = dict(listing)
    for key, value in detail.items():
        if _detail_value_present(value):
            merged[key] = value
    return merged


def _extract_documents(parser: HTMLParser) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for link in parser.css("a[href]"):
        href = (link.attributes.get("href") or "").strip()
        if not href:
            continue
        normalized_href = href.split("?", 1)[0]
        normalized_href = normalized_href.split("#", 1)[0]
        if not _FILE_EXT_RE.search(normalized_href):
            continue

        absolute_url = (
            href
            if href.startswith("http://") or href.startswith("https://")
            else EtsTenderConnector.BASE_URL + href
        )
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)

        name = _norm_text(link) or normalized_href.rsplit("/", 1)[-1]
        documents.append(
            {
                "category": None,
                "name": name or "Document",
                "url": absolute_url,
                "ext": normalized_href.rsplit(".", 1)[-1].upper(),
                "source": "detail_page",
            }
        )

    return documents


def _parse_listing_row(tr: Node) -> dict[str, Any] | None:
    """Pull one ETS-Tender listing row into a flat dict.

    Layout (5 cells per row):

      0: title link (with optional <div class="search-results-title-desc">)
      1: organizer link
      2: published date (DD.MM.YYYY HH:MM) or "Скрыто"
      3: deadline date  (DD.MM.YYYY HH:MM) or "Скрыто"
      4: favorite-icon column (skipped)

    Returns ``None`` for rows we can't extract an ``external_id`` from
    (e.g. a header row injected into ``<tbody>``, an ad placeholder, or
    a malformed entry). The caller filters those out.
    """
    tds = tr.css("td")
    if len(tds) < 4:
        return None

    title_cell = tds[0]
    title_link = title_cell.css_first("a.search-results-title")
    if title_link is None:
        title_link = title_cell.css_first("a")
    if title_link is None:
        return None

    raw_href = (title_link.attributes.get("href") or "").strip()
    detail_url = _strip_fragment(raw_href)
    if not detail_url:
        return None
    id_match = _TENDER_ID_RE.search(detail_url)
    if id_match is None:
        return None
    external_id = id_match.group(1)

    desc_node = title_link.css_first("div.search-results-title-desc")
    description_text = _norm_text(desc_node) or None
    # Title prefix is everything in the <a> before the <div>. We pull
    # this by cloning the title link, stripping the desc div, and
    # reading the remainder.
    title_prefix_text = title_link.text(deep=True)
    if desc_node is not None:
        # text() includes the description block; subtract it.
        desc_blob = desc_node.text(deep=True)
        if desc_blob and desc_blob in title_prefix_text:
            title_prefix_text = title_prefix_text.replace(desc_blob, "", 1)
    title_prefix = " ".join(title_prefix_text.split())

    # Split "Запрос предложений № 2085996" into ("Запрос предложений", "2085996").
    procedure_type_text = title_prefix
    title_short = title_prefix
    if "№" in title_prefix:
        procedure_type_text, _, _ = title_prefix.partition("№")
        procedure_type_text = procedure_type_text.strip()
        title_short = title_prefix  # keep full "<type> № <id>" for storage

    organizer_cell = tds[1]
    organizer_link = organizer_cell.css_first("a")
    buyer_name = ""
    buyer_url = ""
    if organizer_link is not None:
        buyer_name = organizer_link.text().strip()
        buyer_url = (organizer_link.attributes.get("href") or "").strip()

    published_text = _norm_text(tds[2])
    deadline_text = _norm_text(tds[3])

    return {
        "external_id": external_id,
        "procedure_type_text": procedure_type_text,
        "title_short": title_short,
        "title_description": description_text,
        "detail_url": detail_url,
        "buyer_name": buyer_name or None,
        "buyer_url": buyer_url or None,
        "published_text": published_text,
        "deadline_text": deadline_text,
    }


def _parse_detail(html: str) -> dict[str, Any]:
    """Extract the ETS-Tender detail page into a flat dict.

    The page has two tables (auction info, "Дополнительная информация"
    extras). Both are ``<th>label</th><td>value</td>`` row-shaped and
    we collapse them into one keyspace via ``_DETAIL_LABEL_KEYS``.

    Title and description live outside the tables; we pull them by
    selector. The ENSTRU cell is "<code> — <label>"; we split on the
    first em/en/hyphen for the code prefix when it's a digit run.

    Every key in the returned dict is always present (``None`` on
    miss) so downstream merging doesn't have to special-case absent
    fields. The amount-with-VAT cell additionally produces ``vat_note``
    (the parenthetical, e.g. ``"(цена с НДС, НДС: 16%)"``) so the
    notification template can surface it without re-parsing.
    """
    parser = HTMLParser(html)
    out: dict[str, Any] = dict.fromkeys(_DETAIL_LABEL_KEYS.values())
    out["title_full"] = None
    out["description_full"] = None
    out["enstru_code"] = None
    out["enstru_label"] = None
    out["vat_note"] = None
    out["organizer_link_text"] = None
    out["organizer_link_url"] = None
    out["_documents"] = []

    # Title: only the dedicated h2.tender-title element. Broader
    # fallbacks (h1 / any h2) tend to pick up anti-bot challenge
    # banners or unrelated headers when the real detail page wasn't
    # served (e.g. ETS-Tender's reCAPTCHA interstitial). Keep it tight
    # and let the listing's title_short be the fallback at normalize.
    title_node = parser.css_first("h2.tender-title")
    out["title_full"] = _norm_text(title_node) or None

    # Description: .expandable-text inside .tender-description.
    desc_node = parser.css_first(".tender-description .expandable-text")
    if desc_node is None:
        desc_node = parser.css_first(".expandable-text")
    out["description_full"] = _norm_text(desc_node) or None

    # Walk both detail layouts seen on ETS:
    #   <tr><th>label</th><td>value</td></tr>
    #   <tr><td class="fname">label:</td><td>value</td></tr>
    for tr in parser.css("table tr"):
        label_node = tr.css_first("th")
        value_node = tr.css_first("td")
        if label_node is None:
            cells = tr.css("td")
            if len(cells) < 2:
                continue
            first_class = cells[0].attributes.get("class") or ""
            if "fname" not in first_class.split():
                continue
            label_node = cells[0]
            value_node = cells[1]

        label = _norm_label(label_node)
        # For the organizer cell we want the link's text + href, not the
        # td.text() (which would already be the same for plain links but
        # we capture the URL explicitly).
        if label == "Организатор":
            link = value_node.css_first("a")
            if link is not None:
                out["organizer_link_text"] = link.text().strip() or None
                out["organizer_link_url"] = (
                    (link.attributes.get("href") or "").strip() or None
                )
            continue
        value = _norm_text(value_node)
        if not label or not value:
            continue
        field = _DETAIL_LABEL_KEYS.get(label)
        if field is None:
            continue
        out[field] = value

    enstru = out.get("enstru_text")
    if isinstance(enstru, str) and enstru:
        # "241031.900.000011 — Лист стальной…" — split on the first
        # whitespace-em-whitespace separator. Fall back to "code only"
        # when the label part is absent.
        parts = re.split(r"\s+[—–-]\s+", enstru, maxsplit=1)
        head = parts[0].strip()
        out["enstru_code"] = head or None
        if len(parts) > 1:
            tail = parts[1].strip()
            out["enstru_label"] = tail or None

    total_price = out.get("total_price_text")
    if isinstance(total_price, str) and "(" in total_price:
        start = total_price.index("(")
        end = total_price.find(")", start)
        if end > start:
            out["vat_note"] = total_price[start : end + 1]

    documents = _extract_documents(parser)
    if documents:
        out["_documents"] = documents

    return out


@register
class EtsTenderConnector(Connector):
    source_name: ClassVar[str] = "ets_tender"

    BASE_URL: ClassVar[str] = "https://www.ets-tender.kz"
    LISTING_URL: ClassVar[str] = f"{BASE_URL}/market/"
    LISTING_PARAMS: ClassVar[dict[str, str]] = {"show": "actual"}
    MAX_PAGES: ClassVar[int] = 20
    PAGE_SIZE_HINT: ClassVar[int] = 10
    SINCE_OLD_THRESHOLD: ClassVar[int] = 10

    REQUIRED_HEADERS: ClassVar[dict[str, str]] = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        # ETS-Tender's edge gates the listing on these two cookies; the
        # site sets them via JS on first load. Sending them up front
        # avoids the "are you human?" interstitial that strips the
        # tbody before it reaches us.
        "Cookie": "lang=rus; testcookie=1",
    }

    def _make_client(self) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return make_client(headers=self.REQUIRED_HEADERS)

    @with_retry(max_attempts=3)
    async def _do_listing_request(
        self, client: httpx.AsyncClient, page: int
    ) -> httpx.Response:
        params: dict[str, str | int] = {"show": "actual"}
        if page > 1:
            params["page"] = page
        response = await client.get(self.LISTING_URL, params=params)
        response.raise_for_status()
        return response

    @with_retry(max_attempts=3)
    async def _do_detail_request(
        self, client: httpx.AsyncClient, detail_url: str
    ) -> httpx.Response:
        response = await client.get(
            self.BASE_URL + detail_url,
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
                f"ets_tender listing page={page} failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        tree = HTMLParser(response.text)
        table = tree.css_first("table.search-results")
        if table is None:
            return []
        rows: list[dict[str, Any]] = []
        for tr in table.css("tbody tr"):
            parsed = _parse_listing_row(tr)
            if parsed is not None:
                rows.append(parsed)
        return rows

    async def _fetch_detail(
        self, client: httpx.AsyncClient, detail_url: str, external_id: str
    ) -> dict[str, Any] | None:
        try:
            response = await self._do_detail_request(client, detail_url)
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            logger.warning(
                "ets_tender.detail_fetch_failed",
                external_id=external_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None
        text = response.text
        # The portal serves a Google-reCAPTCHA interstitial in place of
        # the detail page when it suspects bot activity. The body
        # carries "Превышен максимальный лимит" and a g-recaptcha
        # widget; "tender-title" is absent. Recognize the pattern so
        # ops can tell "blocked by anti-bot" from "real parse failure".
        if "g-recaptcha" in text and "tender-title" not in text:
            logger.warning(
                "ets_tender.detail_recaptcha_interstitial",
                external_id=external_id,
            )
            return None
        try:
            return _parse_detail(text)
        except Exception as exc:  # defensive: malformed HTML mustn't sink the run
            logger.warning(
                "ets_tender.detail_parse_failed",
                external_id=external_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        in_window: list[dict[str, Any]] = []
        stopped_on_since = False
        pages_walked = 0
        consecutive_olds = 0

        async with self._make_client() as client:
            seen_external_ids: set[str] = set()
            for page in range(1, self.MAX_PAGES + 1):
                page_rows = await self._fetch_listing_page(client, page)
                pages_walked = page
                if not page_rows:
                    break

                fresh_rows = 0
                for row in page_rows:
                    external_id = row.get("external_id")
                    if not isinstance(external_id, str) or not external_id:
                        continue
                    if external_id in seen_external_ids:
                        continue
                    seen_external_ids.add(external_id)
                    fresh_rows += 1
                    published = parse_kz_local_datetime_dmy(
                        row.get("published_text")
                    )
                    if since is not None and published is not None and published < since:
                        consecutive_olds += 1
                        if consecutive_olds >= self.SINCE_OLD_THRESHOLD:
                            stopped_on_since = True
                            break
                        continue
                    consecutive_olds = 0
                    in_window.append(row)

                if stopped_on_since:
                    break
                if page_rows and fresh_rows == 0:
                    logger.info(
                        "ets_tender.pagination_stalled",
                        page=page,
                    )
                    break
                if len(page_rows) < self.PAGE_SIZE_HINT:
                    # Short page → almost certainly the last page.
                    break

            logger.info(
                "ets_tender.listing_complete",
                pages_walked=pages_walked,
                rows_in_window=len(in_window),
                stopped_on_since=stopped_on_since,
            )

            combined: list[dict[str, Any]] = []
            for row in in_window:
                detail = await self._fetch_detail(
                    client, row["detail_url"], row["external_id"]
                )
                if detail is None:
                    # Detail unavailable (403/404 for closed procedures,
                    # network failure after retries, etc.) — keep the
                    # listing-only fields. The normalize step will leave
                    # value_amount and the more-precise detail dates as
                    # None.
                    combined.append(dict(row))
                    continue
                combined.append(_merge_listing_and_detail(row, detail))

        return combined

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        external_id = raw.get("external_id")
        if not external_id:
            raise ParseError("ets_tender row is missing external_id")

        title = (
            raw.get("title_full")
            or raw.get("title_description")
            or raw.get("title_short")
        )
        if not title:
            raise ParseError(
                f"ets_tender tender {external_id} has no title in any field"
            )

        buyer_name = raw.get("buyer_name")

        value_amount = parse_kzt_amount(raw.get("total_price_text"))
        value_currency = "KZT" if value_amount is not None else None

        # Detail page wins over listing for dates when both are present
        # (the listing strips seconds; the detail string already does
        # too, but we still prefer it for consistency once we've paid
        # the fetch cost).
        published_at = parse_kz_local_datetime_dmy(
            raw.get("published_text")
        )
        deadline_at = parse_kz_local_datetime_dmy(
            raw.get("deadline_text")
        )

        source_url = self.BASE_URL + raw.get("detail_url", "")

        raw_json: dict[str, Any] = dict(raw)
        # Synthetic single-element _lots so the keyword matcher walks
        # the description text alongside the title (other HTML scrapers
        # do the same; see CLAUDE.md → Keyword matching).
        raw_json["_lots"] = [
            {
                "name_ru": title,
                "description_ru": (
                    raw.get("description_full")
                    or raw.get("title_description")
                ),
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
            buyer_external_id=None,
            country=Country.KZ,
            sector=None,
            value_amount=value_amount,
            value_currency=value_currency,
            published_at=published_at,
            deadline_at=deadline_at,
            status=TenderStatus.open,  # We only fetch ?show=actual.
            source_url=source_url,
            language=Language.ru,
            raw_json=raw_json,
        )


__all__ = [
    "EtsTenderConnector",
    "_parse_detail",
    "_parse_listing_row",
]
