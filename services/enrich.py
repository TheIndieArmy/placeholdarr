from services.postgres.db import get_session
from services.postgres.models import SubFlow, Episode, Season, Movie
from services.arr_clients import fetch_sonarr_episodes
from services.postgres.db import get_engine
from core.logger import logger
from sqlalchemy import text
from datetime import datetime
from core.config import settings
from services.orchestrator import OrchestratorRun


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
            # If this SubFlow is a series-level SubFlow, create episode subflows
            if getattr(sf, 'series_id', None):
                try:
                    from services.list_capture import create_episode_subflows_for_series
                    # create a logical run id for this operation
                    run = OrchestratorRun(types=['tv'], note=f'create_episode_subflows_for_series:subflow:{sf.id}')
                    created = create_episode_subflows_for_series(sf.series_id, run.run_id, include_specials=getattr(settings, 'INCLUDE_SPECIALS', False))
                    logger.info(f"Created {created} episode subflows for series subflow {sf.id}")
                except Exception as ex:
                    logger.exception(f"Failed to create episode subflows for series {getattr(sf, 'series_id', None)}: {ex}")
                    # transient failure: let caller requeue
                    return False
                # mark the series-level subflow as DONE
                try:
                    sf.status = 'DONE'
                    session.add(sf)
                    session.commit()
                except Exception:
                    session.rollback()
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
                                    mv.moviefile_path = moviefile_path
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
                            # mark subflow done
                            sf.status = 'DONE'
                            session.add(sf)
                            session.commit()
                            logger.info(f"Enriched Movie SubFlow {sf.id} from Radarr (movie_id={sf.movie_id})")
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
                        episode.episodefile_path = ep_file_path
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
                    episode.episodefile_path = ep_file_path
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
                    session.commit()
                    return True
                else:
                    logger.info(f"Could not find episode data for episode id {episode_id} from Sonarr")
                    return True
            finally:
                _pg_advisory_unlock(conn, int(episode_id))
    finally:
        session.close()
