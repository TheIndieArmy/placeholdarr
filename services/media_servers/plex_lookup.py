from __future__ import annotations

import threading
import time
from typing import Any

from core.config import settings
from core.logger import logger

_PLEX = None

# LibrarySection.all() pulls the full movie/show list from Plex. Doing that once per title
# during a batch (when rating_key is missing) can stall for many minutes. Cache per section
# for a short TTL so one batch reuses a single listing.
_plex_section_all_cache: dict[tuple[str, str], tuple[float, list[Any]]] = {}


def _plex_section_list_cache_ttl() -> float:
    try:
        return max(5.0, float(getattr(settings, "PLEX_SECTION_LIST_CACHE_SECONDS", 90) or 90))
    except Exception:
        return 90.0


def _cached_section_all(section, kind: str) -> list[Any]:
    """Return section.all() with a short process-local TTL cache to avoid repeated full scans."""
    try:
        sk = str(int(section.key))
    except Exception:
        sk = str(getattr(section, "key", "unknown"))
    cache_key = (kind, sk)
    now = time.monotonic()
    ttl = _plex_section_list_cache_ttl()
    hit = _plex_section_all_cache.get(cache_key)
    if hit and (now - hit[0]) <= ttl:
        logger.debug(
            f"Plex: using cached {kind} list section_key={sk} age_s={now - hit[0]:.1f}",
            extra={"emoji_type": "debug"},
        )
        return hit[1]

    try:
        sec_title = str(getattr(section, "title", None) or getattr(section, "libtype", "") or "?")
    except Exception:
        sec_title = "?"
    caller_thread = threading.current_thread().name

    logger.info(
        f"Plex: loading full {kind} list from server "
        f"(section_key={sk}, section_title={sec_title!r}, caller_thread={caller_thread}) — "
        f"only used when a title has no cached Plex id; "
        f"direct ratingKey updates do NOT hit this path. "
        f"Heartbeats every 15s while this PlexAPI call runs; "
        f"lookups within {int(ttl)}s reuse this list…",
        extra={"emoji_type": "info"},
    )

    stop_heartbeat = threading.Event()
    _hb_interval = 15.0

    def _slow_load_heartbeat():
        # While section.all() blocks, log periodically. Short loads may finish before first tick.
        while not stop_heartbeat.wait(_hb_interval):
            logger.info(
                f"Plex: section.all() still in progress kind={kind!r} section_key={sk} "
                f"section_title={sec_title!r} caller_thread={caller_thread}",
                extra={"emoji_type": "info"},
            )

    hb = threading.Thread(
        target=_slow_load_heartbeat,
        name=f"plex-hb-{sk}",
        daemon=True,
    )
    hb.start()
    t0 = time.monotonic()
    try:
        items = section.all()
    except Exception as exc:
        logger.error(
            f"Plex: section.all() failed kind={kind!r} section_key={sk} "
            f"section_title={sec_title!r} caller_thread={caller_thread} "
            f"elapsed_s={time.monotonic() - t0:.1f} error={type(exc).__name__}: {exc}",
            extra={"emoji_type": "error"},
        )
        raise
    finally:
        stop_heartbeat.set()

    elapsed = time.monotonic() - t0
    logger.info(
        f"Plex: loaded {kind} list section_key={sk} count={len(items)} "
        f"elapsed_s={elapsed:.1f} caller_thread={caller_thread}",
        extra={"emoji_type": "info"},
    )
    _plex_section_all_cache[cache_key] = (now, items)
    return items


def get_plex_server(refresh: bool = False):
    """Return a cached Plex server client when Plex is enabled and configured."""
    global _PLEX, _plex_section_all_cache

    if not getattr(settings, "plex_enabled", False):
        _PLEX = None
        return None

    if refresh:
        _plex_section_all_cache.clear()

    if _PLEX is not None and not refresh:
        return _PLEX

    try:
        from plexapi.server import PlexServer

        _plex_section_all_cache.clear()
        _PLEX = PlexServer(settings.PLEX_URL, settings.PLEX_TOKEN)
        return _PLEX
    except Exception as ex:
        logger.warning(f"Plex connection unavailable: {ex}", extra={"emoji_type": "warning"})
        _PLEX = None
        return None


