"""Create/remove Discover show-level series stubs (series folder + dummy S01E01)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, or_

from core.config import settings
from core.logger import logger
from services.discover.mode import is_tmdb_discover_mode
from services.library_destinations import default_tv_dest_folder
from services.placeholder_poster_art import ensure_series_art
from services.placeholders import (
    ensure_episode_nfo,
    ensure_placeholder_file,
    ensure_series_nfo,
    sanitize_filename,
    _apply_dir_chain_permissions,
    _ensure_open_permissions,
)
from services.poster_overlay import is_discover_stub_poster, write_discover_stub_poster
from services.postgres.db import get_session
from services.postgres.models import Placeholder, TmdbSeries
from services.source_of_truth.determiner import (
    DETERMINATION_EXISTS,
    DETERMINATION_NEEDS,
    DETERMINATION_OBSOLETE,
)


def _tv_root() -> str:
    discover = str(getattr(settings, "DISCOVER_TV_LIBRARY_FOLDER", "") or "").strip()
    if not discover:
        discover_root = str(getattr(settings, "DISCOVER_LIBRARY_ROOT", "") or "").strip().rstrip("/")
        if discover_root:
            discover = os.path.join(discover_root, "tv")
    if discover:
        return discover
    try:
        return default_tv_dest_folder()
    except Exception:
        root = str(getattr(settings, "LIBRARY_ROOT", "") or "").rstrip("/")
        return os.path.join(root, "tv") if root else "tv"


def _path_under_root(path: str | None, root: str | None) -> bool:
    p = os.path.normpath(str(path or "").strip())
    r = os.path.normpath(str(root or "").strip())
    if not p or not r or p in {".", "/"} or r in {".", "/"}:
        return False
    try:
        return os.path.commonpath([os.path.abspath(p), os.path.abspath(r)]) == os.path.abspath(r)
    except ValueError:
        return False


def tmdb_series_folder_path(row: TmdbSeries) -> str:
    title = sanitize_filename(row.title)
    year = row.year
    root = _tv_root()
    desired_folder = os.path.join(
        root,
        f"{title} ({year}) {{tmdb-{row.tmdb_id}}}" if year else f"{title} {{tmdb-{row.tmdb_id}}}",
    )
    pinned = str(getattr(row, "placeholder_folder", None) or "").strip()
    if pinned and _path_under_root(pinned, root):
        return pinned
    return desired_folder


def tmdb_series_placeholder_path(row: TmdbSeries) -> str:
    """Playable dummy episode path under Season 01."""
    folder = tmdb_series_folder_path(row)
    title = sanitize_filename(row.title)
    season_dir = os.path.join(folder, "Season 01")
    filename = f"{title} - s01e01 - Discover Stub.mp4"
    return os.path.join(season_dir, filename)


def _series_genre_names(genre_ids: Any) -> list[str] | None:
    if not isinstance(genre_ids, list) or not genre_ids:
        return None
    try:
        from services.tmdb_client import fetch_genres

        by_id = {
            int(g["id"]): str(g.get("name") or "").strip()
            for g in fetch_genres("tv")
            if g.get("id") is not None
        }
    except Exception as exc:
        logger.debug(f"Discover TV genre map unavailable: {exc}", extra={"emoji_type": "debug"})
        return None
    names: list[str] = []
    seen: set[str] = set()
    for raw in genre_ids:
        try:
            gid = int(raw)
        except (TypeError, ValueError):
            continue
        name = by_id.get(gid) or ""
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names or None


def _series_nfo_proxy(row: TmdbSeries) -> SimpleNamespace:
    first_aired = str(getattr(row, "first_air_date", None) or "").strip() or None
    return SimpleNamespace(
        title=row.title,
        year=row.year or 0,
        tvdbid=row.tvdb_id,
        imdbid=None,
        sonarr_tmdbid=row.tmdb_id,
        sonarr_tvmazeid=None,
        sonarr_series_overview=row.overview,
        sonarr_first_aired=first_aired,
        sonarr_network=None,
        sonarr_certification=None,
        sonarr_ratings=None,
        sonarr_genres=_series_genre_names(getattr(row, "genre_ids", None)),
        sonarr_actors=None,
        sonarr_runtime=None,
        remote_poster=row.remote_poster,
        localized_poster=None,
        placeholder_folder=None,
        placeholder_status="REQUEST",
    )


def _episode_proxies(row: TmdbSeries) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    series = _series_nfo_proxy(row)
    season = SimpleNamespace(season_number=1)
    episode = SimpleNamespace(
        title="Discover Stub",
        episode_number=1,
        absolute_episode_number=1,
        overview="Placeholdarr Discover show-level stub. Play to add this series in Sonarr.",
        air_date=getattr(row, "first_air_date", None),
        sonarr_runtime=None,
        placeholder_status="REQUEST",
    )
    return episode, season, series


def apply_tmdb_series_materialization(
    tmdb_id: int,
    *,
    session=None,
    defer_media_refresh: bool = False,
) -> dict[str, Any]:
    """Create/remove show-level stub (series folder + dummy S01E01)."""
    own = session is None
    session = session or get_session()
    result: dict[str, Any] = {"tmdb_id": tmdb_id, "action": None, "refresh_folder": None}
    try:
        row = session.get(TmdbSeries, int(tmdb_id))
        if row is None:
            result["action"] = "missing"
            return result
        det = row.determination or DETERMINATION_NEEDS
        target = tmdb_series_placeholder_path(row)
        folder = tmdb_series_folder_path(row)
        season_dir = os.path.dirname(target)

        if det == DETERMINATION_OBSOLETE:
            ph = session.query(Placeholder).filter(Placeholder.tmdb_series_id == row.tmdb_id).all()
            for p in ph:
                try:
                    if p.path and os.path.isfile(p.path):
                        os.remove(p.path)
                    nfo = os.path.splitext(p.path or "")[0] + ".nfo"
                    if os.path.isfile(nfo):
                        os.remove(nfo)
                except OSError:
                    pass
                session.delete(p)
            for name in ("poster.jpg", "poster-grid.jpg", "folder.jpg", "tvshow.nfo", ".poster-overlay.json"):
                try:
                    p = os.path.join(folder, name)
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
            try:
                if os.path.isdir(season_dir) and not os.listdir(season_dir):
                    os.rmdir(season_dir)
            except OSError:
                pass
            row.has_placeholder = False
            row.placeholder_filepath = None
            row.placeholder_folder = None
            row.determination = "not_needed"
            row.determination_updated_at = datetime.now(timezone.utc)
            session.commit()
            result["action"] = "removed"
            result["refresh_folder"] = folder
            return result

        if det == DETERMINATION_NEEDS or (det == DETERMINATION_EXISTS and not row.has_placeholder):
            os.makedirs(season_dir, exist_ok=True)
            _ensure_open_permissions(folder, is_dir=True)
            _ensure_open_permissions(season_dir, is_dir=True)
            ensure_placeholder_file(target)
            _apply_dir_chain_permissions(target)
            try:
                ensure_series_nfo(_series_nfo_proxy(row), folder=folder)
            except Exception as exc:
                logger.warning(f"Discover series NFO failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})
            try:
                ep, sn, sr = _episode_proxies(row)
                ensure_episode_nfo(target, ep, sn, sr)
            except Exception as exc:
                logger.warning(f"Discover episode NFO failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})
            try:
                if write_discover_stub_poster(
                    folder, title=str(row.title or ""), year=row.year, also_folder_jpg=True
                ):
                    result["stub_poster"] = True
            except Exception as exc:
                logger.warning(f"Discover series stub poster failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})

            ph = session.query(Placeholder).filter(Placeholder.tmdb_series_id == row.tmdb_id).first()
            if ph is None:
                ph = Placeholder(tmdb_series_id=row.tmdb_id, path=target, created_by="tmdb_discover")
                session.add(ph)
            ph.path = target
            ph.has_placeholder = True
            ph.determination = DETERMINATION_EXISTS
            row.has_placeholder = True
            row.placeholder_filepath = target
            row.placeholder_folder = folder
            row.determination = DETERMINATION_EXISTS
            row.determination_updated_at = datetime.now(timezone.utc)
            session.commit()
            result["action"] = "created"
            result["path"] = target
            result["refresh_folder"] = folder
            if not defer_media_refresh:
                try:
                    from services.media_servers.refresh import refresh_all_paths

                    refresh_all_paths({folder}, update_type="Created")
                except Exception as exc:
                    logger.debug(f"Discover series media refresh skipped: {exc}", extra={"emoji_type": "debug"})
            return result

        if det == DETERMINATION_EXISTS and row.has_placeholder:
            try:
                ensure_series_nfo(_series_nfo_proxy(row), folder=folder)
                ep, sn, sr = _episode_proxies(row)
                ensure_episode_nfo(target, ep, sn, sr)
            except Exception as exc:
                logger.warning(f"Discover series NFO refresh failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})
            poster_path = os.path.join(folder, "poster.jpg")
            if not os.path.isfile(poster_path) or is_discover_stub_poster(folder):
                try:
                    if write_discover_stub_poster(
                        folder, title=str(row.title or ""), year=row.year, also_folder_jpg=True
                    ):
                        result["stub_poster"] = True
                except Exception as exc:
                    logger.warning(
                        f"Discover series stub poster refresh failed tmdb={tmdb_id}: {exc}",
                        extra={"emoji_type": "warning"},
                    )
            result["action"] = "noop"
            result["refresh_folder"] = folder
            return result

        result["action"] = "noop"
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        if own:
            session.close()


def apply_tmdb_series_art(
    tmdb_id: int,
    *,
    session=None,
    force: bool = False,
) -> dict[str, Any]:
    own = session is None
    session = session or get_session()
    result: dict[str, Any] = {"tmdb_id": tmdb_id, "action": None, "refresh_folder": None}
    try:
        row = session.get(TmdbSeries, int(tmdb_id))
        if row is None or not row.has_placeholder:
            result["action"] = "skip"
            return result
        folder = row.placeholder_folder or tmdb_series_folder_path(row)
        try:
            ensure_series_nfo(_series_nfo_proxy(row), folder=folder)
        except Exception as exc:
            logger.warning(f"Discover series NFO during art failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})
        poster_path = os.path.join(folder, "poster.jpg")
        stub = is_discover_stub_poster(folder)
        if not force and os.path.isfile(poster_path) and not stub:
            result["action"] = "exists"
            result["refresh_folder"] = folder
            return result
        try:
            ensure_series_art(_series_nfo_proxy(row), folder)
            result["action"] = "wrote"
            result["refresh_folder"] = folder
            result["replaced_stub"] = stub
        except Exception as exc:
            logger.warning(f"Discover series art failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})
            result["action"] = "error"
            result["error"] = str(exc)
        return result
    finally:
        if own:
            session.close()


def run_discover_series_materialization(*, session=None, limit: int | None = None) -> dict[str, Any]:
    if not is_tmdb_discover_mode():
        return {"skipped": True}
    own = session is None
    session = session or get_session()
    stats = {
        "created": 0,
        "removed": 0,
        "noop": 0,
        "errors": 0,
        "media_refresh_folders": 0,
        "candidates": 0,
    }
    try:
        q = session.query(TmdbSeries).filter(
            or_(
                TmdbSeries.determination == DETERMINATION_NEEDS,
                TmdbSeries.determination == DETERMINATION_OBSOLETE,
                and_(
                    TmdbSeries.determination == DETERMINATION_EXISTS,
                    TmdbSeries.has_placeholder.is_(False),
                ),
            )
        )
        rows = q.order_by(TmdbSeries.tmdb_id.asc()).all()
        if limit is not None:
            rows = rows[: int(limit)]
        ids = [int(r.tmdb_id) for r in rows]
        stats["candidates"] = len(ids)
    finally:
        if own:
            session.close()
            session = None

    total = len(ids)
    logger.info(
        f"Discover series materialize: {total} actionable title(s)",
        extra={"emoji_type": "info"},
    )
    refresh_folders: set[str] = set()
    for i, tid in enumerate(ids, start=1):
        try:
            out = apply_tmdb_series_materialization(tid, defer_media_refresh=True)
            act = out.get("action")
            if act == "created":
                stats["created"] += 1
            elif act == "removed":
                stats["removed"] += 1
            else:
                stats["noop"] += 1
            folder = out.get("refresh_folder")
            if folder and act in {"created", "removed"}:
                refresh_folders.add(str(folder))
        except Exception as exc:
            stats["errors"] += 1
            logger.error(f"Discover series materialize tmdb={tid} failed: {exc}", extra={"emoji_type": "error"})
        if total and (i == 1 or i % 200 == 0 or i == total):
            logger.info(
                f"Discover series materialize: {i}/{total} "
                f"(created={stats['created']} removed={stats['removed']} "
                f"noop={stats['noop']} errors={stats['errors']})",
                extra={"emoji_type": "info"},
            )

    if refresh_folders:
        try:
            from services.media_servers.refresh import refresh_all_paths

            refresh_all_paths(refresh_folders, update_type="Created")
            stats["media_refresh_folders"] = len(refresh_folders)
        except Exception as exc:
            logger.warning(f"Discover series batch media refresh failed: {exc}", extra={"emoji_type": "warning"})
    return stats


def run_discover_series_art_backfill(*, session=None, limit: int | None = None) -> dict[str, Any]:
    if not is_tmdb_discover_mode():
        return {"skipped": True}
    own = session is None
    session = session or get_session()
    try:
        q = (
            session.query(TmdbSeries)
            .filter(TmdbSeries.has_placeholder.is_(True))
            .order_by(TmdbSeries.tmdb_id.asc())
        )
        rows = q.all()
        if limit is not None:
            rows = rows[: int(limit)]
        work: list[int] = []
        for r in rows:
            folder = r.placeholder_folder or tmdb_series_folder_path(r)
            poster = os.path.join(folder, "poster.jpg")
            if (not os.path.isfile(poster)) or is_discover_stub_poster(folder):
                work.append(int(r.tmdb_id))
    finally:
        if own:
            session.close()

    stats = {"wrote": 0, "exists": 0, "errors": 0, "skipped": 0}
    total = len(work)
    logger.info(
        f"Discover series art backfill: {total} title(s) missing or stub poster.jpg",
        extra={"emoji_type": "info"},
    )
    for i, tid in enumerate(work, start=1):
        try:
            out = apply_tmdb_series_art(tid)
            act = out.get("action")
            if act == "wrote":
                stats["wrote"] += 1
            elif act == "exists":
                stats["exists"] += 1
            elif act == "error":
                stats["errors"] += 1
            else:
                stats["skipped"] += 1
        except Exception as exc:
            stats["errors"] += 1
            logger.error(f"Discover series art tmdb={tid} failed: {exc}", extra={"emoji_type": "error"})
        if total and (i == 1 or i % 200 == 0 or i == total):
            logger.info(
                f"Discover series art backfill: {i}/{total} "
                f"(wrote={stats['wrote']} errors={stats['errors']})",
                extra={"emoji_type": "info"},
            )
    return stats
