import os
import shutil
import time
import threading
import requests
import re
import xml.etree.ElementTree as ET
from core.nfo import generate_movie_nfo, generate_episode_nfo, write_nfo_for_file
from core.config import settings
from core.logger import logger
from services.utils import (
    resolve_final_folder, sanitize_filename, strip_status_markers, get_arr_config,
    normalize_arr_base, join_endpoint, get_root_folder_from_path
)
from services.plex_client import plex

# Global variables
BASE_TITLES = {}
PROGRESS_FLAGS = {}
LAST_RADARR_SEARCH = {}

def get_folder_path(media_type, base_path, title, year=None, media_id=None, season=None, folder_path=None):
    """Generate folder path according to the convention, or use provided folder_path."""
    if folder_path:
        return folder_path
    if media_type == "movie":
        folder_name = f"{sanitize_filename(title)} ({year}) {{tmdb-{media_id}}}"  # removed edition
        return os.path.join(base_path, folder_name)
    else:
        folder_name = f"{sanitize_filename(title)} ({year}) {{tvdb-{media_id}}}"  # removed (dummy)
        season_folder = f"Season {season:02d}" if season is not None else None
        if season_folder:
            return os.path.join(base_path, folder_name, season_folder)
        else:
            return os.path.join(base_path, folder_name)


def place_dummy_file(media_type, title, year=None, media_id=None, base_path=None,
                    season_number=None, episode_range=None, episode_title=None, episode_id=None,
                    dummy_file_override=None, folder_path=None, arr_root_folder=None, season_folder_name=None, relative_path=None,
                    overview=None, request_mark=False, imdb_id=None, genres=None, studio=None, runtime=None, premiered=None, tagline=None,
                    rating=None, votes=None, collection=None, sorttitle=None, actors=None, director=None, credits=None, poster_url=None):
    """
    Create a dummy video file in the correct location. Uses resolve_final_folder for path resolution.
    Accepts optional `overview` to embed in NFO and `request_mark` to prefix episode titles with [REQUEST].
    """
    # Centralized guard: if placeholder creation/deletion is disabled, skip and return None.
    if not getattr(settings, 'ENABLE_PLACEHOLDERS', True):
        logger.info("Placeholder creation disabled by ENABLE_PLACEHOLDERS; skipping creation", extra={'emoji_type': 'skip'})
        return None
    import os
    try:
        dummy_source = dummy_file_override or settings.DUMMY_FILE_PATH
        if not os.path.exists(dummy_source):
            logger.error(f"Dummy video file does not exist at {dummy_source}", extra={'emoji_type': 'error'})
            return None
        final_folder = resolve_final_folder(media_type, title, year, media_id, season_number, folder_path, arr_root_folder, season_folder_name, relative_path)
        if not final_folder:
            logger.error("No valid folder path for dummy file creation.", extra={'emoji_type': 'error'})
            return None
        os.makedirs(final_folder, exist_ok=True)
        clean_title = sanitize_filename(title)
        clean_title = re.sub(r'\s*\(\d{4}\)', '', clean_title).strip()
        year_str = f" ({year})" if year else ""

        # TV episodes (possibly a range)
        if media_type == 'tv' and season_number is not None and episode_range:
            start_ep, end_ep = episode_range
            for ep_num in range(int(start_ep), int(end_ep) + 1):
                ep_title = episode_title or f"Episode {ep_num}"
                file_name = f"{clean_title}{year_str} - s{season_number:02d}e{ep_num:02d} - {ep_title}.mp4"
                file_path = os.path.join(final_folder, sanitize_filename(file_name))

                # Remove any existing placeholder file
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        logger.debug(f"Could not remove existing file before creating placeholder: {file_path}", extra={'emoji_type': 'debug'})

                try:
                    if settings.PLACEHOLDER_STRATEGY == "copy":
                        shutil.copy2(dummy_source, file_path)
                    else:
                        try:
                            os.link(dummy_source, file_path)
                        except OSError as e:
                            # Log a clear warning and fall back to copying the dummy file
                            logger.warning(
                                f"Failed to create hardlink for {file_path} from {dummy_source}: {e}; falling back to copy",
                                extra={'emoji_type': 'warning'}
                            )
                            try:
                                shutil.copy2(dummy_source, file_path)
                                logger.debug(f"Copied dummy file as fallback: {file_path}", extra={'emoji_type': 'create'})
                            except Exception as ce:
                                logger.error(
                                    f"Failed to copy dummy file for {file_path} after hardlink failure: {ce}",
                                    extra={'emoji_type': 'error'}
                                )
                                raise
                    logger.debug(f"Created dummy file: {file_path}", extra={'emoji_type': 'create'})
                    # Write an NFO next to the dummy to help media servers detect metadata quickly
                    try:
                        meta = {
                            'title': f"[REQUEST] {ep_title}" if request_mark and ep_title else ep_title,
                            'showtitle': title,
                            'season': season_number,
                            'episode': ep_num,
                            'tvdb_id': media_id,
                            'plot': overview,
                            'aired': premiered,
                            'genres': genres,
                            'actors': actors,
                            'director': director,
                            'credits': credits,
                            'poster_url': poster_url,
                        }
                        elem = generate_episode_nfo(meta)
                        write_nfo_for_file(file_path, elem, overwrite=False, mtime_source=file_path)
                    except Exception as e:
                        logger.debug(f"Failed to write episode NFO for {file_path}: {e}", extra={'emoji_type': 'debug'})
                    os.utime(file_path, None)
                except Exception as e:
                    logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
                    return None

            # Return path to the first created episode file
            ep_title = episode_title or f"Episode {start_ep}"
            file_name = f"{clean_title}{year_str} - s{season_number:02d}e{start_ep:02d} - {ep_title}.mp4"
            return os.path.join(final_folder, sanitize_filename(file_name))

        # Movie or single dummy
        else:
            file_name = f"{clean_title}{year_str} (dummy).mp4"
            file_path = os.path.join(final_folder, sanitize_filename(file_name))

            # Remove existing placeholder if present
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    logger.debug(f"Could not remove existing file before creating placeholder: {file_path}", extra={'emoji_type': 'debug'})

            try:
                if settings.PLACEHOLDER_STRATEGY == "copy":
                    shutil.copy2(dummy_source, file_path)
                else:
                    try:
                        os.link(dummy_source, file_path)
                    except OSError as e:
                        logger.warning(
                            f"Failed to create hardlink for {file_path} from {dummy_source}: {e}; falling back to copy",
                            extra={'emoji_type': 'warning'}
                        )
                        try:
                            shutil.copy2(dummy_source, file_path)
                            logger.debug(f"Copied dummy file as fallback: {file_path}", extra={'emoji_type': 'create'})
                        except Exception as ce:
                            logger.error(
                                f"Failed to copy dummy file for {file_path} after hardlink failure: {ce}",
                                extra={'emoji_type': 'error'}
                            )
                            raise
                logger.debug(f"Created dummy file: {file_path}", extra={'emoji_type': 'create'})
                # Write a movie NFO to assist media servers
                try:
                    meta = {
                        'title': title,
                        'originaltitle': title,
                        'year': year,
                        'tmdb_id': media_id,
                        'plot': overview,
                        'imdb_id': imdb_id,
                        'genres': genres,
                        'studio': studio,
                        'runtime': runtime,
                        'premiered': premiered,
                        'tagline': tagline,
                        'rating': rating,
                        'votes': votes,
                        'collection': collection,
                        'sorttitle': sorttitle,
                        'actors': actors,
                        'poster_url': poster_url,
                    }
                    elem = generate_movie_nfo(meta)
                    write_nfo_for_file(file_path, elem, overwrite=False, mtime_source=file_path)
                except Exception as e:
                    logger.debug(f"Failed to write movie NFO for {file_path}: {e}", extra={'emoji_type': 'debug'})
                os.utime(file_path, None)
            except Exception as e:
                logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
                return None

            return file_path
    except Exception as e:
        logger.error(f"Error creating dummy file: {str(e)}", extra={'emoji_type': 'error'})
        return None


