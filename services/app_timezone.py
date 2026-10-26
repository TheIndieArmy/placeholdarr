"""Application local timezone from container env ``TZ`` (compose convention).

No in-app timezone override. Civil-date helpers use this zone; timestamps stay UTC.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _zone_from_name(name: str) -> Optional[ZoneInfo]:
    text = str(name or "").strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except ZoneInfoNotFoundError:
        return None
    except Exception:
        return None


@lru_cache(maxsize=1)
def get_app_zone() -> ZoneInfo:
    """Resolve IANA zone from ``TZ`` env; fall back to UTC."""
    env_tz = os.environ.get("TZ", "").strip()
    zone = _zone_from_name(env_tz)
    if zone is not None:
        return zone
    # Some images set /etc/localtime without TZ; ZoneInfo("localtime") is not portable.
    return ZoneInfo("UTC")


def clear_app_zone_cache() -> None:
    """Test helper: drop cached zone after monkeypatching ``TZ``."""
    get_app_zone.cache_clear()


def app_timezone_name() -> str:
    """Display name for Settings (key from ZoneInfo)."""
    zone = get_app_zone()
    key = getattr(zone, "key", None)
    if key:
        return str(key)
    return "UTC"


def local_now() -> datetime:
    return datetime.now(get_app_zone())


def local_today() -> date:
    return local_now().date()


def to_app_zone(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_app_zone())


def parse_hhmm(value: object) -> tuple[int, int]:
    """Parse ``HH:MM`` (24h). Raises ValueError on bad input."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("time is required")
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("time must be HH:MM")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("time out of range")
    return hour, minute


def format_hhmm(hour: int, minute: int) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def clamp_month_day(year: int, month: int, day: int) -> int:
    """Clamp day-of-month into a valid day for ``year``/``month`` (1..last)."""
    import calendar

    last = calendar.monthrange(int(year), int(month))[1]
    return max(1, min(int(day), last))
