import os
import time
from typing import Optional

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import FSScanRun, Placeholder


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


def scan_placeholder_roots(roots: list[str]) -> int:
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
        logger.info(
            f"FS scan started roots={len(scanned_roots)}",
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

                    existing = session.query(Placeholder).filter(Placeholder.path == path).first()
                    if existing:
                        existing.has_placeholder = True
                        existing.last_observed_at = func.now()
                        session.add(existing)
                    else:
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
        return upserted
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def scan_once_if_needed(run_id: Optional[str] = None):
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
        created = scan_placeholder_roots(roots)
        logger.debug(f'FS scan completed; placeholders created={created}', extra={'emoji_type': 'placeholder'})
        return created, {'reason': 'ok', 'roots': roots}
    except Exception as e:
        logger.error(f'FS scan failed: {e}', extra={'emoji_type': 'error'})
        return 0, {'reason': 'error', 'message': str(e)}
