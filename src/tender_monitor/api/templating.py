"""Jinja2 template environment + project-specific filters.

Centralised here so both the web routes and tests can import the same
configured ``templates`` object. Filters cover the small set of UI
concerns that don't belong in the route handlers (relative time,
deterministic colour assignment per source, country flags, deadline
urgency styling).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from tender_monitor.core.config import settings

TEMPLATES_DIR = Path(__file__).parent / "templates"

# 8 evenly-distributed Tailwind hues that read as distinct badges in
# the source pill. Order is stable so a source always keeps its colour
# across deploys (the hash is what selects, not the position).
SOURCE_COLORS: tuple[str, ...] = (
    "blue",
    "green",
    "purple",
    "amber",
    "rose",
    "cyan",
    "indigo",
    "fuchsia",
)

GROUP_COLORS: dict[str, str] = {
    "esg": "emerald",
    "credit_rating": "blue",
}

COUNTRY_FLAGS: dict[str, str] = {
    "KZ": "\U0001f1f0\U0001f1ff",  # 🇰🇿
    "UZ": "\U0001f1fa\U0001f1ff",  # 🇺🇿
}


def _coerce_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def timeago(value: datetime | None, *, now: datetime | None = None) -> str:
    """Human-readable relative time. Always renders in past tense."""
    if value is None:
        return "—"
    value = _coerce_aware(value)
    now = _coerce_aware(now or datetime.now(UTC))
    delta_seconds = (now - value).total_seconds()
    if delta_seconds < 0:
        # Future timestamp — fall back to absolute date.
        return value.strftime("%Y-%m-%d")

    minute, hour, day = 60, 3600, 86400
    if delta_seconds < minute:
        return "just now"
    if delta_seconds < hour:
        minutes = int(delta_seconds // minute)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if delta_seconds < day:
        hours = int(delta_seconds // hour)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(delta_seconds // day)
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


def deadline_state(value: datetime | None, *, now: datetime | None = None) -> dict[str, str]:
    """Return label + Tailwind colour key for a deadline.

    Keys: ``label`` (the human text), ``color`` (one of:
    ``gray|yellow|orange|red|past``).
    """
    if value is None:
        return {"label": "no deadline", "color": "gray"}
    value = _coerce_aware(value)
    now = _coerce_aware(now or datetime.now(UTC))
    delta = value - now
    days = delta.total_seconds() / 86400
    if days < 0:
        return {"label": "Past deadline", "color": "past"}
    if days < 1:
        hours = max(0, int(delta.total_seconds() // 3600))
        return {"label": "Today" if hours < 12 else f"{hours}h left", "color": "red"}
    if days < 3:
        return {"label": f"{int(days)} day{'s' if int(days) != 1 else ''}", "color": "orange"}
    if days < 7:
        return {"label": f"{int(days)} days", "color": "yellow"}
    return {"label": f"{int(days)} days", "color": "gray"}


def source_color(name: str) -> str:
    digest = hashlib.md5(name.encode("utf-8"), usedforsecurity=False).digest()
    return SOURCE_COLORS[digest[0] % len(SOURCE_COLORS)]


def group_color(name: str) -> str:
    return GROUP_COLORS.get(name, "gray")


def country_flag(value: Any) -> str:
    key = getattr(value, "value", value)
    return COUNTRY_FLAGS.get(str(key), "\U0001f310")  # 🌐


def isoformat(value: datetime | None) -> str:
    if value is None:
        return ""
    return _coerce_aware(value).isoformat()


def pretty_amount(value: Any, currency: str | None = None) -> str:
    if value is None:
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    formatted = f"{amount:,.2f}".rstrip("0").rstrip(".")
    return f"{formatted} {currency}" if currency else formatted


def amount_in_usd(value: Any, currency: str | None = None) -> float | None:
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount < 0:
        return None
    normalized_currency = (currency or "").strip().upper()
    if not normalized_currency:
        return None
    rate = settings.usd_fx_rates.get(normalized_currency)
    if rate is None:
        return None
    try:
        rate_value = float(rate)
    except (TypeError, ValueError):
        return None
    if rate_value <= 0:
        return None
    if normalized_currency == "USD":
        return amount
    return amount / rate_value


def pretty_amount_with_usd(value: Any, currency: str | None = None) -> str:
    local_value = pretty_amount(value, currency)
    usd_value = amount_in_usd(value, currency)
    if usd_value is None:
        return local_value
    usd_formatted = pretty_amount(usd_value, "USD")
    if (currency or "").strip().upper() == "USD":
        return usd_formatted
    return f"{local_value} ({usd_formatted})"


def pretty_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


_LANG_SUFFIXES: dict[str, str] = {
    "_ru": "RU",
    "_kk": "KK",
    "_kz": "KK",
    "_uz": "UZ",
    "_oz": "UZ",
    "_en": "EN",
}


def humanize_key(key: str) -> str:
    """Turn a source-payload key into a readable label.

    ``announcement_number`` -> ``Announcement number``
    ``announcement_name_ru`` -> ``Announcement name (RU)``
    ``buyer_bin`` -> ``Buyer BIN``
    """
    label = key
    lang_tag = ""
    for suffix, tag in _LANG_SUFFIXES.items():
        if label.endswith(suffix) and len(label) > len(suffix):
            lang_tag = tag
            label = label[: -len(suffix)]
            break
    words = [w for w in label.replace("-", "_").split("_") if w]
    if not words:
        words = [label]
    upper_acronyms = {
        "id", "url", "bin", "tin", "iin", "kpp", "ogrn",
        "esg", "gri", "tcfd", "sasb", "issb", "msci", "cdp",
        "ip", "ftp", "smtp", "html", "json", "xml", "csv", "pdf",
        "kz", "uz", "ru",
    }
    rendered = []
    for i, w in enumerate(words):
        if w.lower() in upper_acronyms:
            rendered.append(w.upper())
        elif i == 0:
            rendered.append(w[:1].upper() + w[1:])
        else:
            rendered.append(w.lower())
    label_text = " ".join(rendered)
    return f"{label_text} ({lang_tag})" if lang_tag else label_text


_ISO_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$"
)


def pretty_scalar(value: Any) -> str:
    """Render a scalar (str / int / float / bool / None) as readable text.

    The Jinja template uses ``is_scalar`` to decide whether to call this
    or fall back to a nested renderer.
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int | float):
        if isinstance(value, float):
            return f"{value:,.4f}".rstrip("0").rstrip(".")
        return f"{value:,}"
    if isinstance(value, datetime):
        return _coerce_aware(value).strftime("%Y-%m-%d %H:%M")
    text = str(value)
    if _ISO_DATE_RE.match(text):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.strftime("%Y-%m-%d %H:%M") if "T" in text or " " in text else parsed.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return text


