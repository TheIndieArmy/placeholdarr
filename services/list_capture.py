from services.arr_clients import fetch_radarr_movies, fetch_sonarr_series, fetch_sonarr_episodes
from services.postgres.db import get_session
from services.postgres.models import Movie, SubFlow, Job, Series, Season, Episode
from services.orchestrator import OrchestratorRun, FAR_FUTURE, PHASES
from datetime import datetime
import os
from typing import List
from core.config import settings
from core.logger import logger

# Batch size when creating episode subflows/jobs from a series
EPISODE_BATCH_SIZE = 500
# Series-specific phase list: series should perform base enrichment then create episode subflows
SERIES_PHASES = ['enrich_base', 'create_episode_subflows']
# Movie-specific phases: skip list_capture (we removed it globally) and go straight to enrichment
MOVIE_PHASES = ['enrich_base', 'fs_scan', 'merge_scan', 'determine', 'materialize']


def upsert_movie_from_radarr_entry(session, entry: dict):
    # Map common Radarr fields into our Movie model. We persist
    # identifiers, release dates, poster, file path/size and a few flags.
    tmdb = entry.get('tmdbId') or entry.get('tmdb')
    title = entry.get('title')
    # Prefer explicit year field when available; fall back to releaseDate's year
    year = None
    if entry.get('year'):
        try:
            year = int(entry.get('year'))
        except Exception:
            year = None
    if not year:
        rd = entry.get('releaseDate') or entry.get('physicalRelease')
        if rd:
            try:
                # releaseDate may include timezone 'Z'
                from datetime import datetime as _dt
                rdnorm = rd.replace('Z', '+00:00') if isinstance(rd, str) else rd
                year = _dt.fromisoformat(rdnorm).year
            except Exception:
                year = None

    radarrid = entry.get('id')
    imdb = entry.get('imdbId') or entry.get('imdb')

    # poster: prefer explicit remotePoster, else scan images for a poster remoteUrl
    remote_poster = entry.get('remotePoster')
    if not remote_poster:
        imgs = entry.get('images') or []
        for img in imgs:
            try:
                # Sonarr/Radarr image shapes vary; common keys: 'remoteUrl', 'coverType'
                if img.get('coverType') and img.get('coverType').lower() == 'poster' and img.get('remoteUrl'):
                    remote_poster = img.get('remoteUrl')
                    break
                if not remote_poster and img.get('remoteUrl'):
                    remote_poster = img.get('remoteUrl')
            except Exception:
                continue

    # Determine the most specific Radarr path available.
    # Prefer the movie-level path (the folder for the movie) or the movieFile path's directory
    # over the library rootFolderPath.
    radarr_path = None
    # movie file information may be nested under 'movieFile'
    moviefile = entry.get('movieFile') or {}
    try:
        # If Radarr provided a movieFile path, use its directory as the canonical movie folder
        if moviefile and isinstance(moviefile, dict) and moviefile.get('path'):
            radarr_path = os.path.dirname(moviefile.get('path'))
        # Prefer explicit movie path if present
        if not radarr_path:
            radarr_path = entry.get('path') or entry.get('rootFolderPath')
    except Exception:
        radarr_path = entry.get('path') or entry.get('rootFolderPath')
    moviefile_path = moviefile.get('path') if isinstance(moviefile, dict) else None
    moviefile_size = None
    try:
        moviefile_size = moviefile.get('size') or moviefile.get('sizeOnDisk') or entry.get('sizeOnDisk')
    except Exception:
        moviefile_size = entry.get('sizeOnDisk')

    has_file = bool(entry.get('hasFile') or (moviefile_path is not None))
    monitored = bool(entry.get('monitored') or entry.get('monitored'))
    radarr_quality = None
    try:
        # Normalize several Radarr shapes to get a human-friendly quality label.
        # Preferred sources (in order):
        # 1) movieFile.quality.quality.name (when a file is imported)
        # 2) movieFile.quality.name or movieFile.quality (some Radarr versions)
        # 3) top-level entry['quality'] or entry['qualityProfile'] (may be dict or string)
        def _extract_name_from_quality_obj(qobj):
            # qobj may be nested like { 'quality': { 'id':.., 'name': '1080p' } }
            if not qobj:
                return None
            try:
                if isinstance(qobj, dict):
                    # nested under 'quality'
                    inner = qobj.get('quality')
                    if isinstance(inner, dict) and inner.get('name'):
                        return inner.get('name')
                    # direct name field
                    if qobj.get('name'):
                        return qobj.get('name')
                else:
                    # could be a string like '1080p BluRay'
                    return str(qobj)
            except Exception:
                return None
            return None

        # Try nested movieFile first (most authoritative)
        try:
            mfq = None
            if isinstance(moviefile, dict):
                mfq = moviefile.get('quality') or moviefile.get('qualityProfile')
            if not mfq and isinstance(entry.get('movieFile'), dict):
                mfq = entry.get('movieFile').get('quality') or entry.get('movieFile').get('qualityProfile')
            radarr_quality = _extract_name_from_quality_obj(mfq)
        except Exception:
            radarr_quality = None

        # Fallback to top-level entry quality/profile
        if not radarr_quality:
            q = entry.get('quality') or entry.get('qualityProfile')
            radarr_quality = _extract_name_from_quality_obj(q)
    except Exception:
        radarr_quality = None
    # End quality extraction

    release_status = entry.get('status') or entry.get('quality') and entry.get('quality').get('revision') if isinstance(entry.get('quality'), dict) else entry.get('status')

    # release dates (ISO strings) -> Date columns
    theater_release_date = None
    digital_release_date = None
    # Ensure this variable is always defined so later code can reference it safely
    physical_release_date = None
    try:
        from datetime import datetime as _dt
        rd = entry.get('releaseDate')
        if rd:
            rdnorm = rd.replace('Z', '+00:00') if isinstance(rd, str) else rd
            theater_release_date = _dt.fromisoformat(rdnorm).date()
        dd = entry.get('digitalRelease')
        if dd:
            ddnorm = dd.replace('Z', '+00:00') if isinstance(dd, str) else dd
            digital_release_date = _dt.fromisoformat(ddnorm).date()
        pd = entry.get('physicalRelease') or entry.get('physical_release')
        if pd:
            pdnorm = pd.replace('Z', '+00:00') if isinstance(pd, str) else pd
            physical_release_date = _dt.fromisoformat(pdnorm).date()
    except Exception:
        theater_release_date = None
        digital_release_date = None
        physical_release_date = None

    # try to find existing by tmdb then radarrid
    mv = None
    if tmdb:
        try:
            mv = session.query(Movie).filter(Movie.tmdbid == int(tmdb)).first()
        except Exception:
            mv = None
    if not mv and radarrid:
        try:
            mv = session.query(Movie).filter(Movie.radarrid == int(radarrid)).first()
        except Exception:
            mv = None

    # Create new if not found
    if not mv:
        mv = Movie(
            title=title or 'Unknown',
            year=year or 0,
            tmdbid=int(tmdb) if tmdb else None,
            radarrid=radarrid,
            imdbid=imdb,
            remote_poster=remote_poster,
            radarrpath=radarr_path,
            moviefile_path=moviefile_path,
            moviefile_size=moviefile_size,
            has_file=has_file,
            radarr_monitored=monitored,
            radarr_quality=radarr_quality,
            radarr_release_status=release_status,
            radarr_overview=entry.get('overview') or entry.get('plot') or entry.get('synopsis'),
            theater_release_date=theater_release_date,
            digital_release_date=digital_release_date,
            physical_release_date=physical_release_date,
        )
        session.add(mv)
        session.commit()
        return mv.id

    # Update fields when changed
    changed = False
    if title and mv.title != title:
        mv.title = title
        changed = True
    if year and getattr(mv, 'year', None) != year:
        try:
            mv.year = int(year)
            changed = True
        except Exception:
            pass
    if tmdb and getattr(mv, 'tmdbid', None) != (int(tmdb) if tmdb else None):
        try:
            mv.tmdbid = int(tmdb)
            changed = True
        except Exception:
            pass
    if radarrid and getattr(mv, 'radarrid', None) != radarrid:
        mv.radarrid = radarrid
        changed = True
    if imdb and getattr(mv, 'imdbid', None) != imdb:
        mv.imdbid = imdb
        changed = True
    if remote_poster and getattr(mv, 'remote_poster', None) != remote_poster:
        mv.remote_poster = remote_poster
        changed = True
    if radarr_path and getattr(mv, 'radarrpath', None) != radarr_path:
        mv.radarrpath = radarr_path
        changed = True
    if moviefile_path and getattr(mv, 'moviefile_path', None) != moviefile_path:
        mv.moviefile_path = moviefile_path
        changed = True
    if moviefile_size and getattr(mv, 'moviefile_size', None) != moviefile_size:
        mv.moviefile_size = moviefile_size
        changed = True
    if mv.has_file != has_file:
        mv.has_file = has_file
        changed = True
    if mv.radarr_monitored != monitored:
        mv.radarr_monitored = monitored
        changed = True
    if radarr_quality and getattr(mv, 'radarr_quality', None) != radarr_quality:
        mv.radarr_quality = radarr_quality
        changed = True
    if release_status and getattr(mv, 'radarr_release_status', None) != release_status:
        mv.radarr_release_status = release_status
        changed = True
    if theater_release_date and getattr(mv, 'theater_release_date', None) != theater_release_date:
        mv.theater_release_date = theater_release_date
        changed = True
    if digital_release_date and getattr(mv, 'digital_release_date', None) != digital_release_date:
        mv.digital_release_date = digital_release_date
        changed = True
    # overview
    overview = entry.get('overview') or entry.get('plot') or entry.get('synopsis')
    if overview and getattr(mv, 'radarr_overview', None) != overview:
        mv.radarr_overview = overview
        changed = True

    if changed:
        session.add(mv)
        session.commit()
    return mv.id


