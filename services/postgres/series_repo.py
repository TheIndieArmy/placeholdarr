import logging
from sqlalchemy.orm import Session
from services.postgres.models import Episode, Series, Season

# Set up logger
logger = logging.getLogger(__name__)

class SeriesRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_by_tvdbid(self, tvdbid: int, is_4k: bool) -> Series | None:
        return self.session.query(Series).filter_by(tvdbid=tvdbid,is_4k=is_4k).first()
    
    def get_by_id(self, seriesid: int) -> Series | None:
        return self.session.query(Series).filter_by(id=seriesid).first()

    def add(self, **kwargs) -> Series:
        series = Series(**kwargs)
        self.session.add(series)
        self.session.commit()
        return series

    def delete_by_tvdbid(self, tvdbid: int) -> bool:
        series = self.get_by_tvdbid(tvdbid)
        if series:
            self.session.delete(series)
            self.session.commit()
            return True
        return False

    def update(self, tvdbid: int, **kwargs) -> Series | None:
        series = self.get_by_tvdbid(tvdbid)
        if series:
            for k, v in kwargs.items():
                setattr(series, k, v)
            self.session.commit()
        return series

    def get_or_create(model, session: Session, defaults=None, **kwargs):
        """
        Try to get an object by kwargs; if not found, create with defaults.
        Returns (instance, created_bool).
        """
        instance = session.query(model).filter_by(**kwargs).first()
        if instance:
            return instance, False
        params = dict(kwargs)
        if defaults:
            params.update(defaults)
        instance = model(**params)
        session.add(instance)
        session.commit()
        return instance, True

    def delete_seasons_and_episodes(self, series: Series, episodes_data: list[dict]):
        """
        Delete all seasons and episodes for the given series.
        episodes_data: list of dicts, each with keys:
        - 'seasonNumber'
        - 'episodeNumber'
        - 'title'
        - ...any other metadata
        """
        # group episodes by season number
        by_season = {}
        for ep in episodes_data:
            sn = ep['seasonNumber']
            by_season.setdefault(sn, []).append(ep)
        
        for season_num, eps in by_season.items():
            # find the Season row
            season = self.session.query(Season).filter_by(
                series_id=series.id,
                season_number=season_num 
            ).first()
            if not season:
                continue
            
            # within that season, mark episodes for deletion
            for ep in eps:
                episode = self.session.query(Episode).filter_by(
                    season_id=season.id,
                    episode_number=ep['episodeNumber']
                ).first()
                if episode:
                    episode.status = "PENDING"
                    episode.action = "handle_episodefiledelete"
                    logger.info(f"Marked Episode S{season_num}E{ep['episodeNumber']} for deletion")
                    
        self.session.commit()


    def add_missing_seasons_and_episodes(self, series: Series, episodes_data: list[dict]):
        """
        episodes_data: list of dicts, each with keys:
        - 'seasonNumber'
        - 'episodeNumber'
        - 'title'
        - ...any other metadata
        """        
        # group episodes by season number
        by_season = {}
        for ep in episodes_data:
            sn = ep['seasonNumber']
            by_season.setdefault(sn, []).append(ep)
        
        for season_num, eps in by_season.items():
            # 2) get or create the Season row
            season_defaults = {
                'title': f"{series.title} S{season_num:02d}",
                'year': series.year,
                'tvdbid': series.tvdbid
            }
            season, created = self.get_or_create(
                Season,
                self.session,
                defaults=season_defaults,
                series_id=series.id,
                season_number=season_num 
            )
            
            # 3) within that season, upsert episodes
            for ep in eps:
                episode_defaults = {
                    'title': ep.get('title', f"Ep {ep['episodeNumber']}"),
                    'year': series.year,
                    'tvdbid': ep.get('tvdbid', None) or 0
                }
                ep_instance, ep_created = self.get_or_create(
                    Episode,
                    self.session,
                    defaults=episode_defaults,
                    season_id=season.id,
                    episode_number=ep['episodeNumber'],
                    status='PENDING',
                    action='handle_seriesadd',
                )
                if ep_created:
                    logger.info(f"Marked Episode S{season_num}E{ep['episodeNumber']} for deletion")

    def get_ep_by_series(self, series, season_num, episode_num):
        """
        Get the episode based on Series ID, season number, and episode number.
        """
        return self.session.query(Episode).filter(
            Episode.series_id == series.id,
            Episode.season_number == season_num,
            Episode.episode_number == episode_num
        ).first()

    def is_deleted(self, series, is_deleted):
        series.is_deleted = is_deleted
        seasons = self.session.query(Season).filter_by(series_id=series.id).all()
        if seasons:
            for season in seasons:
                season.is_deleted = is_deleted
                episodes = self.session.query(Episode).filter_by(season_id=season.id).all()
                for ep in episodes:
                    ep.is_deleted = is_deleted
        self.session.commit()
        return series