def _write_movie_nfo(dummy_path, title, year=None, tmdb_id=None, overview=None, imdb_id=None, genres=None, studio=None, runtime=None, premiered=None, tagline=None, rating=None, votes=None, collection=None, sorttitle=None, actors=None):
    """Write a minimal movie NFO (XML) next to the dummy file to help media servers pick up metadata quickly."""
    try:
        base, _ = os.path.splitext(dummy_path)
        nfo_path = f"{base}.nfo"
        movie = ET.Element('movie')
        # Basic fields
        title_el = ET.SubElement(movie, 'title')
        title_el.text = title
        original_el = ET.SubElement(movie, 'originaltitle')
        original_el.text = title
        if year:
            year_el = ET.SubElement(movie, 'year')
            year_el.text = str(year)
        if overview:
            plot_el = ET.SubElement(movie, 'plot')
            plot_el.text = overview
        if tagline:
            tagline_el = ET.SubElement(movie, 'tagline')
            tagline_el.text = tagline
        if studio:
            studio_el = ET.SubElement(movie, 'studio')
            studio_el.text = studio
        if genres:
            # genres may be list or comma-separated string
            if isinstance(genres, (list, tuple)):
                for g in genres:
                    genre_el = ET.SubElement(movie, 'genre')
                    genre_el.text = g
            else:
                for g in str(genres).split(','):
                    genre_el = ET.SubElement(movie, 'genre')
                    genre_el.text = g.strip()
        if runtime:
            runtime_el = ET.SubElement(movie, 'runtime')
            runtime_el.text = str(runtime)
        if premiered:
            premiered_el = ET.SubElement(movie, 'premiered')
            premiered_el.text = str(premiered)
        # IDs
        if tmdb_id:
            tmdb_el = ET.SubElement(movie, 'tmdbid')
            tmdb_el.text = str(tmdb_id)
            # also add a uniqueid element used by some agents
            unique_el = ET.SubElement(movie, 'uniqueid')
            unique_el.set('type', 'tmdb')
            unique_el.text = str(tmdb_id)
        if imdb_id:
            imdb_el = ET.SubElement(movie, 'imdbid')
            imdb_el.text = str(imdb_id)
        if rating:
            rating_el = ET.SubElement(movie, 'rating')
            rating_el.text = str(rating)
        if votes:
            votes_el = ET.SubElement(movie, 'votes')
            votes_el.text = str(votes)
        if collection:
            collection_el = ET.SubElement(movie, 'collection')
            collection_el.text = str(collection)
        if sorttitle:
            sort_el = ET.SubElement(movie, 'sorttitle')
            sort_el.text = str(sorttitle)
        if actors:
            try:
                # actors may be list of dicts or list of names
                if isinstance(actors, (list, tuple)):
                    actors_el = ET.SubElement(movie, 'actors')
                    for a in actors:
                        actor_el = ET.SubElement(actors_el, 'actor')
                        if isinstance(a, dict):
                            name = a.get('name') or a.get('Name')
                            role = a.get('role') or a.get('Role')
                        else:
                            name = str(a)
                            role = None
                        name_el = ET.SubElement(actor_el, 'name')
                        name_el.text = name
                        if role:
                            role_el = ET.SubElement(actor_el, 'role')
                            role_el.text = str(role)
            except Exception:
                pass
        # Leave placeholders for common optional tags (imdb, plot) if we ever have them
        # Write out the tree
        tree = ET.ElementTree(movie)
        tree.write(nfo_path, encoding='utf-8', xml_declaration=True)
        logger.debug(f"Wrote movie NFO: {nfo_path}", extra={'emoji_type': 'debug'})
    except Exception as e:
        logger.debug(f"Error writing movie NFO for {dummy_path}: {e}", extra={'emoji_type': 'debug'})


def _write_episode_nfo(dummy_path, series_title, season_number, episode_number, tvdb_id=None, episode_title=None, overview=None, request_mark=False, genres=None, aired=None, actors=None, director=None, credits=None):
    """Write a minimal episode NFO next to the dummy file with tvdb/season/episode metadata.
    If request_mark is True, prepend "[REQUEST] " to the episode title so placeholders indicate request state.
    """
    try:
        base, _ = os.path.splitext(dummy_path)
        nfo_path = f"{base}.nfo"
        episode = ET.Element('episodedetails')
        # Basic fields
        # Optionally prepend request marker
        final_title = episode_title or ''
        if request_mark and final_title:
            final_title = f"[REQUEST] {final_title}"
        title_el = ET.SubElement(episode, 'title')
        title_el.text = final_title
        show_el = ET.SubElement(episode, 'showtitle')
        show_el.text = series_title or ''
        season_el = ET.SubElement(episode, 'season')
        season_el.text = str(season_number or 0)
        epnum_el = ET.SubElement(episode, 'episode')
        epnum_el.text = str(episode_number or 0)
        # IDs - tvdb
        if tvdb_id:
            id_el = ET.SubElement(episode, 'id')
            id_el.text = str(tvdb_id)
            # also include tvdb-specific tag
            tvdb_el = ET.SubElement(episode, 'tvdbid')
            tvdb_el.text = str(tvdb_id)
            # add uniqueid element for compatibility
            unique_el = ET.SubElement(episode, 'uniqueid')
            unique_el.set('type', 'tvdb')
            unique_el.text = str(tvdb_id)
        # Optional aired/plot fields could be added if available
        if overview:
            plot_el = ET.SubElement(episode, 'plot')
            plot_el.text = overview
        if aired:
            aired_el = ET.SubElement(episode, 'aired')
            aired_el.text = str(aired)
        if genres:
            if isinstance(genres, (list, tuple)):
                for g in genres:
                    genre_el = ET.SubElement(episode, 'genre')
                    genre_el.text = g
            else:
                for g in str(genres).split(','):
                    genre_el = ET.SubElement(episode, 'genre')
                    genre_el.text = g.strip()
        if actors:
            try:
                actors_el = ET.SubElement(episode, 'actors')
                if isinstance(actors, (list, tuple)):
                    for a in actors:
                        actor_el = ET.SubElement(actors_el, 'actor')
                        if isinstance(a, dict):
                            name = a.get('name') or a.get('Name')
                            role = a.get('role') or a.get('Role')
                        else:
                            name = str(a)
                            role = None
                        name_el = ET.SubElement(actor_el, 'name')
                        name_el.text = name
                        if role:
                            role_el = ET.SubElement(actor_el, 'role')
                            role_el.text = str(role)
            except Exception:
                pass
        if director:
            director_el = ET.SubElement(episode, 'director')
            director_el.text = str(director)
        if credits:
            credits_el = ET.SubElement(episode, 'credits')
            credits_el.text = str(credits)
        tree = ET.ElementTree(episode)
        tree.write(nfo_path, encoding='utf-8', xml_declaration=True)
        logger.debug(f"Wrote episode NFO: {nfo_path}", extra={'emoji_type': 'debug'})
    except Exception as e:
        logger.debug(f"Error writing episode NFO for {dummy_path}: {e}", extra={'emoji_type': 'debug'})


def _write_series_nfo(series_folder, title, year=None, tvdb_id=None, tmdb_id=None, overview=None, poster_url=None, imdb_id=None, tvmaze_id=None, rating=None, status=None, tags=None):
    """Write a Sonarr-like series/tvshow NFO (tvshow.nfo) in the series folder using core.nfo utilities."""
    try:
        if not series_folder:
            logger.debug("No series folder provided for series NFO write", extra={'emoji_type': 'debug'})
            return
        os.makedirs(series_folder, exist_ok=True)
        meta = {
            'title': title,
            'rating': rating,
            'plot': overview,
            'id': tvdb_id,
            'tmdb_id': tmdb_id,
            'imdb_id': imdb_id,
            'tvmaze_id': tvmaze_id,
            'genres': None,
            'tags': tags,
            'status': status,
            'premiered': year,
            'studio': None,
            'poster_url': poster_url,
        }
        from core.nfo import generate_series_nfo, write_nfo_to_path
        tvshow_elem = generate_series_nfo(meta)
        nfo_path = os.path.join(series_folder, 'tvshow.nfo')
        write_nfo_to_path(nfo_path, tvshow_elem, overwrite=False, mtime_source=None)
        logger.debug(f"Wrote series NFO: {nfo_path}", extra={'emoji_type': 'debug'})
    except Exception as e:
        logger.debug(f"Error writing series NFO for {series_folder}: {e}", extra={'emoji_type': 'debug'})