def capture_movies_fullsync_and_create_run(run_note: str = None) -> OrchestratorRun:
    entries = fetch_radarr_movies()
    session = get_session()
    try:
        movie_ids = []
        for e in entries:
            try:
                mid = upsert_movie_from_radarr_entry(session, e)
                if mid:
                    movie_ids.append(mid)
            except Exception:
                continue
    finally:
        session.close()

    # Create an orchestrator run record (logical) and also create SubFlow rows for each movie.
    run = OrchestratorRun(types=['movie'], note=run_note or f'fullsync:{datetime.now().isoformat()}')

    # Persist SubFlows for each movie. We'll create one SubFlow per movie with the
    # full set of phases and leave step_index at 0. Then create only the first-step
    # jobs (enrich_base) for each SubFlow. Jobs are created with run_after=FAR_FUTURE
    # so workers will not pick them up until you explicitly unlock them.
    session = get_session()
    try:
        subflow_ids = []
        for mid in movie_ids:
            # Use movie-specific phases which are intentionally shorter and do not
            # include list_capture. This keeps movie SubFlows focused on enrichment
            # and avoids creating an extra capturing phase per-item.
            sf = SubFlow(movie_id=mid, action='fullsync', branch='main', steps=','.join(MOVIE_PHASES), step_index=0, status='PENDING')
            session.add(sf)
        session.commit()

        # Collect the created SubFlow ids and create first-step jobs
        for sf in session.query(SubFlow).filter(SubFlow.movie_id.in_(movie_ids)).all():
            subflow_ids.append(sf.id)

        # Create one job per SubFlow for the first actionable step (enrich_base)
        grp = f"{run.run_id}:enrich_base"
        created = 0
        for sfid in subflow_ids:
            payload = {'run_id': run.run_id, 'phase': 'enrich_base', 'subflow_id': sfid, 'step_index': 0}
            j = Job(job_type='subjob:enrich_base', payload=payload, status='PENDING', run_after=FAR_FUTURE, group_id=grp)
            session.add(j)
            created += 1
        session.commit()

        # Fetch movie titles for nicer logging
        movie_rows = session.query(Movie.id, Movie.title, Movie.year).filter(Movie.id.in_(movie_ids)).all()
        movie_descriptions = [f"{r.title} ({r.year})" for r in movie_rows]
        logger.info(f"Movie fullsync run {run.run_id}: created {len(movie_ids)} movie subflows and {created} initial jobs")
        logger.verbose(f"Movies captured: {movie_descriptions}")
    finally:
        session.close()

    return run


