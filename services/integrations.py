import os
import shutil
import logging
from typing import List, Optional
from core.config import settings

logger = logging.getLogger('services.integrations')


def _safe_join(root: str, *parts: str) -> str:
    root = os.path.abspath(root)
    path = os.path.abspath(os.path.join(root, *parts))
    if not path.startswith(root):
        raise ValueError('Path escapes library root')
    return path


def place_dummy_file(media_type: str, title: str, year: int, media_id: int, library_root: str) -> Optional[str]:
    """Create a simple placeholder file and return its path. Safe, local-only.

    Tests patch os.makedirs/shutil.copy2 so this function will be test-friendly.
    """
    try:
        if not library_root:
            logger.debug('No library root configured for dummy placement')
            return None
        folder = _safe_join(library_root, str(media_id))
        os.makedirs(folder, exist_ok=True)
        filename = f"{title.replace(' ', '_')}_{media_id}.dummy"
        path = os.path.join(folder, filename)
        # create an empty file if it doesn't exist
        if not os.path.exists(path):
            with open(path, 'wb') as fh:
                fh.write(b'')
        logger.info(f"Placed dummy file at {path}", extra={'emoji_type': 'create'})
        return path
    except Exception as e:
        logger.error(f"place_dummy_file failed: {e}", extra={'emoji_type': 'error'})
        return None


def delete_dummy_file(path: str) -> bool:
    try:
        if path and os.path.exists(path):
            os.remove(path)
            logger.info(f"Deleted dummy file {path}")
            return True
        return False
    except Exception as e:
        logger.error(f"delete_dummy_file failed: {e}")
        return False


def create_dummy_movie_folder(movie_id: int, library_root: str) -> Optional[str]:
    try:
        if not library_root:
            return None
        path = _safe_join(library_root, str(movie_id))
        os.makedirs(path, exist_ok=True)
        return path
    except Exception as e:
        logger.error(f"create_dummy_movie_folder failed: {e}")
        return None


def create_dummy_series_folder(series_id: int, library_root: str) -> Optional[str]:
    return create_dummy_movie_folder(series_id, library_root)


def delete_folder(path: str) -> bool:
    try:
        if path and os.path.exists(path):
            shutil.rmtree(path)
            return True
        return False
    except Exception as e:
        logger.error(f"delete_folder failed: {e}")
        return False


def update_placeholder_status(session, ent_id: int, model) -> bool:
    try:
        if not session or not model:
            return False
        obj = session.query(model).get(ent_id)
        if not obj:
            return False
        # mark placeholder_exists if dummypath is set
        try:
            obj.placeholder_exists = bool(getattr(obj, 'dummypath', None))
            session.add(obj)
            session.commit()
            return True
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
            return False
    except Exception:
        return False


def delayed_placeholders(session, ent_id: int, model) -> bool:
    # Lightweight placeholder: no-op for now (kept for flow compatibility)
    return True


def flow_attach_dummypaths(session, ent_id: int, model) -> bool:
    """Attach a dummypath to a movie/episode/series record if missing."""
    try:
        obj = session.query(model).get(ent_id)
        if not obj:
            return False
        # prefer configured MOVIE or TV library
        lib = settings.MOVIE_LIBRARY_FOLDER if hasattr(obj, 'moviefile_path') or hasattr(obj, 'tmdbid') else settings.TV_LIBRARY_FOLDER
        path = place_dummy_file('movie' if lib == settings.MOVIE_LIBRARY_FOLDER else 'tv', getattr(obj, 'title', 'unknown'), getattr(obj, 'year', 0), getattr(obj, 'id', 0), lib)
        if path:
            try:
                obj.dummypath = path
                session.add(obj)
                session.commit()
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
        return True
    except Exception as e:
        logger.error(f"flow_attach_dummypaths failed: {e}")
        return False


def flow_enrich_series(session, ent_id: int, model) -> bool:
    # Lightweight stub for enrichment
    return True


def trigger_radarr_search(radarr_id: int, title: str) -> bool:
    # No external call: stubbed
    logger.info(f"Stub trigger_radarr_search for id={radarr_id} title={title}")
    return True


def trigger_sonarr_search(series_id: int, episode_ids: List[int] = None, series_title: str = None, is_4k: bool = False) -> bool:
    logger.info(f"Stub trigger_sonarr_search for series_id={series_id} episode_ids={episode_ids}")
    return True


def api_monitor_episodes(sonarr_id: int, episode_ids: List[int], is_4k: bool = False) -> bool:
    logger.info(f"Stub api_monitor_episodes sonarr_id={sonarr_id} episodes={episode_ids}")
    return True


def mark_movie_monitored(radarr_id: int, is_4k: bool = False) -> bool:
    logger.info(f"Stub mark_movie_monitored radarr_id={radarr_id} is_4k={is_4k}")
    return True


def check_all_arr_webhooks() -> bool:
    # Simple check: return True if any ARR URL+API_KEY present
    cfg_ok = any([
        getattr(settings, 'RADARR_URL', None) and getattr(settings, 'RADARR_API_KEY', None),
        getattr(settings, 'RADARR_4K_URL', None) and getattr(settings, 'RADARR_4K_API_KEY', None),
        getattr(settings, 'SONARR_URL', None) and getattr(settings, 'SONARR_API_KEY', None),
        getattr(settings, 'SONARR_4K_URL', None) and getattr(settings, 'SONARR_4K_API_KEY', None),
    ])
    return bool(cfg_ok)
