"""Connector for tendersinfo.com -- a commercial procurement aggregator
that re-publishes tenders from many platforms in English.

Three things make this source structurally different from every other
connector we've written so far:

- **Multi-country.** One ``fetch_latest`` call hits the upstream API
  twice -- once with ``country_code=KZ``, once with ``country_code=UZ``
  -- and emits a mix of ``Country.KZ`` and ``Country.UZ`` tenders.
  The per-tender country comes from the response, not from any
  source-row constant. The ``sources.yaml`` country slot is a
  placeholder; the scheduler treats the tender's ``country`` as
  authoritative.

- **English-language.** Titles are pre-translated to English. We set
  ``language=Language.en`` on every emitted tender, which is what
  finally lets the English half of ``config/keywords.yaml``
  (``"ESG audit"``, ``"credit rating"``, ``rating agency`` etc.) fire
  against live data. The matcher's haystack-build at
  ``matching/keywords.py`` puts ``tender.title`` into the haystack
  unconditionally, so English keywords match on the title without
  any per-connector haystack tweak.

- **jQuery DataTables protocol.** The endpoint is a URL-encoded POST
  with the boilerplate ``draw`` / ``columns[i][*]`` / ``order[i][*]``
  fields. The response is the DataTables envelope:
  ``{"draw", "recordsTotal", "recordsFiltered", "data": [...]}``.
  Our pagination uses ``start``/``length`` and we stop early once
  ``(page+1) * PAGE_SIZE >= recordsTotal``.

Data-quality artifacts the parser handles:

- ``short_desc`` sometimes carries an aggregator-injected
  ``"\\nopen In A New Window"`` suffix; we strip it.
- ``organisation_h`` is HTML-wrapped
  ``"<br><b>Tender Authority: </b>UNDP"``; we extract the value
  after the label.
- ``est_cost_h`` is empty far more often than not, so
  ``value_amount`` / ``value_currency`` will be ``None`` on most
  rows in v1. We don't attempt to parse the cost column until the
  format stabilizes.
- The same upstream tender can appear under multiple
  ``site_tender_id`` values because the aggregator splits by
  region/sector. We accept the duplication; cross-source dedup is a
  separate concern. Exact repeated rows within the same crawl,
  however, are deduped by ``url`` (fallback ``site_tender_id``) so
  pagination loops do not keep emitting the same aggregator row.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, ClassVar

import httpx

from tender_monitor.connectors._html import parse_dmy_month_name
from tender_monitor.connectors.base import Connector
from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.http import make_client, with_retry
from tender_monitor.connectors.registry import register
from tender_monitor.core.enums import Country, Language, TenderStatus
from tender_monitor.core.logging import get_logger
from tender_monitor.core.schemas import TenderUpsert

logger = get_logger(__name__)


_OPEN_IN_NEW_WINDOW_RE = re.compile(
    r"\s*open\s+In\s+A\s+New\s+Window\s*$", flags=re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_AUTHORITY_RE = re.compile(
    r"Tender\s+Authority\s*:\s*(.+?)\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)


def clean_title(text: str | None) -> str:
    """Strip the aggregator's "open In A New Window" suffix and
    surrounding whitespace.

    Returns the empty string for ``None`` / empty / whitespace-only
    input. ``_normalize`` rejects empty-after-cleaning titles with
    ``ParseError`` -- the title is the matcher's primary signal and
    a blank row is not worth storing.
    """
    if not text:
        return ""
    return _OPEN_IN_NEW_WINDOW_RE.sub("", text).strip()


def extract_authority(html: str | None) -> str | None:
    """Pull the buyer/authority name out of the ``organisation_h``
    HTML blob.

    The canonical shape is
    ``"<br><b>Tender Authority: </b>UNDP"``. We strip the tags,
    then look for the ``Tender Authority:`` label and return the
    value after it. If the label is missing but the plain text is
    non-empty (some rows show just the authority name in HTML
    wrapping), return that. Returns ``None`` on absent / empty
    input.
    """
    if not html:
        return None
    plain = _TAG_RE.sub("", html).strip()
    if not plain:
        return None
    match = _AUTHORITY_RE.search(plain)
    if match is not None:
        value = match.group(1).strip()
        return value or None
    return plain


@register
class TendersinfoConnector(Connector):
    source_name: ClassVar[str] = "tendersinfo"

    LISTING_URL: ClassVar[str] = (
        "https://www.tendersinfo.com/esearch/tender_sector_test"
    )
    COUNTRY_CODES: ClassVar[tuple[str, ...]] = ("KZ", "UZ")
    PAGE_SIZE: ClassVar[int] = 100
    MAX_PAGES: ClassVar[int] = 20

    # The DataTables protocol is positional: every column we ask the
    # server to send back gets a ``columns[i][*]`` block. We copy the
    # capture verbatim because the server validates the shape; the
    # values don't change per call.
    DATATABLES_BOILERPLATE: ClassVar[dict[str, str]] = {
        "draw": "1",
        # column 0: site_tender_id
        "columns[0][data]": "site_tender_id",
        "columns[0][name]": "",
        "columns[0][searchable]": "true",
        "columns[0][orderable]": "false",
        "columns[0][search][value]": "",
        "columns[0][search][regex]": "false",
        # column 1: region_name
        "columns[1][data]": "region_name",
        "columns[1][name]": "",
        "columns[1][searchable]": "true",
        "columns[1][orderable]": "false",
        "columns[1][search][value]": "",
        "columns[1][search][regex]": "false",
        # column 2: country
        "columns[2][data]": "country",
        "columns[2][name]": "",
        "columns[2][searchable]": "true",
        "columns[2][orderable]": "false",
        "columns[2][search][value]": "",
        "columns[2][search][regex]": "false",
        # column 3: sector_name
        "columns[3][data]": "sector_name",
        "columns[3][name]": "",
        "columns[3][searchable]": "true",
        "columns[3][orderable]": "false",
        "columns[3][search][value]": "",
        "columns[3][search][regex]": "false",
        # column 4: short_desc
        "columns[4][data]": "short_desc",
        "columns[4][name]": "",
        "columns[4][searchable]": "true",
        "columns[4][orderable]": "false",
        "columns[4][search][value]": "",
        "columns[4][search][regex]": "false",
        # column 5: date_c (published)
        "columns[5][data]": "date_c",
        "columns[5][name]": "",
        "columns[5][searchable]": "true",
        "columns[5][orderable]": "false",
        "columns[5][search][value]": "",
        "columns[5][search][regex]": "false",
        # column 6: doc_last (deadline)
        "columns[6][data]": "doc_last",
        "columns[6][name]": "",
        "columns[6][searchable]": "true",
        "columns[6][orderable]": "false",
        "columns[6][search][value]": "",
        "columns[6][search][regex]": "false",
        # column 7: est_cost_h
        "columns[7][data]": "est_cost_h",
        "columns[7][name]": "",
        "columns[7][searchable]": "true",
        "columns[7][orderable]": "false",
        "columns[7][search][value]": "",
        "columns[7][search][regex]": "false",
        # column 8: organisation_h
        "columns[8][data]": "organisation_h",
        "columns[8][name]": "",
        "columns[8][searchable]": "true",
        "columns[8][orderable]": "false",
        "columns[8][search][value]": "",
        "columns[8][search][regex]": "false",
        # ordering boilerplate (orderable=false on every column, but
        # the request still has to carry an order block).
        "order[0][column]": "0",
        "order[0][dir]": "asc",
        # global search (empty -- we don't use full-text search).
        "search[value]": "",
        "search[regex]": "false",
        # filter slots (empty -- country_code is the only filter we
        # use).
        "sectortxt": "",
        "region_txt": "",
        "cpvtxt": "",
        # notice_type=1,3,8 covers the procurement notice types we
        # care about (tenders + RFQs + EOIs) and excludes contract
        # awards and amendments.
        "notice_type": "1, 3, 8",
    }

    REQUIRED_HEADERS: ClassVar[dict[str, str]] = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://www.tendersinfo.com",
        "Referer": "https://www.tendersinfo.com/global-uzbekistan-tenders.php",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }

    def _make_client(self) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return make_client(headers=self.REQUIRED_HEADERS)

    def _build_body(
        self, *, country_code: str, start: int, length: int
    ) -> dict[str, str]:
        return {
            **self.DATATABLES_BOILERPLATE,
            "start": str(start),
            "length": str(length),
            "country_code": country_code,
            # countrytxt is the display-name filter; leaving it empty
            # so country_code is the only thing that actually filters.
            "countrytxt": "",
        }

    @with_retry(max_attempts=3)
    async def _do_listing_request(
        self,
        client: httpx.AsyncClient,
        *,
        country_code: str,
        start: int,
    ) -> httpx.Response:
        response = await client.post(
            self.LISTING_URL,
            data=self._build_body(
                country_code=country_code,
                start=start,
                length=self.PAGE_SIZE,
            ),
        )
        response.raise_for_status()
        return response

    async def _fetch_country_page(
        self,
        client: httpx.AsyncClient,
        *,
        country_code: str,
        start: int,
    ) -> dict[str, Any]:
        try:
            response = await self._do_listing_request(
                client, country_code=country_code, start=start
            )
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            raise FetchError(
                f"tendersinfo listing country={country_code} start={start} "
                f"failed: {type(exc).__name__}: {exc}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise FetchError(
                f"tendersinfo listing country={country_code} start={start} "
                f"returned non-dict payload: {type(payload).__name__}"
            )
        return payload

    async def _fetch_country(
        self, client: httpx.AsyncClient, country_code: str
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for page_index in range(self.MAX_PAGES):
            start = page_index * self.PAGE_SIZE
            payload = await self._fetch_country_page(
                client, country_code=country_code, start=start
            )
            items = payload.get("data") or []
            if not isinstance(items, list):
                raise FetchError(
                    f"tendersinfo country={country_code} start={start} 'data' "
                    f"is not a list: {type(items).__name__}"
                )
            if not items:
                break
            fresh_items = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                dedupe_key = str(
                    item.get("url")
                    or item.get("site_tender_id")
                    or ""
                ).strip()
                if not dedupe_key or dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                collected.append(item)
                fresh_items += 1
            if items and fresh_items == 0:
                logger.info(
                    "tendersinfo.pagination_stalled",
                    country=country_code,
                    start=start,
                )
                break
            total = payload.get("recordsTotal")
            if not isinstance(total, int):
                total = payload.get("recordsFiltered")
            if isinstance(total, int) and (page_index + 1) * self.PAGE_SIZE >= total:
                break
        logger.info(
            "tendersinfo.country_complete",
            country=country_code,
            items_collected=len(collected),
        )
        return collected

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        accumulated: list[dict[str, Any]] = []
        async with self._make_client() as client:
            for country_code in self.COUNTRY_CODES:
                accumulated.extend(await self._fetch_country(client, country_code))

        if since is None:
            return accumulated

        in_window: list[dict[str, Any]] = []
        for item in accumulated:
            published = parse_dmy_month_name(item.get("date_c"))
            # Keep rows whose date_c didn't parse -- we have no
            # basis to drop them, and the aggregator occasionally
            # emits blank/odd dates on rows we still want.
            if published is None or published >= since:
                in_window.append(item)
        logger.info(
            "tendersinfo.since_filter_applied",
            input_items=len(accumulated),
            kept=len(in_window),
            since=since.isoformat(),
        )
        return in_window

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        external_id_raw = raw.get("site_tender_id")
        if external_id_raw is None or str(external_id_raw).strip() == "":
            raise ParseError("tendersinfo row is missing 'site_tender_id'")
        external_id = str(external_id_raw)

        title = clean_title(raw.get("short_desc"))
        if not title:
            raise ParseError(
                f"tendersinfo tender {external_id} has empty title "
                "after cleaning"
            )

        buyer_name = extract_authority(raw.get("organisation_h"))

        country_code = raw.get("country")
        if not country_code:
            raise ParseError(
                f"tendersinfo tender {external_id} is missing 'country'"
            )
        # Country(...) raises ValueError for non-{KZ,UZ}, which we let
        # propagate -- a third-country row in our KZ+UZ filter is a
        # real bug to surface, not silently swallow.
        country = Country(country_code)

        published_at = parse_dmy_month_name(raw.get("date_c"))
        deadline_at = parse_dmy_month_name(raw.get("doc_last"))

        raw_json: dict[str, Any] = dict(raw)
        # Synthetic _lots so the per-source raw_json self-describes
        # the English title. The matcher walks ``tender.title``
        # directly, so the title is in the haystack independent of
        # this dict shape -- the entry is here for downstream
        # consumers that prefer the lots-array convention.
        raw_json["_lots"] = [
            {
                "name_ru": None,
                "name_en": title,
                "description_ru": None,
                "description_en": None,
            }
        ]

        return TenderUpsert(
            source_name=self.source_name,
            external_id=external_id,
            title=title,
            buyer_name=buyer_name,
            buyer_external_id=None,
            country=country,
            sector=raw.get("sector_name"),
            value_amount=None,  # est_cost_h is empty on most rows; defer
            value_currency=None,
            published_at=published_at,
            deadline_at=deadline_at,
            status=TenderStatus.open,  # aggregator only lists currently-open
            source_url=raw["url"],
            language=Language.en,
            raw_json=raw_json,
        )


__all__ = [
    "TendersinfoConnector",
    "clean_title",
    "extract_authority",
]
