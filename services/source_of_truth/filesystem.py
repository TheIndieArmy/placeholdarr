import os
import time
from typing import Any, Optional

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Episode, FSScanRun, Movie, Placeholder, Season, Series
from services.placeholders import episode_placeholder_path, movie_placeholder_path


NON_PLACEHOLDER_EXTENSIONS = {
    '.nfo',
    '.jpg',
    '.jpeg',
    '.png',
    '.gif',
    '.webp',
    '.tbn',
    '.srt',
    '.sub',
    '.idx',
    '.txt',
}

FS_SCAN_PROGRESS_EVERY_FILES = 2000
FS_STALE_PROGRESS_EVERY_ROWS = 5000

_NEEDS_PLACEHOLDER = "needs_placeholder"


def looks_like_placeholder_file(path: str) -> bool:
    """Treat regular media files under configured placeholder roots as placeholders.

    In rewrite mode we do not require '(dummy)' naming tags. We only exclude
    common sidecar/non-media extensions.
    """
    name = os.path.basename(path).lower()
    if not name or name.startswith('.'):
        return False
    _, ext = os.path.splitext(name)
    return ext not in NON_PLACEHOLDER_EXTENSIONS


def _norm_scan_path(path: str) -> str:
    return os.path.abspath(os.path.normpath(path))


def configured_roots() -> list[str]:
    roots = [
        getattr(settings, 'MOVIE_LIBRARY_FOLDER', None),
        getattr(settings, 'TV_LIBRARY_FOLDER', None),
        getattr(settings, 'MOVIE_LIBRARY_4K_FOLDER', None),
        getattr(settings, 'TV_LIBRARY_4K_FOLDER', None),
    ]
    return [root for root in dict.fromkeys(roots) if root]


def _is_path_under_roots(path: str, roots: list[str]) -> bool:
    if not path:
        return False
    try:
        abs_path = os.path.abspath(path)
    except Exception:
        return False

    for root in roots:
        try:
            abs_root = os.path.abspath(root)
            if os.path.commonpath([abs_path, abs_root]) == abs_root:
                return True
        except Exception:
            continue
    return False


def is_path_under_configured_roots(path: str | None) -> bool:
    """True when ``path`` lies under any configured movie/TV library root."""
    if not path:
        return False
    roots = [os.path.abspath(r) for r in configured_roots() if r]
    return _is_path_under_roots(path, roots)


def is_path_under_tv_library_roots(path: str | None) -> bool:
    """True when ``path`` lies under configured TV library folder(s)."""
    if not path:
        return False
    roots: list[str] = []
    for key in ("TV_LIBRARY_FOLDER", "TV_LIBRARY_4K_FOLDER"):
        raw = getattr(settings, key, None)
        if raw:
            roots.append(os.path.abspath(str(raw)))
    roots = list(dict.fromkeys(roots))
    return _is_path_under_roots(path, roots)


def is_path_under_movie_library_roots(path: str | None) -> bool:
    """True when ``path`` lies under configured movie library folder(s)."""
    if not path:
        return False
    roots: list[str] = []
    for key in ("MOVIE_LIBRARY_FOLDER", "MOVIE_LIBRARY_4K_FOLDER"):
        raw = getattr(settings, key, None)
        if raw:
            roots.append(os.path.abspath(str(raw)))
    roots = list(dict.fromkeys(roots))
    return _is_path_under_roots(path, roots)


def _disconnect_placeholder_row(row: Placeholder) -> bool:
    """Clear media-server linkage so re-materialization performs a fresh observe pass."""
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


def _placeholder_lifecycle_is_deleted(row: Placeholder) -> bool:
    return str(getattr(row, "lifecycle_status", "") or "").strip().upper() == "DELETED"


def _placeholder_linked_entity_is_deleted(session, row: Placeholder) -> bool:
    """True when the row is tied to a deleted movie/episode/series (or deleted season)."""
    try:
        mid = getattr(row, "movie_id", None)
        if mid is not None:
            mv = session.query(Movie).filter(Movie.id == int(mid)).first()
            return bool(mv is None or getattr(mv, "is_deleted", False))
        eid = getattr(row, "episode_id", None)
        if eid is not None:
            ep = session.query(Episode).filter(Episode.id == int(eid)).first()
            if ep is None or getattr(ep, "is_deleted", False):
                return True
            season = session.query(Season).filter(Season.id == ep.season_id).first()
            if season is None or getattr(season, "is_deleted", False):
                return True
            series = session.query(Series).filter(Series.id == season.series_id).first()
            return bool(series is None or getattr(series, "is_deleted", False))
        sid = getattr(row, "series_id", None)
        if sid is not None:
            series = session.query(Series).filter(Series.id == int(sid)).first()
            return bool(series is None or getattr(series, "is_deleted", False))
    except Exception:
        return False
    return False


