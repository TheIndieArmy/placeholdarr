from __future__ import annotations

import os
import shutil
import time

from core.config import settings
from services.media_servers.refresh import refresh_all_sections
from core.logger import logger
from services.placeholders import ensure_placeholder_file, episode_placeholder_path
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Placeholder, Season, Series
from services.source_of_truth.determiner import DETERMINATION_EXISTS, DETERMINATION_NEEDS
from services.source_of_truth.materializer import (
    apply_episode_materialization,
    apply_movie_materialization,
)


def _configured_roots() -> list[str]:
    roots = [
        getattr(settings, "MOVIE_LIBRARY_FOLDER", None),
        getattr(settings, "TV_LIBRARY_FOLDER", None),
        getattr(settings, "MOVIE_LIBRARY_4K_FOLDER", None),
        getattr(settings, "TV_LIBRARY_4K_FOLDER", None),
    ]
    return [os.path.abspath(root) for root in roots if root]


def _count_placeholders_under_root(session, root: str) -> int:
    prefix = f"{os.path.abspath(root).rstrip(os.sep)}{os.sep}%"
    return (
        session.query(Placeholder)
        .filter(Placeholder.has_placeholder == True)  # noqa: E712
        .filter(Placeholder.path.like(prefix))
        .count()
    )


def _empty_roots(session) -> list[str]:
    empty: list[str] = []
    for root in _configured_roots():
        try:
            if _count_placeholders_under_root(session, root) == 0:
                empty.append(root)
        except Exception:
            continue
    return empty


def _select_needs_movie_ids(session, limit: int) -> list[int]:
    rows = (
        session.query(Movie.id)
        .filter(Movie.is_deleted == False)  # noqa: E712
        .filter(Movie.has_file == False)  # noqa: E712
        .filter(Movie.determination == DETERMINATION_NEEDS)
        .order_by(Movie.title.asc(), Movie.year.asc())
        .limit(max(0, int(limit)))
        .all()
    )
    return [int(row[0]) for row in rows]


def _select_needs_episode_ids(session, series_limit: int, per_series: int) -> list[int]:
    rows = (
        session.query(Episode.id, Series.id)
        .join(Season, Episode.season_id == Season.id)
        .join(Series, Season.series_id == Series.id)
        .filter(Episode.is_deleted == False)  # noqa: E712
        .filter(Episode.has_file == False)  # noqa: E712
        .filter(Episode.determination == DETERMINATION_NEEDS)
        .order_by(Series.title.asc(), Season.season_number.asc(), Episode.episode_number.asc())
        .all()
    )

    chosen: list[int] = []
    counts_by_series: dict[int, int] = {}
    selected_series: set[int] = set()

    for episode_id, series_id in rows:
        series_id = int(series_id)
        current = counts_by_series.get(series_id, 0)
        if current >= per_series:
            continue
        if len(selected_series) >= series_limit and series_id not in selected_series:
            continue
        counts_by_series[series_id] = current + 1
        selected_series.add(series_id)
        chosen.append(int(episode_id))

    return chosen


def _select_existing_movie_paths(session, limit: int) -> list[str]:
    rows = (
        session.query(Movie.placeholder_filepath)
        .filter(Movie.is_deleted == False)  # noqa: E712
        .filter(Movie.has_file == False)  # noqa: E712
        .filter(Movie.has_placeholder == True)  # noqa: E712
        .filter(Movie.determination == DETERMINATION_EXISTS)
        .filter(Movie.placeholder_filepath.isnot(None))
        .order_by(Movie.title.asc(), Movie.year.asc())
        .limit(max(0, int(limit)))
        .all()
    )
    return [str(row[0]) for row in rows if row and row[0]]


def _select_existing_episode_paths(session, series_limit: int, per_series: int) -> list[str]:
    rows = (
        session.query(Episode.id, Series.id, Episode.placeholder_filepath, Season, Series, Episode)
        .join(Season, Episode.season_id == Season.id)
        .join(Series, Season.series_id == Series.id)
        .filter(Episode.is_deleted == False)  # noqa: E712
        .filter(Episode.has_file == False)  # noqa: E712
        .filter(Episode.has_placeholder == True)  # noqa: E712
        .filter(Episode.determination == DETERMINATION_EXISTS)
        .order_by(Series.title.asc(), Season.season_number.asc(), Episode.episode_number.asc())
        .all()
    )

    chosen: list[str] = []
    counts_by_series: dict[int, int] = {}
    selected_series: set[int] = set()

    for _, series_id, path, season, series, episode in rows:
        series_id = int(series_id)
        current = counts_by_series.get(series_id, 0)
        if current >= per_series:
            continue
        if len(selected_series) >= series_limit and series_id not in selected_series:
            continue

        resolved = str(path) if path else episode_placeholder_path(episode, season, series)
        if not resolved:
            continue

        counts_by_series[series_id] = current + 1
        selected_series.add(series_id)
        chosen.append(resolved)

    return chosen


def _to_copy(path: str) -> None:
    """Ensure a primer placeholder is an independent copy, not a hardlink.

    Called after materialisation so Plex always sees a distinct inode/hash
    for each primer file regardless of the global PLACEHOLDER_STRATEGY.
    """
    dummy_path = getattr(settings, "DUMMY_FILE_PATH", None)
    if not dummy_path or not os.path.isfile(dummy_path) or not os.path.isfile(path):
        return
    try:
        tmp = path + ".tmp"
        shutil.copy2(dummy_path, tmp)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"Primer copy-upgrade failed for {path}: {e}", extra={"emoji_type": "warning"})