def fetch_sonarr_series(series_id):
    """Fetch series metadata from Sonarr by series ID. Returns a dict with common fields or None on error."""
    try:
        if not series_id:
            return None
        sonarr_cfg = get_arr_config('tv', False)
        url = join_endpoint(sonarr_cfg['url'], f'series/{series_id}')
        headers = {'X-Api-Key': sonarr_cfg['api_key']}
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        # Extract common fields
        # attempt to extract a poster/url for use in NFO thumb tag
        poster_url = None
        if data.get('remotePoster'):
            poster_url = data.get('remotePoster')
        elif data.get('images') and isinstance(data.get('images'), list):
            # Sonarr may provide images array with 'cover' or 'poster' types
            for img in data.get('images'):
                if isinstance(img, dict) and img.get('coverType') in ('poster', 'poster', 'cover'):
                    poster_url = img.get('url') or img.get('remoteUrl')
                    break
            if not poster_url and data.get('images'):
                first = data.get('images')[0]
                if isinstance(first, dict):
                    poster_url = first.get('url') or first.get('remoteUrl')

        result = {
            'overview': data.get('overview') or data.get('summary') or None,
            'genres': data.get('genres') or None,
            'premiered': data.get('firstAired') or data.get('premiered') or None,
            'imdb_id': data.get('imdbId') or None,
            'tvdb_id': data.get('tvdbId') or None,
            'studio': data.get('studio') or None,
            'actors': data.get('actors') or None,
            'poster_url': poster_url
        }
        return result
    except Exception as e:
        logger.debug(f"Failed to fetch Sonarr series {series_id}: {e}", extra={'emoji_type': 'debug'})
        return None

def update_title_status(media_type, media_id, title, status, **kwargs):
    """Abstract update of media title status on Plex and/or Jellyfin"""
    results = []

    # Plex update
    if settings.plex_enabled:
        from services.plex_client import update_plex_title_status
        logger.debug(f"[Plex] Attempting update – type={media_type} id={media_id} status={status} kwargs={kwargs}", 
                     extra={'emoji_type': 'debug'})
        try:
            success = update_plex_title_status(
                media_type=media_type,
                media_id=media_id,
                title=title,
                status=status,
                **kwargs
            )
            logger.debug(f"[Plex] Update {'succeeded' if success else 'failed'} for id={media_id} kwargs={kwargs}", 
                         extra={'emoji_type': 'debug'})
            results.append(success)
        except Exception as e:
            logger.error(f"[Plex] Exception updating id={media_id} kwargs={kwargs}: {e}", extra={'emoji_type': 'error'})
            results.append(False)

    # Jellyfin update
    if settings.jellyfin_enabled:
        from services.jellyfin_client import update_jellyfin_title_status
        logger.debug(f"[Jellyfin] Attempting update – type={media_type} id={media_id} status={status} kwargs={kwargs}", 
                     extra={'emoji_type': 'debug'})
        try:
            success = update_jellyfin_title_status(
                media_type=media_type,
                media_id=media_id,
                title=title,
                status=status,
                **kwargs
            )
            logger.debug(f"[Jellyfin] Update {'succeeded' if success else 'failed'} for id={media_id} kwargs={kwargs}", 
                         extra={'emoji_type': 'debug'})
            results.append(success)
        except Exception as e:
            logger.error(f"[Jellyfin] Exception updating id={media_id} kwargs={kwargs}: {e}", extra={'emoji_type': 'error'})
            results.append(False)

    # No server case
    if not results:
        logger.error("No media server configured for title update", extra={'emoji_type': 'error'})
        return False

    # Return True if at least one succeeded
    overall = any(results)
    logger.debug(f"update_title_status overall result: {overall}", extra={'emoji_type': 'debug'})
    return overall

# Title update and scheduling functions
def schedule_episode_request_update(series_title, season_num, episode_num, media_id, delay=10, retries=5):
    """Schedule an update to the episode title with [Request] tag"""
    def attempt_update(attempt=1):
        try:
            # Use our new ID-based title update function
            result = update_title_status(
                media_type='tv',
                media_id=media_id,
                title=series_title,
                status='Request',
                season=season_num,
                episode=episode_num
            )
            
            if result:
                logger.debug(f"Successfully scheduled update for '{series_title}' S{season_num:02d}E{episode_num:02d}",
                           extra={'emoji_type': 'debug'})
            else:
                # If update fails but we have retries left, try again
                if attempt < retries:
                    threading.Timer(delay, attempt_update, args=[attempt+1]).start()
                else:
                    logger.error(f"Failed to update title for '{series_title}' S{season_num:02d}E{episode_num:02d} after {retries} attempts", 
                               extra={'emoji_type': 'error'})
                               
        except Exception as e:
            logger.error(f"Error updating title for '{series_title}' S{season_num:02d}E{episode_num:02d}: {e}", 
                       extra={'emoji_type': 'error'})
            # Try again if we have retries left
            if attempt < retries:
                threading.Timer(delay, attempt_update, args=[attempt+1]).start()

    # Start the first attempt after initial delay
    threading.Timer(delay, attempt_update).start()

def schedule_movie_request_update(movie_title, media_id, year=None, delay=10, retries=5):
    """Schedule an update to the movie title with [Request] tag"""
    def attempt_update(attempt=1):
        try:
            # Use our new ID-based title update function
            result = update_title_status(
                media_type='movie',
                media_id=media_id,
                title=movie_title,
                status='Request',
                year=year
            )
            
            if result:
                logger.debug(f"Successfully scheduled update for movie '{movie_title}'",
                           extra={'emoji_type': 'debug'})
            else:
                # If update fails but we have retries left, try again
                if attempt < retries:
                    logger.debug(f"Retrying movie title update for '{movie_title}' (attempt {attempt}/{retries})", 
                               extra={'emoji_type': 'debug'})
                    threading.Timer(delay, attempt_update, args=[attempt+1]).start()
                else:
                    logger.error(f"Failed to update title for '{movie_title}' after {retries} attempts", 
                               extra={'emoji_type': 'error'})
                               
        except Exception as e:
            logger.error(f"Error updating title for '{movie_title}': {e}", 
                       extra={'emoji_type': 'error'})
            # Try again if we have retries left
            if attempt < retries:
                threading.Timer(delay, attempt_update, args=[attempt+1]).start()

    # Start the first attempt after initial delay
    threading.Timer(delay, attempt_update).start()

# Radarr integration functions
def trigger_radarr_search(movie_id, movie_title=None):
    try:
        radarr_cfg = get_arr_config('movie', False)
        cmd_url = join_endpoint(radarr_cfg['url'], 'command')
        response = requests.post(cmd_url, json={'name': 'MoviesSearch', 'movieIds': [movie_id]}, headers={'X-Api-Key': radarr_cfg['api_key']})
        response.raise_for_status()
        logger.debug(f"Radarr search triggered for movie id {movie_id}", extra={'emoji_type': 'debug'})
        if movie_title:
            logger.info(f"Triggered search for {movie_title}", extra={'emoji_type': 'search'})
        return True
    except Exception as e:
        logger.error(f"Radarr search failed: {e}", extra={'emoji_type': 'error'})
        return False

