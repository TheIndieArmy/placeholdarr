from __future__ import annotations

import os
import shutil
import time

from core.config import settings
from services.media_servers.refresh import refresh_all_sections
from core.logger import logger
from services.placeholders import (
    ensure_placeholder_file,
    episode_placeholder_path,
    resolve_calendar_variant_dummy_path,
)
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


def _select_needs_coming_soon_episode_ids(session, limit: int) -> list[int]:
    """Return episode IDs whose placeholder is tagged coming_soon variant but has no file on disk.

    These are episodes that went through the calendar phase in a prior run so
    their placeholder.extra['calendar_dummy_variant'] == 'coming_soon', but the
    placeholder file has since been purged/marked-missing and needs to be primed
    as an independent copy so Plex sees a distinct inode for the coming-soon dummy.
    """
    rows = (
        session.query(Episode.id, Placeholder.extra)
        .join(Placeholder, Placeholder.episode_id == Episode.id)
        .filter(Episode.is_deleted == False)  # noqa: E712
        .filter(Episode.has_file == False)  # noqa: E712
        .filter(Episode.determination == DETERMINATION_NEEDS)
        .filter(Placeholder.has_placeholder == False)  # noqa: E712
        .order_by(Episode.id.asc())
        .all()
    )
    result: list[int] = []
    for episode_id, extra in rows:
        if len(result) >= limit:
            break
        extra_dict = dict(extra or {})
        if str(extra_dict.get("calendar_dummy_variant") or "").strip() == "coming_soon":
            result.append(int(episode_id))
    return result


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


def _is_coming_soon_status(status: str | None) -> bool:
    return str(status or "") in {
        "COMING_SOON",
        "COMING_SOON_30",
        "COMING_SOON_14",
        "COMING_SOON_7",
        "COMING_SOON_1",
        "COMING_SOON_TODAY",
    }


def _dummy_file_path_for_variant(variant: str) -> str | None:
    path = resolve_calendar_variant_dummy_path(variant)
    return path or None


def _resolve_variant_for_path(session, path: str) -> str:
    row = (
        session.query(Placeholder)
        .filter(Placeholder.path == path)
        .order_by(Placeholder.id.desc())
        .first()
    )
    if not row:
        return "request"

    extra = dict(getattr(row, "extra", {}) or {})
    stored_variant = str(extra.get("calendar_dummy_variant") or "").strip()
    if stored_variant in {"coming_soon", "request"}:
        return stored_variant

    return "coming_soon" if _is_coming_soon_status(getattr(row, "display_status", None)) else "request"


def _resolve_variant_for_row(row: Placeholder) -> str:
    extra = dict(getattr(row, "extra", {}) or {})
    stored_variant = str(extra.get("calendar_dummy_variant") or "").strip()
    if stored_variant in {"coming_soon", "request"}:
        return stored_variant
    return "coming_soon" if _is_coming_soon_status(getattr(row, "display_status", None)) else "request"


def _is_independent_copy(path: str, variant: str) -> bool:
    """Return True if *path* is NOT a hardlink to the variant's dummy source file.

    A placeholder that shares an inode with the dummy has never been primer-seeded
    and still needs to be converted to an independent copy.  If the dummy path
    cannot be resolved we conservatively return False so the primer will run.
    """
    dummy_path = _dummy_file_path_for_variant(variant)
    if not dummy_path or not os.path.isfile(dummy_path):
        return False
    if not os.path.isfile(path):
        return False
    try:
        return os.stat(path).st_ino != os.stat(dummy_path).st_ino
    except OSError:
        return False


def _variant_coverage(session) -> dict[str, bool]:
    """Return whether active placeholders already cover each dummy variant
    with at least one independent copy (i.e. a file whose inode differs from
    the dummy source).  A placeholder that is still a hardlink to the dummy
    has not been primer-seeded and does NOT count as covered."""
    covered = {"request": False, "coming_soon": False}
    rows = (
        session.query(Placeholder)
        .filter(Placeholder.has_placeholder == True)  # noqa: E712
        .filter(Placeholder.path.isnot(None))
        .all()
    )
    for row in rows:
        variant = _resolve_variant_for_row(row)
        if covered.get(variant):
            continue
        path = getattr(row, "path", None)
        if path and _is_independent_copy(str(path), variant):
            covered[variant] = True
        if covered["request"] and covered["coming_soon"]:
            break
    return covered


