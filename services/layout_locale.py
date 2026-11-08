"""Layout / overlay copy locale (setting-owned; not container LANG).

Layout JSON stays locale-agnostic (tokens + formats). Rendering uses
``PLACEHOLDER_LAYOUT_LOCALE`` so shared designs respect each host's language.
Launch ships English only; more locales and a Settings UI come later.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

DEFAULT_LAYOUT_LOCALE = "en"
SUPPORTED_LAYOUT_LOCALES: tuple[str, ...] = (DEFAULT_LAYOUT_LOCALE,)

_EN_MONTHS_SHORT = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_EN_MONTHS_LONG = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_EN_STRINGS: dict[str, str] = {
    "placeholder": "PLACEHOLDER",
    "coming_soon": "Coming Soon",
    "new_ep_prefix": "New Ep.",
    "airing": "Airing",
    "release": "Release",
    "theatrical": "Theatrical",
    "digital": "Digital",
    "physical": "Physical",
}

_RELEASE_TYPE_STRING_KEYS = {
    "inCinemas": "theatrical",
    "digitalRelease": "digital",
    "physicalRelease": "physical",
}

_LOCALE_TABLES: dict[str, dict[str, Any]] = {
    "en": {
        "label": "English",
        "months_short": _EN_MONTHS_SHORT,
        "months_long": _EN_MONTHS_LONG,
        "strings": _EN_STRINGS,
    },
}


def normalize_layout_locale(raw: Any) -> str:
    key = str(raw or "").strip().lower().replace("_", "-") or DEFAULT_LAYOUT_LOCALE
    # Accept BCP47-ish ``en-US`` → ``en`` when only the language is shipped.
    if key in _LOCALE_TABLES:
        return key
    primary = key.split("-", 1)[0]
    if primary in _LOCALE_TABLES:
        return primary
    return DEFAULT_LAYOUT_LOCALE


def get_layout_locale() -> str:
    """Active layout locale from settings (default English)."""
    try:
        from core.config import settings

        return normalize_layout_locale(getattr(settings, "PLACEHOLDER_LAYOUT_LOCALE", DEFAULT_LAYOUT_LOCALE))
    except Exception:
        return DEFAULT_LAYOUT_LOCALE


def layout_locale_catalog() -> list[dict[str, str]]:
    """UI/API catalog of supported layout locales."""
    out: list[dict[str, str]] = []
    for code in SUPPORTED_LAYOUT_LOCALES:
        table = _LOCALE_TABLES.get(code) or {}
        out.append({"id": code, "label": str(table.get("label") or code)})
    return out


def _table(locale: str | None = None) -> dict[str, Any]:
    code = normalize_layout_locale(locale or get_layout_locale())
    return _LOCALE_TABLES.get(code) or _LOCALE_TABLES[DEFAULT_LAYOUT_LOCALE]


def layout_string(key: str, *, locale: str | None = None) -> str:
    """Localized overlay/layout string; falls back to English then the key."""
    table = _table(locale)
    strings = table.get("strings") if isinstance(table.get("strings"), dict) else {}
    text = strings.get(key)
    if text:
        return str(text)
    en = _EN_STRINGS.get(key)
    return str(en) if en else str(key)


def release_type_label(release_type: str | None, *, locale: str | None = None) -> str:
    key = str(release_type or "").strip()
    string_key = _RELEASE_TYPE_STRING_KEYS.get(key, "release")
    return layout_string(string_key, locale=locale)


def format_layout_month_day(value: date, *, locale: str | None = None, long_month: bool = False) -> str:
    """Month + day without year, e.g. ``Sep 30`` / ``September 30``."""
    table = _table(locale)
    months = table.get("months_long" if long_month else "months_short") or _EN_MONTHS_SHORT
    try:
        name = months[value.month - 1]
    except Exception:
        name = _EN_MONTHS_SHORT[value.month - 1]
    return f"{name} {value.day}"


def format_layout_month_day_year(
    value: date, *, locale: str | None = None, long_month: bool = False
) -> str:
    """Month + day + year, e.g. ``Sep 30, 2026``."""
    return f"{format_layout_month_day(value, locale=locale, long_month=long_month)}, {value.year}"


def coerce_layout_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None