def search_in_radarr(tmdb_id, rating_key, is_4k=False, title=None, imdb_id=None, year=None, file_path=None):
    """Search for a movie in Radarr"""
    config = get_arr_config('movie', is_4k)
    # Validate tmdb_id is an integer
    try:
        tmdb_id_int = int(tmdb_id)
    except (ValueError, TypeError):
        logger.error(f"Invalid TMDB ID received: {tmdb_id}", extra={'emoji_type': 'error'})
        return False
    try:
        movies_response = requests.get(join_endpoint(config['url'], 'movie'), headers={'X-Api-Key': config['api_key']})
        movies_response.raise_for_status()
        try:
            movies = movies_response.json()
        except ValueError:
            logger.error(
                f"Failed to parse JSON from Radarr /movie: status={movies_response.status_code} content-type={movies_response.headers.get('Content-Type')} body={movies_response.text[:200]}",
                extra={'emoji_type': 'error'}
            )
            return False
        if not isinstance(movies, list):
            logger.error(f"Expected list from Radarr /movie endpoint but got {type(movies)}", extra={'emoji_type': 'error'})
            return False
        
        existing = [m for m in movies if int(m.get("tmdbId", 0)) == tmdb_id_int]
        if existing:
            movie_data = existing[0]
            logger.info(f"Movie already exists in Radarr: {movie_data['title']}", extra={'emoji_type': 'info'})
            if not movie_data.get("monitored", False):
                movie_data["monitored"] = True
                put_response = requests.put(join_endpoint(config['url'], f"movie/{movie_data['id']}"), json=movie_data, headers={'X-Api-Key': config['api_key']})
                put_response.raise_for_status()
                logger.info(f"Movie {movie_data['title']} marked as monitored", extra={'emoji_type': 'monitored'})
            now = time.time()
            if rating_key not in LAST_RADARR_SEARCH or (now - LAST_RADARR_SEARCH[rating_key] >= 30):
                LAST_RADARR_SEARCH[rating_key] = now
                trigger_radarr_search(movie_data['id'], movie_data['title'])
            else:
                logger.debug("Manual search already triggered recently; skipping duplicate search", extra={'emoji_type': 'debug'})
            # Do not schedule further timer retries if TMDB ID is invalid
            return movie_data['id']

        lookup = requests.get(join_endpoint(config['url'], 'movie/lookup'), params={'term': f"tmdb:{tmdb_id_int}"}, headers={'X-Api-Key': config['api_key']})
        lookup.raise_for_status()
        try:
            lookup_json = lookup.json()
            movie_data = lookup_json[0]
        except ValueError:
            logger.error(
                f"Failed to parse JSON from Radarr /movie/lookup: status={lookup.status_code} content-type={lookup.headers.get('Content-Type')} body={lookup.text[:200]}",
                extra={'emoji_type': 'error'}
            )
            return False
        except (IndexError, TypeError) as e:
            logger.error(
                f"Unexpected lookup response structure: {e} - body={lookup.text[:200]}",
                extra={'emoji_type': 'error'}
            )
            return False
        payload = {
            'title': movie_data['title'],
            'qualityProfileId': 7,
            'tmdbId': int(movie_data['tmdbId']),
            'year': int(movie_data['year']),
            'rootFolderPath': get_root_folder_from_path(file_path) or settings.MOVIE_LIBRARY_FOLDER,
            'monitored': True,
            'addOptions': {
                'searchForMovie': True,
                'addMethod': 'manual',
                'monitor': 'movieOnly'
            }
        }
        response = requests.post(join_endpoint(config['url'], 'movie'), json=payload, headers={'X-Api-Key': config['api_key']})
        response.raise_for_status()
        logger.info(f"Added movie: {movie_data['title']}", extra={'emoji_type': 'success'})
        # Safely parse response JSON for the added movie ID
        try:
            resp_json = response.json()
            added_id = resp_json.get('id')
        except ValueError:
            logger.error(
                f"Failed to parse JSON from Radarr /movie add response: status={response.status_code} content-type={response.headers.get('Content-Type')} body={response.text[:200]}",
                extra={'emoji_type': 'error'}
            )
            return False

        now = time.time()
        if rating_key not in LAST_RADARR_SEARCH or (now - LAST_RADARR_SEARCH[rating_key] >= 30):
            LAST_RADARR_SEARCH[rating_key] = now
            if added_id:
                trigger_radarr_search(added_id, movie_data['title'])
        else:
            logger.debug("Manual search already triggered recently; skipping duplicate search", extra={'emoji_type': 'debug'})
        return True

    except Exception as e:
        logger.error(f"Radarr operation failed: {e}", extra={'emoji_type': 'error'})
        return False


def trigger_sonarr_search(series_id, season_number=None, episode_ids=None, series_title="Unknown Series", is_4k=False):
    """Trigger a search in Sonarr for episodes"""
    try:
        # Get Sonarr configuration
        config = get_arr_config('tv', is_4k)
        url = config.get('url')
        api_key = config.get('api_key')
        
        if not url or not api_key:
            logger.error("Sonarr configuration missing", extra={'emoji_type': 'error'})
            return False
            
        headers = {'X-Api-Key': api_key}
        
        # Determine search type and prepare request
        if episode_ids:
            # Search for specific episodes (batch support)
            data = {
                'name': 'episodeSearch',
                'episodeIds': episode_ids
            }
            log_message = f"Triggered episode search for {series_title} ({len(episode_ids)} episodes)"
            
        elif season_number is not None:
            # Search for a season
            data = {
                'name': 'seasonSearch',
                'seriesId': series_id,
                'seasonNumber': season_number
            }
            log_message = f"Triggered season search for {series_title} S{season_number:02d}"
            
        else:
            # Search for entire series
            data = {
                'name': 'seriesSearch',
                'seriesId': series_id
            }
            log_message = f"Triggered series search for {series_title}"
            
        # Send search command to Sonarr
        r = requests.post(join_endpoint(url, 'command'), json=data, headers=headers)
        r.raise_for_status()
        
        # Log single message for the search operation
        logger.info(log_message, extra={'emoji_type': 'search'})
        
        return True
        
    except Exception as e:
        logger.error(f"Sonarr search failed: {e}", extra={'emoji_type': 'error'})
        return False

def trigger_sonarr_episode_search(episode_id):
    """Trigger a specific episode search in Sonarr"""
    try:
        episode_id_int = int(episode_id)
        cfg = get_arr_config('tv', False)
        response = requests.post(
            join_endpoint(cfg['url'], 'command'),
            json={'name': 'EpisodeSearch', 'episodeIds': [episode_id_int]},
            headers={'X-Api-Key': cfg['api_key']}
        )
        response.raise_for_status()
        logger.debug(f"Sonarr episode search triggered for episode id {episode_id_int}", extra={'emoji_type': 'debug'})
        return True
    except Exception as e:
        logger.error(f"Sonarr episode search failed: {e}", extra={'emoji_type': 'error'})
        return False

