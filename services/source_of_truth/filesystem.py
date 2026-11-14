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
FS_SCAN_DB_CHUNK = 200

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
    try:
        from services.library_destinations import all_configured_dest_roots

        return all_configured_dest_roots()
    except Exception:
        roots = [
            getattr(settings, 'MOVIE_LIBRARY_FOLDER', None),
            getattr(settings, 'TV_LIBRARY_FOLDER', None),
        ]
        return [root for root in dict.fromkeys(roots) if root]


def _translate_media_server_path(path: str | None) -> str | None:
    """Apply media-server remote→local mappings before root checks."""
    if not path:
        return path
    try:
        from services.media_path_mappings import apply_media_server_path_mappings

        return apply_media_server_path_mappings(path)
    except Exception:
        return path


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
    path = _translate_media_server_path(path) or path
    roots = [os.path.abspath(r) for r in configured_roots() if r]
    return _is_path_under_roots(path, roots)


def is_path_under_tv_library_roots(path: str | None) -> bool:
    """True when ``path`` lies under configured TV library folder(s)."""
    if not path:
        return False
    path = _translate_media_server_path(path) or path
    try:
        from services.library_destinations import all_tv_dest_roots

        roots = [os.path.abspath(r) for r in all_tv_dest_roots() if r]
    except Exception:
        roots = []
        for key in ("TV_LIBRARY_FOLDER",):
            raw = getattr(settings, key, None)
            if raw:
                roots.append(os.path.abspath(str(raw)))
        roots = list(dict.fromkeys(roots))
    return _is_path_under_roots(path, roots)


def is_path_under_movie_library_roots(path: str | None) -> bool:
    """True when ``path`` lies under configured movie library folder(s)."""
    if not path:
        return False
    path = _translate_media_server_path(path) or path
    try:
        from services.library_destinations import all_movie_dest_roots

        roots = [os.path.abspath(r) for r in all_movie_dest_roots() if r]
    except Exception:
        roots = []
        for key in ("MOVIE_LIBRARY_FOLDER",):
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


def _incremental_canonical_path_for_row(session, row: Placeholder, scanned_roots: list[str]) -> str | None:
    """DB-only: compute a candidate canonical path string (no ``isfile``)."""
    try:
        mid = getattr(row, "movie_id", None)
        if mid:
            mv = session.query(Movie).filter(Movie.id == int(mid)).first()
            if mv and not getattr(mv, "is_deleted", False):
                cp = _norm_scan_path(movie_placeholder_path(mv))
                if looks_like_placeholder_file(cp) and _is_path_under_roots(cp, scanned_roots):
                    return cp
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
                    if looks_like_placeholder_file(cp) and _is_path_under_roots(cp, scanned_roots):
                        return cp
    except Exception:
        return None
    return None


