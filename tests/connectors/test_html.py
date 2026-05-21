"""Tests for the shared HTML-scraping helpers in
``tender_monitor.connectors._html``. Currency parsing in particular
sees four real formats in the wild and we pin them here so future
edits don't accidentally regress one when fixing another.

We construct strings with explicit ``\\u00a0`` (NO-BREAK SPACE) and
``\\u202f`` (NARROW NO-BREAK SPACE) escapes rather than inlining the
characters so the source stays readable and ruff doesn't have to be
silenced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tender_monitor.connectors._html import (
    parse_kz_local_datetime_dmy,
    parse_kzt_amount,
)

NBSP = " "
NARROW_NBSP = " "


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # MITWORK / National Bank live format: NBSP thousands, comma
        # decimal, "KZT" suffix.
        (f"46{NBSP}347,00 KZT", Decimal("46347.00")),
        (f"1{NBSP}164{NBSP}620,82 KZT", Decimal("1164620.82")),
        # Narrow NBSP variant.
        (f"2{NARROW_NBSP}864{NARROW_NBSP}224,19 KZT", Decimal("2864224.19")),
        # Goszakup live format: plain space thousands, PERIOD decimal,
        # NO currency suffix in the value cell.
        ("17 241.37", Decimal("17241.37")),
        ("1 234 567.89", Decimal("1234567.89")),
        # Edge: zero with comma decimal + suffix.
        ("0,00 KZT", Decimal("0.00")),
        # Edge: tight (no separators).
        ("100", Decimal("100")),
        # ETS-Tender live format: NBSP thousands, comma decimal, full
        # Russian "тенге" suffix (not "KZT").
        (f"1{NBSP}550{NBSP}000,00 тенге", Decimal("1550000.00")),
        ("1 550 000,00 тенге", Decimal("1550000.00")),
        # ETS-Tender with a parenthetical VAT note that must be
        # ignored (the regex stops at the first non-numeric run).
        (
            "155 000,00 тенге (цена с НДС, НДС: 16%)",
            Decimal("155000.00"),
        ),
        # Defensive: English-locale "thousands=comma, decimal=period"
        # variant. Some legacy fixtures and ad-hoc USD strings show up
        # this way; the refactor handles them so a future source doesn't
        # need a separate parser.
        ("1,234,567.89 USD", Decimal("1234567.89")),
    ],
)
def test_parse_kzt_amount_pins_real_formats(text: str, expected: Decimal) -> None:
    assert parse_kzt_amount(text) == expected


@pytest.mark.parametrize("text", ["", "   ", None, "garbage"])
def test_parse_kzt_amount_returns_none_on_garbage(text: str | None) -> None:
    assert parse_kzt_amount(text) is None


def test_parse_kz_local_datetime_dmy_converts_to_utc() -> None:
    result = parse_kz_local_datetime_dmy("18.05.2026 11:12")
    assert result is not None
    # 11:12 KZ (UTC+5) → 06:12 UTC.
    assert result == datetime(2026, 5, 18, 6, 12, 0, tzinfo=UTC)
    assert result.tzinfo is UTC


def test_parse_kz_local_datetime_dmy_handles_hidden() -> None:
    # "Скрыто" shows up in date columns on closed/private ETS-Tender
    # procedures. It is NOT a parseable datetime and must produce None.
    assert parse_kz_local_datetime_dmy("Скрыто") is None


@pytest.mark.parametrize("text", ["", "   ", None, "not a date", "18-05-2026 11:12"])
def test_parse_kz_local_datetime_dmy_returns_none_on_garbage(
    text: str | None,
) -> None:
    assert parse_kz_local_datetime_dmy(text) is None