def get_episodes_for_lookahead(series_id, current_season, current_episode, lookahead=5):
    """
    Get episodes for lookahead processing with proper range limiting and specials handling
    """
    logger.debug(f"Selecting episodes starting from S{current_season}E{current_episode} with lookahead {lookahead}", 
                extra={'emoji_type': 'debug'})
    
    # Get all episodes for the series from Sonarr
    sonarr_cfg = get_arr_config('tv', False)
    url = join_endpoint(sonarr_cfg['url'], 'episode')
    params = {'seriesId': series_id}
    headers = {'X-Api-Key': sonarr_cfg['api_key']}

    try:
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        all_episodes = response.json()
    except Exception as e:
        logger.error(f"Failed to fetch episodes: {str(e)}", extra={'emoji_type': 'error'})
        return [], False
    
    # Determine if we include specials
    include_specials = getattr(settings, 'INCLUDE_SPECIALS', False)
    
    # Filter episodes based on season number
    if include_specials:
        episodes = all_episodes
    else:
        episodes = [ep for ep in all_episodes if ep.get('seasonNumber', 0) > 0]
    
    # Find the absolute last episode in the series
    if episodes:
        last_episode = max(episodes, key=lambda x: (x.get('seasonNumber', 0), x.get('episodeNumber', 0)))
        last_season = last_episode.get('seasonNumber', 0)
        last_episode_num = last_episode.get('episodeNumber', 0)
    else:
        last_season = 0
        last_episode_num = 0
    
    # Get max episode number for each season for range calculation
    max_episodes_by_season = {}
    for ep in episodes:
        season = ep.get('seasonNumber', 0)
        episode = ep.get('episodeNumber', 0)
        max_episodes_by_season[season] = max(episode, max_episodes_by_season.get(season, 0))
    
    # Calculate the end point of the lookahead range
    range_end_season = current_season
    range_end_episode = current_episode + lookahead
    
    # If we exceed episode count in this season, roll over to next season
    while (range_end_season in max_episodes_by_season and 
           range_end_episode > max_episodes_by_season[range_end_season]):
        # Calculate how many episodes to carry over
        overflow = range_end_episode - max_episodes_by_season[range_end_season]
        # Move to next season
        range_end_season += 1
        # Start from episode 1, plus overflow
        range_end_episode = overflow
    
    # Check if our range extends to or beyond the last episode
    reached_end = (range_end_season > last_season or 
                  (range_end_season == last_season and range_end_episode >= last_episode_num))
    
    # Filter episodes within range that don't have files
    filtered_episodes = []
    for ep in episodes:
        season = ep.get('seasonNumber', 0)
        episode = ep.get('episodeNumber', 0)
        
        # Episode is within range if:
        # 1. It's after current position (same season & later episode OR later season)
        # 2. It's within the end range boundary
        # 3. It doesn't have a file
        if ((season > current_season or (season == current_season and episode >= current_episode)) and
            (season < range_end_season or (season == range_end_season and episode <= range_end_episode)) and
            not ep.get('hasFile', False)):
            filtered_episodes.append(ep)
    
    # Log the episodes we're going to monitor
    if filtered_episodes:
        start_ep = filtered_episodes[0]
        end_ep = filtered_episodes[-1]
        start_season = start_ep.get('seasonNumber')
        start_episode = start_ep.get('episodeNumber')
        end_season = end_ep.get('seasonNumber')
        end_episode = end_ep.get('episodeNumber')
        
        if start_season == end_season:
            logger.info(f"Episode Selection: Monitoring S{start_season}E{start_episode}-E{end_episode}", 
                       extra={'emoji_type': 'info'})
        else:
            logger.info(f"Episode Selection: Monitoring episodes across seasons S{start_season}E{start_episode} to S{end_season}E{end_episode}", 
                       extra={'emoji_type': 'info'})
        
        if reached_end:
            logger.info("End of Episodes Detection: Reached end of known episodes, will mark entire series as monitored", 
                       extra={'emoji_type': 'info'})
    else:
        # No episodes found — let caller (handlers.py) decide how to log/user-notify.
        pass
    
    return filtered_episodes, reached_end


def fetch_sonarr_episodes(series_id):
    """Fetch all episodes for a series from Sonarr and return a mapping keyed by (season, episode).
    The returned dict contains episode metadata such as overview, title, episodeId, airDate, guestStars, directors, tvdbId, tmdbId, imdbId
    """
    try:
        if not series_id:
            return {}
        sonarr_cfg = get_arr_config('tv', False)
        url = join_endpoint(sonarr_cfg['url'], 'episode')
        params = {'seriesId': series_id}
        headers = {'X-Api-Key': sonarr_cfg['api_key']}
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        eps = r.json()
        mapping = {}
        for ep in eps:
            s = ep.get('seasonNumber')
            e = ep.get('episodeNumber')
            if s is None or e is None:
                continue
            key = (int(s), int(e))
            mapping[key] = {
                'overview': ep.get('overview') or ep.get('summary') or None,
                'title': ep.get('title') or None,
                'episodeId': ep.get('id') or None,
                'airDate': ep.get('airDate') or ep.get('airDateUtc') or None,
                'guestStars': ep.get('guestStars') or None,
                'directors': ep.get('directors') or None,
                'tvdbId': ep.get('tvdbId') or None,
                'tmdbId': ep.get('tmdbId') or None,
                'imdbId': ep.get('imdbId') or None,
            }
        return mapping
    except Exception as e:
        logger.debug(f"Failed to fetch episodes for series {series_id}: {e}", extra={'emoji_type': 'debug'})
        return {}

def monitor_episodes(series_id, episode_ids, monitor=True):
    """Mark multiple episodes as monitored/unmonitored in batch"""
    try:
        # Get episode details first to preserve other properties
        sonarr_cfg = get_arr_config('tv', False)
        url = join_endpoint(sonarr_cfg['url'], 'episode')
        params = {'seriesId': series_id}
        headers = {'X-Api-Key': sonarr_cfg['api_key']}

        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        episodes = response.json()

        # Filter to requested episodes and update monitored status
        to_update = [
            {**ep, 'monitored': monitor}
            for ep in episodes if ep['id'] in episode_ids
        ]

        # Update episodes in batch
        if to_update:
            for ep in to_update:
                update_url = join_endpoint(sonarr_cfg['url'], f"episode/{ep['id']}")
                update_response = requests.put(update_url, json=ep, headers=headers)
                update_response.raise_for_status()

            logger.info(f"Marked {len(to_update)} episodes as {'monitored' if monitor else 'unmonitored'}", 
                      extra={'emoji_type': 'monitored'})
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to update episode monitored status: {str(e)}", extra={'emoji_type': 'error'})
        return False

def mark_series_monitored(series_id, mark_seasons=False, include_specials=False):
    """Mark series as monitored, with options to control season monitoring"""
    try:
        # Get series details
        sonarr_cfg = get_arr_config('tv', False)
        url = join_endpoint(sonarr_cfg['url'], f'series/{series_id}')
        headers = {'X-Api-Key': sonarr_cfg['api_key']}

        response = requests.get(url, headers=headers)
        response.raise_for_status()
        series = response.json()

        # Always mark series as monitored
        series['monitored'] = True

        # Optionally mark seasons as monitored
        if mark_seasons:
            for season in series.get('seasons', []):
                season_number = season.get('seasonNumber', -1)
                # Mark normal seasons, only mark specials if requested
                if season_number > 0 or (season_number == 0 and include_specials):
                    season['monitored'] = True

        # Update the series
        update_response = requests.put(url, json=series, headers=headers)
        update_response.raise_for_status()

        log_message = f"Marked series '{series.get('title')}' as monitored"
        if mark_seasons:
            log_message += " with all seasons"
            if not include_specials:
                log_message += " (except specials)"
        logger.info(log_message, extra={'emoji_type': 'monitored'})
        return True
    except Exception as e:
        logger.error(f"Failed to mark series as monitored: {str(e)}", extra={'emoji_type': 'error'})
        return False

def monitor_season(series_id, season_number):
    """Mark a specific season as monitored"""
    try:
        # Get series details
        sonarr_cfg = get_arr_config('tv', False)
        url = join_endpoint(sonarr_cfg['url'], f'series/{series_id}')
        headers = {'X-Api-Key': sonarr_cfg['api_key']}

        response = requests.get(url, headers=headers)
        response.raise_for_status()
        series = response.json()

        # Mark series as monitored
        series['monitored'] = True

        # Mark the specific season as monitored
        for season in series.get('seasons', []):
            if season.get('seasonNumber') == int(season_number):
                season['monitored'] = True
                break

        # Update the series
        update_response = requests.put(url, json=series, headers=headers)
        update_response.raise_for_status()

        logger.info(f"Marked season {season_number} of '{series.get('title')}' as monitored", 
                  extra={'emoji_type': 'monitored'})
        return True
    except Exception as e:
        logger.error(f"Failed to mark season as monitored: {str(e)}", extra={'emoji_type': 'error'})
        return False

# For brevity, any additional integration functions (including Sonarr functions) are implemented similarly.
def update_plex_title(rating_key, base_title, status):
    """Update a Plex item's title using PlexAPI directly rather than URL construction"""
    try:
        # Get the item directly using PlexAPI
        item = plex.fetchItem(int(rating_key))
        base_title = strip_status_markers(base_title)
        new_title = f"{base_title} - {status}"
        # Use PlexAPI's built-in title update
        item.editTitle(new_title)
        item.reload()
        logger.info(f"Updated Plex title to: {new_title}", extra={'emoji_type': 'update'})
    except Exception as e:
        logger.error(f"Failed to update Plex title for {rating_key}: {str(e)}", extra={'emoji_type': 'error'})

