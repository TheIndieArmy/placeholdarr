from __future__ import annotations

import os
from datetime import date, datetime, timezone

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Placeholder, Season
from services.source_of_truth.filesystem import configured_roots


DETERMINATION_OBSOLETE = 'obsolete_placeholder'
DETERMINATION_NOT_NEEDED = 'not_needed'
DETERMINATION_EXISTS = 'placeholder_exists'
DETERMINATION_NEEDS = 'needs_placeholder'


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
    stats = {
        'movies_reset': 0,
        'episodes_reset': 0,
        'movies_linked': 0,
        'episodes_linked': 0,
        'invalid_placeholders_marked_missing': 0,
        'invalid_placeholders_disconnected': 0,
        'movie_placeholders_seen': 0,
        'episode_placeholders_seen': 0,
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
        for row in active_rows:
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
                    row.has_placeholder = False
                    if hasattr(row, 'lifecycle_status'):
                        row.lifecycle_status = 'MISSING'
                    if _disconnect_placeholder_row(row):
                        stats['invalid_placeholders_disconnected'] += 1
                    stats['invalid_placeholders_marked_missing'] += 1

                if hasattr(row, 'updated_at'):
                    row.updated_at = func.now()
                session.add(row)

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
            for movie in movies:
                movie.has_placeholder = True
                movie.placeholder_filepath = movie_path_by_id.get(movie.id)
                session.add(movie)
                stats['movies_linked'] += 1

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
            for episode in episodes:
                episode.has_placeholder = True
                episode.placeholder_filepath = episode_path_by_id.get(episode.id)
                session.add(episode)
                stats['episodes_linked'] += 1

        session.commit()
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
            # Strict mode: unknown selected release date is treated as out-of-window.
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
    }

    try:
        placeholders_enabled = bool(getattr(settings, 'ENABLE_COMING_SOON_PLACEHOLDERS', True))
        lookahead_days = int(getattr(settings, 'CALENDAR_LOOKAHEAD_DAYS', 30) or 30)
        now_date = datetime.now(timezone.utc).date()

        movies = session.query(Movie).all()
        stats['movies_total'] = len(movies)
        for movie in movies:
            value = _compute_determination(
                bool(getattr(movie, 'has_placeholder', False)),
                bool(getattr(movie, 'has_file', False)),
                bool(getattr(movie, 'is_deleted', False)),
                target_date=_preferred_movie_release_date(movie),
                lookahead_days=lookahead_days,
                placeholders_enabled=placeholders_enabled,
                now_date=now_date,
            )
            stats[value] += 1
            if getattr(movie, 'determination', None) != value:
                movie.determination = value
                movie.determination_updated_at = func.now()
                session.add(movie)
                stats['movies_changed'] += 1

        include_specials = bool(getattr(settings, 'INCLUDE_SPECIALS', False))
        episodes = session.query(Episode).all()
        stats['episodes_total'] = len(episodes)
        for episode in episodes:
            # Treat season 0 episodes as not_needed when specials are disabled
            if not include_specials:
                season = session.query(Season).filter(Season.id == episode.season_id).first()
                if season and int(getattr(season, 'season_number', -1)) == 0:
                    value = DETERMINATION_NOT_NEEDED
                    stats[value] += 1
                    if getattr(episode, 'determination', None) != value:
                        episode.determination = value
                        episode.determination_updated_at = func.now()
                        session.add(episode)
                        stats['episodes_changed'] += 1
                    continue
            value = _compute_determination(
                bool(getattr(episode, 'has_placeholder', False)),
                bool(getattr(episode, 'has_file', False)),
                bool(getattr(episode, 'is_deleted', False)),
                target_date=getattr(episode, 'air_date', None),
                lookahead_days=lookahead_days,
                placeholders_enabled=placeholders_enabled,
                now_date=now_date,
            )
            stats[value] += 1
            if getattr(episode, 'determination', None) != value:
                episode.determination = value
                episode.determination_updated_at = func.now()
                session.add(episode)
                stats['episodes_changed'] += 1

        session.commit()
        logger.info(f"Determination phase complete: {stats}", extra={'emoji_type': 'success'})
        return stats
    except Exception as e:
        session.rollback()
        logger.error(f"Determination phase failed: {e}", extra={'emoji_type': 'error'})
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
    stats = {
        'movies_total': 0,
        'movies_changed': 0,
        'episodes_total': 0,
        'episodes_changed': 0,
        'obsolete_placeholder': 0,
        'not_needed': 0,
        'placeholder_exists': 0,
        'needs_placeholder': 0,
    }

    try:
        placeholders_enabled = bool(getattr(settings, 'ENABLE_COMING_SOON_PLACEHOLDERS', True))
        lookahead_days = int(getattr(settings, 'CALENDAR_LOOKAHEAD_DAYS', 30) or 30)
        now_date = datetime.now(timezone.utc).date()

        movies_q = session.query(Movie)
        if movie_ids:
            movies_q = movies_q.filter(Movie.id.in_(movie_ids))
        else:
            movies_q = movies_q.filter(Movie.id == -1)
        movies = movies_q.all()
        stats['movies_total'] = len(movies)

        for movie in movies:
            value = _compute_determination(
                bool(getattr(movie, 'has_placeholder', False)),
                bool(getattr(movie, 'has_file', False)),
                bool(getattr(movie, 'is_deleted', False)),
                target_date=_preferred_movie_release_date(movie),
                lookahead_days=lookahead_days,
                placeholders_enabled=placeholders_enabled,
                now_date=now_date,
            )
            stats[value] += 1
            if getattr(movie, 'determination', None) != value:
                movie.determination = value
                movie.determination_updated_at = func.now()
                session.add(movie)
                stats['movies_changed'] += 1

        include_specials = bool(getattr(settings, 'INCLUDE_SPECIALS', False))
        episodes_q = session.query(Episode)
        if episode_ids:
            episodes_q = episodes_q.filter(Episode.id.in_(episode_ids))
        else:
            episodes_q = episodes_q.filter(Episode.id == -1)
        episodes = episodes_q.all()
        stats['episodes_total'] = len(episodes)

        for episode in episodes:
            if not include_specials:
                season = session.query(Season).filter(Season.id == episode.season_id).first()
                if season and int(getattr(season, 'season_number', -1)) == 0:
                    value = DETERMINATION_NOT_NEEDED
                    stats[value] += 1
                    if getattr(episode, 'determination', None) != value:
                        episode.determination = value
                        episode.determination_updated_at = func.now()
                        session.add(episode)
                        stats['episodes_changed'] += 1
                    continue

            value = _compute_determination(
                bool(getattr(episode, 'has_placeholder', False)),
                bool(getattr(episode, 'has_file', False)),
                bool(getattr(episode, 'is_deleted', False)),
                target_date=getattr(episode, 'air_date', None),
                lookahead_days=lookahead_days,
                placeholders_enabled=placeholders_enabled,
                now_date=now_date,
            )
            stats[value] += 1
            if getattr(episode, 'determination', None) != value:
                episode.determination = value
                episode.determination_updated_at = func.now()
                session.add(episode)
                stats['episodes_changed'] += 1

        session.commit()
        logger.info(f"Scoped determination complete: {stats}", extra={'emoji_type': 'success'})
        return stats
    except Exception as e:
        session.rollback()
        logger.error(f"Scoped determination failed: {e}", extra={'emoji_type': 'error'})
        raise
    finally:
        session.close()
