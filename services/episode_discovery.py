from typing import List
from services.arr_clients import fetch_sonarr_episodes
from services.postgres.db import get_session
from services.postgres.models import Season, Episode, SubFlow, Job
from services.orchestrator import PHASES
from core.config import settings
from core.logger import logger


def discover_and_create_episode_subflows(series_id: int, run_id: str, include_specials: bool = None, batch_size: int = 200, run_jobs_immediately: bool = False) -> int:
    """Fetch episodes for a series from Sonarr, upsert seasons & episodes in batches,
    and create Episode-level SubFlows + initial jobs for episodes that don't already
    have an active SubFlow.

    Returns the number of episode subflows created.
    """
    if include_specials is None:
        include_specials = bool(settings.INCLUDE_SPECIALS)

    logger.info(f"Discovering episodes for series {series_id} (include_specials={include_specials})")
    entries = fetch_sonarr_episodes(series_id)
    if not entries:
        logger.info(f"No episodes returned for series {series_id}")
        return 0

    # Fetch series-level info once (for series path and monitored defaults)
    try:
        from services.arr_clients import fetch_sonarr_series_by_id
        series_info = fetch_sonarr_series_by_id(series_id)
    except Exception:
        series_info = None

    # Filter specials if configured
    filtered = [e for e in entries if not (e.get('seasonNumber') == 0 and not include_specials)]
    if not filtered:
        return 0

    created_subflows = 0

    # Process in batches to avoid huge transactions
    def _process_batch(batch: List[dict]):
        nonlocal created_subflows
        session = get_session()
        try:
            # Upsert seasons
            season_numbers = sorted({e.get('seasonNumber') or 0 for e in batch})
            season_map = {}  # season_number -> season_id
            if season_numbers:
                existing_seasons = session.query(Season).filter(Season.series_id == series_id, Season.season_number.in_(season_numbers)).all()
                for s in existing_seasons:
                    season_map[s.season_number] = s.id
                # Insert missing seasons
                for sn in season_numbers:
                    if sn not in season_map:
                        new_s = Season(series_id=series_id, season_number=sn, title=f"Season {sn}", year=0)
                        session.add(new_s)
                        session.flush()
                        season_map[sn] = new_s.id

            # Upsert episodes: gather existing episodes by (season_id, episode_number)
            episode_keys = []  # tuples (season_id, episode_number)
            for ent in batch:
                s_num = ent.get('seasonNumber') or 0
                season_id = season_map.get(s_num)
                if season_id is None:
                    continue
                ep_num = ent.get('episodeNumber')
                if ep_num is None:
                    continue
                episode_keys.append((season_id, int(ep_num)))

            existing_eps = []
            if episode_keys:
                # Build conditions to fetch existing episodes
                conds = []
                # fetch by season_id in season_map values and episode_number in set
                season_ids = list(set([k[0] for k in episode_keys]))
                ep_nums = list(set([k[1] for k in episode_keys]))
                existing_eps = session.query(Episode).filter(Episode.season_id.in_(season_ids), Episode.episode_number.in_(ep_nums)).all()

            existing_map = {(ep.season_id, ep.episode_number): ep for ep in existing_eps}

            episode_ids = []
            for ent in batch:
                s_num = ent.get('seasonNumber') or 0
                season_id = season_map.get(s_num)
                if season_id is None:
                    continue
                ep_num = ent.get('episodeNumber')
                if ep_num is None:
                    continue
                key = (season_id, int(ep_num))
                ep_file = ent.get('episodeFile') or {}
                # If episodeFile is not present but Sonarr provided episodeFileId, fetch it
                if (not ep_file or not isinstance(ep_file, dict)) and ent.get('episodeFileId'):
                    try:
                        from services.arr_clients import fetch_sonarr_episodefile
                        ef = fetch_sonarr_episodefile(ent.get('episodeFileId'))
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

                ep_title = ent.get('title') or f"Episode {ep_num}"
                ep_year = ent.get('year') or 0
                # monitored flag and air_date
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
                ep_sonarrid = ent.get('id')
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

                existing = existing_map.get(key)
                sonarr_path = None
                try:
                    # try series_info.paths or path-like fields if present
                    if series_info and isinstance(series_info, dict):
                        sonarr_path = series_info.get('path') or series_info.get('rootFolderPath')
                except Exception:
                    sonarr_path = None

                if not existing:
                    new_ep = Episode(
                        season_id=season_id,
                        episode_number=int(ep_num),
                        title=ep_title,
                        year=ep_year,
                        episodefile_path=ep_file_path,
                        episodefile_size=ep_file_size,
                        sonarr_episode_overview=ep_overview,
                        has_file=ep_has_file,
                        sonarr_quality=ep_quality,
                        sonarrid=ep_sonarrid,
                        sonarrpath=sonarr_path,
                        sonarr_monitored=ep_monitored,
                        air_date=ep_air,
                    )
                    session.add(new_ep)
                    session.flush()
                    episode_ids.append(new_ep.id)
                else:
                    # update changed fields
                    changed = False
                    if existing.title != ep_title:
                        existing.title = ep_title
                        changed = True
                    if getattr(existing, 'year', None) != ep_year:
                        try:
                            existing.year = int(ep_year)
                            changed = True
                        except Exception:
                            pass
                    if existing.episodefile_path != ep_file_path:
                        existing.episodefile_path = ep_file_path
                        changed = True
                    if existing.episodefile_size != ep_file_size:
                        existing.episodefile_size = ep_file_size
                        changed = True
                    if existing.sonarr_episode_overview != ep_overview:
                        existing.sonarr_episode_overview = ep_overview
                        changed = True
                    if existing.has_file != ep_has_file:
                        existing.has_file = ep_has_file
                        changed = True
                    if existing.sonarr_quality != ep_quality:
                        existing.sonarr_quality = ep_quality
                        changed = True
                    if existing.sonarrid != ep_sonarrid and ep_sonarrid is not None:
                        existing.sonarrid = ep_sonarrid
                        changed = True
                    if changed:
                        session.add(existing)
                    episode_ids.append(existing.id)

            # Create subflows for episodes that don't already have one (PENDING/CLAIMED)
            if episode_ids:
                existing_subflows = session.query(SubFlow.episode_id).filter(SubFlow.episode_id.in_(episode_ids), SubFlow.status.in_(['PENDING', 'CLAIMED'])).all()
                existing_sf_ids = {r[0] for r in existing_subflows}
                grp = f"{run_id}:enrich_base"
                for eid in episode_ids:
                    if eid in existing_sf_ids:
                        continue
                    sf = SubFlow(episode_id=eid, action='fullsync', branch='main', steps=','.join(PHASES), step_index=0, status='PENDING')
                    session.add(sf)
                    session.flush()
                    payload = {'run_id': run_id, 'phase': 'enrich_base', 'subflow_id': sf.id, 'step_index': 0}
                    # For live testing make jobs runnable immediately by default
                    from datetime import datetime as _dt
                    run_after = _dt.utcnow()
                    from services.jobs import insert_job_with_session
                    group_id = f"subflow:{sf.id}:enrich_base"
                    insert_job_with_session(session, 'subjob:enrich_base', payload, group_id=group_id)
                    created_subflows += 1

            session.commit()
        finally:
            session.close()

    # Batch and process
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