def _to_copy(session, path: str) -> None:
    """Ensure a primer placeholder is an independent copy, not a hardlink.

    Called after materialisation so Plex always sees a distinct inode/hash
    for each primer file regardless of the global PLACEHOLDER_STRATEGY.
    """
    variant = _resolve_variant_for_path(session, path)
    dummy_path = _dummy_file_path_for_variant(variant)
    if not dummy_path or not os.path.isfile(dummy_path) or not os.path.isfile(path):
        return
    try:
        tmp = path + ".tmp"
        shutil.copy2(dummy_path, tmp)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"Primer copy-upgrade failed for {path}: {e}", extra={"emoji_type": "warning"})





def _reprime_paths(session, paths: list[str]) -> tuple[int, set[str]]:
    updated = 0
    folders: set[str] = set()

    for path in dict.fromkeys(paths):
        if not path:
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
            variant = _resolve_variant_for_path(session, path)
            created = ensure_placeholder_file(path, dummy_file_path=_dummy_file_path_for_variant(variant))
            if created:
                updated += 1
                folders.add(os.path.dirname(path))
        except Exception as e:
            logger.warning(f"Force-prime path rewrite failed path={path}: {e}", extra={"emoji_type": "warning"})

    return updated, folders


def run_primer_phase() -> dict:
    strategy = str(getattr(settings, "PLACEHOLDER_STRATEGY", "hardlink") or "hardlink").strip().lower()
    force_prime = bool(getattr(settings, "FORCE_PRIME_ON_STARTUP", False))

    stats = {
        "force_enabled": force_prime,
        "strategy": strategy,
        "skipped": False,
        "reason": None,
        "empty_roots": [],
        "variant_request_covered": False,
        "variant_coming_soon_covered": False,
        "attempted_movies": 0,
        "attempted_episodes": 0,
        "materialized_movies": 0,
        "materialized_episodes": 0,
        "force_reprimed": 0,
        "refresh_requested": 0,
        "refresh_failed": 0,
        "wait_seconds": 0,
    }

    # Hardlink mode needs primer protections by default. Non-hardlink mode only
    # runs primer when force is explicitly requested.
    if strategy != "hardlink" and not force_prime:
        stats["skipped"] = True
        stats["reason"] = "non_hardlink_no_force"
        logger.info(
            "Primer phase skipped: PLACEHOLDER_STRATEGY is not hardlink and force prime is off",
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
        coverage = _variant_coverage(session)
        stats["variant_request_covered"] = bool(coverage.get("request"))
        stats["variant_coming_soon_covered"] = bool(coverage.get("coming_soon"))

        # Auto-primer should stop once both variants are covered, unless forced.
        if not force_prime and coverage.get("request") and coverage.get("coming_soon"):
            stats["skipped"] = True
            stats["reason"] = "already_primed_both_variants"
            logger.info(
                "Primer phase skipped: request and coming-soon variants already primed",
                extra={"emoji_type": "info"},
            )
            return stats

        movie_ids = _select_needs_movie_ids(session, target_movies)
        episode_ids = _select_needs_episode_ids(session, target_series, target_episodes)

        # When the coming-soon variant isn't covered yet, explicitly seek out
        # episodes tagged as coming_soon from a prior calendar-phase run.
        # These won't be reached by the normal selection (which sorts by series/
        # season/episode and stops at per-series limits), so we add them separately.
        if not coverage.get("coming_soon"):
            coming_soon_ids = _select_needs_coming_soon_episode_ids(session, target_series)
            # Prepend so they're primed even if we hit limits on other episodes.
            episode_ids = coming_soon_ids + [eid for eid in episode_ids if eid not in set(coming_soon_ids)]

        stats["attempted_movies"] = len(movie_ids)
        stats["attempted_episodes"] = len(episode_ids)
    finally:
        session.close()

    changed_folders: set[str] = set()
    variant_session = get_session()

    try:
        for movie_id in movie_ids:
            result = apply_movie_materialization(movie_id)
            if result.get("ok") and result.get("action") == "created_or_exists":
                path = result.get("path")
                if path:
                    _to_copy(variant_session, path)
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
                    _to_copy(variant_session, path)
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

            reprime_count, reprime_folders = _reprime_paths(variant_session, movie_paths + episode_paths)
            stats["force_reprimed"] = reprime_count
            changed_folders.update(reprime_folders)
    finally:
        variant_session.close()

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
