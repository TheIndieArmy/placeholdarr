from services.postgres.db import get_session
from services.postgres.models import SubFlow, Episode, Season, Movie, Series
from services.arr_clients import fetch_sonarr_episodes
from services.postgres.db import get_engine
from core.logger import logger
import threading
import time

# Summary aggregation for enriched movies
_enrich_lock = threading.Lock()
_enrich_count = 0
_enrich_since = 0.0
_enrich_ids = []
_ENRICH_COUNT_THRESHOLD = 1000
_ENRICH_TIME_THRESHOLD = 10.0


def _maybe_flush_enrich_summary(force: bool = False):
    global _enrich_count, _enrich_since, _enrich_ids
    now = time.time()
    with _enrich_lock:
        if not force and _enrich_count < _ENRICH_COUNT_THRESHOLD and (now - _enrich_since) < _ENRICH_TIME_THRESHOLD:
            return
        if _enrich_count == 0:
            _enrich_since = now
            return
        # Log a compact INFO summary
        try:
            logger.info(f"Processed {_enrich_count} enriched movies; sample ids={_enrich_ids[:10]}")
        except Exception:
            pass
        # reset
        _enrich_count = 0
        _enrich_ids = []
        _enrich_since = now

# Ensure we flush at process exit
try:
    import atexit
    atexit.register(lambda: _maybe_flush_enrich_summary(force=True))
except Exception:
    pass
from sqlalchemy import text
from datetime import datetime
from core.config import settings
from services.orchestrator import OrchestratorRun
from services.integrations import flow_enrich_series


def _pg_try_advisory_lock(conn, key: int) -> bool:
    try:
        res = conn.execute(text('SELECT pg_try_advisory_lock(:k)'), {'k': key}).scalar()
        return bool(res)
    except Exception:
        return False


def _pg_advisory_unlock(conn, key: int) -> None:
    try:
        conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': key})
    except Exception:
        pass


