from dataclasses import dataclass
import os
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.placeholders import episode_placeholder_path, movie_placeholder_path
from services.postgres.models import ArrState, Episode, Movie, Placeholder, Season, Series
from services.source_of_truth.arr_api import (
    fetch_radarr_movies,
    fetch_sonarr_episodes,
    fetch_sonarr_series,
)
from services.source_of_truth.calendar_phase import run_calendar_phase
from services.source_of_truth.calendar_date_refresh import run_calendar_date_refresh
from services.source_of_truth.determiner import (
    run_determination_for_entities,
    run_determination_pass,
    run_placeholder_link_reconcile,
)
from services.source_of_truth.filesystem import scan_once_if_needed
from services.source_of_truth.materializer import run_materialization_for_entities, run_materialization_pass
from services.source_of_truth.placeholder_cleanup import run_orphan_placeholder_cleanup
from services.source_of_truth.sync_runner import run_full_sync, sync_radarr_movies_by_ids, sync_sonarr_series_by_ids


@dataclass
class FullSyncRunRef:
    run_id: str
    sync_stats: dict


def _normalize_path(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return os.path.normpath(text)


def _radarr_path_drift_movie_ids(session, *, instance_key: str, api_movies: list) -> set[int]:
    """Detect Radarr items whose current API path differs from stored DB path.

    ``api_movies`` must be the current Radarr ``/api/v3/movie`` payload for this instance (caller should
    fetch once per lite pass to avoid duplicate HTTP calls).
    """
    rows = (
        session.query(Movie.radarrid, Movie.radarrpath)
        .filter(Movie.instance_key == instance_key, Movie.radarrid.isnot(None), Movie.is_deleted == False)  # noqa: E712
        .all()
    )
    if not rows:
        return set()

    api_path_by_id: dict[int, str | None] = {}
    for item in api_movies:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        try:
            mid_int = int(mid)
        except Exception:
            continue
        api_path_by_id[mid_int] = _normalize_path(
            item.get("path") or item.get("folderPath") or item.get("rootFolderPath")
        )

    changed: set[int] = set()
    for row_id, row_path in rows:
        try:
            movie_id = int(row_id)
        except Exception:
            continue
        if movie_id not in api_path_by_id:
            continue
        if _normalize_path(row_path) != api_path_by_id[movie_id]:
            changed.add(movie_id)
    return changed


def _radarr_movie_ids_removed_from_catalog(session, *, instance_key: str, api_movies: list) -> set[int]:
    """Radarr IDs present in our DB for this instance but absent from the current Radarr catalog.

    Lite sync previously relied on ``/history`` + path drift; **removals** often do not yield a stable
    ``movieId`` in history payloads, so rows stayed ``is_deleted=False`` until the next full sync.
    """
    api_ids: set[int] = set()
    for item in api_movies or []:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        try:
            api_ids.add(int(mid))
        except Exception:
            continue
    rows = (
        session.query(Movie.radarrid)
        .filter(Movie.instance_key == instance_key, Movie.radarrid.isnot(None), Movie.is_deleted == False)  # noqa: E712
        .all()
    )
    out: set[int] = set()
    for (rid,) in rows:
        if rid is None:
            continue
        try:
            rint = int(rid)
        except Exception:
            continue
        if rint not in api_ids:
            out.add(rint)
    return out


def _sonarr_path_drift_series_ids(session, *, instance_key: str, api_series: list) -> set[int]:
    """Detect Sonarr series whose current API path differs from stored DB path.

    ``api_series`` must be the current Sonarr ``/api/v3/series`` payload for this instance.
    """
    rows = (
        session.query(Series.sonarrid, Series.sonarrpath)
        .filter(Series.instance_key == instance_key, Series.sonarrid.isnot(None), Series.is_deleted == False)  # noqa: E712
        .all()
    )
    if not rows:
        return set()

    api_path_by_id: dict[int, str | None] = {}
    for item in api_series:
        if not isinstance(item, dict):
            continue
        sid = item.get("id")
        try:
            sid_int = int(sid)
        except Exception:
            continue
        api_path_by_id[sid_int] = _normalize_path(item.get("path") or item.get("folderPath") or item.get("rootFolderPath"))

    changed: set[int] = set()
    for row_id, row_path in rows:
        try:
            series_id = int(row_id)
        except Exception:
            continue
        if series_id not in api_path_by_id:
            continue
        if _normalize_path(row_path) != api_path_by_id[series_id]:
            changed.add(series_id)
    return changed


def _sonarr_series_ids_removed_from_catalog(session, *, instance_key: str, api_series: list) -> set[int]:
    """Sonarr series IDs in our DB for this instance that no longer exist in the Sonarr catalog."""
    api_ids: set[int] = set()
    for item in api_series or []:
        if not isinstance(item, dict):
            continue
        sid = item.get("id")
        try:
            api_ids.add(int(sid))
        except Exception:
            continue
    rows = (
        session.query(Series.sonarrid)
        .filter(Series.instance_key == instance_key, Series.sonarrid.isnot(None), Series.is_deleted == False)  # noqa: E712
        .all()
    )
    out: set[int] = set()
    for (rid,) in rows:
        if rid is None:
            continue
        try:
            rint = int(rid)
        except Exception:
            continue
        if rint not in api_ids:
            out.add(rint)
    return out


def _create_run(content_type: str, is_secondary: bool = False, run_note: str | None = None, instance_key: str | None = None) -> FullSyncRunRef:
    suffix = 'secondary' if is_secondary else 'primary'
    run_id = f'fullsync:{content_type}:{suffix}:{uuid.uuid4()}'
    sync_types = ('movie',) if content_type == 'movie' else ('series',)
    logger.info(
        f"Starting startup {content_type} fullsync ({suffix}) run {run_id} ({run_note or 'no note'})",
        extra={'emoji_type': 'gear'},
    )
    sync_stats = run_full_sync(dry_run=False, types=sync_types, instance_key=instance_key)
    logger.info(f"Finished startup {content_type} fullsync ({suffix}) run {run_id}", extra={'emoji_type': 'success'})
    return FullSyncRunRef(run_id=run_id, sync_stats=sync_stats or {})


def capture_movies_fullsync_and_create_run(run_note: str | None = None) -> FullSyncRunRef:
    return _create_run('movie', is_secondary=False, run_note=run_note)


def capture_series_fullsync_and_create_run(run_note: str | None = None) -> FullSyncRunRef:
    return _create_run('series', is_secondary=False, run_note=run_note)


def _configured_arr_instances() -> list[dict]:
    instances: list[dict] = []
    for item in (getattr(settings, 'configured_arr_instances', []) or []):
        arr_type = str(item.get('arr_type') or '').strip().lower()
        instance_key = str(item.get('instance_key') or '').strip().lower()
        base_url = str(item.get('url') or '').strip()
        api_key = str(item.get('api_key') or '').strip()
        if arr_type not in {'radarr', 'sonarr'} or not instance_key or not base_url or not api_key:
            continue
        instances.append(
            {
                'instance_key': instance_key,
                'arr_type': arr_type,
                'content_type': 'movie' if arr_type == 'radarr' else 'series',
                'base_url': base_url,
                'api_key': api_key,
                'role': str(item.get('role') or 'primary').strip().lower() or 'primary',
            }
        )
    return instances


def _get_or_create_arr_state(session, instance_key: str, arr_type: str) -> ArrState:
    row = session.query(ArrState).filter(ArrState.instance_key == instance_key).first()
    if row:
        return row
    row = ArrState(instance_key=instance_key, arr_type=arr_type)
    session.add(row)
    session.flush()
    return row


def _api_movie_has_file(item: dict) -> bool:
    return bool(item.get("hasFile") or (item.get("movieFile") or {}).get("path"))


def _api_movie_file_path(item: dict) -> str | None:
    return _normalize_path((item.get("movieFile") or {}).get("path"))


def _api_movie_library_path(item: dict) -> str | None:
    return _normalize_path(item.get("path") or item.get("folderPath") or item.get("rootFolderPath"))


def _api_series_library_path(item: dict) -> str | None:
    return _normalize_path(item.get("path") or item.get("folderPath") or item.get("rootFolderPath"))


def _api_series_monitored(item: dict) -> bool:
    return bool(item.get("monitored"))


def _episode_monitored_signature(episode_rows: list[tuple[int, bool]]) -> int:
    """Sum of Sonarr episode ids that are monitored — stable lite-sync drift signal."""
    return sum(int(ep_id) for ep_id, monitored in episode_rows if monitored)


def _db_episode_monitored_rows_by_series(session, instance_key: str) -> dict[int, list[tuple[int, bool]]]:
    rows = (
        session.query(Series.sonarrid, Episode.sonarrid, Episode.sonarr_monitored)
        .join(Season, Season.series_id == Series.id)
        .join(Episode, Episode.season_id == Season.id)
        .filter(
            Series.instance_key == instance_key,
            Series.sonarrid.isnot(None),
            Series.is_deleted == False,  # noqa: E712
            Episode.is_deleted == False,  # noqa: E712
            Episode.sonarrid.isnot(None),
        )
        .all()
    )
    out: dict[int, list[tuple[int, bool]]] = {}
    for sid, eid, monitored in rows:
        try:
            sid_i = int(sid)
            eid_i = int(eid)
        except Exception:
            continue
        out.setdefault(sid_i, []).append((eid_i, bool(monitored)))
    return out


def _sonarr_episode_monitored_drift_series_ids(
    session,
    *,
    instance_key: str,
    base_url: str,
    api_key: str,
    candidate_series_ids: set[int],
) -> set[int]:
    """Series whose per-episode monitored flags differ between DB and Sonarr."""
    if not candidate_series_ids:
        return set()
    db_by_series = _db_episode_monitored_rows_by_series(session, instance_key)
    drift: set[int] = set()
    for sid in candidate_series_ids:
        db_rows = db_by_series.get(int(sid))
        if not db_rows:
            continue
        db_sig = _episode_monitored_signature(db_rows)
        api_eps = fetch_sonarr_episodes(int(sid), base_url, api_key) or []
        api_rows: list[tuple[int, bool]] = []
        for ep in api_eps:
            if not isinstance(ep, dict):
                continue
            try:
                eid = int(ep.get("id"))
            except Exception:
                continue
            api_rows.append((eid, bool(ep.get("monitored"))))
        if _episode_monitored_signature(api_rows) != db_sig:
            drift.add(int(sid))
    return drift


def _api_series_total_episode_count(item: dict) -> int | None:
    """Sonarr ``statistics.totalEpisodeCount`` — full episode catalog size (includes specials).

    Lite series catalog diff compares this to DB non-deleted episode row totals.
    """
    stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
    value = stats.get("totalEpisodeCount")
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _api_series_episode_file_count(item: dict) -> int | None:
    stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
    value = stats.get("episodeFileCount")
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


_LITE_SNAPSHOT_NAME_CAP = 100


def _radarr_item_title(item: dict | None, *, radarr_id: int | None = None) -> str:
    if isinstance(item, dict):
        t = str(item.get("title") or item.get("sortTitle") or "").strip()
        if t:
            return t
    return f"Movie id {radarr_id}" if radarr_id is not None else "Unknown movie"


def _sonarr_item_title(item: dict | None, *, sonarr_id: int | None = None) -> str:
    if isinstance(item, dict):
        t = str(item.get("title") or item.get("sortTitle") or "").strip()
        if t:
            return t
    return f"Series id {sonarr_id}" if sonarr_id is not None else "Unknown series"


def _lite_format_title_block(label: str, titles: list[str]) -> str:
    if not titles:
        return ""
    titles_sorted = sorted(titles, key=lambda s: s.casefold())
    if len(titles_sorted) <= _LITE_SNAPSHOT_NAME_CAP:
        joined = ", ".join(titles_sorted)
    else:
        joined = ", ".join(titles_sorted[:_LITE_SNAPSHOT_NAME_CAP]) + f" … and {len(titles_sorted) - _LITE_SNAPSHOT_NAME_CAP} more"
    return f"{label} ({len(titles)}): {joined}"


def _log_startup_lite_radarr_catalog_breakdown(
    session,
    *,
    instance_key: str,
    catalog_count: int,
    api_by_id: dict[int, dict],
    added_ids: set[int],
    changed_ids: set[int],
    drift_ids: set[int],
    removed_ids: set[int],
    prep_elapsed_s: float,
) -> None:
    title_by_id: dict[int, str] = {}
    for mid in added_ids | changed_ids | drift_ids:
        title_by_id[mid] = _radarr_item_title(api_by_id.get(mid), radarr_id=mid)
    if removed_ids:
        rows = (
            session.query(Movie.radarrid, Movie.title)
            .filter(Movie.instance_key == instance_key, Movie.radarrid.in_(list(removed_ids)))
            .all()
        )
        for rid, ttl in rows:
            if rid is not None:
                title_by_id[int(rid)] = str(ttl).strip() or _radarr_item_title(None, radarr_id=int(rid))
        for mid in removed_ids:
            title_by_id.setdefault(mid, _radarr_item_title(None, radarr_id=mid))

    logger.info(
        f"Startup lite · {instance_key} · movies: {catalog_count} titles in Radarr",
        extra={'emoji_type': 'info'},
    )
    for label, idset in (
        ("Added", added_ids),
        ("Updated", changed_ids),
        ("Library path changed", drift_ids),
        ("Removed from Radarr", removed_ids),
    ):
        if not idset:
            continue
        names = [title_by_id.get(i) or _radarr_item_title(None, radarr_id=i) for i in idset]
        logger.info(f"  {_lite_format_title_block(label, names)}", extra={'emoji_type': 'info'})
    n_target = len(added_ids | changed_ids | drift_ids | removed_ids)
    logger.info(
        f"  → Refreshing {n_target} movies · catalog check took {prep_elapsed_s:.1f}s",
        extra={'emoji_type': 'info'},
    )


def _log_startup_lite_sonarr_catalog_breakdown(
    session,
    *,
    instance_key: str,
    catalog_count: int,
    api_by_id: dict[int, dict],
    added_ids: set[int],
    changed_ids: set[int],
    drift_ids: set[int],
    removed_ids: set[int],
    prep_elapsed_s: float,
) -> None:
    title_by_id: dict[int, str] = {}
    for sid in added_ids | changed_ids | drift_ids:
        title_by_id[sid] = _sonarr_item_title(api_by_id.get(sid), sonarr_id=sid)
    if removed_ids:
        rows = (
            session.query(Series.sonarrid, Series.title)
            .filter(Series.instance_key == instance_key, Series.sonarrid.in_(list(removed_ids)))
            .all()
        )
        for rid, ttl in rows:
            if rid is not None:
                title_by_id[int(rid)] = str(ttl).strip() or _sonarr_item_title(None, sonarr_id=int(rid))
        for sid in removed_ids:
            title_by_id.setdefault(sid, _sonarr_item_title(None, sonarr_id=sid))

    logger.info(
        f"Startup lite · {instance_key} · TV shows: {catalog_count} series in Sonarr",
        extra={'emoji_type': 'info'},
    )
    for label, idset in (
        ("Added", added_ids),
        ("Updated", changed_ids),
        ("Library path changed", drift_ids),
        ("Removed from Sonarr", removed_ids),
    ):
        if not idset:
            continue
        names = [title_by_id.get(i) or _sonarr_item_title(None, sonarr_id=i) for i in idset]
        logger.info(f"  {_lite_format_title_block(label, names)}", extra={'emoji_type': 'info'})
    n_target = len(added_ids | changed_ids | drift_ids | removed_ids)
    logger.info(
        f"  → Refreshing {n_target} series · catalog check took {prep_elapsed_s:.1f}s",
        extra={'emoji_type': 'info'},
    )


def _refresh_placeholder_presence_for_entities(*, movie_row_ids: list[int], episode_row_ids: list[int]) -> dict:
    """Recompute has_placeholder/path for changed entities only (cheap startup-lite consistency check)."""
    stats = {
        "movies_checked": 0,
        "movies_updated": 0,
        "episodes_checked": 0,
        "episodes_updated": 0,
        "movie_placeholders_synced": 0,
        "episode_placeholders_synced": 0,
    }
    if not movie_row_ids and not episode_row_ids:
        return stats

    session = get_session()
    try:
        if movie_row_ids:
            movies = session.query(Movie).filter(Movie.id.in_(movie_row_ids)).all()
            for movie in movies:
                stats["movies_checked"] += 1
                expected_path = movie_placeholder_path(movie)
                exists = bool(expected_path and os.path.isfile(expected_path))
                new_path = expected_path if exists else None
                if bool(getattr(movie, "has_placeholder", False)) != exists or (
                    _normalize_path(getattr(movie, "placeholder_filepath", None)) != _normalize_path(new_path)
                ):
                    movie.has_placeholder = exists
                    movie.placeholder_filepath = new_path
                    movie.updated_at = func.now()
                    session.add(movie)
                    stats["movies_updated"] += 1
                linked_rows = session.query(Placeholder).filter(Placeholder.movie_id == int(movie.id)).all()
                for row in linked_rows:
                    row_changed = False
                    if bool(getattr(row, "has_placeholder", False)) != exists:
                        row.has_placeholder = exists
                        row_changed = True
                    if exists and new_path and _normalize_path(getattr(row, "path", None)) != _normalize_path(new_path):
                        row.path = new_path
                        row_changed = True
                    if exists and hasattr(row, "lifecycle_status") and str(getattr(row, "lifecycle_status", "") or "").upper() == "MISSING":
                        row.lifecycle_status = None
                        row_changed = True
                    if row_changed:
                        row.last_observed_at = func.now()
                        row.updated_at = func.now()
                        session.add(row)
                        stats["movie_placeholders_synced"] += 1

        if episode_row_ids:
            rows = (
                session.query(Episode, Season, Series)
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
                .filter(Episode.id.in_(episode_row_ids))
                .all()
            )
            for episode, season, series in rows:
                stats["episodes_checked"] += 1
                expected_path = episode_placeholder_path(episode, season, series)
                exists = bool(expected_path and os.path.isfile(expected_path))
                new_path = expected_path if exists else None
                if bool(getattr(episode, "has_placeholder", False)) != exists or (
                    _normalize_path(getattr(episode, "placeholder_filepath", None)) != _normalize_path(new_path)
                ):
                    episode.has_placeholder = exists
                    episode.placeholder_filepath = new_path
                    episode.updated_at = func.now()
                    session.add(episode)
                    stats["episodes_updated"] += 1
                linked_rows = session.query(Placeholder).filter(Placeholder.episode_id == int(episode.id)).all()
                for row in linked_rows:
                    row_changed = False
                    if bool(getattr(row, "has_placeholder", False)) != exists:
                        row.has_placeholder = exists
                        row_changed = True
                    if exists and new_path and _normalize_path(getattr(row, "path", None)) != _normalize_path(new_path):
                        row.path = new_path
                        row_changed = True
                    if exists and hasattr(row, "lifecycle_status") and str(getattr(row, "lifecycle_status", "") or "").upper() == "MISSING":
                        row.lifecycle_status = None
                        row_changed = True
                    if row_changed:
                        row.last_observed_at = func.now()
                        row.updated_at = func.now()
                        session.add(row)
                        stats["episode_placeholders_synced"] += 1

        session.commit()
        return stats
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _run_startup_full_for_instances(instances: list[dict], run_ids: list[str]) -> dict:
    stats = {'instances': 0, 'succeeded': 0, 'failed': 0, 'movies_seen': 0, 'series_seen': 0, 'episodes_seen': 0}
    if not instances:
        return stats

    session = get_session()
    try:
        for instance in instances:
            stats['instances'] += 1
            is_secondary = str(instance.get('role') or 'primary').strip().lower() != 'primary'
            suffix = 'secondary' if is_secondary else 'primary'
            try:
                run = _create_run(
                    instance['content_type'],
                    is_secondary=is_secondary,
                    run_note=f"startup {instance['arr_type']} {suffix} fullsync",
                    instance_key=instance['instance_key'],
                )
                run_ids.append(run.run_id)
                run_stats = run.sync_stats if isinstance(run.sync_stats, dict) else {}
                stats['movies_seen'] += int(run_stats.get('movies_seen', 0) or 0)
                stats['series_seen'] += int(run_stats.get('series_seen', 0) or 0)
                stats['episodes_seen'] += int(run_stats.get('episodes_seen', 0) or 0)
                row = _get_or_create_arr_state(session, instance['instance_key'], instance['arr_type'])
                row.first_full_sync_completed_at = datetime.now(timezone.utc)
                row.updated_at = datetime.now(timezone.utc)
                row.last_history_checked_at = datetime.now(timezone.utc)
                session.add(row)
                session.commit()
                stats['succeeded'] += 1
            except Exception as e:
                session.rollback()
                stats['failed'] += 1
                logger.error(
                    f"Startup full sync failed for {instance['instance_key']}: {e}",
                    extra={'emoji_type': 'error'},
                )
        return stats
    finally:
        session.close()


def _run_startup_lite_snapshot_for_instances(
    instances: list[dict],
    *,
    seed_movie_row_ids: set[int] | None = None,
    seed_episode_row_ids: set[int] | None = None,
    lite_reconciliation_pre: dict | None = None,
) -> dict:
    stats = {
        'instances': 0,
        'instances_failed': 0,
        'snapshot_rows_seen': 0,
        'targeted_sync_runs': 0,
        'path_drift_ids': 0,
        'movies_seen': 0,
        'series_seen': 0,
        'episodes_seen': 0,
        # DB rows reconciled as removed-from-ARR via catalog diff (not from history payloads).
        'movies_catalog_removed': 0,
        'series_catalog_removed': 0,
        'movies_marked_deleted': 0,
        'series_marked_deleted': 0,
        'movies_targeted': 0,
        'series_targeted': 0,
        'movie_row_ids': [],
        'episode_row_ids': [],
        'lite_reconciliation_pre': lite_reconciliation_pre or {},
    }
    if not instances:
        return stats

    session = get_session()
    touched_movie_row_ids: set[int] = set(seed_movie_row_ids or ())
    touched_episode_row_ids: set[int] = set(seed_episode_row_ids or ())
    try:
        for instance in instances:
            stats['instances'] += 1
            instance_key = instance['instance_key']
            try:
                row = _get_or_create_arr_state(session, instance_key, instance['arr_type'])
                t0 = time.monotonic()
                logger.info(
                    f"Startup lite · {instance_key} · {'movies' if instance['arr_type'] == 'radarr' else 'TV shows'} · catalog check",
                    extra={'emoji_type': 'info'},
                )

                if instance['arr_type'] == 'radarr':
                    api_movies = fetch_radarr_movies(instance['base_url'], instance['api_key']) or []
                    stats['snapshot_rows_seen'] += len(api_movies)
                    drift_ids = _radarr_path_drift_movie_ids(session, instance_key=instance_key, api_movies=api_movies)
                    removed_ids = _radarr_movie_ids_removed_from_catalog(session, instance_key=instance_key, api_movies=api_movies)
                    api_by_id: dict[int, dict] = {}
                    for item in api_movies:
                        if not isinstance(item, dict):
                            continue
                        mid = item.get("id")
                        try:
                            api_by_id[int(mid)] = item
                        except Exception:
                            continue

                    db_rows = (
                        session.query(
                            Movie.radarrid,
                            Movie.has_file,
                            Movie.radarrpath,
                            Movie.radarr_filepath,
                            Movie.radarr_monitored,
                            Movie.is_deleted,
                        )
                        .filter(Movie.instance_key == instance_key, Movie.radarrid.isnot(None))
                        .all()
                    )
                    db_by_id: dict[int, tuple[bool, str | None, str | None, bool, bool]] = {}
                    for rid, has_file, radarrpath, radarr_filepath, monitored, is_deleted in db_rows:
                        try:
                            db_by_id[int(rid)] = (
                                bool(has_file),
                                _normalize_path(radarrpath),
                                _normalize_path(radarr_filepath),
                                bool(monitored),
                                bool(is_deleted),
                            )
                        except Exception:
                            continue

                    added_ids = set(api_by_id.keys()) - set(db_by_id.keys())
                    changed_ids: set[int] = set()
                    for mid, (db_has_file, db_path, db_file_path, db_monitored, db_is_deleted) in db_by_id.items():
                        item = api_by_id.get(mid)
                        if not item:
                            continue
                        api_has_file = _api_movie_has_file(item)
                        api_path = _api_movie_library_path(item)
                        api_file_path = _api_movie_file_path(item)
                        api_monitored = bool(item.get("monitored"))
                        if (
                            db_has_file != api_has_file
                            or db_path != api_path
                            or db_file_path != api_file_path
                            or db_monitored != api_monitored
                            or db_is_deleted
                        ):
                            changed_ids.add(mid)

                    target_ids = set(added_ids) | set(changed_ids) | set(drift_ids) | set(removed_ids)
                    stats['path_drift_ids'] += len(drift_ids)
                    stats['movies_catalog_removed'] += len(removed_ids)
                    stats['movies_targeted'] += len(target_ids)
                    _log_startup_lite_radarr_catalog_breakdown(
                        session,
                        instance_key=instance_key,
                        catalog_count=len(api_movies),
                        api_by_id=api_by_id,
                        added_ids=added_ids,
                        changed_ids=changed_ids,
                        drift_ids=drift_ids,
                        removed_ids=removed_ids,
                        prep_elapsed_s=time.monotonic() - t0,
                    )
                    if target_ids:
                        sync_stats = sync_radarr_movies_by_ids(
                            target_ids,
                            base_url=instance['base_url'],
                            api_key=instance['api_key'],
                            instance_key=instance_key,
                        )
                        stats['targeted_sync_runs'] += 1
                        stats['movies_seen'] += int(sync_stats.get('movies_seen', 0) or 0)
                        stats['movies_marked_deleted'] += int(sync_stats.get('movies_marked_deleted', 0) or 0)
                        logger.info(
                            f"Startup lite · {instance_key} · movies: "
                            f"{int(sync_stats.get('movies_seen', 0) or 0)} synced from Radarr "
                            f"({int(sync_stats.get('movies_created', 0) or 0)} new, "
                            f"{int(sync_stats.get('movies_updated', 0) or 0)} updated, "
                            f"{int(sync_stats.get('movies_marked_deleted', 0) or 0)} removed from library here)",
                            extra={'emoji_type': 'info'},
                        )
                        raw_touched = sync_stats.get("touched_movie_row_ids")
                        if isinstance(raw_touched, list) and raw_touched:
                            touched_movie_row_ids.update(int(x) for x in raw_touched if x is not None)
                        else:
                            row_ids = [
                                int(r[0])
                                for r in session.query(Movie.id)
                                .filter(Movie.instance_key == instance_key, Movie.radarrid.in_(list(target_ids)))
                                .all()
                            ]
                            touched_movie_row_ids.update(row_ids)
                else:
                    api_series = fetch_sonarr_series(instance['base_url'], instance['api_key']) or []
                    stats['snapshot_rows_seen'] += len(api_series)
                    drift_ids = _sonarr_path_drift_series_ids(session, instance_key=instance_key, api_series=api_series)
                    removed_ids = _sonarr_series_ids_removed_from_catalog(session, instance_key=instance_key, api_series=api_series)
                    api_by_id: dict[int, dict] = {}
                    for item in api_series:
                        if not isinstance(item, dict):
                            continue
                        sid = item.get("id")
                        try:
                            api_by_id[int(sid)] = item
                        except Exception:
                            continue

                    db_series_rows = (
                        session.query(
                            Series.sonarrid,
                            Series.sonarrpath,
                            Series.sonarr_monitored,
                            Series.is_deleted,
                            Series.seriesfile_count,
                            Series.id,
                        )
                        .filter(Series.instance_key == instance_key, Series.sonarrid.isnot(None))
                        .all()
                    )
                    db_by_id: dict[int, tuple[str | None, bool, bool, int, int]] = {}
                    for sid, path, monitored, is_deleted, seriesfile_count, row_id in db_series_rows:
                        try:
                            db_by_id[int(sid)] = (
                                _normalize_path(path),
                                bool(monitored),
                                bool(is_deleted),
                                int(seriesfile_count or 0),
                                int(row_id),
                            )
                        except Exception:
                            continue

                    db_episode_counts: dict[int, int] = {}
                    db_episode_file_counts: dict[int, int] = {}
                    grouped = (
                        session.query(
                            Series.sonarrid,
                            func.count(Episode.id),
                        )
                        .join(Season, Season.series_id == Series.id)
                        .join(Episode, Episode.season_id == Season.id)
                        .filter(
                            Series.instance_key == instance_key,
                            Series.sonarrid.isnot(None),
                            Series.is_deleted == False,  # noqa: E712
                            Episode.is_deleted == False,  # noqa: E712
                        )
                        .group_by(Series.sonarrid)
                        .all()
                    )
                    file_grouped = (
                        session.query(
                            Series.sonarrid,
                            func.count(Episode.id),
                        )
                        .join(Season, Season.series_id == Series.id)
                        .join(Episode, Episode.season_id == Season.id)
                        .filter(
                            Series.instance_key == instance_key,
                            Series.sonarrid.isnot(None),
                            Series.is_deleted == False,  # noqa: E712
                            Episode.is_deleted == False,  # noqa: E712
                            Episode.has_file == True,  # noqa: E712
                        )
                        .group_by(Series.sonarrid)
                        .all()
                    )
                    file_count_by_sid: dict[int, int] = {}
                    for sid, ep_file_count in file_grouped:
                        try:
                            file_count_by_sid[int(sid)] = int(ep_file_count or 0)
                        except Exception:
                            continue

                    for sid, ep_count in grouped:
                        try:
                            db_episode_counts[int(sid)] = int(ep_count or 0)
                            db_episode_file_counts[int(sid)] = int(file_count_by_sid.get(int(sid), 0))
                        except Exception:
                            continue

                    added_ids = set(api_by_id.keys()) - set(db_by_id.keys())
                    changed_ids: set[int] = set()
                    for sid, (db_path, db_monitored, db_is_deleted, db_seriesfile_count, _row_id) in db_by_id.items():
                        item = api_by_id.get(sid)
                        if not item:
                            continue
                        api_path = _api_series_library_path(item)
                        api_monitored = _api_series_monitored(item)
                        api_total_episodes = _api_series_total_episode_count(item)
                        api_episode_file_count = _api_series_episode_file_count(item)
                        db_episode_count = int(db_episode_counts.get(sid, 0))
                        db_episode_file_count = int(db_episode_file_counts.get(sid, db_seriesfile_count))
                        if (
                            db_path != api_path
                            or db_monitored != api_monitored
                            or db_is_deleted
                            or (
                                api_total_episodes is not None
                                and db_episode_count != api_total_episodes
                            )
                            or (api_episode_file_count is not None and db_episode_file_count != api_episode_file_count)
                        ):
                            changed_ids.add(sid)

                    if bool(getattr(settings, "SKIP_PLACEHOLDERS_WHEN_MONITORED", False)):
                        episode_mon_candidates = set(db_by_id.keys()) - changed_ids
                        ep_mon_drift = _sonarr_episode_monitored_drift_series_ids(
                            session,
                            instance_key=instance_key,
                            base_url=instance["base_url"],
                            api_key=instance["api_key"],
                            candidate_series_ids=episode_mon_candidates,
                        )
                        if ep_mon_drift:
                            changed_ids |= ep_mon_drift

                    if changed_ids:
                        n_path = n_mon = n_del = n_tot = n_file = 0
                        for sid in changed_ids:
                            item = api_by_id.get(sid)
                            if not item:
                                continue
                            db_row = db_by_id.get(sid)
                            if not db_row:
                                continue
                            db_path, db_monitored, db_is_deleted, db_seriesfile_count, _ = db_row
                            api_path = _api_series_library_path(item)
                            api_monitored = _api_series_monitored(item)
                            api_total = _api_series_total_episode_count(item)
                            api_files = _api_series_episode_file_count(item)
                            db_ep = int(db_episode_counts.get(sid, 0))
                            db_files = int(db_episode_file_counts.get(sid, db_seriesfile_count))
                            hit_path = db_path != api_path
                            hit_mon = db_monitored != api_monitored
                            hit_del = bool(db_is_deleted)
                            hit_tot = api_total is not None and db_ep != api_total
                            hit_file = api_files is not None and db_files != api_files
                            if hit_path:
                                n_path += 1
                            if hit_mon:
                                n_mon += 1
                            if hit_del:
                                n_del += 1
                            if hit_tot:
                                n_tot += 1
                            if hit_file:
                                n_file += 1
                        logger.info(
                            f"Startup lite · {instance_key} · TV catalog diff · among changed={len(changed_ids)}: "
                            f"path={n_path} monitored={n_mon} series_row_deleted={n_del} "
                            f"total_episodes_vs_DB={n_tot} episode_files_vs_DB={n_file}",
                            extra={"emoji_type": "info"},
                        )

                    target_ids = set(added_ids) | set(changed_ids) | set(drift_ids) | set(removed_ids)
                    stats['path_drift_ids'] += len(drift_ids)
                    stats['series_catalog_removed'] += len(removed_ids)
                    stats['series_targeted'] += len(target_ids)
                    _log_startup_lite_sonarr_catalog_breakdown(
                        session,
                        instance_key=instance_key,
                        catalog_count=len(api_series),
                        api_by_id=api_by_id,
                        added_ids=added_ids,
                        changed_ids=changed_ids,
                        drift_ids=drift_ids,
                        removed_ids=removed_ids,
                        prep_elapsed_s=time.monotonic() - t0,
                    )
                    if target_ids:
                        sync_stats = sync_sonarr_series_by_ids(
                            target_ids,
                            base_url=instance['base_url'],
                            api_key=instance['api_key'],
                            instance_key=instance_key,
                        )
                        stats['targeted_sync_runs'] += 1
                        stats['series_seen'] += int(sync_stats.get('series_seen', 0) or 0)
                        stats['episodes_seen'] += int(sync_stats.get('episodes_seen', 0) or 0)
                        stats['series_marked_deleted'] += int(sync_stats.get('series_marked_deleted', 0) or 0)
                        logger.info(
                            f"Startup lite · {instance_key} · TV shows: "
                            f"{sync_stats.get('series_seen', 0)} series and "
                            f"{sync_stats.get('episodes_seen', 0)} episodes updated from Sonarr "
                            f"({sync_stats.get('series_created', 0)} new series, "
                            f"{sync_stats.get('series_marked_deleted', 0)} series removed here)",
                            extra={'emoji_type': 'info'},
                        )
                        raw_ep = sync_stats.get("touched_episode_row_ids")
                        if isinstance(raw_ep, list) and raw_ep:
                            touched_episode_row_ids.update(int(x) for x in raw_ep if x is not None)
                        else:
                            series_row_ids = [
                                int(r[0])
                                for r in session.query(Series.id)
                                .filter(Series.instance_key == instance_key, Series.sonarrid.in_(list(target_ids)))
                                .all()
                            ]
                            if series_row_ids:
                                episode_row_ids = [
                                    int(r[0])
                                    for r in session.query(Episode.id)
                                    .join(Season, Episode.season_id == Season.id)
                                    .filter(Season.series_id.in_(series_row_ids))
                                    .all()
                                ]
                                touched_episode_row_ids.update(episode_row_ids)

                row.last_history_checked_at = datetime.now(timezone.utc)
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                session.commit()
            except Exception as e:
                session.rollback()
                stats['instances_failed'] += 1
                logger.warning(
                    f"Startup lite snapshot sync failed for {instance_key}: {e}",
                    extra={'emoji_type': 'warning'},
                )

        stats["movie_row_ids"] = sorted(touched_movie_row_ids)
        stats["episode_row_ids"] = sorted(touched_episode_row_ids)
        return stats
    finally:
        session.close()


def _resolve_startup_sync_mode(instances: list[dict]) -> str:
    mode = str(getattr(settings, 'STARTUP_SYNC_MODE', 'auto') or 'auto').strip().lower()
    if mode in ('off', 'full', 'lite'):
        return mode

    if mode != 'auto':
        logger.warning(
            f"Unknown STARTUP_SYNC_MODE={mode!r}; defaulting to auto",
            extra={'emoji_type': 'warning'},
        )

    if not instances:
        return 'off'

    session = get_session()
    try:
        for instance in instances:
            row = session.query(ArrState).filter(ArrState.instance_key == instance['instance_key']).first()
            if not row or row.first_full_sync_completed_at is None:
                return 'full'
    finally:
        session.close()
    return 'lite'


def run_startup_source_of_truth() -> dict:
    """Execute configured startup fullsyncs, filesystem scan, determine, and materialize."""
    settings.REFRESH_TRIGGER_SUPPRESSED = True
    try:
        try:
            from services.source_of_truth.arr_instance_reconcile import tombstone_unconfigured_arr_rows

            detach_stats = tombstone_unconfigured_arr_rows(str(getattr(settings, 'ARR_INSTANCES_JSON', '') or ''))
            if any(
                int(detach_stats.get(key) or 0)
                for key in ('movies_tombstoned', 'series_tombstoned', 'seasons_tombstoned', 'episodes_tombstoned')
            ):
                logger.info(
                    f'Startup: tombstoned DB rows for instance keys not in ARR_INSTANCES_JSON: '
                    f"movies={detach_stats.get('movies_tombstoned')} series={detach_stats.get('series_tombstoned')} "
                    f"seasons={detach_stats.get('seasons_tombstoned')} episodes={detach_stats.get('episodes_tombstoned')}",
                    extra={'emoji_type': 'info'},
                )
        except Exception as exc:
            logger.warning(
                f'Startup ARR instance-key tombstone pass failed (non-fatal): {exc}',
                extra={'emoji_type': 'warning'},
            )

        from services.startup_sync_activity import record_startup_sync_progress

        run_ids: list[str] = []
        instances = _configured_arr_instances()
        selected_mode = _resolve_startup_sync_mode(instances)
        startup_sync_stats: dict = {}
        determination_stats: dict = {}
        materialization_stats: dict = {}

        logger.info(
            f"Startup sync mode selected: {selected_mode} (configured_instances={len(instances)})",
            extra={'emoji_type': 'info'},
        )
        started_at = datetime.now(timezone.utc)

        if selected_mode == 'full':
            startup_sync_stats = _run_startup_full_for_instances(instances, run_ids)
        elif selected_mode == 'lite':
            # Activity UI + operators otherwise see nothing until this phase finishes (can be many minutes:
            # full /movie or /series catalogs per instance, then per-item targeted sync).
            record_startup_sync_progress(
                mode=selected_mode,
                started_at=started_at,
                current_phase="discovery",
                startup_sync_stats=None,
                determination_stats=None,
                materialization_stats=None,
            )
            from services.source_of_truth.lite_reconcile import (
                run_lite_startup_reconciliation_pre_discovery,
                run_specials_backfill_if_pending,
            )

            specials_backfill_stats = run_specials_backfill_if_pending(instances=instances)
            pre_movie_ids, pre_episode_ids, recon_stats = run_lite_startup_reconciliation_pre_discovery()
            startup_sync_stats = _run_startup_lite_snapshot_for_instances(
                instances,
                seed_movie_row_ids=set(pre_movie_ids),
                seed_episode_row_ids=set(pre_episode_ids),
                lite_reconciliation_pre=recon_stats,
            )
            specials_determination_ids = {
                int(x)
                for x in (specials_backfill_stats.get("determination_episode_row_ids") or [])
                if x is not None
            }
            if specials_determination_ids:
                merged_episode_ids = {
                    int(x) for x in (startup_sync_stats.get("episode_row_ids") or []) if x is not None
                }
                merged_episode_ids.update(specials_determination_ids)
                startup_sync_stats["episode_row_ids"] = sorted(merged_episode_ids)
            startup_sync_stats["specials_backfill"] = specials_backfill_stats
            logger.info(
                "Startup lite · catalog refresh finished · "
                f"{int(startup_sync_stats.get('movies_targeted', 0) or 0)} movies and "
                f"{int(startup_sync_stats.get('series_targeted', 0) or 0)} series were checked against Radarr/Sonarr "
                f"({int(startup_sync_stats.get('movies_seen', 0) or 0)} movie rows updated, "
                f"{int(startup_sync_stats.get('series_seen', 0) or 0)} series / "
                f"{int(startup_sync_stats.get('episodes_seen', 0) or 0)} episode rows updated)",
                extra={'emoji_type': 'success'},
            )
        elif selected_mode == 'off':
            startup_sync_stats = {'instances': len(instances), 'skipped': True}
        else:
            # Defensive fallback. _resolve_startup_sync_mode should prevent this branch.
            startup_sync_stats = {'instances': len(instances), 'skipped': True, 'reason': f'unknown_mode:{selected_mode}'}

        if selected_mode == "lite":
            scan_count = 0
            scan_info = {"skipped": True, "reason": "lite_snapshot_diff"}
            reconcile_stats = {"skipped": True, "reason": "lite_snapshot_diff"}
            movie_row_ids = [int(x) for x in (startup_sync_stats.get("movie_row_ids") or []) if x is not None]
            episode_row_ids = [int(x) for x in (startup_sync_stats.get("episode_row_ids") or []) if x is not None]
            placeholder_truth_stats = _refresh_placeholder_presence_for_entities(
                movie_row_ids=movie_row_ids,
                episode_row_ids=episode_row_ids,
            )
            logger.info(
                "Startup lite placeholder truth refresh complete: "
                f"movies_checked={placeholder_truth_stats.get('movies_checked', 0)} "
                f"movies_updated={placeholder_truth_stats.get('movies_updated', 0)} "
                f"episodes_checked={placeholder_truth_stats.get('episodes_checked', 0)} "
                f"episodes_updated={placeholder_truth_stats.get('episodes_updated', 0)}",
                extra={'emoji_type': 'info'},
            )
        else:
            record_startup_sync_progress(
                mode=selected_mode,
                started_at=started_at,
                # Not determination yet: next steps are full-tree FS scan + placeholder reconcile (+ calendar refresh).
                current_phase="fs_scan",
                startup_sync_stats=startup_sync_stats,
                determination_stats=None,
                materialization_stats=None,
            )

            scan_run_id = run_ids[0] if run_ids else f'fullsync:startup:{int(time.time())}'
            scan_result = scan_once_if_needed(scan_run_id, prefer_incremental=True)
            if isinstance(scan_result, tuple):
                scan_count, scan_info = scan_result
            else:
                scan_count, scan_info = scan_result, {'reason': 'ok'}

            phase_started = time.monotonic()
            reconcile_stats = run_placeholder_link_reconcile()
            logger.info(
                f"Startup phase complete: placeholder_reconcile elapsed_s={time.monotonic() - phase_started:.1f}",
                extra={'emoji_type': 'info'},
            )

        phase_started = time.monotonic()
        calendar_date_refresh_stats = run_calendar_date_refresh()
        logger.info(
            f"Startup phase complete: calendar_date_refresh elapsed_s={time.monotonic() - phase_started:.1f}",
            extra={'emoji_type': 'info'},
        )
        phase_started = time.monotonic()
        record_startup_sync_progress(
            mode=selected_mode,
            started_at=started_at,
            current_phase='determination',
            startup_sync_stats=startup_sync_stats,
            determination_stats=None,
            materialization_stats=None,
        )
        if selected_mode == "lite":
            movie_row_ids = [int(x) for x in (startup_sync_stats.get("movie_row_ids") or []) if x is not None]
            episode_row_ids = [int(x) for x in (startup_sync_stats.get("episode_row_ids") or []) if x is not None]
            if movie_row_ids or episode_row_ids:
                determination_stats = run_determination_for_entities(
                    movie_ids=movie_row_ids,
                    episode_ids=episode_row_ids,
                )
            else:
                determination_stats = {
                    "movies_total": 0,
                    "movies_changed": 0,
                    "episodes_total": 0,
                    "episodes_changed": 0,
                    "obsolete_placeholder": 0,
                    "not_needed": 0,
                    "placeholder_exists": 0,
                    "needs_placeholder": 0,
                    "path_drift_movies": 0,
                    "path_drift_episodes": 0,
                    "skipped": True,
                    "reason": "no_lite_changes_detected",
                }
                logger.info(
                    "Startup lite determination skipped: no changed entities detected",
                    extra={"emoji_type": "info"},
                )
        else:
            determination_stats = run_determination_pass()
        logger.info(
            f"Startup phase complete: determination elapsed_s={time.monotonic() - phase_started:.1f}",
            extra={'emoji_type': 'info'},
        )
        record_startup_sync_progress(
            mode=selected_mode,
            started_at=started_at,
            current_phase='materialization',
            startup_sync_stats=startup_sync_stats,
            determination_stats=determination_stats,
            materialization_stats=None,
        )
        # Primer phase removed to accelerate startup and simplify sync pipeline.
        primer_stats = {"skipped": True, "reason": "deprecated"}
        phase_started = time.monotonic()
        if selected_mode == "lite":
            movie_row_ids = [int(x) for x in (startup_sync_stats.get("movie_row_ids") or []) if x is not None]
            episode_row_ids = [int(x) for x in (startup_sync_stats.get("episode_row_ids") or []) if x is not None]
            materialization_stats = run_materialization_for_entities(
                movie_ids=movie_row_ids,
                episode_ids=episode_row_ids,
                observation_source="startup_lite_materialization",
            ) if (movie_row_ids or episode_row_ids) else {
                "movies_considered": 0,
                "episodes_considered": 0,
                "created": 0,
                "deleted": 0,
                "noop": 0,
                "errors": 0,
                "files_created": 0,
                "files_deleted": 0,
                "directories_deleted": 0,
                "nfo_written": 0,
                "nfo_deleted": 0,
                "series_nfo_deleted": 0,
                "skipped": True,
                "reason": "no_lite_changes_detected",
            }
            if not (movie_row_ids or episode_row_ids):
                logger.info(
                    "Startup lite materialization skipped: no changed entities detected",
                    extra={"emoji_type": "info"},
                )
        else:
            materialization_stats = run_materialization_pass()
        logger.info(
            f"Startup phase complete: materialization elapsed_s={time.monotonic() - phase_started:.1f}",
            extra={'emoji_type': 'info'},
        )

        request_nfo_backfill_stats: dict[str, object] = {"skipped": True, "reason": "not_run"}
        try:
            from services.source_of_truth.status_reconciler import enqueue_request_status_nfo_backfill

            request_nfo_backfill_stats = enqueue_request_status_nfo_backfill()
            if request_nfo_backfill_stats.get("skipped"):
                logger.debug(
                    f"REQUEST NFO backfill skipped: {request_nfo_backfill_stats.get('reason', '')}",
                    extra={"emoji_type": "debug"},
                )
            elif request_nfo_backfill_stats.get("ok") and int(request_nfo_backfill_stats.get("placeholder_count") or 0) > 0:
                logger.info(
                    "Startup · REQUEST status NFO backfill queued · "
                    f"placeholders={request_nfo_backfill_stats.get('placeholder_count')} · "
                    f"jobs_created={request_nfo_backfill_stats.get('jobs_created', 0)} · "
                    f"jobs_updated={request_nfo_backfill_stats.get('jobs_updated', 0)}",
                    extra={"emoji_type": "info"},
                )
            elif request_nfo_backfill_stats.get("ok"):
                logger.debug(
                    "Startup · REQUEST status NFO backfill: no matching placeholders",
                    extra={"emoji_type": "debug"},
                )
            else:
                logger.warning(
                    f"Startup · REQUEST status NFO backfill enqueue issue: {request_nfo_backfill_stats}",
                    extra={"emoji_type": "warning"},
                )
        except Exception as exc:
            logger.warning(
                f"Startup · REQUEST status NFO backfill failed (non-fatal): {exc}",
                extra={"emoji_type": "warning"},
                exc_info=True,
            )
            request_nfo_backfill_stats = {"ok": False, "error": str(exc)}

        record_startup_sync_progress(
            mode=selected_mode,
            started_at=started_at,
            current_phase='complete',
            startup_sync_stats=startup_sync_stats,
            determination_stats=determination_stats,
            materialization_stats=materialization_stats,
            completed_at=datetime.now(timezone.utc),
        )
        phase_started = time.monotonic()
        calendar_stats = run_calendar_phase()
        logger.info(
            f"Startup phase complete: calendar elapsed_s={time.monotonic() - phase_started:.1f}",
            extra={'emoji_type': 'info'},
        )
        phase_started = time.monotonic()
        orphan_placeholder_stats = run_orphan_placeholder_cleanup()
        logger.info(
            f"Startup phase complete: orphan_placeholder_cleanup elapsed_s={time.monotonic() - phase_started:.1f}",
            extra={'emoji_type': 'info'},
        )
        status_reconcile_stats = {"skipped": True, "reason": "deprecated"}

        result = {
            'ran': True,
            'startup_sync_mode': selected_mode,
            'fullsync_ran': selected_mode == 'full' and bool(run_ids),
            'startup_sync': startup_sync_stats,
            'run_ids': run_ids,
            'scan': {
                'count': scan_count,
                'info': scan_info,
            },
            'reconcile': reconcile_stats,
            'calendar_date_refresh': calendar_date_refresh_stats,
            'determination': determination_stats,
            'primer': primer_stats,
            'materialization': materialization_stats,
            'calendar': calendar_stats,
            'orphan_placeholders': orphan_placeholder_stats,
            'status_reconcile': status_reconcile_stats,
            'request_nfo_backfill': request_nfo_backfill_stats,
        }
        try:
            from services.activity_markers import record_startup_source_of_truth_activity

            record_startup_source_of_truth_activity(
                mode=selected_mode,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                determination=determination_stats if isinstance(determination_stats, dict) else None,
                materialization=materialization_stats if isinstance(materialization_stats, dict) else None,
            )
        except Exception:
            logger.debug(
                "Startup activity marker persistence skipped",
                extra={'emoji_type': 'debug'},
                exc_info=True,
            )
        return result
    except Exception as exc:
        try:
            from services.startup_sync_activity import record_startup_sync_progress

            record_startup_sync_progress(
                mode=str(getattr(settings, 'STARTUP_SYNC_MODE', 'auto') or 'auto'),
                started_at=started_at if 'started_at' in locals() else datetime.now(timezone.utc),
                current_phase='failed',
                startup_sync_stats=startup_sync_stats if 'startup_sync_stats' in locals() else None,
                determination_stats=determination_stats if 'determination_stats' in locals() else None,
                materialization_stats=materialization_stats if 'materialization_stats' in locals() else None,
                completed_at=datetime.now(timezone.utc),
                failed=True,
                error_message=str(exc),
            )
        except Exception:
            pass
        raise
    finally:
        settings.REFRESH_TRIGGER_SUPPRESSED = False
