from datetime import datetime
import logging
import os
from typing import Type, List
from sqlalchemy.orm import Session
from core.config import settings
from services.postgres.models import Movie, Episode, SubFlow
from services.integrations import (
    trigger_radarr_search,
    trigger_sonarr_search,
    api_monitor_episodes,
    mark_movie_monitored
)

logger = logging.getLogger("playback_flow")
logger.setLevel(logging.INFO)


def identify_source(session: Session, ent_id: int, model: Type) -> bool:
    rec = session.query(model).get(ent_id)
    path = getattr(rec, 'dummypath', None)
    if not path or not os.path.exists(path):
        logger.error(f"[{model.__name__}] placeholder missing for id {ent_id}")
        return False
    return True

def db_get_episodes(session: Session, series_id: int, season_num: int, ep_num: int, lookahead: int) -> List[Episode]:
    """
    Fetch next `lookahead` episodes from DB after given season and episode.
    """
    # Query episodes in same season >= current ep
    eps = (
        session.query(Episode)
            .join(Episode.season)
            .filter(Episode.season.has(series_id=series_id))
            .order_by(Episode.season.has(season_num), Episode.episode_number)
            .all()
    )
    # flatten in order by season,ep
    ordered = sorted(eps, key=lambda e: (e.season.season_number, e.episode_number))
    # find index of current
    idx = next((i for i,e in enumerate(ordered) if e.season.season_number==season_num and e.episode_number==ep_num), None)
    if idx is None:
        return []
    return ordered[idx:idx+lookahead]


def lookup_and_monitor(session: Session,
                       ent_id: int,
                       model: Type) -> bool:
    if model is Movie:
        m = session.query(Movie).get(ent_id)
        success = mark_movie_monitored(
            m.radarrid,
            is_4k=m.is_4k
        )
        return bool(success)

    # Episode flow
    ep: Episode = session.query(Episode).get(ent_id)
    series_id = ep.season.series_id
    play_mode = settings.TV_PLAY_MODE.lower()
    ep_ids: List[int] = []

    if play_mode == 'episode':
        eps = db_get_episodes(
            session,
            series_id,
            ep.season.season_number,
            ep.episode_number,
            lookahead=settings.EPISODES_LOOKAHEAD
        )
        ep_ids = [e.sonarrid for e in eps if e.sonarrid]

    elif play_mode == 'season':
        S = ep.season.season_number
        eps = (session.query(Episode)
                   .filter_by(season_id=ep.season_id)
                   .order_by(Episode.episode_number)
                   .all())
        ep_ids = [e.sonarrid for e in eps if e.sonarrid]
        # if last ep, include next season
        if ep.episode_number == eps[-1].episode_number:
            next_eps = (
                session.query(Episode)
                    .join(Episode.season)
                    .filter(
                        Episode.season.has(series_id=series_id, season_number=S+1)
                    )
                    .all()
            )
            ep_ids += [e.sonarrid for e in next_eps if e.sonarrid]

    elif play_mode == 'series':
        all_eps = (
            session.query(Episode)
                .join(Episode.season)
                .filter(Episode.season.has(series_id=series_id))
                .all()
        )
        ep_ids = [e.sonarrid for e in all_eps if e.sonarrid]

    else:
        logger.warning(f"Unknown TV_PLAY_MODE '{play_mode}'")
        ep_ids = [ep.sonarrid] if ep.sonarrid else []

    if not ep_ids:
        return False

    series = ep.season.series
    sonarr_id = series.sonarrid
    # call actual API to mark monitored
    api_monitor_episodes(sonarr_id, ep_ids, is_4k=series.is_4k)

    # stash context in DB
    sf: SubFlow = (session.query(SubFlow)
                      .filter_by(episode_id=ent_id, status='QUEUED')
                      .first())
    sf.context = ','.join(map(str, ep_ids))
    session.add(sf)
    return True

def trigger_search(session: Session, ent_id: int, model: Type) -> bool:
    if model is Movie:
        m = session.query(Movie).get(ent_id)
        success = trigger_radarr_search(m.radarrid, m.title)
        return bool(success)

    sf: SubFlow = session.query(SubFlow).get(ent_id)
    ep_ids = [int(x) for x in sf.context.split(',') if x]
    if not ep_ids:
        return False

    # new signature: series_id, season_number=None, episode_ids, series_title, is_4k
    series_id = sf.movie_id or sf.episode_id
    series = session.query(Episode).get(sf.episode_id).season.series
    return trigger_sonarr_search(
        series_id.sonarrid,
        episode_ids=ep_ids,
        series_title=None,
        is_4k=series.is_4k
    )

def mark_done(session: Session, ent_id: int, model: Type) -> bool:
    rec = session.query(model).get(ent_id)
    rec.status = 'DONE'
    session.add(rec)
    return True

def enqueue_monitor(session: Session, ent_id: int, model: Type) -> bool:
    """
    Create a SubFlow with status IN_QUEUE and action playback
    
    Args:
        session: Database session
        ent_id: Entity ID (Movie or Episode ID)
        model: Model type (Movie or Episode class)
        
    Returns:
        SubFlow: Created SubFlow instance
    """
    try:
        # Create SubFlow based on model type
        if model == Movie:
            existing = session.query(SubFlow).filter_by(
            movie_id=ent_id,
            episode_id=None,
            action='playback'
            ).first()
            
            if existing:
                logger.info(f"SubFlow for Movie ID {ent_id} already exists", extra={'emoji_type': 'info'})
                return True

            subflow = SubFlow(
            movie_id=ent_id,
            episode_id=None,
            status='IN_QUEUE',
            action='playback'
            )
            logger.info(f"Created SubFlow for Movie ID {ent_id}", extra={'emoji_type': 'queue'})
            
        elif model == Episode:
            existing = session.query(SubFlow).filter_by(
            movie_id=None,
            episode_id=ent_id,
            action='playback'
            ).first()
            
            if existing:
                logger.info(f"SubFlow for Episode ID {ent_id} already exists", extra={'emoji_type': 'info'})
                return True
            
            subflow = SubFlow(
            movie_id=None,
            episode_id=ent_id,
            status='IN_QUEUE', 
            action='playback'
            )
            logger.info(f"Created SubFlow for Episode ID {ent_id}", extra={'emoji_type': 'queue'})
            
        else:
            return False
        
        # Add to session and commit
        session.add(subflow)
        session.commit()
        
        from services.queue_monitor import trigger_monitoring
        trigger_monitoring()
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to enqueue {model.__name__} ID {ent_id}: {e}", extra={'emoji_type': 'error'})
        session.rollback()
        return False


def steps():
    return [
        identify_source,
        lookup_and_monitor,
        trigger_search,
        mark_done,
        enqueue_monitor
    ]