def get_sonarr_queue(is_4k=False):
    """Get current queue items from Sonarr"""
    try:
        cfg = get_arr_config('tv', is_4k)
        url = join_endpoint(cfg['url'], 'queue')
        params = {'pageSize': 50}
        headers = {'X-Api-Key': cfg['api_key']}
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get('records', [])
    except Exception as e:
        logger.error(f"Error fetching Sonarr queue: {e}", extra={'emoji_type': 'error'})
        return []

def get_radarr_queue(is_4k=False):
    """Get current queue items from Radarr"""
    try:
        cfg = get_arr_config('movie', is_4k)
        url = join_endpoint(cfg['url'], 'queue')
        params = {'pageSize': 50}
        headers = {'X-Api-Key': cfg['api_key']}
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get('records', [])
    except Exception as e:
        logger.error(f"Error fetching Radarr queue: {e}", extra={'emoji_type': 'error'})
        return []

def search_in_sonarr(tvdb_id=None, title=None, year=None, rating_key=None, season_number=None, episode_number=None, is_4k=False, file_path=None):
    """
    Find a TV series in Sonarr using multiple fallback methods:
    1. Path-based ID matching (most reliable)
    2. TVDB ID matching
    3. Title matching
    Returns the series ID if found, None otherwise
    """
    try:
        # Determine which Sonarr instance to use
        cfg = get_arr_config('tv', is_4k)
        sonarr_url = cfg['url']
        headers = {"X-Api-Key": cfg['api_key']}
        
        # Prepare series endpoint
        series_url = join_endpoint(sonarr_url, 'series')

        # Prefer exact ID lookups first to avoid brittle folder-name matches
        try:
            if tvdb_id:
                try:
                    resp = requests.get(series_url, params={'tvdbId': tvdb_id}, headers=headers, timeout=8)
                    resp.raise_for_status()
                    j = resp.json()
                    if j:
                        sid = j[0].get('id')
                        logger.info(f" SONARR MATCH: id={sid} rule=tvdb_lookup candidate={tvdb_id}", extra={'emoji_type': 'info'})
                        return sid
                except Exception:
                    pass

            # If file path carries embedded IDs, try those lookups too (fast exact match)
            if file_path:
                imdb_match = re.search(r'{imdb-([^}]+)}', file_path)
                tvdb_match = re.search(r'{tvdb-(\d+)}', file_path)
                tmdb_match = re.search(r'{tmdb-(\d+)}', file_path)

                if imdb_match:
                    path_imdb = imdb_match.group(1)
                    try:
                        resp = requests.get(series_url, params={'imdbId': path_imdb}, headers=headers, timeout=8)
                        resp.raise_for_status()
                        j = resp.json()
                        if j:
                            sid = j[0].get('id')
                            logger.info(f" SONARR MATCH: id={sid} rule=path_imdb candidate={path_imdb}", extra={'emoji_type': 'info'})
                            return sid
                    except Exception:
                        pass

                if tvdb_match:
                    path_tvdb = tvdb_match.group(1)
                    try:
                        resp = requests.get(series_url, params={'tvdbId': path_tvdb}, headers=headers, timeout=8)
                        resp.raise_for_status()
                        j = resp.json()
                        if j:
                            sid = j[0].get('id')
                            logger.info(f" SONARR MATCH: id={sid} rule=path_tvdb candidate={path_tvdb}", extra={'emoji_type': 'info'})
                            return sid
                    except Exception:
                        pass

                if tmdb_match:
                    path_tmdb = tmdb_match.group(1)
                    try:
                        resp = requests.get(series_url, params={'tmdbId': path_tmdb}, headers=headers, timeout=8)
                        resp.raise_for_status()
                        j = resp.json()
                        if j:
                            sid = j[0].get('id')
                            logger.info(f" SONARR MATCH: id={sid} rule=path_tmdb candidate={path_tmdb}", extra={'emoji_type': 'info'})
                            return sid
                    except Exception:
                        pass
        except Exception:
            # Any lookup failure falls through to full-series scan
            pass

        # Fall back to pulling the full series list for fallback matching
        series_response = requests.get(series_url, headers=headers)
        if series_response.status_code != 200:
            logger.error(f" Failed to get series from Sonarr: {series_response.text}", extra={'emoji_type': 'error'})
            return None
        all_series = series_response.json()
        
        # METHOD 1: Try to match by filepath ID (most reliable)
        if file_path:
            # Extract ID using regex pattern matching
            imdb_match = re.search(r'{imdb-([^}]+)}', file_path)
            tvdb_match = re.search(r'{tvdb-(\d+)}', file_path)
            tmdb_match = re.search(r'{tmdb-(\d+)}', file_path)
            
            if imdb_match:
                path_id = imdb_match.group(1)
                for series in all_series:
                    if series.get('imdbId') == path_id:
                        logger.info(f" Found series in Sonarr by path IMDB ID: {series['title']}", extra={'emoji_type': 'info'})
                        return series['id']
            
            if tvdb_match:
                path_id = tvdb_match.group(1)
                for series in all_series:
                    if str(series.get('tvdbId')) == str(path_id):
                        logger.info(f" Found series in Sonarr by path TVDB ID: {series['title']}", extra={'emoji_type': 'info'})
                        return series['id']
            
            if tmdb_match:
                path_id = tmdb_match.group(1)
                for series in all_series:
                    if str(series.get('tmdbId')) == str(path_id):
                        logger.info(f" Found series in Sonarr by path TMDB ID: {series['title']}", extra={'emoji_type': 'info'})
                        return series['id']
                        
            # Also try to match by series folder name in path (last-resort).
            # Find actual season folder using a strict regex like 'Season 01' to avoid title collisions
            season_re = re.compile(r'(?i)^season\s*\d+')
            path_parts = file_path.split('/')
            for idx, part in enumerate(path_parts):
                next_part = path_parts[idx+1] if idx+1 < len(path_parts) else ''
                if season_re.match(next_part.strip()):
                    series_folder = part
                    series_folder_clean = series_folder.strip()
                    series_folder_lower = series_folder_clean.lower()
                    # Compare candidate against Sonarr series paths
                    for series in all_series:
                        series_path = series.get('path', '') or ''
                        series_path_clean = series_path.rstrip('/ ')
                        series_path_lower = series_path_clean.lower()
                        series_basename = os.path.basename(series_path_clean)

                        # Preserve substring matching but require season-like next segment
                        if series_folder_clean in series_path_clean:
                            # Emit concise match log
                            sid = series.get('id')
                            logger.info(f"SONARR MATCH: id={sid} title='{series.get('title')}' rule=folder_substring candidate={series_folder_clean}", extra={'emoji_type': 'info'})
                            return sid
        
        # METHOD 2: Try TVDB ID matching (next most reliable)
        if tvdb_id:
            for series in all_series:
                if str(series.get('tvdbId')) == str(tvdb_id):
                    logger.info(f"Found series in Sonarr by TVDB ID: {series['title']}", extra={'emoji_type': 'info'})
                    return series['id']
        
        # METHOD 3: Try title matching (least reliable but good fallback)
        if title:
            # Try exact match first
            for series in all_series:
                if series.get('title', '').lower() == title.lower():
                    logger.info(f"Found series in Sonarr by title: {series['title']}", extra={'emoji_type': 'info'})
                    return series['id']
        
        # If we get here, series wasn't found
        logger.warning(f"Series not found in Sonarr: {'TVDB:'+str(tvdb_id) if tvdb_id else title}", extra={'emoji_type': 'warning'})
        return None
        
    except Exception as e:
        logger.error(f"Error finding series in Sonarr: {e}", extra={'emoji_type': 'error'})
        return None