def upsert_series_from_sonarr_entry(session, entry: dict):
    tvdb = entry.get('tvdbId') or entry.get('tvdb') or entry.get('tvdbId')
    title = entry.get('title')
    year = entry.get('year') or None
    sonarrid = entry.get('id')

    # Sonarr may provide imdb id and a remote poster at the series level
    imdb = entry.get('imdbId') or entry.get('imdb')
    remote_poster = entry.get('remotePoster')
    if not remote_poster:
        imgs = entry.get('images') or []
        for img in imgs:
            try:
                if img.get('coverType') and img.get('coverType').lower() == 'poster' and img.get('remoteUrl'):
                    remote_poster = img.get('remoteUrl')
                    break
                if not remote_poster and img.get('remoteUrl'):
                    remote_poster = img.get('remoteUrl')
            except Exception:
                continue

    s = None
    if tvdb:
        try:
            s = session.query(Series).filter(Series.tvdbid == int(tvdb)).first()
        except Exception:
            pass
    if not s and sonarrid:
        s = session.query(Series).filter(Series.sonarrid == int(sonarrid)).first()
    if not s:
        s = Series(
            title=title or 'Unknown',
            year=year or 0,
            tvdbid=int(tvdb) if tvdb else None,
            sonarrid=sonarrid,
            imdbid=imdb,
            remote_poster=remote_poster,
            sonarr_series_overview=entry.get('overview') or entry.get('description'),
            # Persist Sonarr series path if provided
            sonarrpath=entry.get('path') or entry.get('rootFolderPath'),
            # Persist whether Sonarr is monitoring this series and a representative quality label
            sonarr_monitored=bool(entry.get('monitored') or False),
            sonarr_quality=(lambda q: (q.get('quality') or q.get('name')) if isinstance(q, dict) else q)(entry.get('quality') or entry.get('qualityProfile') or None),
        )
        session.add(s)
        session.commit()
        # Persist any season summaries present on the entry (idempotent)
        try:
            seasons = entry.get('seasons') or []
            # Determine aggregate has_files at series level
            series_has_files = bool(entry.get('hasFile', False))
            for sd in seasons:
                try:
                    sn = sd.get('seasonNumber')
                    if sn is None:
                        continue
                    # find existing season row
                    season_row = session.query(Season).filter(Season.series_id == s.id, Season.season_number == int(sn)).first()
                    if not season_row:
                        season_row = Season(series_id=s.id, season_number=int(sn), title=f"Season {sn}", year=entry.get('year') or 0)
                        session.add(season_row)
                        session.flush()
                    # persist Sonarr season overview if present
                    so = sd.get('overview') or sd.get('description')
                    if so and getattr(season_row, 'sonarr_season_overview', None) != so:
                        season_row.sonarr_season_overview = so
                        session.add(season_row)
                    # persist per-season aggregates if provided
                    try:
                        stats = sd.get('statistics') or {}
                        if stats:
                            if getattr(season_row, 'seasonfile_count', None) != stats.get('episodeFileCount'):
                                season_row.seasonfile_count = stats.get('episodeFileCount')
                            if getattr(season_row, 'has_files', None) != bool(stats.get('episodeFileCount')):
                                season_row.has_files = bool(stats.get('episodeFileCount'))
                            # If any season has files, mark series_has_files True
                            if bool(stats.get('episodeFileCount')):
                                series_has_files = True
                            # Persist monitored status at season-level if Sonarr reports it
                            try:
                                if 'monitored' in sd:
                                    season_mon = bool(sd.get('monitored'))
                                    if getattr(season_row, 'sonarr_monitored', None) != season_mon:
                                        season_row.sonarr_monitored = season_mon
                            except Exception:
                                pass
                            # Persist Sonarr season id/status when present
                            try:
                                if sd.get('id') and getattr(season_row, 'sonarrid', None) != sd.get('id'):
                                    season_row.sonarrid = sd.get('id')
                                if sd.get('status') and getattr(season_row, 'sonarr_status', None) != sd.get('status'):
                                    season_row.sonarr_status = sd.get('status')
                            except Exception:
                                pass
                            # Persist season path when Sonarr provides it
                            try:
                                sp = sd.get('path')
                                # Prefer explicit season path from Sonarr; otherwise mirror the
                                # parent series sonarrpath for consistency.
                                sp_final = sp or s.sonarrpath
                                if sp_final and getattr(season_row, 'sonarrpath', None) != sp_final:
                                    season_row.sonarrpath = sp_final
                            except Exception:
                                pass
                            session.add(season_row)
                    except Exception:
                        pass
                except Exception:
                    continue
            # Persist aggregate to series row if computed
            try:
                if getattr(s, 'has_files', None) != series_has_files:
                    s.has_files = series_has_files
                    session.add(s)
            except Exception:
                pass
            session.commit()
            logger.verbose(f"Persisted seasons for series {s.title} (tvdb={s.tvdbid})", extra={'emoji_type': 'tv'})
        except Exception:
            # don't fail upsert if season persistence errors
            session.rollback()
        return s.id
    else:
        changed = False
        if title and s.title != title:
            s.title = title
            changed = True
        if year and getattr(s, 'year', None) != year:
            try:
                s.year = int(year)
                changed = True
            except Exception:
                pass
        if sonarrid and s.sonarrid != sonarrid:
            s.sonarrid = sonarrid
            changed = True
        # Persist series-level auxiliary fields
        if imdb and getattr(s, 'imdbid', None) != imdb:
            s.imdbid = imdb
            changed = True
        if remote_poster and getattr(s, 'remote_poster', None) != remote_poster:
            s.remote_poster = remote_poster
            changed = True
        # Persist Sonarr series path if present
        try:
            spath = entry.get('path') or entry.get('rootFolderPath')
            if spath and getattr(s, 'sonarrpath', None) != spath:
                s.sonarrpath = spath
                changed = True
        except Exception:
            pass
        # Persist monitored status and quality for series when available
        try:
            series_mon = bool(entry.get('monitored') or False)
            if getattr(s, 'sonarr_monitored', None) != series_mon:
                s.sonarr_monitored = series_mon
                changed = True
        except Exception:
            pass
        try:
            sq = entry.get('quality') or entry.get('qualityProfile') or None
            series_quality = (sq.get('quality') or sq.get('name')) if isinstance(sq, dict) else sq
            if series_quality and getattr(s, 'sonarr_quality', None) != series_quality:
                s.sonarr_quality = series_quality
                changed = True
        except Exception:
            pass
        # Persist series overview if present
        ov = entry.get('overview') or entry.get('description')
        if ov and getattr(s, 'sonarr_series_overview', None) != ov:
            s.sonarr_series_overview = ov
            changed = True
        if changed:
            session.add(s)
            session.commit()

        # Always process season summaries present on the Sonarr entry so per-season
        # monitored/status/path values are persisted even when the series record
        # itself didn't change. This ensures season-level metadata stays up-to-date.
        try:
            seasons = entry.get('seasons') or []
            # Determine aggregate has_files at series level
            series_has_files = bool(entry.get('hasFile', False))
            for sd in seasons:
                try:
                    sn = sd.get('seasonNumber')
                    if sn is None:
                        continue
                    season_row = session.query(Season).filter(Season.series_id == s.id, Season.season_number == int(sn)).first()
                    if not season_row:
                        season_row = Season(series_id=s.id, season_number=int(sn), title=f"Season {sn}", year=entry.get('year') or 0)
                        session.add(season_row)
                        session.flush()
                    so = sd.get('overview') or sd.get('description')
                    if so and getattr(season_row, 'sonarr_season_overview', None) != so:
                        season_row.sonarr_season_overview = so
                        session.add(season_row)
                    # persist per-season aggregates if provided
                    try:
                        stats = sd.get('statistics') or {}
                        if stats:
                            if getattr(season_row, 'seasonfile_count', None) != stats.get('episodeFileCount'):
                                season_row.seasonfile_count = stats.get('episodeFileCount')
                            if getattr(season_row, 'has_files', None) != bool(stats.get('episodeFileCount')):
                                season_row.has_files = bool(stats.get('episodeFileCount'))
                            # If any season has files, mark series_has_files True
                            if bool(stats.get('episodeFileCount')):
                                series_has_files = True
                            # Persist monitored status at season-level if Sonarr reports it
                            try:
                                if 'monitored' in sd:
                                    season_mon = bool(sd.get('monitored'))
                                    if getattr(season_row, 'sonarr_monitored', None) != season_mon:
                                        season_row.sonarr_monitored = season_mon
                            except Exception:
                                pass
                            # Persist Sonarr season id/status when present
                            try:
                                if sd.get('id') and getattr(season_row, 'sonarrid', None) != sd.get('id'):
                                    season_row.sonarrid = sd.get('id')
                                if sd.get('status') and getattr(season_row, 'sonarr_status', None) != sd.get('status'):
                                    season_row.sonarr_status = sd.get('status')
                            except Exception:
                                pass
                            # Persist season path when Sonarr provides it, else mirror series path
                            try:
                                sp = sd.get('path')
                                sp_final = sp or s.sonarrpath
                                if sp_final and getattr(season_row, 'sonarrpath', None) != sp_final:
                                    season_row.sonarrpath = sp_final
                            except Exception:
                                pass
                            session.add(season_row)
                    except Exception:
                        pass
                except Exception:
                    continue
            # Persist aggregate to series row if computed/changed
            try:
                if getattr(s, 'has_files', None) != series_has_files:
                    s.has_files = series_has_files
                    session.add(s)
            except Exception:
                pass
            session.commit()
            logger.verbose(f"Updated seasons for series {s.title} (tvdb={s.tvdbid})", extra={'emoji_type': 'tv'})
        except Exception:
            session.rollback()
        return s.id


