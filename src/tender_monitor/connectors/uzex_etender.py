"""Connector for etender.uzex.uz -- the UZEX e-Tender platform.

First non-Kazakhstan source. Shape notes that differ from the KZ
connectors:

- POST-JSON listing similar to zakup_unified, but cleaner: every
  field we need for v1 (title, buyer name + TIN, dates, amount,
  currency, region) is in the listing payload. NO detail-page fetch.
- ONE TENDER ROW = ONE LISTING ITEM. ``external_id`` is
  ``str(item["id"])``.
- Pagination uses ``From``/``To`` 1-indexed offsets in the body, not
  page numbers: page 1 -> From=1,To=50; page 2 -> From=51,To=100.
- Default order is roughly id-descending (newest-created first), but
  ``start_date`` ordering is NOT strictly monotone -- a re-announced
  tender can carry an older ``start_date`` than its position in the
  listing implies. We therefore paginate to MAX_PAGES unconditionally
  and apply the ``since`` filter post-pagination. Early termination
  on the first older-than-since hit would drop legitimately new but
  later-positioned items.
- Dates are naive ISO ``YYYY-MM-DDTHH:MM:SS`` with no timezone
  marker. Treat them as Asia/Tashkent local (UTC+5), localize, then
  convert to UTC for storage.
- Currency is UZS (Uzbek sum). The listing carries the ISO alpha
  code as ``currency_codeabc`` so we use that verbatim.
- TypeId=1 filters to active tenders per the DevTools capture, so
  every ingested row is implicitly ``TenderStatus.open``.
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

    LISTING_URL: ClassVar[str] = (
        "https://apietender.uzex.uz/api/common/TradeList"
    )
    PAGE_SIZE: ClassVar[int] = 50
    MAX_PAGES: ClassVar[int] = 5
    LISTING_BODY_TEMPLATE: ClassVar[dict[str, int]] = {
        "TypeId": 1,  # active tenders
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
        self, client: httpx.AsyncClient, *, from_offset: int, to_offset: int
    ) -> httpx.Response:
        body: dict[str, int] = {
            **self.LISTING_BODY_TEMPLATE,
            "From": from_offset,
            "To": to_offset,
        }
        response = await client.post(self.LISTING_URL, json=body)
        response.raise_for_status()
        return response

    async def _fetch_listing_page(
        self, client: httpx.AsyncClient, *, page_index: int
    ) -> list[dict[str, Any]]:
        from_offset = page_index * self.PAGE_SIZE + 1
        to_offset = (page_index + 1) * self.PAGE_SIZE
        try:
            response = await self._do_listing_request(
                client, from_offset=from_offset, to_offset=to_offset
            )
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
        ) as exc:
            raise FetchError(
                f"uzex_etender listing From={from_offset} To={to_offset} "
                f"failed: {type(exc).__name__}: {exc}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, list):
            raise FetchError(
                f"uzex_etender listing From={from_offset} returned "
                f"non-list payload: {type(payload).__name__}"
            )
        return payload

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        accumulated: list[dict[str, Any]] = []
        pages_walked = 0
        async with self._make_client() as client:
            for page_index in range(self.MAX_PAGES):
                page_items = await self._fetch_listing_page(
                    client, page_index=page_index
                )
                pages_walked = page_index + 1
                if not page_items:
                    break
                accumulated.extend(page_items)
                if len(page_items) < self.PAGE_SIZE:
                    # Short page is the last page -- the API doesn't
                    # set a "has more" flag.
                    break

        logger.info(
            "uzex_etender.listing_complete",
            pages_walked=pages_walked,
            items_collected=len(accumulated),
        )

        if since is None:
            return accumulated

        # Post-pagination filter: see module docstring for why early
        # termination is unsafe (start_date is not strictly monotone
        # with respect to listing position).
        in_window: list[dict[str, Any]] = []
        for item in accumulated:
            started = parse_naive_tashkent(item.get("start_date"))
            if started is None or started >= since:
                in_window.append(item)
        logger.info(
            "uzex_etender.since_filter_applied",
            input_items=len(accumulated),
            kept=len(in_window),
            since=since.isoformat(),
        )
        return in_window

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        item_id = raw.get("id")
        if item_id is None:
            raise ParseError("uzex_etender item is missing 'id'")

        title = raw.get("name")
        if not title:
            raise ParseError(f"uzex_etender item {item_id} has empty name")

        buyer_name = raw.get("seller_name")
        buyer_external_id = raw.get("seller_tin")

        cost = raw.get("cost")
        value_amount: Decimal | None = (
            Decimal(str(cost)) if cost is not None else None
        )
        value_currency = raw.get("currency_codeabc") if value_amount is not None else None

        published_at = parse_naive_tashkent(raw.get("start_date"))
        deadline_at = parse_naive_tashkent(raw.get("end_date"))

        source_url = f"https://etender.uzex.uz/lot/{item_id}"

        raw_json: dict[str, Any] = dict(raw)
        # Synthetic single-element _lots wrap so the keyword matcher's
        # haystack walk picks up the title in the same shape it does
        # for the KZ connectors. ``description_ru`` stays None because
        # the listing has no description field; the bilingual title
        # IS the only signal source for v1.
        raw_json["_lots"] = [
            {
                "name_ru": raw.get("name"),
                "description_ru": None,
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
            status=TenderStatus.open,  # TypeId=1 filters to active tenders.
            source_url=source_url,
            language=Language.ru,
            raw_json=raw_json,
        )


__all__ = [
    "UzexEtenderConnector",
    "parse_naive_tashkent",
]