def _normalize_title(title: str | None) -> str:
    if not title:
        return ""
    text = str(title).strip().lower()
    if text.endswith(")") and "(" in text:
        open_idx = text.rfind("(")
        year = text[open_idx + 1 : -1]
        if len(year) == 4 and year.isdigit():
            return text[:open_idx].strip()
    return text


def _extract_guid_numeric(item: Any, provider: str) -> str | None:
    needle = f"{provider.lower()}://"
    try:
        for guid in getattr(item, "guids", []) or []:
            guid_id = str(getattr(guid, "id", "") or "")
            if guid_id.lower().startswith(needle):
                return guid_id.split("://", 1)[1]
    except Exception:
        return None
    return None


def _extract_path_numeric(item: Any, provider: str) -> str | None:
    needle = f"{provider.lower()}-"
    try:
        for location in getattr(item, "locations", []) or []:
            text = str(location or "").lower()
            idx = text.find(needle)
            if idx < 0:
                continue
            rest = text[idx + len(needle) :]
            digits = ""
            for ch in rest:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                return digits
    except Exception:
        return None
    return None


def find_show_by_id(tvdb_id, title=None):
    """Find a TV show in Plex by TVDB ID with title fallback."""
    plex = get_plex_server()
    if not plex:
        return None

    try:
        tv_section = plex.library.sectionByID(settings.PLEX_TV_SECTION_ID)
        all_shows = _cached_section_all(tv_section, "TV show")

        target_tvdb = str(tvdb_id)
        for show in all_shows:
            guid_id = _extract_guid_numeric(show, "tvdb")
            if guid_id and guid_id == target_tvdb:
                return show

        for show in all_shows:
            path_id = _extract_path_numeric(show, "tvdb")
            if path_id and path_id == target_tvdb:
                return show

        if title:
            clean_title = _normalize_title(title)
            for show in all_shows:
                if _normalize_title(getattr(show, "title", None)) == clean_title:
                    return show
            try:
                return tv_section.get(title)
            except Exception:
                return None

        return None
    except Exception as ex:
        logger.error(f"Error finding show by ID: {ex}", extra={"emoji_type": "error"})
        return None


def find_movie_by_id(tmdb_id, title=None, year=None):
    """Find a movie in Plex by TMDB ID with title/year fallback."""
    plex = get_plex_server()
    if not plex:
        return None

    try:
        movie_section = plex.library.sectionByID(settings.PLEX_MOVIE_SECTION_ID)
        all_movies = _cached_section_all(movie_section, "movie")

        target_tmdb = str(tmdb_id)
        for movie in all_movies:
            guid_id = _extract_guid_numeric(movie, "tmdb")
            if guid_id and guid_id == target_tmdb:
                return movie

        for movie in all_movies:
            path_id = _extract_path_numeric(movie, "tmdb")
            if path_id and path_id == target_tmdb:
                return movie

        if title:
            clean_title = _normalize_title(title)
            if year is not None:
                try:
                    target_year = int(year)
                except Exception:
                    target_year = None
                if target_year is not None:
                    for movie in all_movies:
                        if (
                            _normalize_title(getattr(movie, "title", None)) == clean_title
                            and int(getattr(movie, "year", 0) or 0) == target_year
                        ):
                            return movie

            for movie in all_movies:
                if _normalize_title(getattr(movie, "title", None)) == clean_title:
                    return movie
            try:
                return movie_section.get(title)
            except Exception:
                return None

        return None
    except Exception as ex:
        logger.error(f"Error finding movie by ID: {ex}", extra={"emoji_type": "error"})
        return None


def find_episode_by_series_tvdb(
    tvdb_id,
    season_number: int,
    episode_number: int,
    *,
    series_title: str | None = None,
):
    """Resolve a Plex episode from the series TVDB id and SxxEyy indices."""
    show = find_show_by_id(tvdb_id, title=series_title)
    if not show:
        return None
    try:
        return show.episode(season=int(season_number), episode=int(episode_number))
    except Exception as ex:
        logger.debug(
            f"find_episode_by_series_tvdb: no episode S{season_number}E{episode_number} for tvdb={tvdb_id}: {ex}",
            extra={"emoji_type": "debug"},
        )
        return None
