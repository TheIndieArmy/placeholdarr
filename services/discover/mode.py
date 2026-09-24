"""TMDB Discover catalog mode (movies + show-level TV)."""

from __future__ import annotations

from typing import Literal

from core.config import settings

CatalogMode = Literal["arr_catalog", "tmdb_discover"]

DiscoverStartupSyncMode = Literal["on", "auto", "off"]


def _setting_bool(name: str, default: bool = True) -> bool:
    raw = getattr(settings, name, default)
    if isinstance(raw, bool):
        return raw
    text = str(raw if raw is not None else default).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return bool(default)


def get_catalog_mode() -> CatalogMode:
    raw = str(getattr(settings, "CATALOG_MODE", "arr_catalog") or "arr_catalog").strip().lower()
    if raw in ("tmdb_discover", "discover", "tmdb"):
        return "tmdb_discover"
    return "arr_catalog"


def is_tmdb_discover_mode() -> bool:
    return get_catalog_mode() == "tmdb_discover"


def skip_placeholder_when_monitored() -> bool:
    return _setting_bool("DISCOVER_SKIP_PLACEHOLDER_WHEN_MONITORED", True)


def skip_monitored_any_instance() -> bool:
    return _setting_bool("DISCOVER_SKIP_MONITORED_ANY_INSTANCE", True)


def get_discover_startup_sync_mode() -> DiscoverStartupSyncMode:
    raw = str(getattr(settings, "DISCOVER_STARTUP_SYNC_MODE", "auto") or "auto").strip().lower()
    if raw in {"on", "full", "always", "true", "1", "yes"}:
        return "on"
    if raw in {"off", "false", "0", "no", "disabled"}:
        return "off"
    return "auto"


def should_run_discover_startup_sync() -> tuple[bool, str]:
    """Return (run, reason) for boot-time Discover catalog sync."""
    if not is_tmdb_discover_mode():
        return False, "not_tmdb_discover_mode"
    mode = get_discover_startup_sync_mode()
    if mode == "off":
        return False, "discover_startup_sync_off"
    if mode == "on":
        return True, "discover_startup_sync_on"
    # auto: only when catalog has no movies yet
    try:
        from services.postgres.db import get_session
        from services.postgres.models import TmdbMovie

        session = get_session()
        try:
            count = int(session.query(TmdbMovie).count() or 0)
        finally:
            session.close()
        if count <= 0:
            return True, "discover_catalog_empty"
        return False, f"discover_catalog_has_{count}_movies"
    except Exception as exc:
        # Fail open so first boots still seed if the count query fails.
        return True, f"discover_catalog_count_failed:{exc}"
