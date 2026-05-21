"""Playwright-driven browser session for zakup.sk.kz.

This exists because the gateway requires a per-URL ``tor`` HMAC header
minted by the site's own JS interceptor. We can't replicate the
algorithm in Python (the bundle is heavily obfuscated). Instead, we
let a real headless Chromium load the SPA, click through the UI to
make the SPA fire the XHRs we need, and capture the responses.

Cost: ~150 MB of Chromium and ~3 seconds of navigation overhead per
advert. Not great. Acceptable for a 30-minute scheduler cadence with
~10 fresh adverts per cycle.

If/when we either (a) reverse-engineer the JS minter, or (b) get an
official API key, this whole module goes away and the connector
returns to plain ``requests``/``httpx``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from types import TracebackType
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import Playwright as AsyncPlaywright
from playwright.async_api import Response as PlaywrightResponse

from tender_monitor.core.logging import get_logger

logger = get_logger(__name__)

# Strict patterns. ``_is_advert`` used to be a startswith() check on
# `/4dv3rts/`, which accidentally matched the listing endpoint
# `/4dv3rts/filter` and stole the listing's POST body into the advert
# capture slot.
_LISTING_PATH_RE = re.compile(r"^/eprocsearch/api/external/4dv3rts/filter$")
_DETAIL_PATH_RE = re.compile(r"^/eprocsearch/api/external/4dv3rts/\d+$")
_LOTS_PATH_RE = re.compile(r"^/eprocsearch/api/external/4dv3rts/lots/\d+$")
SPA_ENTRY = "https://zakup.sk.kz/#/ext"

# Time the SPA needs after `goto` before its XHRs settle. Empirically
# ~3s on a warm browser; we wait for `networkidle` too as a belt-and-
# braces signal.
SETTLE_SECONDS = 3.0
# How long to wait for a clicked card to trigger its detail XHR.
CARD_CLICK_TIMEOUT = 15.0


def _path_of(url: str) -> str:
    # zakup.sk.kz URLs all share the same host; the path identifies which
    # endpoint we're looking at.
    return url.split("zakup.sk.kz")[-1].split("?")[0]


def _is_listing(url: str) -> bool:
    return bool(_LISTING_PATH_RE.match(_path_of(url)))


def _is_lots(url: str) -> bool:
    return bool(_LOTS_PATH_RE.match(_path_of(url)))


def _is_advert(url: str) -> bool:
    return bool(_DETAIL_PATH_RE.match(_path_of(url)))


class SamrukKazynaBrowser:
    """Async context manager wrapping a Playwright browser + page.

    Single-use: open one, fetch listing + N adverts, close. Concurrent
    use of the same instance is not supported (the SPA is stateful).
    """

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._pw: AsyncPlaywright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> SamrukKazynaBrowser:
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=self._headless)
            self._context = await self._browser.new_context(
                viewport={"width": 1366, "height": 900}
            )
            self._page = await self._context.new_page()
        except BaseException:
            await self._pw.stop()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError(
                "SamrukKazynaBrowser used outside its `async with` block"
            )
        return self._page

    async def _navigate_and_capture(
        self,
        captures: dict[str, Any],
        is_target: Callable[[str], bool],
        target_name: str,
    ) -> None:
        """Navigate to SPA_ENTRY and capture the next matching response.

        Caller registers a response listener that fills ``captures``.
        We just drive the page and wait for `target_name` to land.
        """
        page = self._require_page()
        captures.pop(target_name, None)
        await page.goto(SPA_ENTRY, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(SETTLE_SECONDS)
        if target_name not in captures:
            # Some pages need a tick more to settle.
            await asyncio.sleep(SETTLE_SECONDS)

    async def fetch_listing(self) -> list[dict[str, Any]]:
        """Navigate to /#/ext and return the listing JSON body.

        Raises RuntimeError if the listing XHR did not fire within the
        page-load timeout — that's the gateway's way of telling us
        something is wrong (rare in practice; ``networkidle`` waits
        until the SPA has finished its initial fetches).
        """
        page = self._require_page()
        captures: dict[str, Any] = {}

        async def on_response(response: PlaywrightResponse) -> None:
            if _is_listing(response.url) and "listing" not in captures:
                try:
                    captures["listing"] = await response.json()
                except Exception as exc:
                    logger.warning(
                        "samruk_kazyna.browser.listing_parse_failed",
                        error=str(exc),
                    )

        page.on("response", on_response)
        try:
            await page.goto(SPA_ENTRY, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(SETTLE_SECONDS)
            if "listing" not in captures:
                await asyncio.sleep(SETTLE_SECONDS)
        finally:
            page.remove_listener("response", on_response)

        listing = captures.get("listing")
        if not isinstance(listing, list):
            raise RuntimeError(
                "samruk_kazyna browser: listing XHR not captured "
                "or returned non-list payload"
            )
        return listing

    async def fetch_advert(
        self, advert_id: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Click the card for ``advert_id`` and capture detail + lots.

        Returns (detail, lots) on success, or None if the card wasn't
        found or the XHRs didn't fire in time. Per-item failure is the
        caller's problem to log/skip.
        """
        page = self._require_page()
        captures: dict[str, Any] = {}

        async def on_response(response: PlaywrightResponse) -> None:
            url = response.url
            if _is_lots(url) and "lots" not in captures:
                try:
                    captures["lots"] = await response.json()
                except Exception:
                    captures["lots"] = None
            elif _is_advert(url) and "advert" not in captures:
                try:
                    captures["advert"] = await response.json()
                except Exception:
                    captures["advert"] = None

        page.on("response", on_response)
        try:
            # Reload the listing so the card for `advert_id` is in the DOM.
            await page.goto(SPA_ENTRY, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(SETTLE_SECONDS)

            clicked = await page.evaluate(
                """
                (advertId) => {
                  const needle = '№ ' + advertId;
                  for (const el of document.querySelectorAll('div.m-found-item, [class*="m-found-item"]')) {
                    const t = (el.textContent || '').trim();
                    if (t.startsWith(needle)) {
                      el.click();
                      return true;
                    }
                  }
                  return false;
                }
                """,
                advert_id,
            )
            if not clicked:
                logger.warning(
                    "samruk_kazyna.browser.card_not_found",
                    advert_id=advert_id,
                )
                return None

            # Poll for both responses to land.
            deadline = asyncio.get_event_loop().time() + CARD_CLICK_TIMEOUT
            while asyncio.get_event_loop().time() < deadline:
                if "advert" in captures and "lots" in captures:
                    break
                await asyncio.sleep(0.25)

            advert = captures.get("advert")
            lots = captures.get("lots")
            if not isinstance(advert, dict) or not isinstance(lots, list):
                logger.warning(
                    "samruk_kazyna.browser.detail_incomplete",
                    advert_id=advert_id,
                    have_advert=isinstance(advert, dict),
                    have_lots=isinstance(lots, list),
                )
                return None
            return advert, lots
        finally:
            page.remove_listener("response", on_response)


__all__ = ["SamrukKazynaBrowser"]
