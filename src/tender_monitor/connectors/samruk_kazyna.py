"""Connector for zakup.sk.kz — Samruk-Kazyna's procurement portal.

Live API access goes through a headless Chromium (Playwright). The
gateway requires a per-URL ``tor`` HMAC header that's minted by the
site's own JS interceptor; rather than reverse-engineer the (heavily
obfuscated) signing code, we let a real browser load the SPA, click
through the UI to make the SPA fire the XHRs we need, and capture the
responses. See ``_browser_samruk.SamrukKazynaBrowser`` for the
mechanics; this module just orchestrates listing → per-advert detail
+ lots → ``TenderUpsert``.

Shape notes:
  - Listing returns up to 10 items per page (the gateway hard-rejects
    larger sizes). We only fetch page 0 in v1; the SPA only fires
    page-1+ on user interaction we'd have to script separately.
  - One advert = one tender. ``raw_json["_lots"]`` carries the lot
    rows so the keyword matcher's generic lot walker picks them up.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar, Protocol

from tender_monitor.connectors.base import Connector
from tender_monitor.connectors.errors import FetchError, ParseError
from tender_monitor.connectors.registry import register
from tender_monitor.core.enums import Country, Language, TenderStatus
from tender_monitor.core.logging import get_logger
from tender_monitor.core.schemas import TenderUpsert

logger = get_logger(__name__)


class _BrowserProtocol(Protocol):
    async def fetch_listing(self) -> list[dict[str, Any]]: ...

    async def fetch_advert(
        self, advert_id: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None: ...


BrowserFactory = "Callable[[], AbstractAsyncContextManager[_BrowserProtocol]]"


STATUS_MAPPING: dict[str, TenderStatus] = {
    "PUBLISHED": TenderStatus.open,
}


def _parse_iso(value: str) -> datetime:
    """Parse both "...Z" (listing) and "...+05:00" (detail) ISO strings."""
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _default_browser_factory() -> AbstractAsyncContextManager[_BrowserProtocol]:
    # Lazy-imported so we don't drag Playwright into projects that
    # never enable this source.
    from tender_monitor.connectors._browser_samruk import SamrukKazynaBrowser

    return SamrukKazynaBrowser()


@register
class SamrukKazynaConnector(Connector):
    source_name: ClassVar[str] = "samruk_kazyna"

    def __init__(
        self,
        http_client_factory: Any = None,  # accepted for Connector compat
        *,
        browser_factory: Any = None,
    ) -> None:
        super().__init__()
        # The factory returns an async context manager that yields a
        # browser session implementing _BrowserProtocol.
        self._browser_factory = browser_factory or _default_browser_factory

    async def _fetch_raw(self, since: datetime | None) -> list[dict[str, Any]]:
        accumulated: list[dict[str, Any]] = []
        try:
            browser_cm = self._browser_factory()
        except Exception as exc:
            raise FetchError(
                f"samruk_kazyna: failed to construct browser factory: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        async with browser_cm as browser:
            try:
                listing = await browser.fetch_listing()
            except Exception as exc:
                raise FetchError(
                    f"samruk_kazyna listing failed: {type(exc).__name__}: {exc}"
                ) from exc

            for item in listing:
                if since is not None:
                    begin = item.get("acceptanceBeginDateTime")
                    if begin is not None and _parse_iso(begin) < since:
                        continue
                accumulated.append(item)

            logger.info(
                "samruk_kazyna.listing_complete",
                items_in_window=len(accumulated),
                listing_total=len(listing),
            )

            seen: set[int] = set()
            unique_ids: list[int] = []
            for item in accumulated:
                advert_id = item.get("id")
                if not isinstance(advert_id, int):
                    logger.warning(
                        "samruk_kazyna.listing_item_missing_id", item=item
                    )
                    continue
                if advert_id in seen:
                    continue
                seen.add(advert_id)
                unique_ids.append(advert_id)

            adverts: list[dict[str, Any]] = []
            for advert_id in unique_ids:
                try:
                    pair = await browser.fetch_advert(advert_id)
                except Exception as exc:
                    logger.warning(
                        "samruk_kazyna.advert_fetch_exception",
                        advert_id=advert_id,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    continue
                if pair is None:
                    continue
                detail, lots = pair
                detail["_lots"] = lots
                adverts.append(detail)

        return adverts

    def _normalize(self, raw: dict[str, Any]) -> TenderUpsert:
        advert_id = raw.get("id")
        if advert_id is None:
            raise ParseError("advert is missing 'id'")

        title = raw.get("nameRu")
        if not title:
            raise ParseError(f"advert {advert_id} has empty nameRu")

        customer = raw.get("customer") or {}
        buyer_name = customer.get("nameRu")
        buyer_external_id = customer.get("bin")

        sum_value = raw.get("sumTruNoNds")
        value_amount: Decimal | None
        value_currency: str | None
        if sum_value is not None:
            value_amount = Decimal(str(sum_value))
            value_currency = "KZT"
        else:
            value_amount = None
            value_currency = None

        published_raw = raw.get("acceptanceBeginDateTime")
        published_at = _parse_iso(published_raw) if published_raw else None
        deadline_raw = raw.get("acceptanceEndDateTime")
        deadline_at = _parse_iso(deadline_raw) if deadline_raw else None

        advert_status = raw.get("advertStatus")
        status = (
            STATUS_MAPPING.get(advert_status, TenderStatus.unknown)
            if isinstance(advert_status, str)
            else TenderStatus.unknown
        )

        return TenderUpsert(
            source_name=self.source_name,
            external_id=str(advert_id),
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
            source_url=f"https://zakup.sk.kz/#/ext?tabs=advert&id={advert_id}",
            language=Language.ru,
            raw_json=raw,
        )


__all__ = ["SamrukKazynaConnector"]