def _incremental_placeholder_scan(roots: list[str]) -> tuple[int, dict[str, Any]]:
    """Re-stat known placeholder paths + canonical NEEDS paths (no ``os.walk`` full-tree pass).

    Short DB plan → close → disk ``isfile`` with no checkout → short write chunks.
    Global stale marking is skipped here; the hourly scheduled self-heal (full scan) covers drift.
    """
    scanned_roots = [os.path.abspath(root) for root in roots if root]

    session = get_session()
    try:
        healed = _heal_tombstoned_placeholder_paths(session)
        session.commit()
    finally:
        try:
            session.close()
        except Exception:
            pass

    ph_plans: list[dict[str, Any]] = []
    needs_movies: list[tuple[int, str]] = []
    needs_episodes: list[tuple[int, int, int, str]] = []

    session = get_session()
    try:
        for row in session.query(Placeholder).filter(Placeholder.has_placeholder == True).all():  # noqa: E712
            pid = int(row.id) if getattr(row, "id", None) is not None else None
            if pid is None:
                continue
            eligible = _placeholder_row_eligible_for_fs_refresh(session, row)
            lifecycle_deleted = _placeholder_lifecycle_is_deleted(row)
            media_linked = bool(getattr(row, "movie_id", None) or getattr(row, "episode_id", None))
            raw = getattr(row, "path", None)
            abs_path = None
            if raw:
                try:
                    abs_path = _norm_scan_path(str(raw))
                except Exception:
                    abs_path = None
            canonical = None
            if eligible and media_linked:
                canonical = _incremental_canonical_path_for_row(session, row, scanned_roots)
            ph_plans.append(
                {
                    "id": pid,
                    "eligible": eligible,
                    "lifecycle_deleted": lifecycle_deleted,
                    "media_linked": media_linked,
                    "abs_path": abs_path,
                    "canonical_path": canonical,
                }
            )

        for mv in session.query(Movie).filter(
            Movie.determination == _NEEDS_PLACEHOLDER,
            Movie.is_deleted == False,  # noqa: E712
        ).all():
            try:
                ap = _norm_scan_path(movie_placeholder_path(mv))
            except Exception:
                continue
            if not looks_like_placeholder_file(ap) or not _is_path_under_roots(ap, scanned_roots):
                continue
            needs_movies.append((int(mv.id), ap))

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
            needs_episodes.append((int(ep.id), int(season.id), int(series.id), ap))
    finally:
        try:
            session.close()
        except Exception:
            pass

    # Disk stats with no DB checkout held.
    actions: list[dict[str, Any]] = []
    for plan in ph_plans:
        pid = int(plan["id"])
        if not plan["eligible"]:
            actions.append(
                {
                    "op": "ineligible",
                    "id": pid,
                    "lifecycle_deleted": bool(plan["lifecycle_deleted"]),
                }
            )
            continue
        abs_path = plan.get("abs_path")
        if not abs_path:
            actions.append({"op": "stale", "id": pid})
            continue
        under = _is_path_under_roots(str(abs_path), scanned_roots)
        if under and os.path.isfile(str(abs_path)):
            actions.append({"op": "refresh", "id": pid, "path": str(abs_path)})
            continue
        canonical = plan.get("canonical_path")
        if canonical and os.path.isfile(str(canonical)):
            actions.append({"op": "refresh", "id": pid, "path": str(canonical)})
            continue
        # Path-only rows: defer orphan stale detection to the scheduled full walk.
        if not plan["media_linked"]:
            continue
        actions.append({"op": "stale", "id": pid})

    for mid, ap in needs_movies:
        if os.path.isfile(ap):
            actions.append({"op": "needs_movie", "movie_id": int(mid), "path": ap})

    for eid, season_id, series_id, ap in needs_episodes:
        if os.path.isfile(ap):
            actions.append(
                {
                    "op": "needs_episode",
                    "episode_id": int(eid),
                    "season_id": int(season_id),
                    "series_id": int(series_id),
                    "path": ap,
                }
            )

    upserted = 0
    refreshed = 0
    stale_marked = 0
    disconnected = 0
    needs_canonical_hits = 0

    for offset in range(0, len(actions), FS_SCAN_DB_CHUNK):
        chunk = actions[offset : offset + FS_SCAN_DB_CHUNK]
        chunk_session = get_session()
        try:
            for action in chunk:
                op = action.get("op")
                if op == "ineligible":
                    row = chunk_session.get(Placeholder, int(action["id"]))
                    if row is None:
                        continue
                    row.has_placeholder = False
                    if hasattr(row, "lifecycle_status") and not bool(action.get("lifecycle_deleted")):
                        row.lifecycle_status = "DELETED"
                    if getattr(row, "path", None):
                        row.path = ""
                    if _disconnect_placeholder_row(row):
                        disconnected += 1
                    chunk_session.add(row)
                    stale_marked += 1
                elif op == "refresh":
                    row = chunk_session.get(Placeholder, int(action["id"]))
                    if row is None:
                        continue
                    abs_p = str(action["path"])
                    if row.path != abs_p:
                        row.path = abs_p
                    row.has_placeholder = True
                    row.last_observed_at = func.now()
                    chunk_session.add(row)
                    refreshed += 1
                elif op == "stale":
                    row = chunk_session.get(Placeholder, int(action["id"]))
                    if row is None:
                        continue
                    row.has_placeholder = False
                    if hasattr(row, "lifecycle_status"):
                        row.lifecycle_status = "MISSING"
                    if _disconnect_placeholder_row(row):
                        disconnected += 1
                    chunk_session.add(row)
                    stale_marked += 1
                elif op == "needs_movie":
                    needs_canonical_hits += 1
                    mid = int(action["movie_id"])
                    ap = str(action["path"])
                    plink = (
                        chunk_session.query(Placeholder)
                        .filter(Placeholder.movie_id == mid)
                        .order_by(Placeholder.id.desc())
                        .first()
                    )
                    if plink:
                        if plink.path != ap:
                            plink.path = ap
                        plink.has_placeholder = True
                        plink.last_observed_at = func.now()
                        chunk_session.add(plink)
                        refreshed += 1
                    else:
                        chunk_session.add(
                            Placeholder(
                                path=ap,
                                movie_id=mid,
                                has_placeholder=True,
                                created_by="fs_scan_incremental",
                                last_observed_at=func.now(),
                            )
                        )
                        upserted += 1
                elif op == "needs_episode":
                    needs_canonical_hits += 1
                    eid = int(action["episode_id"])
                    ap = str(action["path"])
                    plink = (
                        chunk_session.query(Placeholder)
                        .filter(Placeholder.episode_id == eid)
                        .order_by(Placeholder.id.desc())
                        .first()
                    )
                    if plink:
                        if plink.path != ap:
                            plink.path = ap
                        plink.has_placeholder = True
                        plink.last_observed_at = func.now()
                        chunk_session.add(plink)
                        refreshed += 1
                    else:
                        chunk_session.add(
                            Placeholder(
                                path=ap,
                                episode_id=eid,
                                series_id=int(action["series_id"]),
                                season_id=int(action["season_id"]),
                                has_placeholder=True,
                                created_by="fs_scan_incremental",
                                last_observed_at=func.now(),
                            )
                        )
                        upserted += 1
            chunk_session.commit()
        except Exception:
            try:
                chunk_session.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                chunk_session.close()
            except Exception:
                pass

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