def capture_series_fullsync_and_create_run(run_note: str = None) -> OrchestratorRun:
    entries = fetch_sonarr_series()
    session = get_session()
    try:
        series_ids = []
        for e in entries:
            try:
                sid = upsert_series_from_sonarr_entry(session, e)
                if sid:
                    series_ids.append(sid)
            except Exception:
                continue
    finally:
        session.close()

    run = OrchestratorRun(types=['tv'], note=run_note or f'fullsync_tv:{datetime.now().isoformat()}')

    # Create SubFlows for each series and only enqueue the enrich_base job (FAR_FUTURE)
    session = get_session()
    try:
        for sid in series_ids:
            # Use lightweight series phases so the series step can create episode subflows
            sf = SubFlow(series_id=sid, action='fullsync', branch='main', steps=','.join(SERIES_PHASES), step_index=0, status='PENDING')
            session.add(sf)
        session.commit()

        # For series we create only the series-level subflow and DO NOT create
        # episode subflows here. Episode subflow creation is performed later by
        # the series-step 'create_episode_subflows' to allow batching, dedupe and
        # gating of specials.
        grp = f"{run.run_id}:enrich_base"
        created = 0
        # create jobs for newly created series subflows (first step)
        for sf in session.query(SubFlow).filter(SubFlow.series_id.in_(series_ids)).all():
            payload = {'run_id': run.run_id, 'phase': 'enrich_base', 'subflow_id': sf.id, 'step_index': 0}
            j = Job(job_type='subjob:enrich_base', payload=payload, status='PENDING', run_after=FAR_FUTURE, group_id=grp)
            session.add(j)
            created += 1
        session.commit()

        # Fetch series titles for nicer logging
        series_rows = session.query(Series.id, Series.title, Series.year).filter(Series.id.in_(series_ids)).all()
        series_descriptions = [f"{r.title} ({r.year})" for r in series_rows]
        logger.info(f"TV fullsync run {run.run_id}: created {len(series_ids)} series subflows and {created} initial jobs")
        logger.verbose(f"Series captured: {series_descriptions}")
    finally:
        session.close()

    return run


