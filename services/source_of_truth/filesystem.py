import os
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
    scanned_roots = [os.path.abspath(root) for root in roots if root]
    observed_paths: set[str] = set()
    try:
        for root in roots:
            if not root:
                continue
            if not os.path.isdir(root):
                logger.warning(f'FS-scan root not found: {root}', extra={'emoji_type': 'warning'})
                continue

            for dirpath, _, filenames in os.walk(root):
                for filename in filenames:
                    path = os.path.join(dirpath, filename)
                    if not looks_like_placeholder_file(path):
                        continue
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

        # Mark stale placeholder rows as missing whenever they were previously active
        # but were not observed in this scan. This intentionally includes rows whose
        # path drifted outside configured roots to self-heal legacy mismatches.
        active_query = session.query(Placeholder).filter(Placeholder.has_placeholder == True)  # noqa: E712

        for row in active_query.all():
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

        session.commit()
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
