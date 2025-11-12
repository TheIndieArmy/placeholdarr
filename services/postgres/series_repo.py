import logging
import re

import requests
from core.config import settings
from sqlalchemy.orm import Session
from services.postgres.models import Episode, Series, Season

# Set up logger
logger = logging.getLogger(__name__)


def _camel_candidates(key: str) -> list:
    """Return candidate column names for a camelCase key.
    Examples:
      tvdbId -> ['tvdbId','tvdbid','tvdb_id']
      sonarrMonitored -> ['sonarrMonitored','sonarrmonitored','sonarr_monitored']
    """
    # original and lower
    orig = key
    lower = key.lower()
    # snake_case
    snake = re.sub(r'([A-Z])', lambda m: '_' + m.group(1).lower(), key)
    # joined lower (remove underscores for keys that are like tvdbId -> tvdbid)
    joined = re.sub(r'[_\-]', '', snake)
    return [orig, lower, joined, snake]


def _map_keys_to_allowed(payload: dict, allowed: set) -> dict:
    mapped = {}
    for k, v in payload.items():
        # special-case known fields we intentionally drop or rename
        if k in ('tmdbId', 'tmdbid', 'tmdb_id'):
            # we intentionally do not persist tmdb for Series
            continue

        candidates = _camel_candidates(k)
        found = None
        for c in candidates:
            if c in allowed:
                found = c
                break
        if found:
            mapped[found] = v
        else:
            # also allow exact underscore removal mapping: e.g. sonarr_path -> filepath
            no_underscore = k.replace('_', '').replace('-', '')
            for c in _camel_candidates(no_underscore):
                if c in allowed:
                    found = c
                    break
            if found:
                mapped[found] = v
            else:
                # not a known column, ignore silently
                continue
    return mapped


