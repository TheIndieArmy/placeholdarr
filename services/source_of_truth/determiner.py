from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.placeholders import episode_placeholder_path, movie_placeholder_path
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Placeholder, Season, Series
from services.source_of_truth.filesystem import configured_roots


DETERMINATION_OBSOLETE = 'obsolete_placeholder'
DETERMINATION_NOT_NEEDED = 'not_needed'
DETERMINATION_EXISTS = 'placeholder_exists'
DETERMINATION_NEEDS = 'needs_placeholder'
RECONCILE_PROGRESS_EVERY_ROWS = 5000
RECONCILE_LINK_PROGRESS_EVERY_ROWS = 5000
# Determination can run tens of thousands of ORM checks; log by row count and wall time so operators see steady progress.
DETERMINATION_PROGRESS_EVERY_ROWS = 500
DETERMINATION_PROGRESS_MIN_INTERVAL_S = 20.0


def _determination_log_progress(
    *,
    scope: str,
    label: str,
    idx: int,
    total: int,
    stats: dict,
    started_mono: float,
    last_log_mono: list[float],
    movie_phase: bool,
) -> None:
    """Emit a progress line when row interval or min wall time since last log is reached."""
    if total <= 0 or idx <= 0:
        return
    now = time.monotonic()
    if idx % DETERMINATION_PROGRESS_EVERY_ROWS != 0 and (now - last_log_mono[0]) < DETERMINATION_PROGRESS_MIN_INTERVAL_S:
        return
    last_log_mono[0] = now
    updates = int(stats.get("movies_changed", 0) or 0) if movie_phase else int(stats.get("episodes_changed", 0) or 0)
    logger.info(
        f"Determination · {scope} · {label}: {idx}/{total} checked · "
        f"{'movie' if movie_phase else 'episode'}_rows_updated={updates} · "
        f"obsolete_placeholder={int(stats.get('obsolete_placeholder', 0) or 0)} · "
        f"elapsed_s={now - started_mono:.1f}",
        extra={'emoji_type': 'info'},
    )