def scan_placeholder_roots(
    roots: list[str],
    *,
    full_scan: bool = True,
    on_progress=None,
) -> tuple[int, dict[str, Any]]:
    """Walk placeholder roots.

    ``on_progress(files_seen, media_candidates)`` is optional; called periodically
    during a full walk (total file count is unknown until the walk finishes).
    """
    session = get_session()
    upserted = 0
    stale_marked = 0
    disconnected = 0
    scanned_files = 0
    scanned_media_candidates = 0
    scanned_roots = [os.path.abspath(root) for root in roots if root]
    observed_paths: set[str] = set()
    started_mono = time.monotonic()
    last_live_mono = [0.0]

    def _emit_fs_progress(*, force: bool = False) -> None:
        if not callable(on_progress):
            return
        now = time.monotonic()
        if (
            not force
            and scanned_media_candidates % FS_SCAN_PROGRESS_EVERY_FILES != 0
            and (now - last_live_mono[0]) < 2.0
        ):
            return
        last_live_mono[0] = now
        try:
            on_progress(int(scanned_files), int(scanned_media_candidates))
        except Exception:
            pass

    try:
        if not full_scan:
            # Incremental owns its short sessions; release the opener before plan/stat/write.
            try:
                session.close()
            except Exception:
                pass
            session = None
            upserted, meta = _incremental_placeholder_scan(roots)
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
        session.commit()
        session.close()
        session = None
        logger.info(
            f"FS scan started roots={len(scanned_roots)} full_scan=True "
            f"tombstoned_paths_cleared={healed}",
            extra={'emoji_type': 'info'},
        )
        # Walk with no DB session so os.walk cannot hold row locks.
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
                    if scanned_media_candidates % FS_SCAN_PROGRESS_EVERY_FILES == 0:
                        elapsed = time.monotonic() - started_mono
                        logger.info(
                            "FS scan walk progress: "
                            f"media_candidates={scanned_media_candidates} "
                            f"total_files_seen={scanned_files} "
                            f"elapsed_s={elapsed:.1f}",
                            extra={'emoji_type': 'info'},
                        )
                    _emit_fs_progress()

        observed_list = sorted(observed_paths)
        for offset in range(0, len(observed_list), FS_SCAN_DB_CHUNK):
            chunk = observed_list[offset : offset + FS_SCAN_DB_CHUNK]
            chunk_session = get_session()
            try:
                for path in chunk:
                    existing = _pick_placeholder_row_for_observed_path(chunk_session, path)
                    if existing:
                        existing.has_placeholder = True
                        if hasattr(existing, "lifecycle_status"):
                            existing.lifecycle_status = "ACTIVE"
                        existing.last_observed_at = func.now()
                        chunk_session.add(existing)
                    else:
                        chunk_session.add(
                            Placeholder(
                                path=path,
                                has_placeholder=True,
                                created_by='fs_scan',
                                last_observed_at=func.now(),
                            )
                        )
                        upserted += 1
                chunk_session.commit()
            except Exception:
                try:
                    chunk_session.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    chunk_session.close()
                except Exception:
                    pass

        # Stale-mark active rows not observed (chunked so we do not lock the whole table).
        stale_session = get_session()
        try:
            active_ids = [
                int(r[0])
                for r in stale_session.query(Placeholder.id)
                .filter(Placeholder.has_placeholder == True)  # noqa: E712
                .all()
                if r and r[0] is not None
            ]
        finally:
            try:
                stale_session.close()
            except Exception:
                pass

        for offset in range(0, len(active_ids), FS_SCAN_DB_CHUNK):
            id_chunk = active_ids[offset : offset + FS_SCAN_DB_CHUNK]
            chunk_session = get_session()
            try:
                rows = (
                    chunk_session.query(Placeholder)
                    .filter(Placeholder.id.in_(id_chunk))
                    .all()
                )
                for row in rows:
                    row_path = getattr(row, 'path', None)
                    if not row_path or row_path in observed_paths:
                        continue
                    row.has_placeholder = False
                    if hasattr(row, 'lifecycle_status'):
                        row.lifecycle_status = 'MISSING'
                    if _disconnect_placeholder_row(row):
                        disconnected += 1
                    if hasattr(row, 'updated_at'):
                        row.updated_at = func.now()
                    chunk_session.add(row)
                    stale_marked += 1
                chunk_session.commit()
            except Exception:
                try:
                    chunk_session.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    chunk_session.close()
                except Exception:
                    pass
            if (offset + len(id_chunk)) % FS_STALE_PROGRESS_EVERY_ROWS < FS_SCAN_DB_CHUNK:
                elapsed = time.monotonic() - started_mono
                logger.info(
                    "FS stale-mark progress: "
                    f"checked={min(offset + len(id_chunk), len(active_ids))}/{len(active_ids)} "
                    f"stale_marked={stale_marked} "
                    f"disconnected={disconnected} "
                    f"elapsed_s={elapsed:.1f}",
                    extra={'emoji_type': 'info'},
                )

        _emit_fs_progress(force=True)
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
        if session is not None:
            try:
                session.rollback()
            except Exception:
                pass
        raise
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def scan_once_if_needed(
    run_id: Optional[str] = None,
    *,
    prefer_incremental: bool = False,
    on_progress=None,
):
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
        from services.task_schedule import get_schedule_for_task, schedule_enabled
        use_incremental = bool(prefer_incremental and schedule_enabled(get_schedule_for_task("full_sync")))
        created, scan_meta = scan_placeholder_roots(
            roots,
            full_scan=not use_incremental,
            on_progress=on_progress,
        )
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
