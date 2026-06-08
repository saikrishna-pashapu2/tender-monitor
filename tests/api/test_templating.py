from __future__ import annotations

from tender_monitor.api.templating import amount_in_usd, pretty_amount_with_usd
from tender_monitor.core.config import settings


def test_amount_in_usd_converts_local_currency(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "usd_fx_rates",
        {"USD": 1.0, "KZT": 500.0, "UZS": 10000.0},
    )

    assert amount_in_usd(250000, "KZT") == 500.0
    assert amount_in_usd(2500000, "UZS") == 250.0


def test_pretty_amount_with_usd_includes_both_amounts(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "usd_fx_rates",
        {"USD": 1.0, "KZT": 500.0},
    )

    assert (
        pretty_amount_with_usd(250000, "KZT")
        == "250,000 KZT (500 USD)"
    )


def test_pretty_amount_with_usd_falls_back_without_rate(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "usd_fx_rates",
        {"USD": 1.0},
    )

    assert pretty_amount_with_usd(250000, "UZS") == "250,000 UZS"
    assert pretty_amount_with_usd(250000, "USD") == "250,000 USD"