def _normalize_placeholder_path(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return os.path.normpath(text)


def _movie_placeholder_path_drifts(movie: Movie) -> bool:
    """True when DB says we have a placeholder file but stored path != canonical path."""
    if getattr(movie, 'has_file', False) or getattr(movie, 'is_deleted', False):
        return False
    stored = getattr(movie, 'placeholder_filepath', None)
    if not isinstance(stored, str) or not stored.strip():
        return False
    expected = movie_placeholder_path(movie)
    exp = _normalize_placeholder_path(expected)
    cur = _normalize_placeholder_path(stored)
    return bool(exp and cur and exp != cur)


def _episode_placeholder_path_drifts(session, episode: Episode) -> bool:
    if getattr(episode, 'has_file', False) or getattr(episode, 'is_deleted', False):
        return False
    stored = getattr(episode, 'placeholder_filepath', None)
    if not isinstance(stored, str) or not stored.strip():
        return False
    season = session.query(Season).filter(Season.id == episode.season_id).first()
    if not season:
        return False
    series = session.query(Series).filter(Series.id == season.series_id).first()
    if not series:
        return False
    expected = episode_placeholder_path(episode, season, series)
    exp = _normalize_placeholder_path(expected)
    cur = _normalize_placeholder_path(stored)
    return bool(exp and cur and exp != cur)


def _resolve_movie_determination(
    movie: Movie,
    *,
    placeholders_enabled: bool,
    lookahead_days: int,
    now_date: date,
) -> tuple[str, bool]:
    """Return (determination_value, path_drift_detected)."""
    base = _compute_determination(
        bool(getattr(movie, 'has_placeholder', False)),
        bool(getattr(movie, 'has_file', False)),
        bool(getattr(movie, 'is_deleted', False)),
        target_date=_preferred_movie_release_date(movie),
        release_status=getattr(movie, 'radarr_release_status', None),
        lookahead_days=lookahead_days,
        placeholders_enabled=placeholders_enabled,
        now_date=now_date,
    )
    if _movie_placeholder_path_drifts(movie):
        return DETERMINATION_OBSOLETE, True
    return base, False


def _resolve_episode_determination(
    session,
    episode: Episode,
    *,
    placeholders_enabled: bool,
    lookahead_days: int,
    now_date: date,
    episode_order_meta: tuple[int, int, int] | None = None,
    series_max_known_order_within_horizon: dict[int, tuple[int, int]] | None = None,
) -> tuple[str, bool]:
    target_date = getattr(episode, 'air_date', None)
    if (
        target_date is None
        and placeholders_enabled
        and lookahead_days >= 0
        and episode_order_meta is not None
        and series_max_known_order_within_horizon
    ):
        # Unknown-air-date episodes in the middle of a run can be safely treated as
        # in-window once we already know a later episode date inside lookahead.
        series_id, season_number, episode_number = episode_order_meta
        max_known_order = series_max_known_order_within_horizon.get(int(series_id))
        if max_known_order is not None and max_known_order > (int(season_number), int(episode_number)):
            target_date = now_date

    base = _compute_determination(
        bool(getattr(episode, 'has_placeholder', False)),
        bool(getattr(episode, 'has_file', False)),
        bool(getattr(episode, 'is_deleted', False)),
        target_date=target_date,
        lookahead_days=lookahead_days,
        placeholders_enabled=placeholders_enabled,
        now_date=now_date,
    )
    if _episode_placeholder_path_drifts(session, episode):
        return DETERMINATION_OBSOLETE, True
    return base, False


def _preferred_movie_release_date(movie: Movie) -> date | None:
    """Return only the configured preferred movie release date.

    Strict behavior: do not fallback to other release types.
    """
    preferred = str(getattr(settings, 'PREFERRED_MOVIE_DATE_TYPE', 'inCinemas') or 'inCinemas').strip()
    mapping = {
        'inCinemas': 'theater_release_date',
        'digitalRelease': 'digital_release_date',
        'physicalRelease': 'physical_release_date',
    }
    preferred_field = mapping.get(preferred, 'theater_release_date')
    candidate = getattr(movie, preferred_field, None)
    if candidate:
        return candidate
    return None


def run_placeholder_link_reconcile() -> dict:
    """Reset and rebuild placeholder linkage on Movie/Episode rows.

    This is the authoritative reconciliation pass for derived placeholder fields:
    - clear Movie/Episode has_placeholder and placeholder_filepath
    - re-link from active Placeholder rows (has_placeholder=True)
    """
    session = get_session()
    started_mono = time.monotonic()
    stats = {
        'movies_reset': 0,
        'episodes_reset': 0,
        'movies_linked': 0,
        'episodes_linked': 0,
        'invalid_placeholders_marked_missing': 0,
        'invalid_placeholders_disconnected': 0,
        'movie_placeholders_seen': 0,
        'episode_placeholders_seen': 0,
        # Linked rows whose on-disk path was valid but differed from current canonical path
        # (typical after Radarr folder rename / root change while the dummy file stayed behind).
        'canonical_path_realigned': 0,
    }

    def _disconnect_placeholder_row(row: Placeholder) -> bool:
        changed = False
        fields_to_clear = (
            'plex_placeholder_id',
            'jellyfin_placeholder_id',
            'emby_placeholder_id',
            'plex_id_observed_at',
            'jellyfin_id_observed_at',
            'emby_id_observed_at',
            'media_lookup_error',
            'media_lookup_last_attempt_at',
        )
        for field in fields_to_clear:
            if hasattr(row, field) and getattr(row, field, None) is not None:
                setattr(row, field, None)
                changed = True
        return changed

    try:
        logger.info("Placeholder reconcile started", extra={'emoji_type': 'info'})
        # Reset derived state first so stale true flags cannot survive crashes.
        stats['movies_reset'] = session.query(Movie).update(
            {
                Movie.has_placeholder: False,
                Movie.placeholder_filepath: None,
            },
            synchronize_session=False,
        )
        stats['episodes_reset'] = session.query(Episode).update(
            {
                Episode.has_placeholder: False,
                Episode.placeholder_filepath: None,
            },
            synchronize_session=False,
        )

        # Validate active placeholder rows before relinking.
        roots = [os.path.abspath(root) for root in configured_roots() if root]
        active_rows = session.query(Placeholder).filter(Placeholder.has_placeholder == True).all()  # noqa: E712
        for idx, row in enumerate(active_rows, start=1):
            path = getattr(row, 'path', None)
            if not path:
                row.has_placeholder = False
                if hasattr(row, 'lifecycle_status'):
                    row.lifecycle_status = 'MISSING'
                if _disconnect_placeholder_row(row):
                    stats['invalid_placeholders_disconnected'] += 1
                session.add(row)
                stats['invalid_placeholders_marked_missing'] += 1
                continue

            abs_path = os.path.abspath(path)
            exists = os.path.isfile(abs_path)
            in_scope = False
            for root in roots:
                try:
                    if os.path.commonpath([abs_path, root]) == root:
                        in_scope = True
                        break
                except Exception:
                    continue

            if not exists or not in_scope:
                remapped = False
                file_name = os.path.basename(abs_path)

                try:
                    if getattr(row, 'movie_id', None):
                        mv = session.query(Movie).filter(Movie.id == int(row.movie_id)).first()
                        mv_folder = getattr(mv, 'placeholder_folder', None) if mv else None
                        if mv_folder:
                            candidate = os.path.join(mv_folder, file_name)
                            if os.path.isfile(candidate):
                                row.path = candidate
                                row.has_placeholder = True
                                if hasattr(row, 'last_observed_at'):
                                    row.last_observed_at = func.now()
                                remapped = True
                    elif getattr(row, 'episode_id', None):
                        ep = session.query(Episode).filter(Episode.id == int(row.episode_id)).first()
                        ep_folder = getattr(ep, 'placeholder_folder', None) if ep else None
                        if ep_folder:
                            candidate = os.path.join(ep_folder, file_name)
                            if os.path.isfile(candidate):
                                row.path = candidate
                                row.has_placeholder = True
                                if hasattr(row, 'last_observed_at'):
                                    row.last_observed_at = func.now()
                                remapped = True
                except Exception:
                    remapped = False

                if not remapped:
                    try:
                        if getattr(row, 'movie_id', None):
                            mv2 = session.query(Movie).filter(Movie.id == int(row.movie_id)).first()
                            if mv2:
                                candidate = os.path.abspath(movie_placeholder_path(mv2))
                                if os.path.isfile(candidate):
                                    for root in roots:
                                        try:
                                            if os.path.commonpath([candidate, root]) == root:
                                                row.path = candidate
                                                row.has_placeholder = True
                                                if hasattr(row, 'last_observed_at'):
                                                    row.last_observed_at = func.now()
                                                remapped = True
                                                break
                                        except Exception:
                                            continue
                        elif getattr(row, 'episode_id', None):
                            ep2 = session.query(Episode).filter(Episode.id == int(row.episode_id)).first()
                            if ep2:
                                season2 = session.query(Season).filter(Season.id == ep2.season_id).first()
                                series2 = (
                                    session.query(Series).filter(Series.id == season2.series_id).first()
                                    if season2
                                    else None
                                )
                                if season2 and series2:
                                    candidate = os.path.abspath(
                                        episode_placeholder_path(ep2, season2, series2)
                                    )
                                    if os.path.isfile(candidate):
                                        for root in roots:
                                            try:
                                                if os.path.commonpath([candidate, root]) == root:
                                                    row.path = candidate
                                                    row.has_placeholder = True
                                                    if hasattr(row, 'last_observed_at'):
                                                        row.last_observed_at = func.now()
                                                    remapped = True
                                                    break
                                            except Exception:
                                                continue
                    except Exception:
                        pass

                if not remapped:
                    row.has_placeholder = False
                    if hasattr(row, 'lifecycle_status'):
                        row.lifecycle_status = 'MISSING'
                    if _disconnect_placeholder_row(row):
                        stats['invalid_placeholders_disconnected'] += 1
                    stats['invalid_placeholders_marked_missing'] += 1

                if hasattr(row, 'updated_at'):
                    row.updated_at = func.now()
                session.add(row)
            else:
                # Path is valid today, but Radarr/metadata may have moved the canonical folder while
                # the dummy file still lives at the previous path. Leaving the stale path linked
                # forces ``_movie_placeholder_path_drifts`` / episode drift → obsolete_placeholder on
                # every determination until cleanup. Prefer the current canonical path when it exists.
                try:
                    if getattr(row, 'movie_id', None):
                        mv_align = session.query(Movie).filter(Movie.id == int(row.movie_id)).first()
                        if mv_align:
                            canonical = os.path.abspath(movie_placeholder_path(mv_align))
                            norm_cur = _normalize_placeholder_path(abs_path)
                            norm_can = _normalize_placeholder_path(canonical)
                            if (
                                canonical
                                and norm_can
                                and norm_cur
                                and norm_can != norm_cur
                                and os.path.isfile(canonical)
                            ):
                                canon_in_scope = False
                                for root in roots:
                                    try:
                                        if os.path.commonpath([canonical, root]) == root:
                                            canon_in_scope = True
                                            break
                                    except Exception:
                                        continue
                                if canon_in_scope:
                                    row.path = canonical
                                    row.has_placeholder = True
                                    if hasattr(row, 'last_observed_at'):
                                        row.last_observed_at = func.now()
                                    if hasattr(row, 'updated_at'):
                                        row.updated_at = func.now()
                                    session.add(row)
                                    stats['canonical_path_realigned'] += 1
                    elif getattr(row, 'episode_id', None):
                        ep_align = session.query(Episode).filter(Episode.id == int(row.episode_id)).first()
                        if ep_align:
                            season_align = session.query(Season).filter(Season.id == ep_align.season_id).first()
                            series_align = (
                                session.query(Series).filter(Series.id == season_align.series_id).first()
                                if season_align
                                else None
                            )
                            if season_align and series_align:
                                canonical = os.path.abspath(
                                    episode_placeholder_path(ep_align, season_align, series_align)
                                )
                                norm_cur = _normalize_placeholder_path(abs_path)
                                norm_can = _normalize_placeholder_path(canonical)
                                if (
                                    canonical
                                    and norm_can
                                    and norm_cur
                                    and norm_can != norm_cur
                                    and os.path.isfile(canonical)
                                ):
                                    canon_in_scope = False
                                    for root in roots:
                                        try:
                                            if os.path.commonpath([canonical, root]) == root:
                                                canon_in_scope = True
                                                break
                                        except Exception:
                                            continue
                                    if canon_in_scope:
                                        row.path = canonical
                                        row.has_placeholder = True
                                        if hasattr(row, 'last_observed_at'):
                                            row.last_observed_at = func.now()
                                        if hasattr(row, 'updated_at'):
                                            row.updated_at = func.now()
                                        session.add(row)
                                        stats['canonical_path_realigned'] += 1
                except Exception:
                    pass
            if idx % RECONCILE_PROGRESS_EVERY_ROWS == 0:
                elapsed = time.monotonic() - started_mono
                logger.info(
                    "Placeholder reconcile progress (validate): "
                    f"checked={idx}/{len(active_rows)} "
                    f"invalid_marked_missing={stats['invalid_placeholders_marked_missing']} "
                    f"disconnected={stats['invalid_placeholders_disconnected']} "
                    f"elapsed_s={elapsed:.1f}",
                    extra={'emoji_type': 'info'},
                )

        # Build canonical path per linked Movie from active Placeholder rows.
        movie_rows = (
            session.query(Placeholder.movie_id, Placeholder.path)
            .filter(
                Placeholder.has_placeholder == True,  # noqa: E712
                Placeholder.movie_id.isnot(None),
            )
            .order_by(Placeholder.last_observed_at.desc(), Placeholder.id.desc())
            .all()
        )
        stats['movie_placeholders_seen'] = len(movie_rows)
        movie_path_by_id: dict[int, str | None] = {}
        for movie_id, path in movie_rows:
            if movie_id not in movie_path_by_id:
                movie_path_by_id[movie_id] = path

        if movie_path_by_id:
            movies = session.query(Movie).filter(Movie.id.in_(list(movie_path_by_id.keys()))).all()
            for idx, movie in enumerate(movies, start=1):
                movie.has_placeholder = True
                movie.placeholder_filepath = movie_path_by_id.get(movie.id)
                session.add(movie)
                stats['movies_linked'] += 1
                if idx % RECONCILE_LINK_PROGRESS_EVERY_ROWS == 0:
                    elapsed = time.monotonic() - started_mono
                    logger.info(
                        "Placeholder reconcile progress (movies link): "
                        f"linked={idx}/{len(movies)} "
                        f"elapsed_s={elapsed:.1f}",
                        extra={'emoji_type': 'info'},
                    )

        # Build canonical path per linked Episode from active Placeholder rows.
        episode_rows = (
            session.query(Placeholder.episode_id, Placeholder.path)
            .filter(
                Placeholder.has_placeholder == True,  # noqa: E712
                Placeholder.episode_id.isnot(None),
            )
            .order_by(Placeholder.last_observed_at.desc(), Placeholder.id.desc())
            .all()
        )
        stats['episode_placeholders_seen'] = len(episode_rows)
        episode_path_by_id: dict[int, str | None] = {}
        for episode_id, path in episode_rows:
            if episode_id not in episode_path_by_id:
                episode_path_by_id[episode_id] = path

        if episode_path_by_id:
            episodes = session.query(Episode).filter(Episode.id.in_(list(episode_path_by_id.keys()))).all()
            for idx, episode in enumerate(episodes, start=1):
                episode.has_placeholder = True
                episode.placeholder_filepath = episode_path_by_id.get(episode.id)
                session.add(episode)
                stats['episodes_linked'] += 1
                if idx % RECONCILE_LINK_PROGRESS_EVERY_ROWS == 0:
                    elapsed = time.monotonic() - started_mono
                    logger.info(
                        "Placeholder reconcile progress (episodes link): "
                        f"linked={idx}/{len(episodes)} "
                        f"elapsed_s={elapsed:.1f}",
                        extra={'emoji_type': 'info'},
                    )

        session.commit()
        elapsed = time.monotonic() - started_mono
        logger.info(
            f"Placeholder reconcile elapsed_s={elapsed:.1f}",
            extra={'emoji_type': 'info'},
        )
        logger.info(f"Placeholder reconcile complete: {stats}", extra={'emoji_type': 'success'})
        return stats
    except Exception as e:
        session.rollback()
        logger.error(f"Placeholder reconcile failed: {e}", extra={'emoji_type': 'error'})
        raise
    finally:
        session.close()


def _compute_determination(
    has_placeholder: bool,
    has_file: bool,
    is_deleted: bool,
    *,
    target_date: date | None = None,
    release_status: str | None = None,
    lookahead_days: int | None = None,
    placeholders_enabled: bool | None = None,
    now_date: date | None = None,
) -> str:
    """Return canonical determination from content flags.

    Rules:
    - obsolete_placeholder: placeholder exists but file now exists or item is deleted
    - not_needed: no placeholder exists and item has file or is deleted
    - placeholder_exists: placeholder exists and item still needs placeholder state
    - needs_placeholder: no placeholder and no real file and not deleted
    """
    # Calendar lookahead guard semantics:
    # - lookahead < 0  => infinite
    # - lookahead == 0 => disabled/off for future placeholders
    # - lookahead > 0  => strict horizon in days
    if placeholders_enabled is True and lookahead_days is not None and not has_file and not is_deleted:
        effective_now = now_date or datetime.now(timezone.utc).date()
        lookahead = int(lookahead_days)

        if lookahead < 0:
            # Infinite mode keeps normal lifecycle behavior, including unknown dates.
            pass
        elif target_date is None:
            # Strict mode suppresses future placeholders when the selected movie
            # release date is unknown, but already-released movies should still
            # participate in normal placeholder lifecycle rules.
            if str(release_status or "").strip().lower() != 'released':
                return DETERMINATION_OBSOLETE if has_placeholder else DETERMINATION_NOT_NEEDED
        else:
            days_until = (target_date - effective_now).days
            if lookahead == 0:
                # Off mode: suppress all future placeholders.
                if days_until > 0:
                    return DETERMINATION_OBSOLETE if has_placeholder else DETERMINATION_NOT_NEEDED
            elif days_until > lookahead:
                return DETERMINATION_OBSOLETE if has_placeholder else DETERMINATION_NOT_NEEDED

    if has_placeholder and (has_file or is_deleted):
        return DETERMINATION_OBSOLETE
    if has_file or is_deleted:
        return DETERMINATION_NOT_NEEDED
    if has_placeholder:
        return DETERMINATION_EXISTS
    return DETERMINATION_NEEDS


def _classify_episode_not_needed_bucket(episode: Episode, *, now_date: date) -> str | None:
    """Classify user-facing not_needed buckets for episode rows."""
    has_file = bool(getattr(episode, 'has_file', False))
    is_deleted = bool(getattr(episode, 'is_deleted', False))
    if has_file or is_deleted:
        return None
    target_date = getattr(episode, 'air_date', None)
    if target_date is None:
        return 'not_needed_air_date_unknown'
    if target_date > now_date:
        return 'not_needed_not_yet_aired'
    return None


def run_determination_pass() -> dict:
    """Compute and persist determinations for movies and episodes.

    This phase is intentionally pure DB-state evaluation and does not perform
    side effects like file creation/deletion yet.
    """
    session = get_session()
    stats = {
        'movies_total': 0,
        'movies_changed': 0,
        'episodes_total': 0,
        'episodes_changed': 0,
        'obsolete_placeholder': 0,
        'not_needed': 0,
        'placeholder_exists': 0,
        'needs_placeholder': 0,
        'not_needed_not_yet_aired': 0,
        'not_needed_air_date_unknown': 0,
        'path_drift_movies': 0,
        'path_drift_episodes': 0,
    }

    try:
        started_mono = time.monotonic()
        last_log_movies: list[float] = [started_mono]
        last_log_episodes: list[float] = [started_mono]
        placeholders_enabled = bool(settings.coming_soon_placeholders_enabled)
        lookahead_days = int(getattr(settings, 'CALENDAR_LOOKAHEAD_DAYS', 30) or 30)
        now_date = datetime.now(timezone.utc).date()

        movies = session.query(Movie).all()
        stats['movies_total'] = len(movies)
        logger.info(
            f"Determination · full_scan · movies: {len(movies)} rows to check",
            extra={'emoji_type': 'info'},
        )
        for idx, movie in enumerate(movies, start=1):
            value, path_drift = _resolve_movie_determination(
                movie,
                placeholders_enabled=placeholders_enabled,
                lookahead_days=lookahead_days,
                now_date=now_date,
            )
            if path_drift:
                stats['path_drift_movies'] += 1
                logger.debug(
                    f'Placeholder path drift movie_id={movie.id} tmdbid={getattr(movie, "tmdbid", None)} '
                    f'expected={movie_placeholder_path(movie)!r} stored={getattr(movie, "placeholder_filepath", None)!r}',
                    extra={'emoji_type': 'debug'},
                )
            stats[value] += 1
            if getattr(movie, 'determination', None) != value:
                movie.determination = value
                movie.determination_updated_at = func.now()
                session.add(movie)
                stats['movies_changed'] += 1
            _determination_log_progress(
                scope="full_scan",
                label="movies",
                idx=idx,
                total=len(movies),
                stats=stats,
                started_mono=started_mono,
                last_log_mono=last_log_movies,
                movie_phase=True,
            )

        include_specials = bool(getattr(settings, 'INCLUDE_SPECIALS', False))
        episodes = session.query(Episode).all()
        stats['episodes_total'] = len(episodes)
        logger.info(
            f"Determination · full_scan · episodes: {len(episodes)} rows to check",
            extra={'emoji_type': 'info'},
        )
        episode_ids = [int(getattr(ep, "id")) for ep in episodes if getattr(ep, "id", None) is not None]
        season_rows = (
            session.query(Episode.id, Season.series_id, Season.season_number, Episode.episode_number)
            .join(Season, Episode.season_id == Season.id)
            .filter(Episode.id.in_(episode_ids))
            .all()
            if episode_ids
            else []
        )
        episode_order_meta_by_id: dict[int, tuple[int, int, int]] = {}
        series_ids: set[int] = set()
        for eid, series_id, season_number, episode_number in season_rows:
            if eid is None or series_id is None:
                continue
            ep_meta = (
                int(series_id),
                int(season_number or 0),
                int(episode_number or 0),
            )
            episode_order_meta_by_id[int(eid)] = ep_meta
            series_ids.add(int(series_id))

        horizon = now_date + timedelta(days=int(lookahead_days)) if int(lookahead_days) >= 0 else None
        known_rows = (
            session.query(Season.series_id, Season.season_number, Episode.episode_number)
            .join(Season, Episode.season_id == Season.id)
            .filter(
                Season.series_id.in_(list(series_ids)),
                Episode.air_date.isnot(None),
                Episode.is_deleted == False,  # noqa: E712
            )
            .filter(Episode.air_date <= horizon if horizon is not None else True)
            .all()
            if series_ids
            else []
        )
        series_max_known_order_within_horizon: dict[int, tuple[int, int]] = {}
        for series_id, season_number, episode_number in known_rows:
            if series_id is None:
                continue
            order = (int(season_number or 0), int(episode_number or 0))
            sid = int(series_id)
            prev = series_max_known_order_within_horizon.get(sid)
            if prev is None or order > prev:
                series_max_known_order_within_horizon[sid] = order

        last_log_episodes[0] = time.monotonic()
        for idx, episode in enumerate(episodes, start=1):
            episode_meta = episode_order_meta_by_id.get(int(episode.id)) if getattr(episode, "id", None) is not None else None
            season_number = int(episode_meta[1]) if episode_meta is not None else -1
            # Treat season 0 episodes as not_needed when specials are disabled
            if not include_specials:
                if season_number == 0:
                    value = DETERMINATION_NOT_NEEDED
                    stats[value] += 1
                    if getattr(episode, 'determination', None) != value:
                        episode.determination = value
                        episode.determination_updated_at = func.now()
                        session.add(episode)
                        stats['episodes_changed'] += 1
                    _determination_log_progress(
                        scope="full_scan",
                        label="episodes",
                        idx=idx,
                        total=len(episodes),
                        stats=stats,
                        started_mono=started_mono,
                        last_log_mono=last_log_episodes,
                        movie_phase=False,
                    )
                    continue
            value, path_drift = _resolve_episode_determination(
                session,
                episode,
                placeholders_enabled=placeholders_enabled,
                lookahead_days=lookahead_days,
                now_date=now_date,
                episode_order_meta=episode_meta,
                series_max_known_order_within_horizon=series_max_known_order_within_horizon,
            )
            if path_drift:
                stats['path_drift_episodes'] += 1
                season = session.query(Season).filter(Season.id == episode.season_id).first()
                series = (
                    session.query(Series).filter(Series.id == season.series_id).first()
                    if season
                    else None
                )
                exp_repr = (
                    episode_placeholder_path(episode, season, series)
                    if season and series
                    else None
                )
                logger.debug(
                    f'Placeholder path drift episode_id={episode.id} stored={getattr(episode, "placeholder_filepath", None)!r} '
                    f'expected={exp_repr!r}',
                    extra={'emoji_type': 'debug'},
                )
            stats[value] += 1
            if value == DETERMINATION_NOT_NEEDED:
                bucket = _classify_episode_not_needed_bucket(episode, now_date=now_date)
                if bucket:
                    stats[bucket] += 1
            if getattr(episode, 'determination', None) != value:
                episode.determination = value
                episode.determination_updated_at = func.now()
                session.add(episode)
                stats['episodes_changed'] += 1
            _determination_log_progress(
                scope="full_scan",
                label="episodes",
                idx=idx,
                total=len(episodes),
                stats=stats,
                started_mono=started_mono,
                last_log_mono=last_log_episodes,
                movie_phase=False,
            )

        session.commit()
        logger.info(f"Determination · full_scan · complete: {stats}", extra={'emoji_type': 'success'})
        return stats
    except Exception as e:
        session.rollback()
        logger.error(f"Determination · full_scan · failed: {e}", extra={'emoji_type': 'error'})
        raise
    finally:
        session.close()


def run_determination_for_entities(
    movie_ids: list[int] | None = None,
    episode_ids: list[int] | None = None,
) -> dict:
    """Compute and persist determinations for a scoped set of entities.

    This is used by event-driven workflows (e.g. *_add) so we can reuse
    the same canonical determination logic without scanning entire tables.
    """
    movie_ids = [int(mid) for mid in (movie_ids or []) if mid is not None]
    episode_ids = [int(eid) for eid in (episode_ids or []) if eid is not None]

    session = get_session()
    try:
        stats = run_determination_for_entities_in_session(
            session,
            movie_ids=movie_ids,
            episode_ids=episode_ids,
        )
        session.commit()
        logger.info(f"Determination · scoped · complete: {stats}", extra={'emoji_type': 'success'})
        return stats
    except Exception as e:
        session.rollback()
        logger.error(f"Determination · scoped · failed: {e}", extra={'emoji_type': 'error'})
        raise
    finally:
        session.close()


def run_determination_for_entities_in_session(
    session,
    movie_ids: list[int] | None = None,
    episode_ids: list[int] | None = None,
) -> dict:
    movie_ids = [int(mid) for mid in (movie_ids or []) if mid is not None]
    episode_ids = [int(eid) for eid in (episode_ids or []) if eid is not None]

    stats = {
        'movies_total': 0,
        'movies_changed': 0,
        'episodes_total': 0,
        'episodes_changed': 0,
        'obsolete_placeholder': 0,
        'not_needed': 0,
        'placeholder_exists': 0,
        'needs_placeholder': 0,
        'not_needed_not_yet_aired': 0,
        'not_needed_air_date_unknown': 0,
        'path_drift_movies': 0,
        'path_drift_episodes': 0,
    }

    started_mono = time.monotonic()
    last_log_movies: list[float] = [started_mono]
    last_log_episodes: list[float] = [started_mono]

    placeholders_enabled = bool(settings.coming_soon_placeholders_enabled)
    lookahead_days = int(getattr(settings, 'CALENDAR_LOOKAHEAD_DAYS', 30) or 30)
    now_date = datetime.now(timezone.utc).date()

    movies_q = session.query(Movie)
    if movie_ids:
        movies_q = movies_q.filter(Movie.id.in_(movie_ids))
    else:
        movies_q = movies_q.filter(Movie.id == -1)
    movies = movies_q.all()
    stats['movies_total'] = len(movies)
    if movies:
        logger.info(
            f"Determination · scoped · movies: {len(movies)} rows to check",
            extra={'emoji_type': 'info'},
        )

    for idx, movie in enumerate(movies, start=1):
        value, path_drift = _resolve_movie_determination(
            movie,
            placeholders_enabled=placeholders_enabled,
            lookahead_days=lookahead_days,
            now_date=now_date,
        )
        if path_drift:
            stats['path_drift_movies'] += 1
        stats[value] += 1
        if getattr(movie, 'determination', None) != value:
            movie.determination = value
            movie.determination_updated_at = func.now()
            session.add(movie)
            stats['movies_changed'] += 1
        _determination_log_progress(
            scope="scoped",
            label="movies",
            idx=idx,
            total=len(movies),
            stats=stats,
            started_mono=started_mono,
            last_log_mono=last_log_movies,
            movie_phase=True,
        )

    include_specials = bool(getattr(settings, 'INCLUDE_SPECIALS', False))
    episodes_q = session.query(Episode)
    if episode_ids:
        episodes_q = episodes_q.filter(Episode.id.in_(episode_ids))
    else:
        episodes_q = episodes_q.filter(Episode.id == -1)
    episodes = episodes_q.all()
    stats['episodes_total'] = len(episodes)
    if episodes:
        logger.info(
            f"Determination · scoped · episodes: {len(episodes)} rows to check",
            extra={'emoji_type': 'info'},
        )
        last_log_episodes[0] = time.monotonic()

    scoped_episode_ids = [int(getattr(ep, "id")) for ep in episodes if getattr(ep, "id", None) is not None]
    scoped_season_rows = (
        session.query(Episode.id, Season.series_id, Season.season_number, Episode.episode_number)
        .join(Season, Episode.season_id == Season.id)
        .filter(Episode.id.in_(scoped_episode_ids))
        .all()
        if scoped_episode_ids
        else []
    )
    episode_order_meta_by_id: dict[int, tuple[int, int, int]] = {}
    series_ids: set[int] = set()
    for eid, series_id, season_number, episode_number in scoped_season_rows:
        if eid is None or series_id is None:
            continue
        ep_meta = (
            int(series_id),
            int(season_number or 0),
            int(episode_number or 0),
        )
        episode_order_meta_by_id[int(eid)] = ep_meta
        series_ids.add(int(series_id))

    horizon = now_date + timedelta(days=int(lookahead_days)) if int(lookahead_days) >= 0 else None
    known_rows = (
        session.query(Season.series_id, Season.season_number, Episode.episode_number)
        .join(Season, Episode.season_id == Season.id)
        .filter(
            Season.series_id.in_(list(series_ids)),
            Episode.air_date.isnot(None),
            Episode.is_deleted == False,  # noqa: E712
        )
        .filter(Episode.air_date <= horizon if horizon is not None else True)
        .all()
        if series_ids
        else []
    )
    series_max_known_order_within_horizon: dict[int, tuple[int, int]] = {}
    for series_id, season_number, episode_number in known_rows:
        if series_id is None:
            continue
        order = (int(season_number or 0), int(episode_number or 0))
        sid = int(series_id)
        prev = series_max_known_order_within_horizon.get(sid)
        if prev is None or order > prev:
            series_max_known_order_within_horizon[sid] = order

    for idx, episode in enumerate(episodes, start=1):
        episode_meta = episode_order_meta_by_id.get(int(episode.id)) if getattr(episode, "id", None) is not None else None
        season_number = int(episode_meta[1]) if episode_meta is not None else -1
        if not include_specials:
            if season_number == 0:
                value = DETERMINATION_NOT_NEEDED
                stats[value] += 1
                if getattr(episode, 'determination', None) != value:
                    episode.determination = value
                    episode.determination_updated_at = func.now()
                    session.add(episode)
                    stats['episodes_changed'] += 1
                _determination_log_progress(
                    scope="scoped",
                    label="episodes",
                    idx=idx,
                    total=len(episodes),
                    stats=stats,
                    started_mono=started_mono,
                    last_log_mono=last_log_episodes,
                    movie_phase=False,
                )
                continue

        value, path_drift = _resolve_episode_determination(
            session,
            episode,
            placeholders_enabled=placeholders_enabled,
            lookahead_days=lookahead_days,
            now_date=now_date,
            episode_order_meta=episode_meta,
            series_max_known_order_within_horizon=series_max_known_order_within_horizon,
        )
        if path_drift:
            stats['path_drift_episodes'] += 1
        stats[value] += 1
        if value == DETERMINATION_NOT_NEEDED:
            bucket = _classify_episode_not_needed_bucket(episode, now_date=now_date)
            if bucket:
                stats[bucket] += 1
        if getattr(episode, 'determination', None) != value:
            episode.determination = value
            episode.determination_updated_at = func.now()
            session.add(episode)
            stats['episodes_changed'] += 1
        _determination_log_progress(
            scope="scoped",
            label="episodes",
            idx=idx,
            total=len(episodes),
            stats=stats,
            started_mono=started_mono,
            last_log_mono=last_log_episodes,
            movie_phase=False,
        )

    return stats
