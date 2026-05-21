"""HTML-scraping helpers shared by Yii2-style KZ portals.

MITWORK, National Bank, Goszakup, and ETS-Tender all render listings
server-side and we keep the small "parse a Cyrillic amount string" and
"parse a KZ-local datetime string" helpers in one place so a fix to a
real-world format quirk lands once.

Currency formats we have seen in the wild:

* ``"46 347,00 KZT"`` (MITWORK, National Bank -- NBSP thousands, comma
  decimal, "KZT" suffix).
* ``"2 864 224,19 KZT"`` (same, with narrow NBSP).
* ``"17 241.37"`` (Goszakup listing -- regular space thousands, PERIOD
  decimal, no currency suffix).
* ``"1 550 000,00 тенге"`` (ETS-Tender detail
  -- NBSP/space thousands, comma decimal, Russian "tenge" suffix).
* ``"155 000,00 тенге (...)"`` (ETS-Tender
  with a parenthetical VAT note that must be ignored).

The refactored ``parse_kzt_amount`` handles all of the above by
first stripping every whitespace flavour from the string, then taking
the longest contiguous digit/separator run, and finally deciding the
decimal separator from the characters present.

Datetime formats:

* ``"2026-05-12 15:10:00"`` (MITWORK, National Bank -- ISO-ish with a
  space separator, full HH:MM:SS).
* ``"18.05.2026 11:12"`` (ETS-Tender -- European DD.MM.YYYY HH:MM, no
  seconds).

Two parsers, one per format, because the ambiguity between
``05/12/2026`` and ``12/05/2026`` styles makes "guess the order"
unsafe.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

KZ_TZ = ZoneInfo("Asia/Almaty")
# Asia/Tashkent is UTC+5 year-round (no DST), same offset as KZ but
# we keep it as its own constant so the intent is readable at call
# sites and a future tzdata change (DST reintroduction, locale split)
# only needs adjusting in one place.
TASHKENT_TZ = ZoneInfo("Asia/Tashkent")


# Whitespace flavours that appear inside amount strings: regular space
# (U+0020), no-break space (U+00A0), narrow no-break space (U+202F).
# Use escape codes so editor / Write-tool normalization can't silently
# collapse them all to regular spaces -- the parser quietly degrades
# without them.
_AMOUNT_WHITESPACE = (" ", " ", " ")

# Contiguous digit-or-separator run AFTER whitespace stripping. At
# this point the only separators left are comma and period; the
# currency suffix ("KZT", Cyrillic "tenge", "USD") and any
# parenthetical VAT note sit outside this match.
_AMOUNT_RUN_RE = re.compile(r"[0-9][0-9,\.]*[0-9]|[0-9]")


def parse_kzt_amount(text: str | None) -> Decimal | None:
    """Parse a KZT (or generally numeric) amount string to ``Decimal``.

    Handles the four observed formats listed at the top of the module
    plus an English-locale "1,234,567.89" fallback. Returns ``None``
    on empty, missing, or non-numeric text (e.g. MITWORK's
    "ne ukazana" placeholder).

    Algorithm:

    1. Strip every whitespace flavour we know about from the input.
    2. Extract the longest contiguous run of digits + commas + periods.
    3. Decide which character is the decimal separator:
        * both ``,`` and ``.`` present -> ``,`` is thousands,
          ``.`` is decimal (English-locale style)
        * only ``,`` -> ``,`` is decimal (Russian-locale style)
        * only ``.`` -> ``.`` is decimal (Goszakup style)
    4. ``Decimal(...)`` it; return ``None`` if that fails.
    """
    if not text:
        return None
    stripped = text
    for sep in _AMOUNT_WHITESPACE:
        stripped = stripped.replace(sep, "")
    if not stripped:
        return None
    candidate_run = _AMOUNT_RUN_RE.search(stripped)
    if candidate_run is None:
        return None
    cleaned = candidate_run.group(0)
    if not cleaned:
        return None
    has_comma = "," in cleaned
    has_period = "." in cleaned
    if has_comma and has_period:
        cleaned = cleaned.replace(",", "")
    elif has_comma:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def parse_kz_local_datetime(text: str | None) -> datetime | None:
    """Parse "YYYY-MM-DD HH:MM:SS" as Asia/Almaty local -> aware UTC.

    Returns None on empty/missing/malformed input.
    """
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        naive = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    localized = naive.replace(tzinfo=KZ_TZ)
    return localized.astimezone(UTC)


def parse_kz_local_datetime_dmy(text: str | None) -> datetime | None:
    """Parse "DD.MM.YYYY HH:MM" as Asia/Almaty local -> aware UTC.

    ETS-Tender uses this European format on both listing and detail
    pages. The Cyrillic sentinel "Skryto" ("Hidden") is treated as
    missing -- it appears in the date columns of closed/private
    procedures and is not a parseable timestamp.

    Returns None on empty/missing/malformed input.
    """
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned or cleaned == "Скрыто":
        return None
    try:
        naive = datetime.strptime(cleaned, "%d.%m.%Y %H:%M")
    except ValueError:
        return None
    localized = naive.replace(tzinfo=KZ_TZ)
    return localized.astimezone(UTC)


def parse_full_month_date(text: str | None) -> datetime | None:
    """Parse "DD Month YYYY" / "DD Mon YYYY" at UTC midnight.

    UzbekistanTenders.com renders deadlines either as full English
    month names (``"30 May 2026"``) or abbreviated (``"02 Jun 2026"``)
    depending on month length. We try the full-name format first,
    then fall back to the 3-letter abbreviation; that order matters
    because ``%B`` rejects ``"Jun"`` and ``%b`` accepts ``"May"``
    (3 letters = same form in both modes), so a both-formats parser
    written ``%B`` first preserves the more informative spelling
    when both match.

    Returns None on empty / missing / unparseable input.
    """
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=UTC)
    return None


def parse_dmy_month_name(text: str | None) -> datetime | None:
    """Parse "DD-MMM-YYYY" (English-abbreviated month) at UTC midnight.

    TendersInfo emits dates in this format and gives no time-of-day,
    so we anchor everything to 00:00 UTC. That choice is consistent
    with "this tender was published on day X" semantics rather than
    pretending to a precision the source doesn't provide. Examples:
    ``"16-May-2026"``, ``"01-Jun-2026"``.

    Returns None on empty / missing / unparseable input.
    """
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        # %b parses the English abbreviation independent of the
        # process locale -- strptime's tables are baked, not locale-
        # driven, which is what we want for an English-only field.
        parsed = datetime.strptime(cleaned, "%d-%b-%Y")
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


__all__ = [
    "KZ_TZ",
    "TASHKENT_TZ",
    "parse_dmy_month_name",
    "parse_full_month_date",
    "parse_kz_local_datetime",
    "parse_kz_local_datetime_dmy",
    "parse_kzt_amount",
]