def create_episode_subflows_for_series(series_id: int, run_id: str, include_specials: bool = None, batch_size: int = EPISODE_BATCH_SIZE):
    """Idempotently upsert episodes for a series and create Episode SubFlows + initial jobs in batches.

    - include_specials: when False, excludes seasonNumber == 0 episodes.
    - batch_size: number of episodes to create per DB transaction.
    """
    # Resolve include_specials default from settings
    if include_specials is None:
        include_specials = bool(settings.INCLUDE_SPECIALS)

    logger.info(f"Creating episode subflows for series {series_id} (include_specials={include_specials})")
    # Fetch episodes from Sonarr
    entries = fetch_sonarr_episodes(series_id)
    if not entries:
        logger.info(f"No episodes returned for series {series_id}")
        return 0

    # Normalize entries and filter specials if needed
    filtered = []
    for e in entries:
        season = e.get('seasonNumber')
        if season == 0 and not include_specials:
            continue
        filtered.append(e)

    # Upsert seasons & episodes and create subflows in batches
    created_subflows = 0
    # Helper to upsert and create for a batch
    def _process_batch(batch):
        nonlocal created_subflows
        session = get_session()
        try:
            # Upsert seasons and episodes
            episode_ids = []
            for ent in batch:
                s_num = ent.get('seasonNumber') or 0
                # Upsert season
                season_row = session.query(Season).filter(Season.series_id == series_id, Season.season_number == s_num).first()
                if not season_row:
                    season_row = Season(series_id=series_id, season_number=s_num, title=f"Season {s_num}", year=ent.get('airDateUtc', None) or 0)
                    session.add(season_row)
                    session.flush()  # get id

                # Upsert episode
                ep_num = ent.get('episodeNumber')
                ep = session.query(Episode).filter(Episode.season_id == season_row.id, Episode.episode_number == ep_num).first()
                if not ep:
                    ep = Episode(season_id=season_row.id, episode_number=ep_num, title=ent.get('title') or f"Episode {ep_num}", year=ent.get('year') or 0, sonarrid=ent.get('id'))
                    session.add(ep)
                    session.flush()

                episode_ids.append(ep.id)

            # Create subflows for the episodes that don't already have one
            existing = session.query(SubFlow.episode_id).filter(SubFlow.episode_id.in_(episode_ids)).all()
            existing_ids = {r[0] for r in existing}
            grp = f"{run_id}:enrich_base"
            for eid in episode_ids:
                if eid in existing_ids:
                    logger.verbose(f"Skipping existing episode subflow for episode id {eid}")
                    continue
                sf = SubFlow(episode_id=eid, action='fullsync', branch='main', steps=','.join(PHASES), step_index=0, status='PENDING')
                session.add(sf)
                session.flush()
                payload = {'run_id': run_id, 'phase': 'enrich_base', 'subflow_id': sf.id, 'step_index': 0}
                j = Job(job_type='subjob:enrich_base', payload=payload, status='PENDING', run_after=FAR_FUTURE, group_id=grp)
                session.add(j)
                created_subflows += 1

            session.commit()
        finally:
            session.close()

    # Process in batches
    batch = []
    for ent in filtered:
        batch.append(ent)
        if len(batch) >= batch_size:
            _process_batch(batch)
            batch = []
    if batch:
        _process_batch(batch)

    logger.info(f"Created {created_subflows} episode subflows for series {series_id}")
    return created_subflows


