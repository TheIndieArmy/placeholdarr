from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Date, BigInteger
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base
from services.postgres.db import Base

class Movie(Base):
    __tablename__ = "movie"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    tmdbid = Column(Integer, unique=True, nullable=False)
    is_4k  = Column(Boolean, default=False)
    dummypath = Column(String, nullable=True)
    # Radarr configured library path (the folder configured in Radarr for the movie)
    radarrpath = Column(String, nullable=True)
    # Exact path to the actual movie file when Radarr has imported one (movieFile.path)
    moviefile_path = Column(String, nullable=True)
    # Size in bytes reported for the movie file (if available)
    moviefile_size = Column(BigInteger, nullable=True)
    # boolean indicating Radarr reports a movie file exists for this movie
    has_file = Column(Boolean, default=False)
    # human-friendly quality label reported by Radarr (if available)
    radarr_quality = Column(String, nullable=True)
    # Radarr release lifecycle status (announced / inCinemas / released)
    radarr_release_status = Column(String, nullable=True)
    # Whether Radarr is monitoring this movie for downloads
    radarr_monitored = Column(Boolean, default=False)
    radarrid = Column(Integer, nullable=True)
    action = Column(String, nullable=True)
    status = Column(String, default='PENDING')
    current_step_name = Column(String, nullable=True)
    jellyfin_title = Column(String, nullable=True)
    jellyfin_id = Column(String, nullable=True)
    jellyfin_dummy_id = Column(String, nullable=True)
    jellyfin_overview = Column(String, nullable=True)
    plex_title = Column(String, nullable=True)
    plex_id = Column(String, nullable=True)
    plex_dummy_id = Column(String, nullable=True)
    plex_overview = Column(String, nullable=True)
    placeholder_status = Column(String, nullable=True)
    # Boolean to track if placeholder file physically exists
    placeholder_exists = Column(Boolean, default=False)
    filepath = Column(String, nullable=True)
    last_search = Column(Date, nullable=True)
    theater_release_date = Column(Date, nullable=True)
    digital_release_date = Column(Date, nullable=True)
    physical_release_date = Column(Date, nullable=True)
    radarr_progress = Column(Integer, nullable=True, default=0)
    radarr_status = Column(String, nullable=True)
    is_deleted = Column(Boolean, default=False)

    subflows = relationship('SubFlow', back_populates='movie')

    def __repr__(self):
        return (
            f"<Movie(id={self.id}, title={self.title!r}, year={self.year}, "
            f"tmdbid={self.tmdbid})>"
        )
    
class SubFlow(Base):
    __tablename__ = 'subflow'
    id = Column(Integer, primary_key=True, autoincrement=True)
    movie_id = Column(Integer, ForeignKey('movie.id'), nullable=True)
    series_id = Column(Integer, ForeignKey('series.id'), nullable=True)
    season_id = Column(Integer, ForeignKey('season.id'), nullable=True)
    episode_id = Column(Integer, ForeignKey('episode.id'), nullable=True)
    action = Column(String, nullable=True)
    branch = Column(String, nullable=False)
    steps = Column(String, nullable=False)
    step_index = Column(Integer, default=0)
    status = Column(String, default='PENDING')
    retry_count = Column(Integer, default=0)
    error_message = Column(String, nullable=True)

    movie = relationship('Movie', back_populates='subflows')
    series = relationship('Series', back_populates='subflows')
    season = relationship('Season', back_populates='subflows')
    episode = relationship('Episode', back_populates='subflows')

class Series(Base):
    __tablename__ = "series"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    tvdbid = Column(Integer, unique=True, nullable=False)
    is_4k  = Column(Boolean, default=False)
    dummypath = Column(String, nullable=True)
    sonarrpath = Column(String, nullable=True)
    # Aggregate flags for files under this series
    has_files = Column(Boolean, default=False)
    seriesfile_count = Column(BigInteger, nullable=True)
    # human-friendly quality label aggregated or representative for the series
    sonarr_quality = Column(String, nullable=True)
    sonarr_status = Column(String, nullable=True)
    sonarrid = Column(Integer, nullable=True)
    # Whether Sonarr is monitoring this series
    sonarr_monitored = Column(Boolean, default=False)
    status = Column(String, default='PENDING')
    jellyfin_title = Column(String, nullable=True)
    jellyfin_id = Column(String, nullable=True)
    jellyfin_dummy_id = Column(String, nullable=True)
    jellyfin_overview = Column(String, nullable=True)
    plex_title = Column(String, nullable=True)
    plex_id = Column(String, nullable=True)
    plex_dummy_id = Column(String, nullable=True)
    plex_overview = Column(String, nullable=True)
    placeholder_status = Column(String, nullable=True)
    is_deleted = Column(Boolean, default=False)

    subflows = relationship('SubFlow', back_populates='series')
    season = relationship('Season', back_populates='series')

    def __repr__(self):
        return (
            f"<Series(id={self.id}, title={self.title!r}, year={self.year}, "
            f"tvdbid={self.tvdbid})>"
        )
