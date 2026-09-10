"""Preferred poster language: Arr remote_poster fast path + TMDB exact-language URL."""

from __future__ import annotations

from typing import Any

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Movie, Season, Series
from services.tmdb_client import (
    TmdbError,
    fetch_movie_images,
    fetch_movie_original_language,
    fetch_tv_images,
    fetch_tv_original_language,
    fetch_tv_season_images,
    pick_exact_poster_from_images,
    tmdb_configured,
    tmdb_poster_cdn_url,
)


def preferred_poster_language() -> str:
    raw = str(getattr(settings, "PREFERRED_POSTER_LANGUAGE", None) or "en").strip().lower()
    return raw or "en"


def prefer_original_poster_language() -> bool:
    return bool(getattr(settings, "PREFER_ORIGINAL_POSTER_LANGUAGE", False))


def preferred_poster_language_feature_enabled() -> bool:
    """Master gate: when off, always use Arr remote_poster (no TMDB language resolve)."""
    return bool(getattr(settings, "ENABLE_PREFERRED_POSTER_LANGUAGE", False))


def poster_language_resolve_enabled() -> bool:
    """True when TMDB exact-language resolve should run."""
    return preferred_poster_language_feature_enabled()


def effective_poster_url(entity: Any) -> str | None:
    """Exact-language localized poster when the feature is on and stamped; otherwise Arr ``remote_poster``."""
    if preferred_poster_language_feature_enabled():
        loc = str(getattr(entity, "localized_poster", None) or "").strip()
        lang = str(getattr(entity, "localized_poster_lang", None) or "").strip().lower()
        if loc and lang:
            return loc
    remote = str(getattr(entity, "remote_poster", None) or "").strip()
    return remote or None


def localized_poster_is_current(entity: Any) -> bool:
    """Exact match already stored (empty means retry on full sync / next resolve)."""
    if not poster_language_resolve_enabled():
        return True
    loc = str(getattr(entity, "localized_poster", None) or "").strip()
    lang = str(getattr(entity, "localized_poster_lang", None) or "").strip().lower()
    return bool(loc and lang)


def clear_stale_localized_posters(session, *, new_lang: str | None = None) -> int:
    """Clear all localized poster columns (language / original setting changed)."""
    del new_lang  # callers may pass preferred lang; we clear all exact-match stamps
    cleared = 0
    for model in (Movie, Series, Season):
        count = (
            session.query(model)
            .filter(
                (model.localized_poster.isnot(None)) | (model.localized_poster_lang.isnot(None))
            )
            .update(
                {model.localized_poster: None, model.localized_poster_lang: None},
                synchronize_session=False,
            )
        )
        cleared += int(count or 0)
    return cleared


def _apply_localized(entity: Any, url: str | None, lang: str | None) -> bool:
    """Set localized columns on entity. Returns True if values changed."""
    new_url = str(url or "").strip() or None
    new_lang = str(lang or "").strip().lower() or None
    if new_url is None:
        new_lang = None
    old_url = str(getattr(entity, "localized_poster", None) or "").strip() or None
    old_lang = str(getattr(entity, "localized_poster_lang", None) or "").strip().lower() or None
    if old_url == new_url and old_lang == new_lang:
        return False
    entity.localized_poster = new_url
    entity.localized_poster_lang = new_lang if new_url else None
    try:
        from datetime import datetime, timezone

        entity.updated_at = datetime.now(timezone.utc)
    except Exception:
        pass
    return True


