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
        # mark has_placeholder/placeholder_filepath if a placeholder path is present
        try:
            # prefer explicit placeholder_filepath, fall back to placeholder_folder
            ph_path = getattr(obj, 'placeholder_filepath', None) or getattr(obj, 'placeholder_folder', None)
            obj.has_placeholder = bool(ph_path)
            if ph_path and not getattr(obj, 'placeholder_filepath', None):
                # if only folder is present, leave filepath empty; callers may attach file later
                obj.placeholder_filepath = ph_path if os.path.isfile(ph_path) else None
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
        lib = settings.MOVIE_LIBRARY_FOLDER if hasattr(obj, 'radarr_filepath') or hasattr(obj, 'tmdbid') else settings.TV_LIBRARY_FOLDER
        path = place_dummy_file('movie' if lib == settings.MOVIE_LIBRARY_FOLDER else 'tv', getattr(obj, 'title', 'unknown'), getattr(obj, 'year', 0), getattr(obj, 'id', 0), lib)
        if path:
            try:
                # persist new canonical placeholder fields
                obj.placeholder_filepath = path
                obj.placeholder_folder = os.path.dirname(path)
                obj.has_placeholder = True
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
    """
    Populate enrichment fields for a Series/Movie/Episode row.

    Primary responsibility implemented here (conservative): derive the
    canonical `placeholder_folder` from ARR-derived metadata (title/year/id)
    and persist it to the DB using a write-once policy (only set if NULL).

    This keeps the change small and low-risk: we do not overwrite an
    existing `placeholder_folder`, and we avoid making heavy external
    network calls here. The resolver mirrors the final folder logic used
    when creating placeholders so later placeholder creation can verify
    the pre-populated folder.
    """
    try:
        # lazy import to avoid heavier deps at module import time
        from services.postgres.models import Series as SeriesModel, Movie as MovieModel, Season as SeasonModel, Episode as EpisodeModel
        # prefer the legacy resolver if present; fallback to a minimal derivation
        try:
            from services.services_old.utils import resolve_final_folder
        except Exception:
            # fallback simple resolver
            def resolve_final_folder(media_type, title=None, year=None, media_id=None, season_number=None, folder_path=None, arr_root_folder=None, season_folder_name=None, relative_path=None, payload=None):
                # Minimal deterministic folder name that mirrors the canonical convention
                from services.services_old.utils import sanitize_filename
                base = settings.MOVIE_LIBRARY_FOLDER if media_type == 'movie' else settings.TV_LIBRARY_FOLDER
                if not title:
                    return None
                folder = f"{sanitize_filename(title)}{(' ('+str(year)+')') if year else ''} {{tmdb-{media_id}}}" if media_type == 'movie' else f"{sanitize_filename(title)}{(' ('+str(year)+')') if year else ''} {{tvdb-{media_id}}}"
                return os.path.join(base, folder)

        # Handle Series, Episode, Movie
        if model == None:
            return False

        # Series
        if model.__name__ == 'Series':
            s = session.query(model).get(ent_id)
            if not s:
                return False
            # only populate if blank
            if not getattr(s, 'placeholder_folder', None):
                try:
                    folder = resolve_final_folder(media_type='tv', title=getattr(s, 'title', None), year=getattr(s, 'year', None), media_id=getattr(s, 'tvdbid', None), payload=None)
                    if folder:
                        s.placeholder_folder = folder
                        session.add(s)
                        session.commit()
                        # Conservative propagation: if seasons or episodes lack a placeholder_folder,
                        # inherit the series placeholder_folder so later phases can match by prefix.
                        try:
                            from services.postgres.models import Season as _Season, Episode as _Episode
                            seasons = session.query(_Season).filter(_Season.series_id == s.id).all()
                            for season in seasons:
                                try:
                                    if not getattr(season, 'placeholder_folder', None):
                                        season.placeholder_folder = s.placeholder_folder
                                        session.add(season)
                                except Exception:
                                    continue
                            session.commit()
                            # propagate down to episodes as well
                            try:
                                for season in seasons:
                                    try:
                                        if getattr(season, 'placeholder_folder', None):
                                            eps = session.query(_Episode).filter(_Episode.season_id == season.id).all()
                                            for ep in eps:
                                                try:
                                                    if not getattr(ep, 'placeholder_folder', None):
                                                        ep.placeholder_folder = season.placeholder_folder
                                                        session.add(ep)
                                                except Exception:
                                                    continue
                                    except Exception:
                                        continue
                                session.commit()
                            except Exception:
                                try:
                                    session.rollback()
                                except Exception:
                                    pass
                        except Exception:
                            try:
                                session.rollback()
                            except Exception:
                                pass
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
            return True

        # Episode -> derive its series and behave like Series case (populate series placeholder_folder)
        if model.__name__ == 'Episode':
            ep = session.query(model).get(ent_id)
            if not ep:
                return False
            season = None
            try:
                from services.postgres.models import Season as _Season
                season = session.query(_Season).get(ep.season_id) if getattr(ep, 'season_id', None) else None
            except Exception:
                season = None
            series = None
            if season:
                try:
                    from services.postgres.models import Series as _Series
                    series = session.query(_Series).get(season.series_id) if getattr(season, 'series_id', None) else None
                except Exception:
                    series = None

            if series and not getattr(series, 'placeholder_folder', None):
                try:
                    folder = resolve_final_folder(media_type='tv', title=getattr(series, 'title', None), year=getattr(series, 'year', None), media_id=getattr(series, 'tvdbid', None), payload=None)
                    if folder:
                        series.placeholder_folder = folder
                        session.add(series)
                        session.commit()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
            return True

        # Movie
        if model.__name__ == 'Movie':
            m = session.query(model).get(ent_id)
            if not m:
                return False
            if not getattr(m, 'placeholder_folder', None):
                try:
                    folder = resolve_final_folder(media_type='movie', title=getattr(m, 'title', None), year=getattr(m, 'year', None), media_id=getattr(m, 'tmdbid', None), payload=None)
                    if folder:
                        m.placeholder_folder = folder
                        session.add(m)
                        session.commit()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
            return True

        return True
    except Exception:
        return False


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