class Season(Base):
    __tablename__ = "season"
    id = Column(Integer, primary_key=True, autoincrement=True)
    series_id = Column(Integer, ForeignKey('series.id'), nullable=False)
    season_number = Column(Integer, nullable=False, index=True, unique=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    dummypath = Column(String, nullable=True)
    sonarrpath = Column(String, nullable=True)
    # Aggregate per-season file info
    has_files = Column(Boolean, default=False)
    seasonfile_count = Column(BigInteger, nullable=True)
    sonarr_status = Column(String, nullable=True)
    sonarrid = Column(Integer, nullable=True)
    sonarr_monitored = Column(Boolean, default=False)
    jellyfin_title = Column(String, nullable=True)
    jellyfin_id = Column(String, nullable=True)
    jellyfin_dummy_id = Column(String, nullable=True)
    jellyfin_overview = Column(String, nullable=True)
    plex_title = Column(String, nullable=True)
    plex_id = Column(String, nullable=True)
    plex_dummy_id = Column(String, nullable=True)
    plex_overview = Column(String, nullable=True)
    placeholder_status = Column(String, nullable=True)
    is_deleted = Column(Boolean, default=False)

    subflows = relationship('SubFlow', back_populates='season')
    series = relationship('Series', back_populates='season')
    episode = relationship('Episode', back_populates='season')

    def __repr__(self):
        return (
            f"<Season(id={self.id}, title={self.title!r}, year={self.year})>"
            f"tvdbid={self.tvdbid})>"

        )
class Episode(Base):
    __tablename__ = "episode"
    id = Column(Integer, primary_key=True, autoincrement=True)
    season_id = Column(Integer, ForeignKey('season.id'), nullable=False)
    episode_number = Column(Integer, nullable=False, index=True, unique=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    dummypath = Column(String, nullable=True)
    sonarrpath = Column(String, nullable=True)
    # Exact path to the episode file when Sonarr has it
    episodefile_path = Column(String, nullable=True)
    episodefile_size = Column(BigInteger, nullable=True)
    # boolean indicating Sonarr reports a file exists for this episode
    has_file = Column(Boolean, default=False)
    sonarr_quality = Column(String, nullable=True)
    sonarr_status = Column(String, nullable=True)
    # Whether Sonarr is monitoring this episode
    sonarr_monitored = Column(Boolean, default=False)
    sonarrid = Column(Integer, nullable=True)
    action = Column(String, nullable=True)
    status = Column(String, default='PENDING')
    current_step_name = Column(String, nullable=True)
    jellyfin_title = Column(String, nullable=True)
    jellyfin_id = Column(String, nullable=True)
    jellyfin_dummy_id = Column(String, nullable=True)
    jellyfin_overview = Column(String, nullable=True)
    plex_title = Column(String, nullable=True)
    plex_id = Column(String, nullable=True)
    plex_dummy_id = Column(String, nullable=True)
    plex_overview = Column(String, nullable=True)
    placeholder_status = Column(String, nullable=True)
    # Boolean to track if placeholder file physically exists
    placeholder_exists = Column(Boolean, default=False)
    filepath = Column(String, nullable=True)
    sonarr_progress = Column(Integer, nullable=True, default=0)
    sonarr_status = Column(String, nullable=True)
    air_date = Column(Date, nullable=True)
    is_deleted = Column(Boolean, default=False)

    subflows = relationship('SubFlow', back_populates='episode')
    season = relationship('Season', back_populates='episode')

    def __repr__(self):
        return (
           f"<Episode(id={self.id}, title={self.title!r}, year={self.year}, "
            f"tvdbid={self.tvdbid})>"
        )