def delete_dummy_files(media_type, title, year, tvdb_id=None, library_path=None, season_number=None, episode_number=None, folder_path=None, arr_root_folder=None, season_folder_name=None, relative_path=None):
    """
    Delete placeholder files. Uses resolve_final_folder for path resolution.
    """
    # Centralized guard: if placeholder creation/deletion is disabled, skip and return False.
    if not getattr(settings, 'ENABLE_PLACEHOLDERS', True):
        logger.info("Placeholder deletion disabled by ENABLE_PLACEHOLDERS; skipping deletion", extra={'emoji_type': 'skip'})
        return False
    import os
    try:
        final_folder = resolve_final_folder(media_type, title, year, tvdb_id, season_number, folder_path, arr_root_folder, season_folder_name, relative_path)
        if not final_folder:
            logger.error("No valid folder path for dummy file deletion.", extra={'emoji_type': 'error'})
            return False
        # For TV episodes, delete only the specific episode file in the correct season folder
        if media_type == 'tv' and season_number is not None and episode_number is not None:
            patterns = [
                f"s{season_number:02d}e{episode_number:02d}",
                f"S{season_number:02d}E{episode_number:02d}",
                f" - s{season_number:02d}e{episode_number:02d}",
                f" - S{season_number:02d}E{episode_number:02d}"
            ]
            files_found = False
            if os.path.exists(final_folder):
                for file in os.listdir(final_folder):
                    if any(pattern in file for pattern in patterns):
                        file_path = os.path.join(final_folder, file)
                        try:
                            os.remove(file_path)
                            logger.info(f"Deleted placeholder file: {file_path}", extra={'emoji_type': 'delete'})
                            files_found = True
                        except Exception as e:
                            logger.error(f"Failed to delete file {file_path}: {e}", extra={'emoji_type': 'error'})
                if not os.listdir(final_folder):
                    os.rmdir(final_folder)
                    logger.info(f"Deleted empty season folder: {final_folder}", extra={'emoji_type': 'delete'})
                if not files_found:
                    logger.debug(f"No matching episode files found in {final_folder}", extra={'emoji_type': 'debug'})
            else:
                logger.debug(f"Season directory not found: {final_folder}", extra={'emoji_type': 'debug'})
        else:
            # Movies or entire TV series - delete the whole folder
            if os.path.exists(final_folder):
                shutil.rmtree(final_folder)
                logger.info(f"Deleted placeholder folder: {final_folder}", extra={'emoji_type': 'delete'})
        return True
    except Exception as e:
        logger.error(f"Error deleting placeholder: {e}", extra={'emoji_type': 'error'})
        return False

def check_arr_webhook(arr_name, arr_url, api_key, webhook_url):
    import urllib.parse
    try:
        headers = {'X-Api-Key': api_key}
        response = requests.get(f"{arr_url}/notification", headers=headers, timeout=10)
        response.raise_for_status()
    except Exception as e:
        # Could not contact the *arr API - show a hint for debugging
        logger.error(f"Failed to call {arr_name} /notification: {e}", extra={'emoji_type': 'error'})
        # Provide a safe curl hint (no API key printed)
        try:
            hint_base = arr_url.rstrip('/')
            logger.debug(f"Hint: verify {arr_name} is reachable from Placeholdarr host. Try: curl -H 'X-Api-Key: <api_key>' -i {hint_base}/notification", extra={'emoji_type': 'debug'})
        except Exception:
            pass
        return False

    try:
        notifications = response.json()
    except ValueError:
        logger.error(
            f"Failed to parse JSON from {arr_name} /notification: status={response.status_code} content-type={response.headers.get('Content-Type')} body={response.text[:200]}",
            extra={'emoji_type': 'error'}
        )
        logger.debug(f"Hint: try: curl -H 'X-Api-Key: <api_key>' -i {arr_url.rstrip('/')}/notification", extra={'emoji_type': 'debug'})
        return False

    found = False
    for n in notifications:
        if n.get('implementation', '').lower() == 'webhook':
            for field in n.get('fields', []):
                if field.get('name') == 'url' and webhook_url and webhook_url in str(field.get('value', '')):
                    found = True
                    break
        if found:
            break

    if found:
        logger.info(f"{arr_name} webhook for Placeholdarr is configured.", extra={'emoji_type': 'success'})
        return True

    # Not found - tailor message based on local vs remote intent
    parsed_arr = urllib.parse.urlparse(arr_url or '')
    arr_host = (parsed_arr.hostname or '').lower()
    parsed_wh = urllib.parse.urlparse(webhook_url or '') if webhook_url else None
    wh_host = (parsed_wh.hostname or '').lower() if parsed_wh else ''
    local_hosts = ('localhost', '127.0.0.1', '::1')
    is_local = (arr_host in local_hosts) or (wh_host in local_hosts) or (not webhook_url and arr_host in local_hosts)

    if is_local:
        # Localhost deployments: provide INFO-level guidance
        target = webhook_url or f"http://localhost:{os.getenv('PLACEHOLDARR_PORT') or getattr(settings, 'PLACEHOLDARR_PORT', 8001)}/webhook"
        logger.info(
            f"{arr_name} webhook for Placeholdarr is NOT configured. Detected local setup; add a webhook in {arr_name} Connect pointing to: {target}. If {arr_name} is remote instead, set PLACEHOLDARR_WEBHOOK_URL to the externally reachable webhook (example: https://placeholdarr.example.com/webhook).",
            extra={'emoji_type': 'info'}
        )
    else:
        # Reverse-proxy/public deployments: warn and recommend explicit override
        if webhook_url:
            logger.warning(
                f"{arr_name} webhook for Placeholdarr is NOT configured. Please add a webhook in {arr_name} Connect settings pointing to: {webhook_url}. If you're using a reverse proxy, set PLACEHOLDARR_WEBHOOK_URL to the externally reachable webhook (example: https://placeholdarr.example.com/webhook).",
                extra={'emoji_type': 'warning'}
            )
        else:
            logger.warning(
                f"{arr_name} webhook for Placeholdarr is NOT configured. Placeholdarr couldn't determine a webhook URL from your settings. Please set PLACEHOLDARR_WEBHOOK_URL to the externally reachable webhook URL (example: https://placeholdarr.example.com/webhook) and restart.",
                extra={'emoji_type': 'warning'}
            )
    return False


def check_all_arr_webhooks():
    # Allow skipping the webhook check for advanced/test setups
    if os.getenv('PLACEHOLDARR_SKIP_WEBHOOK_CHECK', '').lower() == 'true':
        logger.warning("PLACEHOLDARR_SKIP_WEBHOOK_CHECK is set. Skipping all webhook checks! Calendar sync will start regardless of webhook status.", extra={'emoji_type': 'warning'})
        return True

    # Allow user to override the webhook URL
    override_webhook_url = os.getenv('PLACEHOLDARR_WEBHOOK_URL')

    arr_urls = [
        getattr(settings, 'RADARR_URL', None),
        getattr(settings, 'RADARR_4K_URL', None),
        getattr(settings, 'SONARR_URL', None),
        getattr(settings, 'SONARR_4K_URL', None)
    ]
    arr_urls = [u for u in arr_urls if u and 'localhost' not in u and '127.0.0.1' not in u]
    if override_webhook_url:
        webhook_url = override_webhook_url
        logger.info(f"Using user-specified webhook URL for checks: {webhook_url}", extra={'emoji_type': 'info'})
    elif arr_urls:
        import urllib.parse
        parsed = urllib.parse.urlparse(arr_urls[0])
        scheme = parsed.scheme or 'http'
        host = parsed.hostname
        # Fallback to localhost when hostname can't be parsed
        if not host:
            host = 'localhost'
        # Handle IPv6 addresses for display
        if ':' in host and not host.startswith('['):
            host_display = f"[{host}]"
        else:
            host_display = host
        port_env = os.getenv('PLACEHOLDARR_PORT') or getattr(settings, 'PLACEHOLDARR_PORT', None)
        try:
            port_int = int(port_env) if port_env else None
        except Exception:
            port_int = None
        # Only include port when it's non-standard for the scheme
        if port_int and not ((scheme == 'http' and port_int == 80) or (scheme == 'https' and port_int == 443)):
            webhook_url = f"{scheme}://{host_display}:{port_int}/webhook"
        else:
            webhook_url = f"{scheme}://{host_display}/webhook"
        # Sanity-check: if URL looks malformed, don't use it
        if '::' in webhook_url or host_display in (None, ''):
            webhook_url = None
    else:
        port_env = os.getenv('PLACEHOLDARR_PORT') or getattr(settings, 'PLACEHOLDARR_PORT', None)
        try:
            port_int = int(port_env) if port_env else None
        except Exception:
            port_int = None
        if port_int:
            webhook_url = f"http://localhost:{port_int}/webhook"
        else:
            webhook_url = "http://localhost/webhook"

    arrs = []
    # Radarr
    if getattr(settings, 'RADARR_URL', None) and getattr(settings, 'RADARR_API_KEY', None):
        arrs.append(('Radarr', normalize_arr_base(settings.RADARR_URL), settings.RADARR_API_KEY))
    # Radarr 4K
    if getattr(settings, 'RADARR_4K_URL', None) and getattr(settings, 'RADARR_4K_API_KEY', None) and settings.RADARR_4K_URL.strip():
        arrs.append(('Radarr 4K', normalize_arr_base(settings.RADARR_4K_URL), settings.RADARR_4K_API_KEY))
    # Sonarr
    if getattr(settings, 'SONARR_URL', None) and getattr(settings, 'SONARR_API_KEY', None):
        arrs.append(('Sonarr', normalize_arr_base(settings.SONARR_URL), settings.SONARR_API_KEY))
    # Sonarr 4K
    if getattr(settings, 'SONARR_4K_URL', None) and getattr(settings, 'SONARR_4K_API_KEY', None) and settings.SONARR_4K_URL.strip():
        arrs.append(('Sonarr 4K', normalize_arr_base(settings.SONARR_4K_URL), settings.SONARR_4K_API_KEY))

    if not arrs:
        logger.error("No *arr services are configured.", extra={'emoji_type': 'error'})
        return False

    missing = []
    status_msgs = []
    for arr_name, arr_url, api_key in arrs:
        configured = bool(arr_url and api_key)
        webhook_ok = check_arr_webhook(arr_name, arr_url, api_key, webhook_url)
        status_msgs.append(f"{arr_name} (configured: {'yes' if configured else 'no'}, webhook: {'yes' if webhook_ok else 'no'})")
        if not webhook_ok:
            missing.append(arr_name)
    logger.info(f"Waiting for all configured *arrs to have webhooks set up. Detected: {', '.join(status_msgs)}.", extra={'emoji_type': 'info'})
    if missing:
        logger.warning(f"Webhooks still missing for: {', '.join(missing)}. Calendar sync will not start until all are ready.", extra={'emoji_type': 'warning'})
        return False
    return True