def process_enrich_base_subflow(subflow_id: int) -> bool:
    """Process the 'enrich_base' step for an Episode-level SubFlow.

    Steps:
    - load SubFlow row and associated Episode
    - acquire advisory lock keyed by episode_id
    - fetch episode metadata from Sonarr (via fetch_sonarr_episodes for its series)
    - upsert Episode fields (file path, size, quality, has_file, overview)
    - mark SubFlow.status = 'DONE' and update timestamps
    Returns True on success, False on transient failure.
    """
    session = get_session()
    try:
        sf = session.query(SubFlow).filter(SubFlow.id == subflow_id).first()
        if not sf:
            logger.info(f"SubFlow id {subflow_id} not found")
            return True
        if not sf.episode_id:
            # If this SubFlow is a series-level SubFlow, do NOT perform episode
            # discovery inline here. The worker's phase advance logic will enqueue
            # the dedicated 'subjob:create_episode_subflows' job which is the
            # canonical place to run batch discovery and creation. Returning True
            # allows the worker to advance the subflow and insert the next-phase
            # job so that dedupe/gating/batching remains centralized in the
            # job pipeline.
            if getattr(sf, 'series_id', None):
                return True
            # If this is a movie-level SubFlow, perform minimal movie enrichment
            if getattr(sf, 'movie_id', None):
                try:
                    # Enrich movie by fetching Radarr data and persisting key fields
                    mv = session.query(Movie).filter(Movie.id == int(sf.movie_id)).first()
                    if not mv:
                        logger.info(f"Movie id {sf.movie_id} not found for SubFlow {sf.id}")
                        sf.status = 'FAILED'
                        session.add(sf)
                        session.commit()
                        return False

                    # Try to fetch Radarr entries and find the matching movie by radarrid/tmdbid/imdbid
                    try:
                        from services.arr_clients import fetch_radarr_movies
                        entries = fetch_radarr_movies()
                    except Exception:
                        entries = None

                    matched = None
                    if entries:
                        for e in entries:
                            try:
                                # radarr id
                                if mv.radarrid and e.get('id') and int(e.get('id')) == int(mv.radarrid):
                                    matched = e
                                    break
                                # tmdb
                                if mv.tmdbid and (e.get('tmdbId') or e.get('tmdb')) and int((e.get('tmdbId') or e.get('tmdb'))) == int(mv.tmdbid):
                                    matched = e
                                    break
                                # imdb
                                if mv.imdbid and (e.get('imdbId') or e.get('imdb')) and str(e.get('imdbId') or e.get('imdb')) == str(mv.imdbid):
                                    matched = e
                                    break
                            except Exception:
                                continue

                    # If we found metadata, persist sensible fields
                    if matched:
                        try:
                            # title/year
                            mv.title = matched.get('title') or mv.title
                            try:
                                y = matched.get('year') or 0
                                mv.year = int(y) if y else mv.year
                            except Exception:
                                pass

                            # poster
                            remote_poster = matched.get('remotePoster')
                            if not remote_poster:
                                imgs = matched.get('images') or []
                                for img in imgs:
                                    try:
                                        if img.get('coverType') and img.get('coverType').lower() == 'poster' and img.get('remoteUrl'):
                                            remote_poster = img.get('remoteUrl')
                                            break
                                        if not remote_poster and img.get('remoteUrl'):
                                            remote_poster = img.get('remoteUrl')
                                    except Exception:
                                        continue
                            if remote_poster:
                                mv.remote_poster = remote_poster

                            # movie file info
                            moviefile = matched.get('movieFile') or {}
                            moviefile_path = None
                            try:
                                if moviefile and isinstance(moviefile, dict) and moviefile.get('path'):
                                    moviefile_path = moviefile.get('path')
                                if moviefile_path:
                                    mv.radarr_filepath = moviefile_path
                                mv.moviefile_size = moviefile.get('size') or moviefile.get('sizeOnDisk') or mv.moviefile_size
                                mv.has_file = bool(matched.get('hasFile') or moviefile_path)
                            except Exception:
                                pass

                            # quality/monitored/status/overview
                            try:
                                q = None
                                mfq = moviefile.get('quality') if isinstance(moviefile, dict) else None
                                if mfq:
                                    if isinstance(mfq, dict):
                                        q = mfq.get('quality') or mfq.get('name')
                                    else:
                                        q = str(mfq)
                                if not q:
                                    qobj = matched.get('quality') or matched.get('qualityProfile')
                                    if isinstance(qobj, dict):
                                        # prefer nested name or quality keys
                                        q = qobj.get('quality') or qobj.get('name') or None
                                    else:
                                        q = qobj
                                # Ensure q is a string (SQLAlchemy String column cannot accept dicts)
                                if isinstance(q, dict):
                                    # try to extract common name fields or fallback to str()
                                    q = q.get('name') or q.get('quality') or str(q)
                                if q is not None:
                                    mv.radarr_quality = str(q)
                            except Exception:
                                pass
                            try:
                                mv.radarr_monitored = bool(matched.get('monitored') or mv.radarr_monitored)
                            except Exception:
                                pass
                            try:
                                mv.radarr_release_status = matched.get('status') or mv.radarr_release_status
                            except Exception:
                                pass
                            try:
                                overview = matched.get('overview') or matched.get('plot') or matched.get('synopsis')
                                if overview:
                                    mv.radarr_overview = overview
                            except Exception:
                                pass

                            # release dates
                            try:
                                from datetime import datetime as _dt
                                rd = matched.get('releaseDate')
                                if rd:
                                    rdnorm = rd.replace('Z', '+00:00') if isinstance(rd, str) else rd
                                    mv.theater_release_date = _dt.fromisoformat(rdnorm).date()
                                pd = matched.get('physicalRelease') or matched.get('physical_release')
                                if pd:
                                    pdnorm = pd.replace('Z', '+00:00') if isinstance(pd, str) else pd
                                    mv.physical_release_date = _dt.fromisoformat(pdnorm).date()
                            except Exception:
                                pass

                            session.add(mv)
                            # populate placeholder_folder during ARR enrichment so later phases can match
                            try:
                                # Use the integration helper to populate placeholder_folder if blank
                                from services.postgres.models import Movie as MovieModel
                                try:
                                    flow_enrich_series(session, mv.id, MovieModel)
                                except Exception:
                                    # best-effort: do not fail enrichment for placeholder population
                                    logger.debug(f'flow_enrich_series failed for movie {mv.id}', extra={'emoji_type': 'debug'})
                            except Exception:
                                pass
                            # mark subflow done
                            sf.status = 'DONE'
                            session.add(sf)
                            session.commit()
                            # Per-item enrichment messages are VERBOSE; aggregate summaries are INFO
                            try:
                                logger.verbose(f"Enriched Movie SubFlow {sf.id} from Radarr (movie_id={sf.movie_id})")
                            except Exception:
                                pass
                            # Update aggregated counters and maybe flush
                            try:
                                with _enrich_lock:
                                    _enrich_count += 1
                                    _enrich_ids.append(int(sf.movie_id) if sf.movie_id else int(sf.id))
                                    if _enrich_count >= _ENRICH_COUNT_THRESHOLD:
                                        # flush from background context
                                        _maybe_flush_enrich_summary(force=True)
                                    else:
                                        # time-based flush handled by checking in call
                                        _maybe_flush_enrich_summary()
                            except Exception:
                                pass
                            return True
                            return True
                        except Exception as ex:
                            session.rollback()
                            logger.exception(f"Failed to persist Radarr data for movie subflow {sf.id}: {ex}")
                            return False

                    # If no match found, mark subflow DONE to avoid repeated wasted attempts
                    sf.status = 'DONE'
                    session.add(sf)
                    session.commit()
                    logger.info(f"No Radarr metadata found for Movie SubFlow {sf.id}; marked DONE")
                    return True
                except Exception as ex:
                    session.rollback()
                    logger.exception(f"Failed movie-level enrich for subflow {sf.id}: {ex}")
                    return False

            # Not an episode, series nor movie-level SubFlow -> treat as failure
            logger.info(f"SubFlow {subflow_id} has no episode_id and is not a series-level or movie-level subflow")
            sf.status = 'FAILED'
            session.add(sf)
            session.commit()
            return False

        ep_id = sf.episode_id
        # Acquire advisory lock using episode id as key
        eng = get_engine()
        with eng.connect() as conn:
            locked = _pg_try_advisory_lock(conn, int(ep_id))
            if not locked:
                # transient: caller should requeue
                logger.info(f"Could not acquire advisory lock for episode {ep_id}")
                return False
            try:
                # Reload episode within a session
                episode = session.query(Episode).filter(Episode.id == ep_id).first()
                if not episode:
                    logger.info(f"Episode id {ep_id} not found")
                    sf.status = 'FAILED'
                    session.add(sf)
                    session.commit()
                    return False
                # Fetch minimal Season and Series fields via SQL to avoid selecting columns that may not exist
                try:
                    row = session.execute(text("SELECT id, series_id, season_number FROM season WHERE id = :id"), {'id': episode.season_id}).fetchone()
                except Exception:
                    row = None
                if not row:
                    logger.info(f"Episode {ep_id} missing Season row")
                else:
                    season_id = row[0]
                    season_number = row[2]
                    try:
                        srow = session.execute(text("SELECT id, sonarrid FROM series WHERE id = :id"), {'id': row[1]}).fetchone()
                    except Exception:
                        srow = None
                    if not srow:
                        logger.info(f"Episode {ep_id} missing Series row")
                    else:
                        series_sonarrid = srow[1]
                        entries = fetch_sonarr_episodes(series_sonarrid)
                    target = None
                    for e in entries or []:
                        if 'id' in e and episode.sonarrid and e.get('id') == episode.sonarrid:
                            target = e
                            break
                        if e.get('seasonNumber') == season_number and e.get('episodeNumber') == episode.episode_number:
                            target = e
                            break

                    # Fetch series-level info once (may contain path/monitored)
                    try:
                        from services.arr_clients import fetch_sonarr_series_by_id
                        series_info = fetch_sonarr_series_by_id(series_sonarrid)
                    except Exception:
                        series_info = None

                    if target:
                        ep_file = target.get('episodeFile') or {}
                        # If Sonarr reports hasFile but episodeFile is None, Sonarr may provide episodeFileId
                        # Try to fetch the episodeFile object if needed
                        if (not ep_file or not isinstance(ep_file, dict)) and target.get('episodeFileId'):
                            try:
                                from services.arr_clients import fetch_sonarr_episodefile
                                ef = fetch_sonarr_episodefile(target.get('episodeFileId'))
                                if isinstance(ef, dict):
                                    ep_file = ef
                            except Exception:
                                pass

                        ep_file_path = ep_file.get('path') if isinstance(ep_file, dict) else None
                        ep_file_size = None
                        try:
                            if isinstance(ep_file, dict):
                                ep_file_size = ep_file.get('size') or ep_file.get('sizeOnDisk')
                        except Exception:
                            ep_file_size = None

                        # persist file info
                        episode.sonarr_filepath = ep_file_path
                        episode.episodefile_size = ep_file_size
                        episode.sonarr_episode_overview = target.get('overview') or target.get('description') or episode.sonarr_episode_overview
                        episode.has_file = bool(target.get('hasFile') or (ep_file_path is not None))
                        # persist sonarrpath and monitored flags (prefer episode-level, fall back to series)
                        try:
                            if isinstance(series_info, dict):
                                episode.sonarrpath = series_info.get('path') or episode.sonarrpath
                        except Exception:
                            pass
                        try:
                            # prefer episode monitored flag if present, otherwise series-level
                            if target.get('monitored') is not None:
                                episode.sonarr_monitored = bool(target.get('monitored'))
                            elif isinstance(series_info, dict) and series_info.get('monitored') is not None:
                                episode.sonarr_monitored = bool(series_info.get('monitored'))
                        except Exception:
                            pass
                        # persist air_date (prefer airDate, fall back to airDateUtc)
                        try:
                            ad = target.get('airDate') or target.get('airDateUtc')
                            if ad:
                                from datetime import datetime as _dt
                                if isinstance(ad, str) and len(ad) == 10:
                                    episode.air_date = _dt.fromisoformat(ad).date()
                                else:
                                    adnorm = ad.replace('Z', '+00:00') if isinstance(ad, str) else ad
                                    episode.air_date = _dt.fromisoformat(adnorm).date()
                        except Exception:
                            pass
                        # quality
                        try:
                            q = None
                            if isinstance(ep_file, dict):
                                q = ep_file.get('quality')
                            if q is None:
                                q = target.get('quality')
                            # Normalize to string to avoid DB adapter errors
                            if isinstance(q, dict):
                                qq = q.get('quality') or q.get('name') or str(q)
                            else:
                                qq = q
                            if qq is not None:
                                episode.sonarr_quality = str(qq)
                        except Exception:
                            pass
                        session.add(episode)
                        # populate placeholder_folder for the series/episode during ARR enrichment
                        try:
                            from services.postgres.models import Episode as EpisodeModel
                            try:
                                flow_enrich_series(session, episode.id, EpisodeModel)
                            except Exception:
                                logger.debug(f'flow_enrich_series failed for episode {episode.id}', extra={'emoji_type': 'debug'})
                        except Exception:
                            pass
                        # Conservative propagation: if series has placeholder_folder and season/episode lack it, copy down
                        try:
                            if episode and episode.season_id:
                                try:
                                    season_obj = session.query(Season).filter(Season.id == int(episode.season_id)).first()
                                except Exception:
                                    season_obj = None
                                try:
                                    series_obj = None
                                    if season_obj and getattr(season_obj, 'series_id', None):
                                        series_obj = session.query(Series).filter(Series.id == int(season_obj.series_id)).first()
                                except Exception:
                                    series_obj = None
                                if series_obj and getattr(series_obj, 'placeholder_folder', None):
                                    pdir = series_obj.placeholder_folder
                                    try:
                                        if season_obj and not getattr(season_obj, 'placeholder_folder', None):
                                            season_obj.placeholder_folder = pdir
                                            session.add(season_obj)
                                    except Exception:
                                        pass
                                    try:
                                        if not getattr(episode, 'placeholder_folder', None):
                                            episode.placeholder_folder = pdir
                                            session.add(episode)
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                        # mark subflow done
                        sf.status = 'DONE'
                        sf.retry_count = 0
                        session.add(sf)
                        session.commit()
                        return True
                    else:
                        logger.info(f"Could not find episode data from Sonarr for episode id {ep_id}")
                        # mark done anyway to avoid repeated wasted calls, unless you prefer to requeue
                        sf.status = 'DONE'
                        session.add(sf)
                        session.commit()
                        return True
            finally:
                _pg_advisory_unlock(conn, int(ep_id))
    finally:
        session.close()