def _placeholder_row_eligible_for_fs_refresh(session, row: Placeholder) -> bool:
    """FS scan must not revive tombstoned rows or rows for deleted ARR entities."""
    if _placeholder_lifecycle_is_deleted(row):
        return False
    if _placeholder_linked_entity_is_deleted(session, row):
        return False
    return True


def _pick_placeholder_row_for_observed_path(session, path: str) -> Placeholder | None:
    """Choose which Placeholder row to refresh when a file is found on disk.

    Prefer an active (non-tombstoned, non-deleted-entity) row when multiple rows
    share a path. Never returns a deleted/tombstoned row — callers should insert
    a path-only row instead of reactivating those.
    """
    if not path:
        return None
    with session.no_autoflush:
        rows = session.query(Placeholder).filter(Placeholder.path == path).all()
        if not rows:
            # Also match abspath variants when DB stored a normalized path.
            try:
                abs_p = _norm_scan_path(path)
            except Exception:
                abs_p = path
            if abs_p != path:
                rows = session.query(Placeholder).filter(Placeholder.path == abs_p).all()
        eligible = [r for r in rows if _placeholder_row_eligible_for_fs_refresh(session, r)]
        if not eligible:
            return None

        def _score(r: Placeholder) -> tuple:
            linked = 1 if (getattr(r, "movie_id", None) or getattr(r, "episode_id", None)) else 0
            active = 1 if getattr(r, "has_placeholder", False) else 0
            rid = int(getattr(r, "id", 0) or 0)
            # Prefer linked + already-active + newest id among eligible rows.
            return (linked, active, rid)

        return max(eligible, key=_score)


def _heal_tombstoned_placeholder_paths(session) -> int:
    """Clear paths on DELETED lifecycle / deleted-entity rows so FS scan cannot match sibling files."""
    healed = 0
    with session.no_autoflush:
        # Collect ids first, then bulk-update to avoid per-row history hooks / flush storms.
        ids: set[int] = set()

        for (pid,) in (
            session.query(Placeholder.id)
            .filter(
                Placeholder.path.isnot(None),
                Placeholder.path != "",
                Placeholder.lifecycle_status == "DELETED",
            )
            .all()
        ):
            if pid is not None:
                ids.add(int(pid))

        deleted_series_ids = [
            int(r[0]) for r in session.query(Series.id).filter(Series.is_deleted == True).all()  # noqa: E712
        ]
        if deleted_series_ids:
            for (pid,) in (
                session.query(Placeholder.id)
                .filter(
                    Placeholder.series_id.in_(tuple(deleted_series_ids)),
                    Placeholder.path.isnot(None),
                    Placeholder.path != "",
                )
                .all()
            ):
                if pid is not None:
                    ids.add(int(pid))
            ep_ids = [
                int(r[0])
                for r in (
                    session.query(Episode.id)
                    .join(Season, Episode.season_id == Season.id)
                    .filter(Season.series_id.in_(tuple(deleted_series_ids)))
                    .all()
                )
                if r[0] is not None
            ]
            if ep_ids:
                for (pid,) in (
                    session.query(Placeholder.id)
                    .filter(
                        Placeholder.episode_id.in_(tuple(ep_ids)),
                        Placeholder.path.isnot(None),
                        Placeholder.path != "",
                    )
                    .all()
                ):
                    if pid is not None:
                        ids.add(int(pid))

        deleted_movie_ids = [
            int(r[0]) for r in session.query(Movie.id).filter(Movie.is_deleted == True).all()  # noqa: E712
        ]
        if deleted_movie_ids:
            for (pid,) in (
                session.query(Placeholder.id)
                .filter(
                    Placeholder.movie_id.in_(tuple(deleted_movie_ids)),
                    Placeholder.path.isnot(None),
                    Placeholder.path != "",
                )
                .all()
            ):
                if pid is not None:
                    ids.add(int(pid))

        if not ids:
            return 0

        healed = (
            session.query(Placeholder)
            .filter(Placeholder.id.in_(tuple(ids)))
            .update(
                {
                    Placeholder.path: "",
                    Placeholder.has_placeholder: False,
                    Placeholder.lifecycle_status: "DELETED",
                },
                synchronize_session=False,
            )
        )
    return int(healed or 0)


