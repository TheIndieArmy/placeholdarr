import os
import shutil
import logging
import re
from typing import List, Optional
from core.config import settings

logger = logging.getLogger('services.integrations')


def _safe_join(root: str, *parts: str) -> str:
    root = os.path.abspath(root)
    path = os.path.abspath(os.path.join(root, *parts))
    if not path.startswith(root):
        raise ValueError('Path escapes library root')
    return path


def place_dummy_file(media_type: str, title: str, year: int = None, media_id: int = None, library_root: str = None,
                     season_number: int = None, episode_number: int = None, episode_title: str = None,
                     dummy_file_override: str = None) -> Optional[str]:
    """Create a placeholder file using the legacy folder/name conventions.

    Uses the resolver in services.services_old.utils to build the final folder path.
    Attempts to hardlink the configured DUMMY_FILE_PATH, falling back to copy on
    cross-device link errors. Writes .nfo sidecar when possible.
    """
    try:
        # Determine base library if not provided
        if not library_root:
            library_root = settings.MOVIE_LIBRARY_FOLDER if media_type == 'movie' else settings.TV_LIBRARY_FOLDER
        # Lazy import of legacy helpers
        try:
            from services.services_old.utils import resolve_final_folder, sanitize_filename, write_nfo_for_placeholder, render_episode_nfo, render_movie_nfo
        except Exception:
            # if legacy utils not present, fall back to a very conservative behavior
            def sanitize_filename(n):
                return re.sub(r'[<>:\"/\\|?*]', '', str(n or '')).strip()

            def resolve_final_folder(media_type, title=None, year=None, media_id=None, season_number=None, **kwargs):
                base = library_root
                folder = f"{sanitize_filename(title)}{(' ('+str(year)+')') if year else ''} {{tmdb-{media_id}}}" if media_type == 'movie' else f"{sanitize_filename(title)}{(' ('+str(year)+')') if year else ''} {{tvdb-{media_id}}}"
                if media_type != 'movie' and season_number is not None:
                    return os.path.join(base, folder, f"Season {int(season_number):02d}")
                return os.path.join(base, folder)

            def write_nfo_for_placeholder(path, meta, media_type='tv', status='Request'):
                return False

        # Clean title
        clean_title = sanitize_filename(title or 'unknown')
        clean_title = re.sub(r'\s*\(\d{4}\)', '', clean_title).strip()
        year_str = f" ({year})" if year else ""

        dummy_source = dummy_file_override or getattr(settings, 'DUMMY_FILE_PATH', None)
        if not dummy_source or not os.path.exists(dummy_source):
            logger.error(f"Dummy video source not found at {dummy_source}", extra={'emoji_type': 'error'})
            return None

        def create_placeholder_file(src, dst):
            if getattr(settings, 'PLACEHOLDER_STRATEGY', 'link') == 'copy':
                shutil.copy2(src, dst)
                logger.debug(f"Copied dummy file to {dst}", extra={'emoji_type': 'copy'})
            else:
                try:
                    os.link(src, dst)
                    logger.debug(f"Hardlinked dummy file to {dst}", extra={'emoji_type': 'link'})
                except OSError as e:
                    # cross-device link or other
                    try:
                        if getattr(e, 'errno', None) == 18:
                            shutil.copy2(src, dst)
                            logger.warning(f"Hardlink failed (cross-device); copied dummy file instead: {dst}", extra={'emoji_type': 'warning'})
                        else:
                            raise
                    except Exception:
                        raise

        # Resolve final folder
        final_folder = resolve_final_folder(media_type=media_type, title=title, year=year, media_id=media_id, season_number=season_number)
        if not final_folder:
            logger.error(f"No valid folder path for dummy file creation for {title}", extra={'emoji_type': 'error'})
            return None

        os.makedirs(final_folder, exist_ok=True)
        try:
            # Match legacy behavior: chmod season folder and its parent series folder to be permissive
            os.chmod(final_folder, 0o777)
            parent = os.path.dirname(final_folder)
            if parent:
                try:
                    os.chmod(parent, 0o777)
                except Exception:
                    pass
        except Exception:
            # non-fatal
            pass

        if media_type == 'tv' and season_number is not None and episode_number is not None:
            file_name = f"{clean_title}{year_str} - s{int(season_number):02d}e{int(episode_number):02d} - {episode_title or ('Episode '+str(episode_number))}.mp4"
            file_path = os.path.join(final_folder, sanitize_filename(file_name))
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            try:
                create_placeholder_file(dummy_source, file_path)
            except Exception as e:
                logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
                return None

            # Attempt to write episode-level nfo
            try:
                meta = {'title': episode_title or '', 'season': season_number, 'episode': episode_number, 'aired': None, 'sonarr_episode_overview': None, 'tvdb': media_id}
                ok = write_nfo_for_placeholder(file_path, meta, media_type='tv', status='Request')
                if ok:
                    logger.debug(f"Wrote episode NFO for {file_path}", extra={'emoji_type': 'create'})
            except Exception:
                pass

            return file_path

        else:
            # movie
            file_name = f"{clean_title}{year_str} (dummy).mp4"
            file_path = os.path.join(final_folder, sanitize_filename(file_name))
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            try:
                create_placeholder_file(dummy_source, file_path)
            except Exception as e:
                logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
                return None

            # Attempt to write movie nfo
            try:
                meta = {'title': title, 'year': year, 'tmdbid': media_id, 'imdbid': None, 'radarr_overview': None}
                ok = write_nfo_for_placeholder(file_path, meta, media_type='movie', status='Request')
                if ok:
                    logger.debug(f"Wrote movie NFO for {file_path}", extra={'emoji_type': 'create'})
            except Exception:
                pass

            return file_path

    except Exception as e:
        logger.error(f"place_dummy_file failed: {e}", extra={'emoji_type': 'error'})
        return None