def create_episode_subflows_for_season(series_id: int, season_number: int, run_id: str, include_specials: bool = None, batch_size: int = EPISODE_BATCH_SIZE):
    """Create episode subflows for a specific season (identified by series_id + season_number).

    This fetches episodes from Sonarr for the series and filters to the requested season.
    """
    if include_specials is None:
        include_specials = bool(settings.INCLUDE_SPECIALS)

    logger.info(f"Creating episode subflows for series {series_id} season {season_number} (include_specials={include_specials})")
    entries = fetch_sonarr_episodes(series_id)
    if not entries:
        logger.info(f"No episodes returned for series {series_id}")
        return 0

    # Filter to the selected season
    filtered = []
    for e in entries:
        s_num = e.get('seasonNumber')
        if s_num != season_number:
            continue
        if s_num == 0 and not include_specials:
            continue
        filtered.append(e)

    # Reuse batch processing by calling the same internal _process_batch logic via slicing
    created = 0
    # We'll reuse the logic from create_episode_subflows_for_series by processing batches manually
    batch = []
    for ent in filtered:
        batch.append(ent)
        if len(batch) >= batch_size:
            created += create_episode_subflows_for_series_batch_helper(batch, series_id, run_id)
            batch = []
    if batch:
        created += create_episode_subflows_for_series_batch_helper(batch, series_id, run_id)

    logger.info(f"Created {created} episode subflows for series {series_id} season {season_number}")
    return created