def _reprime_paths(paths: list[str]) -> tuple[int, set[str]]:
    updated = 0
    folders: set[str] = set()

    for path in dict.fromkeys(paths):
        if not path:
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
            created = ensure_placeholder_file(path)
            if created:
                updated += 1
                folders.add(os.path.dirname(path))
        except Exception as e:
            logger.warning(f"Force-prime path rewrite failed path={path}: {e}", extra={"emoji_type": "warning"})

    return updated, folders


def run_primer_phase() -> dict:
    strategy = str(getattr(settings, "PLACEHOLDER_STRATEGY", "hardlink") or "hardlink").strip().lower()
    force_prime = bool(getattr(settings, "FORCE_PRIME_ON_STARTUP", False))
    primer_enabled = bool(getattr(settings, "PRIMER_ENABLED", False))

    stats = {
        "enabled": primer_enabled,
        "force_enabled": force_prime,
        "strategy": strategy,
        "skipped": False,
        "reason": None,
        "empty_roots": [],
        "attempted_movies": 0,
        "attempted_episodes": 0,
        "materialized_movies": 0,
        "materialized_episodes": 0,
        "force_reprimed": 0,
        "refresh_requested": 0,
        "refresh_failed": 0,
        "wait_seconds": 0,
    }

    if not primer_enabled and not force_prime:
        stats["skipped"] = True
        stats["reason"] = "disabled"
        logger.info(
            "Primer phase skipped: PRIMER_ENABLED=false and FORCE_PRIME_ON_STARTUP=false",
            extra={"emoji_type": "info"},
        )
        return stats

    target_movies = max(1, int(getattr(settings, "PRIMER_SERIES_COUNT", 3) or 3))
    target_series = max(1, int(getattr(settings, "PRIMER_SERIES_COUNT", 3) or 3))
    target_episodes = max(1, int(getattr(settings, "PRIMER_EPISODES_PER_SERIES", 3) or 3))

    session = get_session()
    try:
        empty_roots = _empty_roots(session)
        stats["empty_roots"] = empty_roots

        should_prime = force_prime or bool(empty_roots)
        if not should_prime:
            stats["skipped"] = True
            stats["reason"] = "roots_not_empty"
            logger.info(
                "Primer phase skipped: all library roots already contain placeholders",
                extra={"emoji_type": "info"},
            )
            return stats

        movie_ids = _select_needs_movie_ids(session, target_movies)
        episode_ids = _select_needs_episode_ids(session, target_series, target_episodes)
        stats["attempted_movies"] = len(movie_ids)
        stats["attempted_episodes"] = len(episode_ids)
    finally:
        session.close()

    changed_folders: set[str] = set()

    for movie_id in movie_ids:
        result = apply_movie_materialization(movie_id)
        if result.get("ok") and result.get("action") == "created_or_exists":
            path = result.get("path")
            if path:
                _to_copy(path)
                changed_folders.add(os.path.dirname(path))
            created = result.get("created", False)
            logger.info(
                f"Primer {'created' if created else 'exists'} movie placeholder: {path}",
                extra={"emoji_type": "info"},
            )
            stats["materialized_movies"] += 1

    for episode_id in episode_ids:
        result = apply_episode_materialization(episode_id)
        if result.get("ok") and result.get("action") == "created_or_exists":
            path = result.get("path")
            if path:
                _to_copy(path)
                changed_folders.add(os.path.dirname(path))
            created = result.get("created", False)
            logger.info(
                f"Primer {'created' if created else 'exists'} episode placeholder: {path}",
                extra={"emoji_type": "info"},
            )
            stats["materialized_episodes"] += 1

    # Force mode fallback: if nothing needed creation, rewrite a small sample of existing placeholders.
    if force_prime and stats["materialized_movies"] == 0 and stats["materialized_episodes"] == 0:
        session = get_session()
        try:
            movie_paths = _select_existing_movie_paths(session, target_movies)
            episode_paths = _select_existing_episode_paths(session, target_series, target_episodes)
        finally:
            session.close()

        reprime_count, reprime_folders = _reprime_paths(movie_paths + episode_paths)
        stats["force_reprimed"] = reprime_count
        changed_folders.update(reprime_folders)

    if changed_folders:
        refresh_stats = refresh_all_sections(
            has_movies=stats["materialized_movies"] > 0,
            has_episodes=stats["materialized_episodes"] > 0,
        )
        stats["refresh_requested"] = int(refresh_stats.get("refreshed", 0) or 0)
        stats["refresh_failed"] = int(refresh_stats.get("failed", 0) or 0)

        wait_seconds = max(0, int(getattr(settings, "PRIMER_REFRESH_WAIT_SECONDS", 15) or 15))
        stats["wait_seconds"] = wait_seconds
        if wait_seconds:
            logger.info(
                f"Primer waiting {wait_seconds}s after Plex refresh before continuing",
                extra={"emoji_type": "info"},
            )
            time.sleep(wait_seconds)
    else:
        stats["skipped"] = True
        stats["reason"] = "no_candidates"

    logger.info(
        (
            f"Primer phase complete: movies={stats['materialized_movies']} "
            f"episodes={stats['materialized_episodes']} force_reprimed={stats['force_reprimed']} "
            f"refresh_requested={stats['refresh_requested']}"
        ),
        extra={"emoji_type": "success"},
    )
    return stats
