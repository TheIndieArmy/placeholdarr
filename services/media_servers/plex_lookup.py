from __future__ import annotations

from typing import Any

from core.config import settings
from core.logger import logger

_PLEX = None


def get_plex_server(refresh: bool = False):
    """Return a cached Plex server client when Plex is enabled and configured."""
    global _PLEX

    if not getattr(settings, "plex_enabled", False):
        _PLEX = None
        return None

    if _PLEX is not None and not refresh:
        return _PLEX

    try:
        from plexapi.server import PlexServer

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
        all_shows = tv_section.all()

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
        all_movies = movie_section.all()

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