def create_episode_subflow_for_episode(episode_id: int, run_id: str):
    """Create a single episode-level SubFlow + initial job for an existing Episode.id (DB id).
    Idempotent: skips if a SubFlow for that episode already exists.
    """
    session = get_session()
    try:
        ep = session.query(Episode).filter(Episode.id == episode_id).first()
        if not ep:
            logger.info(f"Episode id {episode_id} not found in DB")
            return 0

        existing = session.query(SubFlow).filter(SubFlow.episode_id == episode_id).first()
        if existing:
            logger.verbose(f"Episode subflow already exists for {ep.title} S{ep.season_number}E{ep.episode_number}")
            return 0

        sf = SubFlow(episode_id=episode_id, action='fullsync', branch='main', steps=','.join(PHASES), step_index=0, status='PENDING')
        session.add(sf)
        session.flush()
        grp = f"{run_id}:enrich_base"
        payload = {'run_id': run_id, 'phase': 'enrich_base', 'subflow_id': sf.id, 'step_index': 0}
        j = Job(job_type='subjob:enrich_base', payload=payload, status='PENDING', run_after=FAR_FUTURE, group_id=grp)
        session.add(j)
        session.commit()
        logger.info(f"Created episode subflow for {ep.title} S{ep.season_number}E{ep.episode_number}")
        return 1
    finally:
        session.close()


def create_episode_subflows_from_entry(entry: dict, run_id: str, include_specials: bool = None):
    """Flexible entry handler: entry may represent a series, season, or episode.

    Recognised shapes (Sonarr-like):
    - Series-level entry: contains 'seriesId' or 'id' for series
    - Season-level entry: contains 'seriesId' and 'seasonNumber'
    - Episode-level entry: contains 'seriesId', 'seasonNumber', 'episodeNumber' and 'id' (episode id)

    It tries to detect what level the entry is and dispatches accordingly.
    """
    if include_specials is None:
        include_specials = bool(settings.INCLUDE_SPECIALS)

    # Common Sonarr names: 'seriesId' or nested 'series' fields. Try several keys.
    series_id = entry.get('seriesId') or entry.get('series') and entry.get('series').get('id') if isinstance(entry.get('series'), dict) else None
    season_num = entry.get('seasonNumber')
    ep_num = entry.get('episodeNumber')
    ep_id = entry.get('id')

    # If we have episode-level info from Sonarr, upsert that single episode
    if series_id and season_num is not None and ep_num is not None:
        # Build a minimal episode entry with the fields expected by the upsert helper
        logger.verbose(f"Entry appears to be episode-level: series={series_id} season={season_num} episode={ep_num}")
        # Reuse the single-episode upsert by calling Sonarr fetch for the series and searching for this episode id
        entries = fetch_sonarr_episodes(series_id)
        target = None
        for e in entries or []:
            if e.get('seasonNumber') == season_num and e.get('episodeNumber') == ep_num:
                target = e
                break
        if not target:
            logger.info(f"Could not find episode {ep_num} S{season_num} for series {series_id} from Sonarr")
            return 0
        # Process as a single-episode batch
        return create_episode_subflows_for_series_batch_helper([target], series_id, run_id)

    # Season-level request
    if series_id and season_num is not None:
        return create_episode_subflows_for_season(series_id, season_num, run_id, include_specials=include_specials)

    # Series-level request
    if series_id:
        return create_episode_subflows_for_series(series_id, run_id, include_specials=include_specials)

    logger.info('Entry did not contain recognizable series/season/episode information')
    return 0