def delete_dummy_file(path: str) -> bool:
    try:
        if path and os.path.exists(path):
            # if it's a file, remove it; if path is a folder, attempt rmtree
            if os.path.isfile(path):
                os.remove(path)
                logger.info(f"Deleted dummy file {path}")
            else:
                try:
                    shutil.rmtree(path)
                    logger.info(f"Deleted dummy folder {path}")
                except Exception:
                    # fallback to removing file-like
                    try:
                        os.remove(path)
                        logger.info(f"Deleted dummy file {path}")
                    except Exception:
                        logger.debug(f"Failed to delete path {path}")
            return True
        return False
    except Exception as e:
        logger.error(f"delete_dummy_file failed: {e}")
        return False


def delete_dummy_files(media_type: str,
                       title: str = None,
                       year: int = None,
                       tvdb_id: int = None,
                       library_path: str = None,
                       season_number: int = None,
                       episode_number: int = None,
                       folder_path: str = None,
                       arr_root_folder: str = None,
                       season_folder_name: str = None,
                       session=None) -> bool:
    """Delete placeholders by metadata (legacy behavior).

    If `session` (SQLAlchemy session) is provided, also attempt to update DB rows
    (mark deleted / remove placeholder rows) similarly to the legacy flow. This
    function is defensive and best-effort: it will return True even if some
    non-critical operations fail.
    """
    import shutil
    try:
        # Build the folder name using legacy convention where possible
        try:
            from services.services_old.utils import sanitize_filename
        except Exception:
            def sanitize_filename(n):
                return re.sub(r'[<>:\"/\\|?*]', '', str(n or '')).strip()

        folder_name = sanitize_filename(title) if title else None
        if year and folder_name:
            folder_name = f"{folder_name} ({year})"

        if media_type == 'tv' and tvdb_id:
            folder_name = (folder_name or '') + f" {{tvdb-{tvdb_id}}} (dummy)"
        elif media_type == 'movie' and tvdb_id:
            folder_name = (folder_name or '') + f" {{tmdb-{tvdb_id}}}{{edition-Dummy}}"

        dummy_folder = None
        if library_path and folder_name:
            dummy_folder = os.path.join(library_path, folder_name)
        elif folder_path:
            dummy_folder = folder_path

        if not dummy_folder:
            logger.debug(f"delete_dummy_files: no folder computed for media_type={media_type} title={title} id={tvdb_id}")
            return True

        # If TV and specific season/episode provided, attempt to delete the episode file
        if media_type == 'tv' and season_number is not None and episode_number is not None:
            season_dir = os.path.join(dummy_folder, f"Season {int(season_number):02d}")
            if os.path.exists(season_dir):
                files_found = False
                for fname in os.listdir(season_dir):
                    patterns = [f"s{int(season_number):02d}e{int(episode_number):02d}", f"S{int(season_number):02d}E{int(episode_number):02d}"]
                    if any(pat in fname for pat in patterns):
                        fp = os.path.join(season_dir, fname)
                        try:
                            os.remove(fp)
                            logger.info(f"Deleted placeholder file: {fp}", extra={'emoji_type': 'delete'})
                            files_found = True
                        except Exception as e:
                            logger.debug(f"Failed to delete {fp}: {e}")
                # cleanup empty season folder
                try:
                    if os.path.exists(season_dir) and not os.listdir(season_dir):
                        os.rmdir(season_dir)
                        logger.info(f"Deleted empty season folder: {season_dir}", extra={'emoji_type': 'delete'})
                except Exception:
                    pass

                # DB updates when session provided
                if session:
                    try:
                        from services.postgres.models import Series, Season, Episode, Movie
                        # find series by tvdb tag if possible
                        series = None
                        try:
                            if tvdb_id is not None:
                                series = session.query(Series).filter(Series.tvdbid == int(tvdb_id)).first()
                        except Exception:
                            series = None
                        if series:
                            season_row = session.query(Season).filter(Season.series_id == series.id, Season.season_number == int(season_number)).first()
                            if season_row:
                                ep = session.query(Episode).filter(Episode.season_id == season_row.id, Episode.episode_number == int(episode_number)).first()
                                if ep:
                                    ep.is_deleted = True
                                    ep.placeholder_exists = False
                                    session.add(ep)
                                    session.commit()
                    except Exception:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                return True

        # Otherwise remove the whole dummy folder
        try:
            if os.path.exists(dummy_folder):
                shutil.rmtree(dummy_folder)
                logger.info(f"Deleted placeholder folder: {dummy_folder}", extra={'emoji_type': 'delete'})
        except Exception as e:
            logger.debug(f"Failed to delete placeholder folder {dummy_folder}: {e}")

        # DB updates when session provided
        if session:
            try:
                from services.postgres.models import Movie, Series, Season, Episode
                if media_type == 'movie':
                    try:
                        movie = session.query(Movie).filter(Movie.tmdbid == int(tvdb_id)).first() if tvdb_id is not None else None
                    except Exception:
                        movie = session.query(Movie).filter(Movie.tmdbid == tvdb_id).first() if tvdb_id is not None else None
                    if movie:
                        movie.is_deleted = True
                        movie.placeholder_exists = False
                        session.add(movie)
                        session.commit()
                elif media_type == 'tv':
                    try:
                        series = session.query(Series).filter(Series.tvdbid == int(tvdb_id)).first() if tvdb_id is not None else None
                    except Exception:
                        series = session.query(Series).filter(Series.tvdbid == tvdb_id).first() if tvdb_id is not None else None
                    if series:
                        series.is_deleted = True
                        series.placeholder_exists = False
                        session.add(series)
                        seasons = session.query(Season).filter(Season.series_id == series.id).all()
                        for s in seasons:
                            s.is_deleted = True
                            s.placeholder_exists = False
                            session.add(s)
                            eps = session.query(Episode).filter(Episode.season_id == s.id).all()
                            for ep in eps:
                                ep.is_deleted = True
                                ep.placeholder_exists = False
                                session.add(ep)
                        session.commit()
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
        return True
    except Exception as e:
        logger.error(f"delete_dummy_files failed: {e}", extra={'emoji_type': 'error'})
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
