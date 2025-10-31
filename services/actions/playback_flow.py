from datetime import datetime
import logging
import os
from typing import Type, List
from sqlalchemy.orm import Session
from core.config import settings
from services.postgres.models import Movie, Episode, Season, SubFlow
from services.integrations import (
    trigger_radarr_search,
    trigger_sonarr_search,
    api_monitor_episodes,
    mark_movie_monitored
)
from core.logger import logger

# logger = logging.getLogger("playback_flow")
# logger.setLevel(logging.INFO)


def identify_source(session: Session, ent_id: int, model: Type, action: str = None) -> bool:
    """Check that the placeholder file exists for the given record.

    The scheduler invokes this function with (session, ent_id, model, action).
    Previously the function accepted only three args which caused a TypeError
    when scheduler passed the extra action parameter. This keeps behavior the
    same but accepts the extra parameter and logs more info for debugging.
    """
    logger.info(f"identify_source called for {model.__name__} id={ent_id} action={action}")
    rec = session.query(model).get(ent_id)
    path = getattr(rec, 'dummypath', None)
    logger.debug(f"Placeholder path from DB: {path}")
    if not path:
        logger.error(f"[{model.__name__}] no placeholder path recorded for id {ent_id}")
        return False

    if not os.path.exists(path):
        logger.error(f"[{model.__name__}] placeholder missing on disk for id {ent_id}: {path}")
        return False

    logger.info(f"[{model.__name__}] placeholder present for id {ent_id}: {path}")
    return True

def db_get_episodes(session: Session, series_id: int, season_num: int, ep_num: int, lookahead: int) -> List[Episode]:
    """
    Fetch next `lookahead` episodes from DB after given season and episode.
    """
    # Query episodes in same season >= current ep
    eps = (
        session.query(Episode)
            .join(Season)
            .filter(Season.series_id == series_id)
            .order_by(Season.season_number, Episode.episode_number)
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
                       model: Type,
                       action: str = None) -> bool:
    logger.info(f"Starting lookup_and_monitor for {model.__name__} {ent_id}, action: {action}")
    if model is Movie:
        m = session.query(Movie).get(ent_id)
        logger.debug(f"Processing movie: {m.title} (ID: {m.id})")
        success = mark_movie_monitored(
            m.radarrid,
            is_4k=m.is_4k
        )
        logger.info(f"Movie monitoring result for {m.title}: {success}")
        return bool(success)

    # Episode flow
    ep: Episode = session.query(Episode).get(ent_id)
    logger.debug(f"Processing episode: S{ep.season.season_number}E{ep.episode_number} (ID: {ep.id})")
    series_id = ep.season.series_id
    play_mode = settings.TV_PLAY_MODE.lower()
    logger.debug(f"Play mode: {play_mode}, series ID: {series_id}")
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
        logger.debug(f"Episode mode: Found {len(ep_ids)} episodes to monitor")

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
                    .join(Season)
                    .filter(
                        Season.series_id == series_id,
                        Season.season_number == S+1
                    )
                    .all()
            )
            ep_ids += [e.sonarrid for e in next_eps if e.sonarrid]
            eps.extend(next_eps)
        logger.debug(f"Season mode: Found {len(ep_ids)} episodes to monitor")

    elif play_mode == 'series':
        eps = (
            session.query(Episode)
                .join(Episode.season)
                .filter(Season.series_id == series_id)
                .all()
        )
        ep_ids = [e.sonarrid for e in eps if e.sonarrid]
        logger.debug(f"Series mode: Found {len(ep_ids)} episodes to monitor")

    else:
        logger.warning(f"Unknown TV_PLAY_MODE '{play_mode}'")
        ep_ids = [ep.sonarrid] if ep.sonarrid else []

    if not ep_ids:
        logger.warning(f"No episode IDs found for {model.__name__} {ent_id}")
        return False

    series = ep.season.series
    sonarr_id = series.sonarrid
    logger.debug(f"Calling api_monitor_episodes for series {series.title} (Sonarr ID: {sonarr_id}), episodes: {ep_ids}")
    # call actual API to mark monitored
    api_monitor_episodes(sonarr_id, ep_ids, is_4k=series.is_4k)

    for e in eps:
        e.placeholder_status = "Search"
        session.add(e)
    logger.info(f"Updated placeholder_status to 'Search' for {len(eps)} episodes in series {series.title}")
    logger.info(f"Completed lookup_and_monitor for {model.__name__} {ent_id}")
    return True


def trigger_search(session: Session, ent_id: int, model: Type, action: str = None) -> bool:
    if model is Movie:
        m = session.query(Movie).get(ent_id)
        success = trigger_radarr_search(m.radarrid, m.title)
        return bool(success)

    series = session.query(Episode).get(ent_id).season.series
    eps = session.query(Episode).join(Season).filter(Season.series_id == series.id).all()
    ep_ids = [e.sonarrid for e in eps if e.sonarrid and e.placeholder_status == "Search" and e.status not in ["IN_QUEUE", "IN_PROGRESS"]]
    if not ep_ids:
        return False

    return trigger_sonarr_search(
        series.sonarrid,
        episode_ids=ep_ids,
        series_title=None,
        is_4k=series.is_4k
    )


def mark_done(session: Session, ent_id: int, model: Type, action: str = None) -> bool:
    rec = session.query(model).get(ent_id)
    rec.status = 'DONE'
    session.add(rec)
    return True


def enqueue_monitor(session: Session, ent_id: int, model: Type, action: str = None) -> bool:
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
            action='playback',
            steps='monitoring'
            ).first()
            
            if existing:
                logger.info(f"SubFlow for Movie ID {ent_id} already exists", extra={'emoji_type': 'info'})
                return True

            subflow = SubFlow(
            movie_id=ent_id,
            episode_id=None,
            status='IN_QUEUE',
            action='playback',
            branch='playback',
            steps='monitoring'
            )
            movie = session.query(Movie).get(ent_id)
            movie.status = 'IN_QUEUE'
            session.add(subflow)
            session.add(movie)

            logger.info(f"Created SubFlow for Movie ID {ent_id}", extra={'emoji_type': 'queue'})
            
        elif model == Episode:
            series = session.query(Episode).get(ent_id).season.series
            eps = session.query(Episode).join(Season).filter(Season.series_id == series.id, Episode.placeholder_status == "Search").all()
            for e in eps:
                existing = session.query(SubFlow).filter_by(
                movie_id=None,
                episode_id=e.id,
                action='playback',
                steps='monitoring'
                ).first()
                if existing:
                    logger.info(f"SubFlow for Episode ID {e.id} already exists", extra={'emoji_type': 'info'})
                    return True
                subflow = SubFlow(
                movie_id=None,
                episode_id=e.id,
                status='IN_QUEUE',
                action='playback',
                branch='playback',
                steps='monitoring'
                )
                episode = session.query(Episode).get(e.id)
                episode.status = 'IN_QUEUE'
                session.add(subflow)
                session.add(episode)
                logger.info(f"Created SubFlow for Episode ID {e.id}", extra={'emoji_type': 'queue'})
            
        else:
            return False
                
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