# Helper extracted from the batch logic in create_episode_subflows_for_series to allow reuse
def create_episode_subflows_for_series_batch_helper(batch_entries: list, series_id: int, run_id: str):
    """Process a small batch of Sonarr episode entries and create episode rows/subflows/jobs.
    Returns created count.
    """
    created_subflows = 0
    session = get_session()
    try:
        episode_ids = []
        for ent in batch_entries:
            s_num = ent.get('seasonNumber') or 0
            # Upsert season
            season_row = session.query(Season).filter(Season.series_id == series_id, Season.season_number == s_num).first()
            if not season_row:
                season_row = Season(series_id=series_id, season_number=s_num, title=f"Season {s_num}", year=ent.get('airDateUtc', None) or 0)
                session.add(season_row)
                session.flush()

            # Upsert episode
            ep_num = ent.get('episodeNumber')
            ep = session.query(Episode).filter(Episode.season_id == season_row.id, Episode.episode_number == ep_num).first()
            # Extract episode-level fields
            ep_title = ent.get('title') or f"Episode {ep_num}"
            ep_year = ent.get('year') or 0
            ep_sonarrid = ent.get('id')
            # episodeFile payload (if Sonarr has a file)
            ep_file = ent.get('episodeFile') or {}
            ep_file_path = ep_file.get('path') if isinstance(ep_file, dict) else None
            ep_file_size = None
            try:
                ep_file_size = ep_file.get('size') or ep_file.get('sizeOnDisk')
            except Exception:
                ep_file_size = None
            ep_overview = ent.get('overview') or ent.get('description') or None
            ep_has_file = bool(ent.get('hasFile') or (ep_file_path is not None))
            ep_quality = None
            try:
                q = ep_file.get('quality') if isinstance(ep_file, dict) else ent.get('quality')
                if isinstance(q, dict):
                    ep_quality = q.get('quality') or q.get('name')
                else:
                    ep_quality = q
            except Exception:
                ep_quality = None
            ep_status = ent.get('status') or None
            ep_monitored = bool(ent.get('monitored') or False)
            ep_air = None
            try:
                ad = ent.get('airDate') or ent.get('airDateUtc')
                if ad:
                    from datetime import datetime as _dt
                    adn = ad.replace('Z', '+00:00') if isinstance(ad, str) else ad
                    ep_air = _dt.fromisoformat(adn).date()
            except Exception:
                ep_air = None

            if not ep:
                ep = Episode(
                    season_id=season_row.id,
                    episode_number=ep_num,
                    title=ep_title,
                    year=ep_year,
                    sonarrid=ep_sonarrid,
                    episodefile_path=ep_file_path,
                    episodefile_size=ep_file_size,
                    sonarr_episode_overview=ep_overview,
                    has_file=ep_has_file,
                    sonarr_quality=ep_quality,
                    sonarr_status=ep_status,
                    sonarr_monitored=ep_monitored,
                    air_date=ep_air,
                )
                session.add(ep)
                session.flush()
            else:
                changed_ep = False
                if ep.title != ep_title:
                    ep.title = ep_title
                    changed_ep = True
                if getattr(ep, 'year', None) != ep_year:
                    try:
                        ep.year = int(ep_year)
                        changed_ep = True
                    except Exception:
                        pass
                if ep.sonarrid != ep_sonarrid:
                    ep.sonarrid = ep_sonarrid
                    changed_ep = True
                if ep.episodefile_path != ep_file_path:
                    ep.episodefile_path = ep_file_path
                    changed_ep = True
                if ep.episodefile_size != ep_file_size:
                    ep.episodefile_size = ep_file_size
                    changed_ep = True
                if ep.sonarr_episode_overview != ep_overview:
                    ep.sonarr_episode_overview = ep_overview
                    changed_ep = True
                if ep.has_file != ep_has_file:
                    ep.has_file = ep_has_file
                    changed_ep = True
                if ep.sonarr_quality != ep_quality:
                    ep.sonarr_quality = ep_quality
                    changed_ep = True
                if ep.sonarr_status != ep_status:
                    ep.sonarr_status = ep_status
                    changed_ep = True
                if ep.sonarr_monitored != ep_monitored:
                    ep.sonarr_monitored = ep_monitored
                    changed_ep = True
                if ep.air_date != ep_air:
                    ep.air_date = ep_air
                    changed_ep = True
                if changed_ep:
                    session.add(ep)

            episode_ids.append(ep.id)

        # Create subflows for the episodes that don't already have one
        existing = session.query(SubFlow.episode_id).filter(SubFlow.episode_id.in_(episode_ids)).all()
        existing_ids = {r[0] for r in existing}
        grp = f"{run_id}:enrich_base"
        for eid in episode_ids:
            if eid in existing_ids:
                logger.verbose(f"Skipping existing episode subflow for episode id {eid}")
                continue
            sf = SubFlow(episode_id=eid, action='fullsync', branch='main', steps=','.join(PHASES), step_index=0, status='PENDING')
            session.add(sf)
            session.flush()
            payload = {'run_id': run_id, 'phase': 'enrich_base', 'subflow_id': sf.id, 'step_index': 0}
            j = Job(job_type='subjob:enrich_base', payload=payload, status='PENDING', run_after=FAR_FUTURE, group_id=grp)
            session.add(j)
            created_subflows += 1

        session.commit()
    finally:
        session.close()

    return created_subflows
