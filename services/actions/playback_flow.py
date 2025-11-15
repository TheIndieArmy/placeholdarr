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

    # Episode flow - use season-based monitoring
    ep: Episode = session.query(Episode).get(ent_id)
    logger.debug(f"Processing episode: S{ep.season.season_number}E{ep.episode_number} (ID: {ep.id})")
    series = ep.season.series
    series_id = series.id
    play_mode = settings.TV_PLAY_MODE.lower()
    logger.debug(f"Play mode: {play_mode}, series ID: {series_id}")

    from services.integrations import monitor_seasons_and_episodes
    
    if play_mode == 'episode':
        # Episode mode: Monitor current + lookahead episodes
        lookahead_eps = db_get_episodes(
            session,
            series_id,
            ep.season.season_number,
            ep.episode_number,
            lookahead=settings.EPISODES_LOOKAHEAD
        )
        
        # Determine which seasons these episodes span
        season_nums = list(set([e.season.season_number for e in lookahead_eps]))
        
        # Episodes to mark for search
        eps_to_search = lookahead_eps
        
        # Call season-based monitoring with episode restrictions
        # This will monitor only the lookahead episodes and unmonitor others in same seasons
        monitor_result = monitor_seasons_and_episodes(
            series.sonarrid,
            season_numbers=season_nums,
            monitor_episodes=[e.sonarrid for e in lookahead_eps if e.sonarrid],
            is_4k=series.is_4k
        )
        
        logger.debug(f"Episode mode: Monitored seasons {season_nums}, {len(lookahead_eps)} episodes with lookahead={settings.EPISODES_LOOKAHEAD}")

    elif play_mode == 'season':
        # Season mode: Monitor current season + lookahead episodes
        S = ep.season.season_number
        
        # Get current season episodes
        current_season_eps = (session.query(Episode)
                               .filter_by(season_id=ep.season_id)
                               .order_by(Episode.episode_number)
                               .all())
        
        # Start with current season
        season_nums = [S]
        eps_to_search = current_season_eps.copy()
        
        # Check if lookahead goes into next season
        lookahead_eps = db_get_episodes(
            session,
            series_id,
            ep.season.season_number,
            ep.episode_number,
            lookahead=settings.EPISODES_LOOKAHEAD
        )
        
        # If any lookahead episode is in next season, include that season
        lookahead_seasons = set([e.season.season_number for e in lookahead_eps])
        for season_num in lookahead_seasons:
            if season_num not in season_nums:
                season_nums.append(season_num)
                # Add episodes from that season to search list
                next_season = (session.query(Season)
                              .filter(Season.series_id == series_id, Season.season_number == season_num)
                              .first())
                if next_season:
                    next_eps = (session.query(Episode)
                               .filter_by(season_id=next_season.id)
                               .all())
                    eps_to_search.extend(next_eps)
        
        # Monitor all specified seasons (no episode restrictions)
        monitor_result = monitor_seasons_and_episodes(
            series.sonarrid,
            season_numbers=season_nums,
            monitor_episodes=None,  # Monitor all episodes in these seasons
            is_4k=series.is_4k
        )
        
        logger.debug(f"Season mode: Monitored seasons {season_nums}, {len(eps_to_search)} episodes (lookahead={settings.EPISODES_LOOKAHEAD})")

    elif play_mode == 'series':
        # Series mode: Monitor all seasons
        all_seasons = (session.query(Season)
                      .filter(Season.series_id == series_id)
                      .all())
        season_nums = [s.season_number for s in all_seasons]
        eps_to_search = (session.query(Episode)
                        .join(Season)
                        .filter(Season.series_id == series_id)
                        .all())
        
        # Monitor all seasons (no episode restrictions)
        monitor_result = monitor_seasons_and_episodes(
            series.sonarrid,
            season_numbers=season_nums,
            monitor_episodes=None,  # Monitor all episodes in series
            is_4k=series.is_4k
        )
        
        logger.debug(f"Series mode: Monitored all {len(season_nums)} seasons, {len(eps_to_search)} episodes")

    else:
        logger.warning(f"Unknown TV_PLAY_MODE '{play_mode}'")
        eps_to_search = [ep]
        monitor_result = monitor_seasons_and_episodes(
            series.sonarrid,
            season_numbers=[ep.season.season_number],
            monitor_episodes=[ep.sonarrid] if ep.sonarrid else [],
            is_4k=series.is_4k
        )

    if not eps_to_search:
        logger.warning(f"No episodes found to search for {model.__name__} {ent_id}")
        return False

    # Mark episodes for search
    for e in eps_to_search:
        e.placeholder_status = "Search"
        session.add(e)
    
    logger.info(f"Updated placeholder_status to 'Search' for {len(eps_to_search)} episodes in series {series.title}")
    logger.info(f"Completed lookup_and_monitor for {model.__name__} {ent_id}")
    return monitor_result


