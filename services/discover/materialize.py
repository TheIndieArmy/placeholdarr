"""Create/remove Discover movie placeholders on disk."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, or_

from core.config import settings
from core.logger import logger
from services.discover.mode import is_tmdb_discover_mode
from services.library_destinations import default_movie_dest_folder
from services.placeholder_poster_art import ensure_movie_art
from services.placeholders import (
    ensure_movie_nfo,
    ensure_placeholder_file,
    sanitize_filename,
)
from services.poster_overlay import is_discover_stub_poster, write_discover_stub_poster
from services.postgres.db import get_session
from services.postgres.models import Placeholder, TmdbMovie
from services.source_of_truth.determiner import (
    DETERMINATION_EXISTS,
    DETERMINATION_NEEDS,
    DETERMINATION_OBSOLETE,
)


def _movie_root() -> str:
    """Filesystem root for Discover movie placeholders.

    Prefer ``DISCOVER_MOVIE_LIBRARY_FOLDER`` / ``DISCOVER_LIBRARY_ROOT/movies`` so
    Discover never writes into the Arr ``LIBRARY_ROOT`` tree when a Discover root
    is configured.
    """
    discover = str(getattr(settings, "DISCOVER_MOVIE_LIBRARY_FOLDER", "") or "").strip()
    if not discover:
        discover_root = str(getattr(settings, "DISCOVER_LIBRARY_ROOT", "") or "").strip().rstrip("/")
        if discover_root:
            discover = os.path.join(discover_root, "movies")
    if discover:
        return discover
    try:
        return default_movie_dest_folder()
    except Exception:
        root = str(getattr(settings, "LIBRARY_ROOT", "") or "").rstrip("/")
        return os.path.join(root, "movies") if root else "movies"


def _path_under_root(path: str | None, root: str | None) -> bool:
    p = os.path.normpath(str(path or "").strip())
    r = os.path.normpath(str(root or "").strip())
    if not p or not r or p in {".", "/"} or r in {".", "/"}:
        return False
    try:
        return os.path.commonpath([os.path.abspath(p), os.path.abspath(r)]) == os.path.abspath(r)
    except ValueError:
        return False


def tmdb_movie_placeholder_path(row: TmdbMovie) -> str:
    title = sanitize_filename(row.title)
    year = row.year
    root = _movie_root()
    desired_folder = os.path.join(
        root,
        f"{title} ({year}) {{tmdb-{row.tmdb_id}}}" if year else f"{title} {{tmdb-{row.tmdb_id}}}",
    )
    pinned = str(getattr(row, "placeholder_folder", None) or "").strip()
    # Honor a pin only when it already lives under the Discover (or fallback) root.
    # Older Discover runs pinned folders under Arr LIBRARY_ROOT; ignore those so
    # rematerialize relocates into Discover Library Root.
    if pinned and _path_under_root(pinned, root):
        folder = pinned
    else:
        folder = desired_folder
    year_part = f" ({year})" if year else ""
    filename = f"{title}{year_part}.mp4"
    return os.path.join(folder, filename)


def _nfo_proxy(row: TmdbMovie) -> SimpleNamespace:
    return SimpleNamespace(
        title=row.title,
        year=row.year or 0,
        tmdbid=row.tmdb_id,
        imdbid=None,
        radarr_overview=row.overview,
        remote_poster=row.remote_poster,
        localized_poster=None,
        radarr_runtime=None,
        radarr_certification=None,
        radarr_genres=None,
        radarr_studio=None,
        radarr_ratings=None,
        radarr_collection=None,
        radarr_actors=None,
        radarr_directors=None,
        radarr_credits=None,
        radarr_trailer=None,
        radarr_premiered=None,
    )


def apply_tmdb_movie_materialization(
    tmdb_id: int,
    *,
    session=None,
    defer_media_refresh: bool = False,
) -> dict[str, Any]:
    """Create/remove placeholder + NFO only. Poster art is a separate pipeline stage."""
    own = session is None
    session = session or get_session()
    result: dict[str, Any] = {"tmdb_id": tmdb_id, "action": None, "refresh_folder": None}
    try:
        row = session.get(TmdbMovie, int(tmdb_id))
        if row is None:
            result["action"] = "missing"
            return result
        det = row.determination or DETERMINATION_NEEDS
        target = tmdb_movie_placeholder_path(row)
        folder = os.path.dirname(target)

        if det == DETERMINATION_OBSOLETE:
            ph = session.query(Placeholder).filter(Placeholder.tmdb_movie_id == row.tmdb_id).all()
            for p in ph:
                try:
                    if p.path and os.path.isfile(p.path):
                        os.remove(p.path)
                except OSError:
                    pass
                session.delete(p)
            for name in ("poster.jpg", "poster-grid.jpg", "folder.jpg", f"{sanitize_filename(row.title)}.nfo"):
                try:
                    p = os.path.join(folder, name)
                    if os.path.isfile(p):
                        os.remove(p)
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
            os.makedirs(folder, exist_ok=True)
            ensure_placeholder_file(target)
            try:
                ensure_movie_nfo(target, _nfo_proxy(row))
            except Exception as exc:
                logger.warning(f"Discover NFO failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})
            try:
                if write_discover_stub_poster(folder, title=str(row.title or ""), year=row.year):
                    result["stub_poster"] = True
            except Exception as exc:
                logger.warning(f"Discover stub poster failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})

            ph = session.query(Placeholder).filter(Placeholder.tmdb_movie_id == row.tmdb_id).first()
            if ph is None:
                ph = Placeholder(tmdb_movie_id=row.tmdb_id, path=target, created_by="tmdb_discover")
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
                    logger.debug(f"Discover media refresh skipped: {exc}", extra={"emoji_type": "debug"})
            return result

        # Existing placeholders: relocate out of Arr LIBRARY_ROOT when Discover root is set,
        # otherwise fill missing NFO / stub poster (real art is art_backfill).
        if det == DETERMINATION_EXISTS and row.has_placeholder:
            old_folder = str(getattr(row, "placeholder_folder", None) or "").strip()
            old_path = str(getattr(row, "placeholder_filepath", None) or "").strip()
            needs_relocate = bool(old_folder) and os.path.normpath(old_folder) != os.path.normpath(folder)
            if needs_relocate:
                os.makedirs(folder, exist_ok=True)
                ensure_placeholder_file(target)
                try:
                    ensure_movie_nfo(target, _nfo_proxy(row))
                except Exception as exc:
                    logger.warning(f"Discover NFO failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})
                try:
                    if write_discover_stub_poster(folder, title=str(row.title or ""), year=row.year):
                        result["stub_poster"] = True
                except Exception as exc:
                    logger.warning(f"Discover stub poster failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})

                ph = session.query(Placeholder).filter(Placeholder.tmdb_movie_id == row.tmdb_id).first()
                if ph is None:
                    ph = Placeholder(tmdb_movie_id=row.tmdb_id, path=target, created_by="tmdb_discover")
                    session.add(ph)
                ph.path = target
                ph.has_placeholder = True
                ph.determination = DETERMINATION_EXISTS
                row.has_placeholder = True
                row.placeholder_filepath = target
                row.placeholder_folder = folder
                row.determination_updated_at = datetime.now(timezone.utc)
                session.commit()

                # Best-effort cleanup of the previous Arr-tree folder.
                for p in (old_path,):
                    try:
                        if p and os.path.isfile(p) and os.path.normpath(p) != os.path.normpath(target):
                            os.remove(p)
                    except OSError:
                        pass
                if old_folder and os.path.normpath(old_folder) != os.path.normpath(folder):
                    for name in (
                        "poster.jpg",
                        "poster-grid.jpg",
                        "folder.jpg",
                        f"{sanitize_filename(row.title)}.nfo",
                        ".poster-overlay.json",
                    ):
                        try:
                            p = os.path.join(old_folder, name)
                            if os.path.isfile(p):
                                os.remove(p)
                        except OSError:
                            pass
                    try:
                        if os.path.isdir(old_folder) and not os.listdir(old_folder):
                            os.rmdir(old_folder)
                    except OSError:
                        pass

                result["action"] = "relocated"
                result["path"] = target
                result["refresh_folder"] = folder
                result["old_folder"] = old_folder
                if not defer_media_refresh:
                    try:
                        from services.media_servers.refresh import refresh_all_paths

                        refresh_paths = {folder}
                        if old_folder:
                            refresh_paths.add(old_folder)
                        refresh_all_paths(refresh_paths, update_type="Created")
                    except Exception as exc:
                        logger.debug(f"Discover media refresh skipped: {exc}", extra={"emoji_type": "debug"})
                return result

            nfo_name = f"{sanitize_filename(row.title)}.nfo"
            nfo_path = os.path.join(folder, nfo_name)
            if not os.path.isfile(nfo_path):
                try:
                    ensure_movie_nfo(target, _nfo_proxy(row))
                except Exception as exc:
                    logger.warning(f"Discover NFO refresh failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})
            poster_path = os.path.join(folder, "poster.jpg")
            if not os.path.isfile(poster_path) or is_discover_stub_poster(folder):
                try:
                    if write_discover_stub_poster(folder, title=str(row.title or ""), year=row.year):
                        result["stub_poster"] = True
                except Exception as exc:
                    logger.warning(
                        f"Discover stub poster refresh failed tmdb={tmdb_id}: {exc}",
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


def apply_tmdb_movie_art(
    tmdb_id: int,
    *,
    session=None,
    force: bool = False,
) -> dict[str, Any]:
    """Download poster art for an existing Discover placeholder when missing or stub."""
    own = session is None
    session = session or get_session()
    result: dict[str, Any] = {"tmdb_id": tmdb_id, "action": None, "refresh_folder": None}
    try:
        row = session.get(TmdbMovie, int(tmdb_id))
        if row is None or not row.has_placeholder:
            result["action"] = "skip"
            return result
        target = row.placeholder_filepath or tmdb_movie_placeholder_path(row)
        folder = row.placeholder_folder or os.path.dirname(target)
        poster_path = os.path.join(folder, "poster.jpg")
        stub = is_discover_stub_poster(folder)
        if not force and os.path.isfile(poster_path) and not stub:
            result["action"] = "exists"
            result["refresh_folder"] = folder
            return result
        try:
            ensure_movie_art(_nfo_proxy(row), target)
            result["action"] = "wrote"
            result["refresh_folder"] = folder
            result["replaced_stub"] = stub
        except Exception as exc:
            logger.warning(f"Discover art failed tmdb={tmdb_id}: {exc}", extra={"emoji_type": "warning"})
            result["action"] = "error"
            result["error"] = str(exc)
        return result
    finally:
        if own:
            session.close()


def run_discover_materialization(*, session=None, limit: int | None = None) -> dict[str, Any]:
    """Create/remove placeholders and NFOs (no poster downloads).

    Only actionable rows are visited:
    - ``needs_placeholder`` (create)
    - ``obsolete_placeholder`` (remove)
    - ``placeholder_exists`` with ``has_placeholder`` false (repair)

    Healthy ``exists`` titles are skipped so steady syncs do not re-walk the whole library.
    """
    if not is_tmdb_discover_mode():
        return {"skipped": True}
    own = session is None
    session = session or get_session()
    stats = {
        "created": 0,
        "removed": 0,
        "relocated": 0,
        "noop": 0,
        "errors": 0,
        "media_refresh_folders": 0,
        "candidates": 0,
        "relocate_candidates": 0,
        "skipped_healthy_exists": True,
    }
    try:
        q = session.query(TmdbMovie).filter(
            or_(
                TmdbMovie.determination == DETERMINATION_NEEDS,
                TmdbMovie.determination == DETERMINATION_OBSOLETE,
                and_(
                    TmdbMovie.determination == DETERMINATION_EXISTS,
                    TmdbMovie.has_placeholder.is_(False),
                ),
            )
        )
        rows = q.order_by(TmdbMovie.tmdb_id.asc()).all()

        # Relocate Discover titles that still sit under Arr LIBRARY_ROOT (or any
        # path outside the configured Discover movie root).
        root = _movie_root()
        relocate_rows: list[TmdbMovie] = []
        seen_ids = {int(r.tmdb_id) for r in rows}
        if root:
            for row in (
                session.query(TmdbMovie)
                .filter(
                    TmdbMovie.determination == DETERMINATION_EXISTS,
                    TmdbMovie.has_placeholder.is_(True),
                )
                .order_by(TmdbMovie.tmdb_id.asc())
                .all()
            ):
                tid = int(row.tmdb_id)
                if tid in seen_ids:
                    continue
                pinned = str(getattr(row, "placeholder_folder", None) or "").strip()
                if pinned and not _path_under_root(pinned, root):
                    relocate_rows.append(row)
                    seen_ids.add(tid)

        rows = list(rows) + relocate_rows
        if limit is not None:
            rows = rows[: int(limit)]
        ids = [int(r.tmdb_id) for r in rows]
        relocate_ids = {int(r.tmdb_id) for r in relocate_rows}
        needs = sum(1 for r in rows if r.determination == DETERMINATION_NEEDS)
        exists_repair = sum(
            1
            for r in rows
            if r.determination == DETERMINATION_EXISTS and not r.has_placeholder
        )
        obsolete = sum(1 for r in rows if r.determination == DETERMINATION_OBSOLETE)
        relocate = sum(1 for tid in ids if tid in relocate_ids)
        stats["candidates"] = len(ids)
        stats["relocate_candidates"] = relocate
    finally:
        if own:
            session.close()
            session = None

    total = len(ids)
    logger.info(
        f"Discover materialize: {total} actionable title(s) "
        f"(needs={needs} exists_repair={exists_repair} obsolete={obsolete} "
        f"relocate={stats.get('relocate_candidates', 0)}; skipping healthy exists)",
        extra={"emoji_type": "info"},
    )

    refresh_folders: set[str] = set()
    for i, tid in enumerate(ids, start=1):
        try:
            out = apply_tmdb_movie_materialization(tid, defer_media_refresh=True)
            act = out.get("action")
            if act == "created":
                stats["created"] += 1
            elif act == "removed":
                stats["removed"] += 1
            elif act == "relocated":
                stats["relocated"] = int(stats.get("relocated") or 0) + 1
            else:
                stats["noop"] += 1
            folder = out.get("refresh_folder")
            if folder and act in {"created", "removed", "relocated"}:
                refresh_folders.add(str(folder))
            old_folder = out.get("old_folder")
            if old_folder and act == "relocated":
                refresh_folders.add(str(old_folder))
        except Exception as exc:
            stats["errors"] += 1
            logger.error(f"Discover materialize tmdb={tid} failed: {exc}", extra={"emoji_type": "error"})
        if total and (i == 1 or i % 200 == 0 or i == total):
            logger.info(
                f"Discover materialize: {i}/{total} "
                f"(created={stats['created']} removed={stats['removed']} "
                f"relocated={stats.get('relocated', 0)} noop={stats['noop']} "
                f"errors={stats['errors']})",
                extra={"emoji_type": "info"},
            )

    if refresh_folders:
        logger.info(
            f"Discover materialize: batch media refresh for {len(refresh_folders)} folder(s)...",
            extra={"emoji_type": "gear"},
        )
        try:
            from services.media_servers.refresh import refresh_all_paths

            refresh_all_paths(refresh_folders, update_type="Created")
            stats["media_refresh_folders"] = len(refresh_folders)
        except Exception as exc:
            logger.warning(f"Discover batch media refresh failed: {exc}", extra={"emoji_type": "warning"})
    return stats


def run_discover_art_backfill(
    *,
    session=None,
    limit: int | None = None,
    phase_tracker=None,
) -> dict[str, Any]:
    """Download missing poster art for titles that already have placeholders."""
    if not is_tmdb_discover_mode():
        return {"skipped": True}
    own = session is None
    session = session or get_session()
    try:
        q = (
            session.query(TmdbMovie)
            .filter(TmdbMovie.has_placeholder.is_(True))
            .order_by(TmdbMovie.tmdb_id.asc())
        )
        rows = q.all()
        if limit is not None:
            rows = rows[: int(limit)]
        work: list[tuple[int, str]] = []
        for r in rows:
            folder = r.placeholder_folder or (
                os.path.dirname(r.placeholder_filepath) if r.placeholder_filepath else None
            )
            if not folder:
                folder = os.path.dirname(tmdb_movie_placeholder_path(r))
            poster = os.path.join(folder, "poster.jpg")
            if (not os.path.isfile(poster)) or is_discover_stub_poster(folder):
                work.append((int(r.tmdb_id), folder))
    finally:
        if own:
            session.close()

    total = len(work)
    stats = {"wrote": 0, "exists": 0, "errors": 0, "skipped": 0, "media_refresh_folders": 0}
    logger.info(
        f"Discover art backfill: {total} title(s) missing or stub poster.jpg",
        extra={"emoji_type": "info"},
    )
    if not total:
        return stats

    refresh_folders: set[str] = set()
    for i, (tid, folder) in enumerate(work, start=1):
        try:
            out = apply_tmdb_movie_art(tid)
            act = out.get("action")
            if act == "wrote":
                stats["wrote"] += 1
                if out.get("refresh_folder"):
                    refresh_folders.add(str(out["refresh_folder"]))
            elif act == "exists":
                stats["exists"] += 1
            elif act == "error":
                stats["errors"] += 1
            else:
                stats["skipped"] += 1
        except Exception as exc:
            stats["errors"] += 1
            logger.error(f"Discover art backfill tmdb={tid} failed: {exc}", extra={"emoji_type": "error"})
        if i == 1 or i % 50 == 0 or i == total:
            logger.info(
                f"Discover art backfill: {i}/{total} "
                f"(wrote={stats['wrote']} errors={stats['errors']})",
                extra={"emoji_type": "info"},
            )
            if phase_tracker is not None:
                phase_tracker.update_metrics(
                    "art",
                    [
                        {"label": "Progress", "value": f"{i}/{total}"},
                        {"label": "Posters wrote", "value": int(stats["wrote"])},
                        {"label": "Errors", "value": int(stats["errors"])},
                    ],
                )

    if refresh_folders:
        logger.info(
            f"Discover art backfill: batch media refresh for {len(refresh_folders)} folder(s)...",
            extra={"emoji_type": "gear"},
        )
        try:
            from services.media_servers.refresh import refresh_all_paths

            refresh_all_paths(refresh_folders, update_type="Modified")
            stats["media_refresh_folders"] = len(refresh_folders)
        except Exception as exc:
            logger.warning(f"Discover art media refresh failed: {exc}", extra={"emoji_type": "warning"})
    return stats