def batch_poll_and_update_plex_status(tvdb_id, title, episodes, max_attempts=10, initial_delay=0, throttle=0.2):
    """
    Batch poll Plex for summary readiness and update episode summaries with correct status/countdown.
    Args:
        tvdb_id: TVDB ID of the show
        title: Title of the show
        episodes: List of dicts with keys: season_num, episode_num, air_date (datetime), episode_title
        max_attempts: Max polling attempts
        initial_delay: Delay before first poll (0 for instant)
        throttle: Throttle between updates
    """
    import time
    from datetime import datetime, timedelta
    from services.plex_client import batch_update_plex_episode_status, find_show_by_id
    
    def _local_date(dt):
        return dt.astimezone().date() if dt and hasattr(dt, 'tzinfo') and dt.tzinfo else dt.date() if dt else None
    def _get_unaired_status(air_date):
        now = datetime.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)
        if not air_date:
            return None
        local_air_date = _local_date(air_date)
        if local_air_date == today:
            return "Airing today"
        elif local_air_date == tomorrow:
            return "Airing in 1 day"
        elif local_air_date > today:
            days_left = (local_air_date - today).days
            return f"Airing in {days_left} days"
        return None
    ep_lookup = {(int(ep['season_num']), int(ep['episode_num'])): ep for ep in episodes}
    done = set()
    attempt = 0
    prev_done_count = 0
    # Polling ramp-up: 1s, 5s, 10s, 20s, then 20s for all subsequent polls
    poll_schedule = [1, 5, 10, 20]
    delay = initial_delay
    while attempt < max_attempts:
        show = find_show_by_id(tvdb_id, title)
        if not show:
            logger.error(f"[BatchPoll] Could not find show with TVDB ID {tvdb_id}", extra={'emoji_type': 'error'})
            return False
        all_eps = list(show.episodes())
        plex_ep_map = {}
        for ep in all_eps:
            try:
                s = getattr(ep, 'seasonNumber', None) or getattr(ep, 'season', None)
                e = getattr(ep, 'episodeNumber', None) or getattr(ep, 'index', None)
                if s is not None and e is not None:
                    plex_ep_map[(int(s), int(e))] = ep
            except Exception as ex:
                logger.warning(f"[BatchPoll] Error mapping episode: {ex}", extra={'emoji_type': 'skip'})
        status_map = {}
        for key, ep_data in ep_lookup.items():
            if key in done:
                continue
            season, episode = key
            air_date = ep_data.get('air_date')
            # Ensure both air_date and now are comparable (naive or aware)
            if air_date and hasattr(air_date, 'tzinfo') and air_date.tzinfo:
                now = datetime.now(air_date.tzinfo)
            else:
                now = datetime.now()
            aired = air_date and air_date <= now
            plex_ep = plex_ep_map.get(key)
            if not plex_ep:
                continue  # Not yet in Plex
            current_summary = getattr(plex_ep, 'summary', '') or ''
            if aired:
                if current_summary.strip() != '':
                    status_map[key] = "Request"
                    done.add(key)
            else:
                status_map[key] = _get_unaired_status(air_date)
                done.add(key)
        batch_start = time.time()
        if status_map:
            batch_update_plex_episode_status(tvdb_id, title, status_map, throttle=throttle)
        batch_duration = time.time() - batch_start
        if len(done) == prev_done_count and attempt > 0:
            logger.info("[BatchPoll] No new episodes ready since last poll. Finishing early and updating all remaining as blank.", extra={'emoji_type': 'info'})
            remaining_status_map = {}
            for key, ep_data in ep_lookup.items():
                if key not in done:
                    air_date = ep_data.get('air_date')
                    if air_date and hasattr(air_date, 'tzinfo') and air_date.tzinfo:
                        now = datetime.now(air_date.tzinfo)
                    else:
                        now = datetime.now()
                    aired = air_date and air_date <= now
                    if aired:
                        remaining_status_map[key] = "Request"
                    else:
                        remaining_status_map[key] = _get_unaired_status(air_date)
            if remaining_status_map:
                batch_update_plex_episode_status(tvdb_id, title, remaining_status_map, throttle=throttle)
            logger.info(f"[BatchPoll] Early finish: updated all remaining episodes for '{title}'", extra={'emoji_type': 'success'})
            return True
        prev_done_count = len(done)
        if len(done) == len(ep_lookup):
            logger.info(f"[BatchPoll] All {len(done)} episodes updated for '{title}'", extra={'emoji_type': 'success'})
            return True
        attempt += 1
        # Ramp-up poll interval: 1, 5, 10, 20, then 20s for all subsequent polls
        if attempt < len(poll_schedule):
            wait_time = max(0, poll_schedule[attempt] - batch_duration)
        else:
            wait_time = max(0, 20 - batch_duration)
        logger.info(f"[BatchPoll] {len(done)}/{len(ep_lookup)} episodes updated for '{title}'. Polling again in {wait_time:.2f}s...", extra={'emoji_type': 'debug'})
        if wait_time > 0:
            time.sleep(wait_time)
    logger.warning(f"[BatchPoll] Polling ended. Forcing update of all remaining episodes for '{title}'", extra={'emoji_type': 'warning'})
    remaining_status_map = {}
    for key, ep_data in ep_lookup.items():
        if key not in done:
            air_date = ep_data.get('air_date')
            if air_date and hasattr(air_date, 'tzinfo') and air_date.tzinfo:
                now = datetime.now(air_date.tzinfo)
            else:
                now = datetime.now()
            aired = air_date and air_date <= now
            if aired:
                remaining_status_map[key] = "Request"
            else:
                remaining_status_map[key] = _get_unaired_status(air_date)
    if remaining_status_map:
        batch_update_plex_episode_status(tvdb_id, title, remaining_status_map, throttle=throttle)
    logger.info(f"[BatchPoll] Final forced update: updated all remaining episodes for '{title}'", extra={'emoji_type': 'success'})
    return True