def trigger_search(session: Session, ent_id: int, model: Type, action: str = None) -> bool:
    # First log with basic print to ensure it's executed
    print(f"[TRIGGER_SEARCH] Function called for {model.__name__} {ent_id}")
    logger.info(f"Starting trigger_search for {model.__name__} {ent_id}, action: {action}")
    
    if model is Movie:
        m = session.query(Movie).get(ent_id)
        logger.debug(f"Triggering Radarr search for movie: {m.title} (Radarr ID: {m.radarrid})")
        success = trigger_radarr_search(m.radarrid, m.title)
        if success:
            logger.info(f"✅ Successfully triggered Radarr search for {m.title}")
        else:
            logger.error(f"❌ Failed to trigger Radarr search for {m.title}")
        return bool(success)

    # Get the episode and series
    ep = session.query(Episode).get(ent_id)
    series = ep.season.series
    play_mode = settings.TV_PLAY_MODE.lower()
    
    logger.info(f"Triggering Sonarr search for series: {series.title} (Sonarr ID: {series.sonarrid}), mode: {play_mode}")
    
    # Get episodes marked for search based on play mode
    if play_mode == 'episode':
        # Only search episodes marked with "Search" status
        eps = session.query(Episode).join(Season).filter(
            Season.series_id == series.id,
            Episode.placeholder_status == "Search",
            Episode.status.notin_(["IN_QUEUE", "IN_PROGRESS"])
        ).all()
        ep_ids = [e.sonarrid for e in eps if e.sonarrid]
        
        if not ep_ids:
            logger.warning(f"No episodes ready for search in episode mode for series {series.title}")
            return False
            
        logger.info(f"📺 Episode mode: Triggering search for {len(ep_ids)} episodes")
        logger.debug(f"Episode IDs to search: {ep_ids}")
        
        success = trigger_sonarr_search(
            series.sonarrid,
            episode_ids=ep_ids,
            series_title=series.title,
            is_4k=series.is_4k
        )
        
        if success:
            logger.info(f"✅ Successfully triggered episode search for {len(ep_ids)} episodes in {series.title}")
        else:
            logger.error(f"❌ Failed to trigger episode search for {series.title}")
        return success
    
    elif play_mode == 'season':
        # Search all seasons marked for search
        eps = session.query(Episode).join(Season).filter(
            Season.series_id == series.id,
            Episode.placeholder_status == "Search",
            Episode.status.notin_(["IN_QUEUE", "IN_PROGRESS"])
        ).all()
        
        # Get unique seasons
        season_nums = list(set([e.season.season_number for e in eps]))
        
        if not season_nums:
            logger.warning(f"No seasons ready for search in season mode for series {series.title}")
            return False
        
        logger.info(f"🎬 Season mode: Triggering season search for seasons {season_nums} in {series.title}")
        
        # Trigger search for each season
        success = True
        for season_num in season_nums:
            logger.debug(f"Triggering season search for S{season_num:02d}")
            result = trigger_sonarr_search(
                series.sonarrid,
                season_number=season_num,
                series_title=series.title,
                is_4k=series.is_4k
            )
            if not result:
                logger.error(f"❌ Failed to trigger search for season {season_num}")
            success = success and result
        
        if success:
            logger.info(f"✅ Successfully triggered season search for {len(season_nums)} season(s) in {series.title}")
        else:
            logger.error(f"❌ Some season searches failed for {series.title}")
        return success
    
    elif play_mode == 'series':
        # Search entire series
        logger.info(f"📚 Series mode: Triggering series-wide search for {series.title}")
        
        success = trigger_sonarr_search(
            series.sonarrid,
            series_title=series.title,
            is_4k=series.is_4k
        )
        
        if success:
            logger.info(f"✅ Successfully triggered series search for {series.title}")
        else:
            logger.error(f"❌ Failed to trigger series search for {series.title}")
        return success
    
    else:
        # Fallback to episode search
        logger.warning(f"Unknown play mode '{play_mode}', falling back to episode search")
        eps = session.query(Episode).join(Season).filter(
            Season.series_id == series.id,
            Episode.placeholder_status == "Search"
        ).all()
        ep_ids = [e.sonarrid for e in eps if e.sonarrid]
        
        if not ep_ids:
            logger.warning(f"No episodes found for fallback search")
            return False
            
        logger.debug(f"Fallback episode search: {len(ep_ids)} episodes")
        success = trigger_sonarr_search(
            series.sonarrid,
            episode_ids=ep_ids,
            series_title=series.title,
            is_4k=series.is_4k
        )
        
        if success:
            logger.info(f"✅ Successfully triggered fallback search for {len(ep_ids)} episodes")
        else:
            logger.error(f"❌ Failed to trigger fallback search")
        return success


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
        
        # End the handler logging session for playback
        from core.handler_logging import get_handler_session_for_entity, end_handler_logging
        if model == Movie:
            session_id = get_handler_session_for_entity('handle_playback', ent_id)
        elif model == Episode:
            # For episodes, use the first episode's ID or 0 since we don't track individual sessions
            session_id = get_handler_session_for_entity('handle_playback', 0)
        else:
            session_id = None
            
        if session_id:
            end_handler_logging(session_id, success=True, 
                               summary=f"Playback flow completed for {model.__name__} {ent_id}")
            logger.info(f"Closed handler logging session for playback", extra={'emoji_type': 'success'})
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to enqueue {model.__name__} ID {ent_id}: {e}", extra={'emoji_type': 'error'})
        session.rollback()
        
        # Try to end logging session even on failure
        from core.handler_logging import get_handler_session_for_entity, end_handler_logging
        if model == Movie:
            session_id = get_handler_session_for_entity('handle_playback', ent_id)
        elif model == Episode:
            session_id = get_handler_session_for_entity('handle_playback', 0)
        else:
            session_id = None
            
        if session_id:
            end_handler_logging(session_id, success=False, 
                               summary=f"Playback flow failed: {e}")
        
        return False


def steps():
    return [
        identify_source,
        lookup_and_monitor,
        trigger_search,
        mark_done,
        enqueue_monitor
    ]
