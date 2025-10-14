from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Date, BigInteger, DateTime, JSON, Index, text
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.hybrid import hybrid_property
from services.postgres.db import Base
from datetime import datetime, timezone


def utcnow():
    """Return a timezone-aware UTC datetime for SQLAlchemy defaults."""
    return datetime.now(timezone.utc)

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
    # ARR-provided synopsis/overview for NFO/metadata generation
    radarr_overview = Column(String, nullable=True)
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
    # IMDB id reported by Radarr (e.g. 'tt1234567')
    imdbid = Column(String, nullable=True)
    # Remote poster URL reported by Radarr (useful for NFO/poster downloads)
    remote_poster = Column(String, nullable=True)
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
    # persisted canonical determination (one of: obsolete_placeholder, not_needed, placeholder_exists, needs_placeholder)
    determination = Column(String, nullable=True)
    determination_updated_at = Column(DateTime(timezone=True), nullable=True)

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


# Placeholder table: tracks placeholder files attached to movies/episodes
class Placeholder(Base):
    __tablename__ = 'placeholder'
    id = Column(Integer, primary_key=True, autoincrement=True)
    movie_id = Column(Integer, ForeignKey('movie.id'), nullable=True)
    series_id = Column(Integer, ForeignKey('series.id'), nullable=True)
    season_id = Column(Integer, ForeignKey('season.id'), nullable=True)
    episode_id = Column(Integer, ForeignKey('episode.id'), nullable=True)
    path = Column(String, nullable=False)
    exists = Column(Boolean, default=False)
    lifecycle_status = Column(String, nullable=True)
    display_status = Column(String, nullable=True)
    display_progress = Column(Integer, nullable=True)
    display_reason = Column(String, nullable=True)
    format_hint = Column(String, nullable=True)
    extra = Column(JSON, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    # canonical determination mirrored from decider
    determination = Column(String, nullable=True)
    determination_updated_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<Placeholder(id={self.id}, path={self.path!r})>"

# New Job table: simple durable queue for batch/import jobs
class Job(Base):
    __tablename__ = 'job'
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_type = Column(String, nullable=False)               # e.g. 'import_list', 'process_series_add', 'file_import'
    payload = Column(JSON, nullable=True)                   # arbitrary JSON payload for the worker
    status = Column(String, default='PENDING')              # PENDING / CLAIMED / DONE / FAILED
    run_after = Column(DateTime(timezone=True), nullable=True)             # optional delay for scheduling
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)
    group_id = Column(String, nullable=True)                # optional grouping id for coalescing
    expected_counts = Column(JSON, nullable=True)          # optional per-series expected counts {series_id: count}
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    error_message = Column(String, nullable=True)

    __table_args__ = (
        # index to speed up claiming
        Index('ix_job_status_run_after', 'status', 'run_after'),
        # index for lookup/dedupe by group
        Index('ix_job_groupid', 'group_id'),
        # Partial unique index to prevent multiple active combined_refresh jobs with same group_id
        Index('ux_job_combined_refresh_groupid', 'group_id', unique=True, postgresql_where=text("job_type='combined_refresh' AND status IN ('PENDING','CLAIMED','WORKING')")),
    # Partial unique index to prevent multiple active enrichment jobs with same group_id
    Index('ux_job_enrichment_groupid', 'group_id', unique=True, postgresql_where=text("job_type='enrichment' AND status IN ('PENDING','CLAIMED','WORKING')")),
    )

    def __repr__(self):
        return f"<Job(id={self.id}, type={self.job_type!r}, status={self.status})>"

class Series(Base):
    __tablename__ = "series"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    tvdbid = Column(Integer, unique=True, nullable=False)
    is_4k  = Column(Boolean, default=False)
    dummypath = Column(String, nullable=True)
    sonarrpath = Column(String, nullable=True)
    # ARR-provided synopsis/overview for series-level metadata
    sonarr_series_overview = Column(String, nullable=True)
    # Aggregate flags for files under this series
    has_files = Column(Boolean, default=False)
    seriesfile_count = Column(BigInteger, nullable=True)
    # human-friendly quality label aggregated or representative for the series
    sonarr_quality = Column(String, nullable=True)
    sonarr_status = Column(String, nullable=True)
    sonarrid = Column(Integer, nullable=True)
    # IMDB id reported by Sonarr (if present, e.g. 'tt1234567')
    imdbid = Column(String, nullable=True)
    # Remote poster URL reported by Sonarr (useful for NFO/poster downloads)
    remote_poster = Column(String, nullable=True)
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
    # season_number must NOT be unique across the whole table — uniqueness applies per series
    season_number = Column(Integer, nullable=False, index=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    dummypath = Column(String, nullable=True)
    sonarrpath = Column(String, nullable=True)
    # ARR-provided synopsis/overview for series-level metadata
    sonarr_season_overview = Column(String, nullable=True)
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
    # episode_number must NOT be unique across the whole table — uniqueness applies per season
    episode_number = Column(Integer, nullable=False, index=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    dummypath = Column(String, nullable=True)
    sonarrpath = Column(String, nullable=True)
    # Exact path to the episode file when Sonarr has it
    episodefile_path = Column(String, nullable=True)
    episodefile_size = Column(BigInteger, nullable=True)
    # Episode-level overview (if provided by Sonarr)
    sonarr_episode_overview = Column(String, nullable=True)
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
    determination = Column(String, nullable=True)
    determination_updated_at = Column(DateTime(timezone=True), nullable=True)

    subflows = relationship('SubFlow', back_populates='episode')
    season = relationship('Season', back_populates='episode')

    @hybrid_property
    def season_number(self):
        """Convenience property returning the season number from the related Season row."""
        try:
            return self.season.season_number if self.season else None
        except Exception:
            return None

    @hybrid_property
    def series_title(self):
        """Convenience property returning the Series title via Season -> Series."""
        try:
            return self.season.series.title if self.season and self.season.series else None
        except Exception:
            return None

    @hybrid_property
    def series_id(self):
        """Convenience property returning the Series.id via Season -> Series."""
        try:
            return self.season.series.id if self.season and self.season.series else None
        except Exception:
            return None

    def __repr__(self):
        return (
           f"<Episode(id={self.id}, title={self.title!r}, year={self.year}, "
            f"tvdbid={self.tvdbid})>"
        )