def _sought_languages_for_tmdb_id(tmdb_id: int, *, media: str) -> list[str]:
    """Build exact-match language order: original (optional) then preferred."""
    sought: list[str] = []
    if prefer_original_poster_language() and tmdb_id > 0:
        try:
            if media == "movie":
                original = fetch_movie_original_language(tmdb_id)
            else:
                original = fetch_tv_original_language(tmdb_id)
        except TmdbError:
            original = None
        except Exception:
            original = None
        if original:
            sought.append(original)
    pref = preferred_poster_language()
    if pref:
        sought.append(pref)
    # Dedupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for lang in sought:
        key = str(lang or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _pick_exact_url(images: dict[str, Any], sought: list[str]) -> tuple[str | None, str | None]:
    path, matched = pick_exact_poster_from_images(images, sought_languages=sought)
    return tmdb_poster_cdn_url(path), matched


def resolve_localized_poster_for_movie(movie: Any, *, persist: bool = True) -> bool:
    """Resolve exact-language TMDB poster for a movie. Empty + Arr if no exact hit."""
    if not poster_language_resolve_enabled():
        return False
    if localized_poster_is_current(movie):
        return False
    if not tmdb_configured():
        return False
    tmdb_id = int(getattr(movie, "tmdbid", None) or 0)
    if tmdb_id <= 0:
        return False
    sought = _sought_languages_for_tmdb_id(tmdb_id, media="movie")
    if not sought:
        return False
    try:
        images = fetch_movie_images(tmdb_id, sought_languages=sought)
        url, matched = _pick_exact_url(images, sought)
    except TmdbError as exc:
        logger.debug(
            f"poster language resolve skipped movie tmdb={tmdb_id}: {exc}",
            extra={"emoji_type": "debug"},
        )
        return False
    except Exception as exc:
        logger.debug(
            f"poster language resolve failed movie tmdb={tmdb_id}: {exc}",
            extra={"emoji_type": "debug"},
        )
        return False
    if not url or not matched:
        # Leave empty so full sync can retry when TMDB gains that language later.
        return False
    changed = _apply_localized(movie, url, matched)
    if changed and persist and getattr(movie, "id", None):
        _persist_entity_localized("movie", int(movie.id), url, matched)
    return changed


def resolve_localized_poster_for_series(series: Any, *, persist: bool = True) -> bool:
    if not poster_language_resolve_enabled():
        return False
    if localized_poster_is_current(series):
        return False
    if not tmdb_configured():
        return False
    tmdb_id = int(getattr(series, "sonarr_tmdbid", None) or 0)
    if tmdb_id <= 0:
        return False
    sought = _sought_languages_for_tmdb_id(tmdb_id, media="tv")
    if not sought:
        return False
    try:
        images = fetch_tv_images(tmdb_id, sought_languages=sought)
        url, matched = _pick_exact_url(images, sought)
    except TmdbError as exc:
        logger.debug(
            f"poster language resolve skipped series tmdb={tmdb_id}: {exc}",
            extra={"emoji_type": "debug"},
        )
        return False
    except Exception as exc:
        logger.debug(
            f"poster language resolve failed series tmdb={tmdb_id}: {exc}",
            extra={"emoji_type": "debug"},
        )
        return False
    if not url or not matched:
        return False
    changed = _apply_localized(series, url, matched)
    if changed and persist and getattr(series, "id", None):
        _persist_entity_localized("series", int(series.id), url, matched)
    return changed


def resolve_localized_poster_for_season(
    season: Any,
    series: Any,
    *,
    persist: bool = True,
) -> bool:
    if not poster_language_resolve_enabled():
        return False
    if localized_poster_is_current(season):
        return False
    if not tmdb_configured():
        return False
    tmdb_id = int(getattr(series, "sonarr_tmdbid", None) or 0)
    if tmdb_id <= 0:
        return False
    sought = _sought_languages_for_tmdb_id(tmdb_id, media="tv")
    if not sought:
        return False
    season_number = int(getattr(season, "season_number", 0) or 0)
    try:
        images = fetch_tv_season_images(tmdb_id, season_number, sought_languages=sought)
        url, matched = _pick_exact_url(images, sought)
    except TmdbError as exc:
        logger.debug(
            f"poster language resolve skipped season tmdb={tmdb_id} S{season_number}: {exc}",
            extra={"emoji_type": "debug"},
        )
        return False
    except Exception as exc:
        logger.debug(
            f"poster language resolve failed season tmdb={tmdb_id} S{season_number}: {exc}",
            extra={"emoji_type": "debug"},
        )
        return False
    if not url or not matched:
        return False
    changed = _apply_localized(season, url, matched)
    if changed and persist and getattr(season, "id", None):
        _persist_entity_localized("season", int(season.id), url, matched)
    return changed


def ensure_localized_poster_current(entity: Any, *, series: Any | None = None) -> None:
    """Resolve-if-empty for movie/series/season before art download."""
    if entity is None:
        return
    if isinstance(entity, Movie) or getattr(entity, "__tablename__", None) == "movie":
        resolve_localized_poster_for_movie(entity)
        return
    if isinstance(entity, Series) or getattr(entity, "__tablename__", None) == "series":
        resolve_localized_poster_for_series(entity)
        return
    if isinstance(entity, Season) or getattr(entity, "__tablename__", None) == "season":
        if series is None:
            return
        resolve_localized_poster_for_season(entity, series)
        return
    if hasattr(entity, "tmdbid") and hasattr(entity, "remote_poster") and not hasattr(entity, "tvdbid"):
        resolve_localized_poster_for_movie(entity)
    elif hasattr(entity, "tvdbid") and hasattr(entity, "remote_poster"):
        resolve_localized_poster_for_series(entity)
    elif hasattr(entity, "season_number") and series is not None:
        resolve_localized_poster_for_season(entity, series)


def _persist_entity_localized(kind: str, entity_id: int, url: str, lang: str) -> None:
    session = get_session()
    try:
        model = {"movie": Movie, "series": Series, "season": Season}[kind]
        row = session.query(model).get(int(entity_id))
        if not row:
            return
        row.localized_poster = url
        row.localized_poster_lang = lang
        session.add(row)
        session.commit()
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.debug(
            f"persist localized poster failed kind={kind} id={entity_id}: {exc}",
            extra={"emoji_type": "debug"},
        )
    finally:
        try:
            session.close()
        except Exception:
            pass


def resolve_movies_by_ids(session, movie_ids: list[int]) -> dict[str, int]:
    resolved = 0
    skipped = 0
    for mid in movie_ids:
        movie = session.query(Movie).get(int(mid))
        if not movie:
            skipped += 1
            continue
        if resolve_localized_poster_for_movie(movie, persist=False):
            session.add(movie)
            resolved += 1
        else:
            skipped += 1
    return {"resolved": resolved, "skipped": skipped}


def resolve_series_by_ids(session, series_ids: list[int], *, include_seasons: bool = True) -> dict[str, int]:
    resolved = 0
    skipped = 0
    seasons_resolved = 0
    for sid in series_ids:
        series = session.query(Series).get(int(sid))
        if not series:
            skipped += 1
            continue
        if resolve_localized_poster_for_series(series, persist=False):
            session.add(series)
            resolved += 1
        else:
            skipped += 1
        if include_seasons:
            seasons = (
                session.query(Season)
                .filter(Season.series_id == int(sid), Season.is_deleted == False)  # noqa: E712
                .all()
            )
            for season in seasons:
                if resolve_localized_poster_for_season(season, series, persist=False):
                    session.add(season)
                    seasons_resolved += 1
    return {"resolved": resolved, "skipped": skipped, "seasons_resolved": seasons_resolved}


def collect_stale_poster_language_ids(session) -> dict[str, list[int]]:
    """Ids with no exact-language localized poster yet (full sync retries these)."""
    if not poster_language_resolve_enabled() or not tmdb_configured():
        return {"movie_ids": [], "series_ids": []}
    movies = (
        session.query(Movie.id)
        .filter(
            Movie.is_deleted == False,  # noqa: E712
            Movie.tmdbid.isnot(None),
            (Movie.localized_poster.is_(None)) | (Movie.localized_poster_lang.is_(None)),
        )
        .all()
    )
    series = (
        session.query(Series.id)
        .filter(
            Series.is_deleted == False,  # noqa: E712
            Series.sonarr_tmdbid.isnot(None),
            (Series.localized_poster.is_(None)) | (Series.localized_poster_lang.is_(None)),
        )
        .all()
    )
    return {
        "movie_ids": [int(r[0]) for r in movies],
        "series_ids": [int(r[0]) for r in series],
    }