class SeriesRepository:
    def __init__(self, session: Session):
        self.session = session
    def get_by_series_tvdbid(self, tvdbid: int, is_4k: bool) -> Series | None:
        return self.session.query(Series).filter_by(tvdbid=tvdbid,is_4k=is_4k).first()

    def get_by_ep_tvdbid(self, tvdbid: int, is_4k: bool) -> Series | None:
        return self.session.query(Series).join(Series.season).join(Season.episode).filter(
            Episode.tvdbid == tvdbid,
            Series.is_4k == is_4k
        ).first()
    
    def get_by_jellyfin_itemid(self, jellyfin_itemid: str, is_4k: bool) -> Series | None:
        return self.session.query(Series).join(Series.season).join(Season.episode).filter(
            Episode.jellyfin_dummy_id == jellyfin_itemid,
            Series.is_4k == is_4k
        ).first()

    def get_by_id(self, seriesid: int) -> Series | None:
        return self.session.query(Series).filter_by(id=seriesid).first()

    def add(self, **kwargs) -> Series:
        # Filter incoming keys to model columns and map camelCase to column names
        allowed = {c.name for c in Series.__table__.columns}
        mapped = _map_keys_to_allowed(kwargs, allowed)

        # Helpful debug logging to inspect what will be persisted
        logger.debug("Series mapped payload before create: %s", mapped)

        # Ensure tvdbid is present if available under different key forms
        if 'tvdbid' not in mapped:
            # look for tvdb variants in raw kwargs
            for k in ('tvdbId', 'tvdb', 'tvdb_id', 'tvdbid'):
                if k in kwargs:
                    mapped['tvdbid'] = kwargs[k]
                    break

        # If tvdbid is still missing, log and raise a clear error to avoid DB integrity exceptions
        if 'tvdbid' not in mapped or mapped.get('tvdbid') is None:
            logger.error("Attempting to create Series without tvdbid. Mapped: %s Raw: %s", mapped, kwargs)
            raise ValueError("tvdbid is required to create Series")

        series = Series(**mapped)
        self.session.add(series)
        self.session.commit()
        return series

    def delete_by_tvdbid(self, tvdbid: int, is_4k: bool = False) -> bool:
        series = self.get_by_tvdbid(tvdbid, is_4k)
        if series:
            self.session.delete(series)
            self.session.commit()
            return True
        return False

    def update(self, tvdbid: int, **kwargs) -> Series | None:
        series = self.get_by_tvdbid(tvdbid)
        if series:
            allowed = {c.name for c in Series.__table__.columns}
            mapped = _map_keys_to_allowed(kwargs, allowed)
            for k, v in mapped.items():
                setattr(series, k, v)
            self.session.commit()
        return series

    def get_or_create(self, model, session: Session, defaults=None, **kwargs):
        """
        Try to get an object by kwargs; if not found, create with defaults.
        Returns (instance, created_bool).
        """
        allowed = {c.name for c in model.__table__.columns}
        mapped_kwargs = _map_keys_to_allowed(kwargs, allowed)
        instance = session.query(model).filter_by(**mapped_kwargs).first()
        if instance:
            return instance, False
        params = dict(mapped_kwargs)
        if defaults:
            # map defaults similarly
            params.update(_map_keys_to_allowed(defaults, allowed))
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


    def add_missing_seasons_and_episodes(self, series: Series, episodes_data: list[dict] = None):
        """
        episodes_data: list of dicts, each with keys:
        - 'seasonNumber'
        - 'episodeNumber'
        - 'title'
        - ...any other metadata
        """
        # group episodes by season number
        if not episodes_data:
            series_id = series.sonarrid
            if series_id:
                r = requests.get(f"{settings.SONARR_URL}/episode",
                                 params={'seriesId': series_id},
                                 headers={'X-Api-Key': settings.SONARR_API_KEY})
                r.raise_for_status()
                episodes_data = r.json()
            else:
                logger.warning("No series ID provided in seriesadd event.", extra={'emoji_type': 'warning'})
                episodes_data = []
        by_season = {}
        for ep in episodes_data:
            # be robust: Sonarr may provide seasonNumber as int or missing (treat missing as 0)
            try:
                sn = int(ep.get('seasonNumber', 0))
            except Exception:
                sn = 0
            by_season.setdefault(sn, []).append(ep)
        created_count = 0
        for season_num, eps in by_season.items():
            # 2) get or create the Season row
            # Use a friendly title for specials (season 0)
            if season_num == 0:
                season_title = f"{series.title} Specials"
            else:
                season_title = f"{series.title} S{season_num:02d}"

            season_defaults = {
                'title': season_title,
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
                # episode number may be missing or string - be defensive
                try:
                    ep_num = int(ep.get('episodeNumber', 0))
                except Exception:
                    ep_num = 0

                episode_defaults = {
                    'title': ep.get('title', f"Ep {ep_num}"),
                    'year': series.year,
                    # keep tvdbid as None when missing instead of coercing to 0
                    'tvdbid': ep.get('tvdbid', None),
                    'status': 'PENDING',
                    'action': 'handle_seriesadd',
                }
                # Ensure transient/process fields (status/action) are provided as defaults
                # to avoid them being used in the lookup filter and causing duplicate rows.
                ep_defaults_with_process = dict(episode_defaults)
                ep_defaults_with_process.update({
                    'status': 'PENDING',
                    'action': 'handle_seriesadd'
                })

                ep_instance, ep_created = self.get_or_create(
                    Episode,
                    self.session,
                    defaults=ep_defaults_with_process,
                    season_id=season.id,
                    episode_number=ep_num
                )
                if ep_created:
                    logger.info(f"Added Episode S{season_num}E{ep_num} and queued for processing")
                    created_count += 1

        return created_count

    def get_ep_by_series(self, series, season_num, episode_num):
        """
        Get the episode based on Series ID, season number, and episode number.
        """
        # Find the Season row for this series + season number
        season = self.session.query(Season).filter_by(series_id=series.id, season_number=season_num).first()
        if not season:
            return None
        return self.session.query(Episode).filter(
            Episode.season_id == season.id,
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