def enrich_episode(episode_id: int) -> bool:
    """Enrich an Episode row directly (used for job-driven reenrich).

    Acquires advisory lock on episode_id, fetches Sonarr episode data and updates
    Episode fields similarly to process_enrich_base_subflow.
    Returns True on success, False on transient failure.
    """
    session = get_session()
    try:
        episode = session.query(Episode).filter(Episode.id == episode_id).first()
        if not episode:
            logger.info(f"Episode id {episode_id} not found")
            return True

        # Fetch minimal Season and Series fields via SQL to avoid selecting unknown columns
        try:
            row = session.execute(text("SELECT id, series_id, season_number FROM season WHERE id = :id"), {'id': episode.season_id}).fetchone()
        except Exception:
            row = None
        if not row:
            logger.info(f"Episode {episode_id} missing Season row")
            return False
        season_id = row[0]
        season_number = row[2]
        try:
            srow = session.execute(text("SELECT id, sonarrid FROM series WHERE id = :id"), {'id': row[1]}).fetchone()
        except Exception:
            srow = None
        if not srow:
            logger.info(f"Series for episode {episode_id} not found")
            return False
        series_sonarr_id = srow[1]
        if not series_sonarr_id:
            logger.info(f"Series for episode {episode_id} has no sonarrid")
            return False

        eng = get_engine()
        with eng.connect() as conn:
            locked = _pg_try_advisory_lock(conn, int(episode_id))
            if not locked:
                logger.info(f"Could not acquire advisory lock for episode {episode_id}")
                return False
            try:
                entries = fetch_sonarr_episodes(series_sonarr_id)
                target = None
                for e in entries or []:
                    if 'id' in e and episode.sonarrid and e.get('id') == episode.sonarrid:
                        target = e
                        break
                    if e.get('seasonNumber') == season_number and e.get('episodeNumber') == episode.episode_number:
                        target = e
                        break

                # fetch series-level info once (may contain path/monitored)
                try:
                    from services.arr_clients import fetch_sonarr_series_by_id
                    series_info = fetch_sonarr_series_by_id(series_sonarr_id)
                except Exception:
                    series_info = None

                if target:
                    ep_file = target.get('episodeFile') or {}
                    # If Sonarr reports hasFile but episodeFile is None, Sonarr may provide episodeFileId
                    # Try to fetch the episodeFile object if needed
                    if (not ep_file or not isinstance(ep_file, dict)) and target.get('episodeFileId'):
                        try:
                            from services.arr_clients import fetch_sonarr_episodefile
                            ef = fetch_sonarr_episodefile(target.get('episodeFileId'))
                            if isinstance(ef, dict):
                                ep_file = ef
                        except Exception:
                            pass

                    ep_file_path = ep_file.get('path') if isinstance(ep_file, dict) else None
                    ep_file_size = None
                    try:
                        ep_file_size = ep_file.get('size') or ep_file.get('sizeOnDisk')
                    except Exception:
                        ep_file_size = None

                    # persist file info
                    episode.sonarr_filepath = ep_file_path
                    episode.episodefile_size = ep_file_size
                    episode.sonarr_episode_overview = target.get('overview') or target.get('description') or episode.sonarr_episode_overview
                    episode.has_file = bool(target.get('hasFile') or (ep_file_path is not None))
                    # persist sonarrpath and monitored flags (prefer episode-level, fall back to series)
                    try:
                        if isinstance(series_info, dict):
                            episode.sonarrpath = series_info.get('path') or episode.sonarrpath
                    except Exception:
                        pass
                    try:
                        if target.get('monitored') is not None:
                            episode.sonarr_monitored = bool(target.get('monitored'))
                        elif isinstance(series_info, dict) and series_info.get('monitored') is not None:
                            episode.sonarr_monitored = bool(series_info.get('monitored'))
                    except Exception:
                        pass
                    # persist air_date (prefer airDate, fall back to airDateUtc)
                    try:
                        ad = target.get('airDate') or target.get('airDateUtc')
                        if ad:
                            from datetime import datetime as _dt
                            if isinstance(ad, str) and len(ad) == 10:
                                episode.air_date = _dt.fromisoformat(ad).date()
                            else:
                                adnorm = ad.replace('Z', '+00:00') if isinstance(ad, str) else ad
                                episode.air_date = _dt.fromisoformat(adnorm).date()
                    except Exception:
                        pass

                    try:
                        q = ep_file.get('quality') if isinstance(ep_file, dict) else target.get('quality')
                        if isinstance(q, dict):
                            qq = q.get('quality') or q.get('name') or str(q)
                        else:
                            qq = q
                        if qq is not None:
                            episode.sonarr_quality = str(qq)
                    except Exception:
                        pass
                    session.add(episode)
                    # Conservative propagation: if series has placeholder_folder and season/episode lack it, copy down
                    try:
                        try:
                            season_obj = session.query(Season).filter(Season.id == int(episode.season_id)).first()
                        except Exception:
                            season_obj = None
                        try:
                            series_obj = None
                            if season_obj and getattr(season_obj, 'series_id', None):
                                series_obj = session.query(Series).filter(Series.id == int(season_obj.series_id)).first()
                        except Exception:
                            series_obj = None
                        if series_obj and getattr(series_obj, 'placeholder_folder', None):
                            pdir = series_obj.placeholder_folder
                            try:
                                if season_obj and not getattr(season_obj, 'placeholder_folder', None):
                                    season_obj.placeholder_folder = pdir
                                    session.add(season_obj)
                            except Exception:
                                pass
                            try:
                                if not getattr(episode, 'placeholder_folder', None):
                                    episode.placeholder_folder = pdir
                                    session.add(episode)
                            except Exception:
                                pass
                    except Exception:
                        pass

                    session.commit()
                    return True
                else:
                    logger.info(f"Could not find episode data for episode id {episode_id} from Sonarr")
                    return True
            finally:
                _pg_advisory_unlock(conn, int(episode_id))
    finally:
        session.close()