def is_scalar(value: Any) -> bool:
    """True when ``pretty_scalar`` produces a meaningful one-line rendering."""
    return value is None or isinstance(value, str | int | float | bool | datetime)


def is_list_of_scalars(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    return all(is_scalar(item) for item in value)


def pretty_list_of_scalars(value: list[Any]) -> str:
    return ", ".join(pretty_scalar(item) for item in value)


def like_member_keys(value: Any) -> list[str]:
    keys: list[str] = []
    for like in value or []:
        member = getattr(like, "team_member", None)
        key = getattr(member, "member_key", None)
        if isinstance(key, str):
            keys.append(key)
    return keys


def like_names(value: Any) -> list[str]:
    names: list[str] = []
    for like in value or []:
        member = getattr(like, "team_member", None)
        name = getattr(member, "display_name", None)
        if isinstance(name, str):
            names.append(name)
    return names


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.filters["timeago"] = timeago
    env.filters["deadline_state"] = deadline_state
    env.filters["source_color"] = source_color
    env.filters["group_color"] = group_color
    env.filters["country_flag"] = country_flag
    env.filters["isoformat"] = isoformat
    env.filters["pretty_amount"] = pretty_amount
    env.filters["amount_in_usd"] = amount_in_usd
    env.filters["pretty_amount_with_usd"] = pretty_amount_with_usd
    env.filters["pretty_json"] = pretty_json
    env.filters["humanize_key"] = humanize_key
    env.filters["pretty_scalar"] = pretty_scalar
    env.filters["pretty_list_of_scalars"] = pretty_list_of_scalars
    env.filters["like_member_keys"] = like_member_keys
    env.filters["like_names"] = like_names
    env.tests["scalar"] = is_scalar
    env.tests["list_of_scalars"] = is_list_of_scalars
    return templates


templates = build_templates()


__all__ = [
    "COUNTRY_FLAGS",
    "GROUP_COLORS",
    "SOURCE_COLORS",
    "TEMPLATES_DIR",
    "amount_in_usd",
    "build_templates",
    "country_flag",
    "deadline_state",
    "group_color",
    "humanize_key",
    "is_list_of_scalars",
    "is_scalar",
    "isoformat",
    "like_member_keys",
    "like_names",
    "pretty_amount",
    "pretty_amount_with_usd",
    "pretty_json",
    "pretty_list_of_scalars",
    "pretty_scalar",
    "source_color",
    "templates",
    "timeago",
]