def _incremental_placeholder_scan(session, roots: list[str]) -> tuple[int, dict[str, Any]]:
    """Re-stat known placeholder paths + canonical NEEDS paths (no ``os.walk`` full-tree pass).

    Global stale marking is skipped here; the hourly scheduled self-heal (full scan) covers drift.
    """
    healed = _heal_tombstoned_placeholder_paths(session)
    scanned_roots = [os.path.abspath(root) for root in roots if root]
    upserted = 0
    refreshed = 0
    stale_marked = 0
    disconnected = 0
    needs_canonical_hits = 0

    def _refresh_row(row: Placeholder, abs_p: str) -> None:
        nonlocal refreshed
        if row.path != abs_p:
            row.path = abs_p
        row.has_placeholder = True
        row.last_observed_at = func.now()
        session.add(row)
        refreshed += 1

    def _try_canonical_remap(row: Placeholder) -> bool:
        """If linked to Movie/Episode, move row.path to canonical materialized path when that file exists."""
        if not _placeholder_row_eligible_for_fs_refresh(session, row):
            return False
        try:
            mid = getattr(row, "movie_id", None)
            if mid:
                mv = session.query(Movie).filter(Movie.id == int(mid)).first()
                if mv and not getattr(mv, "is_deleted", False):
                    cp = _norm_scan_path(movie_placeholder_path(mv))
                    if (
                        looks_like_placeholder_file(cp)
                        and _is_path_under_roots(cp, scanned_roots)
                        and os.path.isfile(cp)
                    ):
                        _refresh_row(row, cp)
                        return True
            eid = getattr(row, "episode_id", None)
            if eid:
                ep = session.query(Episode).filter(Episode.id == int(eid)).first()
                if ep and not getattr(ep, "is_deleted", False):
                    season = session.query(Season).filter(Season.id == ep.season_id).first()
                    series = (
                        session.query(Series).filter(Series.id == season.series_id).first()
                        if season
                        else None
                    )
                    if (
                        season
                        and series
                        and not getattr(season, "is_deleted", False)
                        and not getattr(series, "is_deleted", False)
                    ):
                        cp = _norm_scan_path(episode_placeholder_path(ep, season, series))
                        if (
                            looks_like_placeholder_file(cp)
                            and _is_path_under_roots(cp, scanned_roots)
                            and os.path.isfile(cp)
                        ):
                            _refresh_row(row, cp)
                            return True
        except Exception:
            return False
        return False

    def _placeholder_media_linked(row: Placeholder) -> bool:
        return bool(getattr(row, "movie_id", None) or getattr(row, "episode_id", None))

    for row in session.query(Placeholder).filter(Placeholder.has_placeholder == True).all():  # noqa: E712
        if not _placeholder_row_eligible_for_fs_refresh(session, row):
            # Defensive: clear accidental reactivation of tombstoned / deleted-entity rows.
            row.has_placeholder = False
            if hasattr(row, "lifecycle_status") and not _placeholder_lifecycle_is_deleted(row):
                row.lifecycle_status = "DELETED"
            if getattr(row, "path", None):
                row.path = ""
            if _disconnect_placeholder_row(row):
                disconnected += 1
            session.add(row)
            stale_marked += 1
            continue
        raw = getattr(row, "path", None)
        if not raw:
            row.has_placeholder = False
            if hasattr(row, "lifecycle_status"):
                row.lifecycle_status = "MISSING"
            if _disconnect_placeholder_row(row):
                disconnected += 1
            session.add(row)
            stale_marked += 1
            continue
        try:
            ap = _norm_scan_path(str(raw))
        except Exception:
            continue

        under = _is_path_under_roots(ap, scanned_roots)
        if under and os.path.isfile(ap):
            _refresh_row(row, ap)
            continue

        if _try_canonical_remap(row):
            continue

        # Path-only rows (typical of ``fs_scan`` upserts with no movie_id/episode_id) cannot be
        # canonical-remapped here. Marking them MISSING when ``under``/``isfile`` disagrees with
        # DB strings would clear thousands of TV placeholders before reconcile → episodes_linked=0
        # and mass ``needs_placeholder``. Defer orphan stale detection to the scheduled full walk.
        if not _placeholder_media_linked(row):
            continue

        if not under:
            row.has_placeholder = False
            if hasattr(row, "lifecycle_status"):
                row.lifecycle_status = "MISSING"
            if _disconnect_placeholder_row(row):
                disconnected += 1
            session.add(row)
            stale_marked += 1
            continue

        row.has_placeholder = False
        if hasattr(row, "lifecycle_status"):
            row.lifecycle_status = "MISSING"
        if _disconnect_placeholder_row(row):
            disconnected += 1
        session.add(row)
        stale_marked += 1

    for mv in session.query(Movie).filter(Movie.determination == _NEEDS_PLACEHOLDER, Movie.is_deleted == False).all():  # noqa: E712
        try:
            ap = _norm_scan_path(movie_placeholder_path(mv))
        except Exception:
            continue
        if not looks_like_placeholder_file(ap) or not _is_path_under_roots(ap, scanned_roots):
            continue
        if not os.path.isfile(ap):
            continue
        needs_canonical_hits += 1
        plink = session.query(Placeholder).filter(Placeholder.movie_id == mv.id).order_by(Placeholder.id.desc()).first()
        if plink:
            _refresh_row(plink, ap)
        else:
            session.add(
                Placeholder(
                    path=ap,
                    movie_id=mv.id,
                    has_placeholder=True,
                    created_by="fs_scan_incremental",
                    last_observed_at=func.now(),
                )
            )
            upserted += 1

    ep_rows = (
        session.query(Episode, Season, Series)
        .join(Season, Episode.season_id == Season.id)
        .join(Series, Season.series_id == Series.id)
        .filter(
            Episode.determination == _NEEDS_PLACEHOLDER,
            Episode.is_deleted == False,  # noqa: E712
            Season.is_deleted == False,  # noqa: E712
            Series.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    for ep, season, series in ep_rows:
        try:
            ap = _norm_scan_path(episode_placeholder_path(ep, season, series))
        except Exception:
            continue
        if not looks_like_placeholder_file(ap) or not _is_path_under_roots(ap, scanned_roots):
            continue
        if not os.path.isfile(ap):
            continue
        needs_canonical_hits += 1
        plink = session.query(Placeholder).filter(Placeholder.episode_id == ep.id).order_by(Placeholder.id.desc()).first()
        if plink:
            _refresh_row(plink, ap)
        else:
            session.add(
                Placeholder(
                    path=ap,
                    episode_id=ep.id,
                    series_id=series.id,
                    season_id=season.id,
                    has_placeholder=True,
                    created_by="fs_scan_incremental",
                    last_observed_at=func.now(),
                )
            )
            upserted += 1

    meta: dict[str, Any] = {
        "full_scan": False,
        "tombstoned_paths_cleared": healed,
        "incremental_refreshed": refreshed,
        "incremental_stale_marked": stale_marked,
        "incremental_disconnected": disconnected,
        "incremental_new_rows": upserted,
        "incremental_needs_canonical_hits": needs_canonical_hits,
    }
    return upserted, meta


def scan_placeholder_roots(roots: list[str], *, full_scan: bool = True) -> tuple[int, dict[str, Any]]:
    session = get_session()
    upserted = 0
    stale_marked = 0
    disconnected = 0
    scanned_files = 0
    scanned_media_candidates = 0
    scanned_roots = [os.path.abspath(root) for root in roots if root]
    observed_paths: set[str] = set()
    started_mono = time.monotonic()
    try:
        if not full_scan:
            upserted, meta = _incremental_placeholder_scan(session, roots)
            session.commit()
            elapsed = time.monotonic() - started_mono
            logger.info(
                "FS incremental scan complete: "
                f"roots={len(scanned_roots)} new_rows={meta.get('incremental_new_rows', 0)} "
                f"refreshed={meta.get('incremental_refreshed', 0)} stale_marked={meta.get('incremental_stale_marked', 0)} "
                f"needs_canonical_hits={meta.get('incremental_needs_canonical_hits', 0)} "
                f"tombstoned_paths_cleared={meta.get('tombstoned_paths_cleared', 0)} "
                f"elapsed_s={elapsed:.1f}",
                extra={"emoji_type": "success"},
            )
            return upserted, meta

        healed = _heal_tombstoned_placeholder_paths(session)
        logger.info(
            f"FS scan started roots={len(scanned_roots)} full_scan=True "
            f"tombstoned_paths_cleared={healed}",
            extra={'emoji_type': 'info'},
        )
        for root in roots:
            if not root:
                continue
            if not os.path.isdir(root):
                logger.warning(f'FS-scan root not found: {root}', extra={'emoji_type': 'warning'})
                continue

            for dirpath, _, filenames in os.walk(root):
                for filename in filenames:
                    scanned_files += 1
                    path = os.path.join(dirpath, filename)
                    if not looks_like_placeholder_file(path):
                        continue
                    scanned_media_candidates += 1
                    observed_paths.add(path)

                    existing = _pick_placeholder_row_for_observed_path(session, path)
                    if existing:
                        existing.has_placeholder = True
                        if hasattr(existing, "lifecycle_status"):
                            existing.lifecycle_status = "ACTIVE"
                        existing.last_observed_at = func.now()
                        session.add(existing)
                    else:
                        # Do not reactivate tombstoned / deleted-entity rows that still
                        # share this path; open a path-only row for reconcile instead.
                        session.add(
                            Placeholder(
                                path=path,
                                has_placeholder=True,
                                created_by='fs_scan',
                                last_observed_at=func.now(),
                            )
                        )
                        upserted += 1
                    if scanned_media_candidates % FS_SCAN_PROGRESS_EVERY_FILES == 0:
                        elapsed = time.monotonic() - started_mono
                        logger.info(
                            "FS scan progress: "
                            f"media_candidates={scanned_media_candidates} "
                            f"total_files_seen={scanned_files} "
                            f"upserted={upserted} "
                            f"elapsed_s={elapsed:.1f}",
                            extra={'emoji_type': 'info'},
                        )

        # Mark stale placeholder rows as missing whenever they were previously active
        # but were not observed in this scan. This intentionally includes rows whose
        # path drifted outside configured roots to self-heal legacy mismatches.
        active_query = session.query(Placeholder).filter(Placeholder.has_placeholder == True)  # noqa: E712

        active_rows = active_query.all()
        for idx, row in enumerate(active_rows, start=1):
            row_path = getattr(row, 'path', None)
            if not row_path:
                continue
            if row_path in observed_paths:
                continue

            row.has_placeholder = False
            if hasattr(row, 'lifecycle_status'):
                row.lifecycle_status = 'MISSING'
            if _disconnect_placeholder_row(row):
                disconnected += 1
            if hasattr(row, 'updated_at'):
                row.updated_at = func.now()
            session.add(row)
            stale_marked += 1
            if idx % FS_STALE_PROGRESS_EVERY_ROWS == 0:
                elapsed = time.monotonic() - started_mono
                logger.info(
                    "FS stale-mark progress: "
                    f"checked={idx}/{len(active_rows)} "
                    f"stale_marked={stale_marked} "
                    f"disconnected={disconnected} "
                    f"elapsed_s={elapsed:.1f}",
                    extra={'emoji_type': 'info'},
                )

        session.commit()
        elapsed = time.monotonic() - started_mono
        meta = {
            "full_scan": True,
            "files_seen": scanned_files,
            "media_candidates": scanned_media_candidates,
            "stale_marked": stale_marked,
            "disconnected": disconnected,
            "tombstoned_paths_cleared": healed,
        }
        logger.info(
            "FS scan complete: "
            f"roots={len(scanned_roots)} files_seen={scanned_files} "
            f"media_candidates={scanned_media_candidates} created={upserted} "
            f"stale_marked={stale_marked} disconnected={disconnected} "
            f"elapsed_s={elapsed:.1f}",
            extra={'emoji_type': 'success'},
        )
        if stale_marked:
            logger.info(
                f'FS scan marked stale placeholders missing={stale_marked} (created={upserted}, disconnected={disconnected})',
                extra={'emoji_type': 'placeholder'},
            )
        return upserted, meta
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def scan_once_if_needed(run_id: Optional[str] = None, *, prefer_incremental: bool = False):
    session = get_session()
    try:
        if run_id:
            existing = session.query(FSScanRun).filter(FSScanRun.run_id == run_id).first()
            if existing:
                return 0, {'reason': 'already_claimed', 'run_id': run_id}
            session.add(FSScanRun(run_id=run_id))
            session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f'FS run claim failed: {e}', extra={'emoji_type': 'error'})
        return 0, {'reason': 'error', 'message': str(e)}
    finally:
        session.close()

    try:
        roots = configured_roots()
        if not roots:
            logger.warning('No library roots configured; skipping FS scan', extra={'emoji_type': 'warning'})
            return 0, {'reason': 'no_roots'}
        interval_h = int(getattr(settings, "FULL_SYNC_INTERVAL_HOURS", 0) or 0)
        use_incremental = bool(prefer_incremental and interval_h > 0)
        created, scan_meta = scan_placeholder_roots(roots, full_scan=not use_incremental)
        logger.debug(
            f'FS scan completed; placeholders created={created} incremental={use_incremental}',
            extra={'emoji_type': 'placeholder'},
        )
        info: dict[str, Any] = {'reason': 'ok', 'roots': roots, 'incremental': use_incremental}
        info.update(scan_meta)
        return created, info
    except Exception as e:
        logger.error(f'FS scan failed: {e}', extra={'emoji_type': 'error'})
        return 0, {'reason': 'error', 'message': str(e)}
