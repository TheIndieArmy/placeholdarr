from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Date, BigInteger, DateTime, JSON, Index, text, func
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.hybrid import hybrid_property
from services.postgres.db import Base
from datetime import datetime, timezone

from core.config import settings


def utcnow():
    """Return a timezone-aware UTC datetime for SQLAlchemy defaults."""
    return datetime.now(timezone.utc)

class Movie(Base):
    __tablename__ = "movie"
    __table_args__ = (
        Index('ix_movie_determination', 'determination'),
        Index('ux_movie_tmdbid_instance_id', 'tmdbid', 'instance_id', unique=True),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    tmdbid = Column(Integer, nullable=False)
    # Canonical ARR instance identity for this row.
    instance_id = Column(String, nullable=False, default='radarr:primary')
    # ARR instance key for this row (for example: radarr_primary, radarr_secondary)
    instance_key = Column(String, nullable=False, default='radarr_std')
    # New preferred placeholder folder (where we'd create a placeholder)
    placeholder_folder = Column(String, nullable=True)
    # Radarr configured library path (the folder configured in Radarr for the movie)
    radarrpath = Column(String, nullable=True)
    # Standardized name for the movie file path when Radarr has imported one
    # (previously moviefile_filepath; renamed to radarr_filepath)
    radarr_filepath = Column(String, nullable=True)
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
    remote_fanart = Column(String, nullable=True)
    radarr_runtime = Column(Integer, nullable=True)
    radarr_certification = Column(String, nullable=True)
    radarr_genres = Column(JSON, nullable=True)
    radarr_studio = Column(String, nullable=True)
    radarr_ratings = Column(JSON, nullable=True)
    radarr_collection = Column(JSON, nullable=True)
    radarr_actors = Column(JSON, nullable=True)
    radarr_directors = Column(JSON, nullable=True)
    radarr_credits = Column(JSON, nullable=True)
    radarr_trailer = Column(String, nullable=True)
    radarr_premiered = Column(Date, nullable=True)
    radarr_payload_raw = Column(JSON, nullable=True)
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
    # Boolean to track if placeholder file physically exists (aggregated)
    has_placeholder = Column(Boolean, default=False)
    # Canonical observed placeholder file path for this movie (derived)
    placeholder_filepath = Column(String, nullable=True)
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
    # Creation timestamp (DB authoritative). Set once at INSERT and do not change.
    created_at = Column(DateTime(timezone=True), server_default=text('now()'))
    # Last time this row was updated by the application/DB
    updated_at = Column(DateTime(timezone=True), server_default=text('now()'))
    # Last time this movie was observed in Radarr (authoritative DB clock)
    last_found_in_radarr = Column(DateTime(timezone=True), nullable=True)

    subflows = relationship('SubFlow', back_populates='movie')

    @hybrid_property
    def is_4k(self) -> bool:
        """True when this row's Radarr instance is configured as the 4K/secondary library.

        Uses ``ARR_INSTANCES_JSON`` (``is_4k`` on the matching ``instance_key``), not substring
        heuristics — keys named ``4k`` still follow config (they may point at primary Radarr).
        """
        key = str(getattr(self, 'instance_key', '') or '').strip().lower()
        if not key:
            return False
        item = settings.resolve_arr_instance("radarr", instance_key=key)
        if item is not None:
            return bool(item.get("is_4k", False))
        # Legacy rows / unknown keys: keep old heuristics
        instance_id = str(getattr(self, 'instance_id', '') or '').strip().lower()
        return ("4k" in key) or key.endswith("_secondary") or instance_id.endswith(":secondary")

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
    updated_at = Column(DateTime(timezone=True), server_default=text('now()'))

    movie = relationship('Movie', back_populates='subflows')
    series = relationship('Series', back_populates='subflows')
    season = relationship('Season', back_populates='subflows')
    episode = relationship('Episode', back_populates='subflows')


class Placeholder(Base):
    __tablename__ = 'placeholder'
    id = Column(Integer, primary_key=True, autoincrement=True)
    movie_id = Column(Integer, ForeignKey('movie.id'), nullable=True)
    series_id = Column(Integer, ForeignKey('series.id'), nullable=True)
    season_id = Column(Integer, ForeignKey('season.id'), nullable=True)
    episode_id = Column(Integer, ForeignKey('episode.id'), nullable=True)
    path = Column(String, nullable=False)
    # Whether the placeholder file currently exists on disk (FS-observed)
    has_placeholder = Column(Boolean, default=False)
    lifecycle_status = Column(String, nullable=True)
    display_status = Column(String, nullable=True)
    display_status_projected = Column(String, nullable=True)
    display_progress = Column(Integer, nullable=True)
    display_reason = Column(String, nullable=True)
    format_hint = Column(String, nullable=True)
    # Per-service placeholder item ids and first-observed timestamps.
    # These are runtime/media-server observations for the placeholder row.
    jellyfin_placeholder_id = Column(String, nullable=True)
    jellyfin_id_observed_at = Column(DateTime(timezone=True), nullable=True)
    emby_placeholder_id = Column(String, nullable=True)
    emby_id_observed_at = Column(DateTime(timezone=True), nullable=True)
    media_lookup_last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    media_lookup_error = Column(String, nullable=True)
    extra = Column(JSON, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text('now()'))
    updated_at = Column(DateTime(timezone=True), server_default=text('now()'))
    # Last time this placeholder file was observed on disk (FS-scan)
    last_observed_at = Column(DateTime(timezone=True), nullable=True)
    # Last time this placeholder was processed by the enrichment/merge phase
    last_enriched_at = Column(DateTime(timezone=True), nullable=True)
    # Short signature/hash of attributes used to determine if a placeholder changed
    # (e.g. fingerprint|size|inode). Helps skip unchanged placeholders during enrichment.
    enriched_signature = Column(String, nullable=True)
    # canonical determination mirrored from decider
    determination = Column(String, nullable=True)
    determination_updated_at = Column(DateTime(timezone=True), nullable=True)
    # When True, the queue-monitor producer should actively poll ARR /queue for this
    # placeholder (playback-initiated search flow). Cleared on terminal status.
    queue_monitor_active = Column(Boolean, default=False, server_default=text('false'))
    queue_monitor_active_set_at = Column(DateTime(timezone=True), nullable=True)
    # (placeholder) last-observed timestamp for the placeholder row

    def __repr__(self):
        return f"<Placeholder(id={self.id}, path={self.path!r})>"


class SystemActivityHistory(Base):
    """Append-only feed rows for `/api/activity` (materialized from EventLog / Job hooks)."""

    __tablename__ = "system_activity_history"
    __table_args__ = (Index("ix_system_activity_history_occurred_at", "occurred_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    origin = Column(String(32), nullable=False)
    ref_id = Column(Integer, nullable=False)
    snapshot = Column(JSON, nullable=False)

    def __repr__(self):
        return f"<SystemActivityHistory(id={self.id}, origin={self.origin!r}, ref_id={self.ref_id})>"


class PlaceholderActivityHistory(Base):
    """Append-only placeholder timeline (Radarr-style history table; written on insert/update and from status events)."""

    __tablename__ = "placeholder_activity_history"
    __table_args__ = (
        Index("ix_placeholder_activity_history_instance_occurred", "instance_key", "occurred_at"),
        Index("ix_placeholder_activity_history_occurred_at", "occurred_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    action = Column(String(16), nullable=False)
    item_type = Column(String(16), nullable=False)
    placeholder_id = Column(Integer, ForeignKey("placeholder.id", ondelete="SET NULL"), nullable=True)
    movie_id = Column(Integer, ForeignKey("movie.id", ondelete="SET NULL"), nullable=True)
    episode_id = Column(Integer, ForeignKey("episode.id", ondelete="SET NULL"), nullable=True)
    series_id = Column(Integer, ForeignKey("series.id", ondelete="SET NULL"), nullable=True)
    season_id = Column(Integer, ForeignKey("season.id", ondelete="SET NULL"), nullable=True)
    # Denormalized season number (TV) for display without joining season.
    season_number = Column(Integer, nullable=True)
    # Denormalized ARR slot (Radarr/Sonarr multi-instance); not the same as ``source`` (status projection).
    instance_key = Column(String(128), nullable=True)
    instance_id = Column(String(128), nullable=True)
    # Stable app-level kind for grouping: placeholder_created / placeholder_deleted / placeholder_status_changed.
    event_type = Column(String(128), nullable=True)
    path = Column(String, nullable=False, default="")
    item_title = Column(String, nullable=False, default="")
    series_title = Column(String, nullable=True)
    reason = Column(String, nullable=False, default="")
    status_label = Column(String, nullable=False, default="")
    source = Column(String(128), nullable=True)
    event_log_id = Column(Integer, ForeignKey("event_log.id", ondelete="SET NULL"), nullable=True)
    extra_snapshot = Column(JSON, nullable=True)

    def __repr__(self):
        return f"<PlaceholderActivityHistory(id={self.id}, action={self.action!r}, at={self.occurred_at!r})>"


class ScheduledTaskRun(Base):
    """Append-only history for scheduled/manual/startup maintenance tasks (Tasks UI)."""

    __tablename__ = "scheduled_task_run"
    __table_args__ = (
        Index("ix_scheduled_task_run_started_at", "started_at"),
        Index("ix_scheduled_task_run_task_key_started", "task_key", "started_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_key = Column(String(32), nullable=False)
    trigger = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(String, nullable=True)
    skip_reason = Column(String(256), nullable=True)
    summary = Column(JSON, nullable=True)

    def __repr__(self):
        return f"<ScheduledTaskRun(id={self.id}, task_key={self.task_key!r}, status={self.status!r})>"


class LibraryCatalogVersion(Base):
    """Singleton version counters for library shelf ETag polling (id=1)."""

    __tablename__ = "library_catalog_version"

    id = Column(Integer, primary_key=True, autoincrement=False, default=1)
    movies_version = Column(Integer, nullable=False, default=0)
    series_version = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()"))

    def __repr__(self):
        return (
            f"<LibraryCatalogVersion(id={self.id}, movies_version={self.movies_version}, "
            f"series_version={self.series_version})>"
        )


class CollectionRecipe(Base):
    """A saved rule-based collection definition synced into a Plex library collection."""

    __tablename__ = "collection_recipe"
    __table_args__ = (
        Index("ix_collection_recipe_enabled", "enabled"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    # Target Plex library section (any section, picked live from the Plex API).
    plex_section_id = Column(Integer, nullable=False)
    # "movie" or "show" — mirrors the Plex section type so the engine knows which catalog to query.
    plex_section_type = Column(String(16), nullable=False)
    collection_title = Column(String(200), nullable=False)
    # Block-based rule definition: {"sources": [...], "filters": [...], "limit": int|None, "sort": str|None}
    definition = Column(JSON, nullable=False, default=dict)
    # Per-recipe schedule override in hours; null = follow COLLECTIONS_SYNC_INTERVAL_HOURS.
    run_interval_hours = Column(Integer, nullable=True)
    # Annually recurring active window: {"start": "MM-DD", "end": "MM-DD", "when_inactive": "keep"|"clear"}.
    # Wrap-around windows (start > end, e.g. Dec 15 -> Jan 6) are supported. Null = always active.
    active_window = Column(JSON, nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    # Counts/status from the most recent run: {"status", "candidates", "matched", "resolved", "synced", "error"}
    last_run_summary = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()"))
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=text("now()"),
        onupdate=func.now(),
    )

    def __repr__(self):
        return f"<CollectionRecipe(id={self.id}, name={self.name!r}, section={self.plex_section_id})>"


class DashboardStatsSnapshot(Base):
    """Singleton materialized counters for `/api/stats` (id=1)."""

    __tablename__ = "dashboard_stats_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=False, default=1)
    movies_total = Column(Integer, nullable=False, default=0)
    movies_placeholders = Column(Integer, nullable=False, default=0)
    movies_downloaded = Column(Integer, nullable=False, default=0)
    movies_future_outside_lookahead = Column(Integer, nullable=False, default=0)
    series_total = Column(Integer, nullable=False, default=0)
    episodes_total = Column(Integer, nullable=False, default=0)
    episodes_placeholders = Column(Integer, nullable=False, default=0)
    episodes_downloaded = Column(Integer, nullable=False, default=0)
    episodes_future_outside_lookahead = Column(Integer, nullable=False, default=0)
    placeholders_on_disk = Column(Integer, nullable=False, default=0)
    jobs_pending = Column(Integer, nullable=False, default=0)
    jobs_failed = Column(Integer, nullable=False, default=0)
    jobs_done = Column(Integer, nullable=False, default=0)
    last_sync = Column(DateTime(timezone=True), nullable=True)
    computed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=text("now()"))

    def __repr__(self):
        return f"<DashboardStatsSnapshot(id={self.id}, computed_at={self.computed_at!r})>"


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
    created_at = Column(DateTime(timezone=True), server_default=text('now()'))
    updated_at = Column(DateTime(timezone=True), server_default=text('now()'))
    error_message = Column(String, nullable=True)
    # Set when status=CLAIMED; cleared when PENDING/DONE/FAILED. Prevents a stale handler from
    # committing after the stale-CLAIMED reaper requeues the job (NOTIFY worker parity).
    claim_token = Column(String(36), nullable=True)
    # Phase 5: job priority. Higher numbers are claimed first (within the
    # set of rows whose ``run_after`` is satisfied). Defaults assigned by
    # ``services.source_of_truth.job_priority.default_priority_for`` so
    # interactive jobs (webhooks, playback) are not starved by big batch
    # phases (calendar / sync).
    priority = Column(Integer, nullable=False, server_default=text('0'))

    __table_args__ = (
        # index to speed up claiming (priority + run_after used by claim ORDER BY)
        Index('ix_job_status_run_after', 'status', 'run_after'),
        Index('ix_job_status_priority_runafter', 'status', 'priority', 'run_after'),
        # index for lookup/dedupe by group
        Index('ix_job_groupid', 'group_id'),
        # Partial unique index to prevent multiple active combined_refresh jobs with same group_id
        Index('ux_job_combined_refresh_groupid', 'group_id', unique=True, postgresql_where=text("job_type='combined_refresh' AND status IN ('PENDING','CLAIMED','WORKING')")),
    # Partial unique index to prevent multiple active enrichment jobs with same group_id
    Index('ux_job_enrichment_groupid', 'group_id', unique=True, postgresql_where=text("job_type='enrichment' AND status IN ('PENDING','CLAIMED','WORKING')")),
    # Partial unique index for Plex-busy deferred observation trail jobs.
    Index('ux_job_obs_hybrid_slice_groupid', 'group_id', unique=True, postgresql_where=text("job_type='placeholder_observation_hybrid_slice' AND status IN ('PENDING','CLAIMED','WORKING')")),
    )

    def __repr__(self):
        return f"<Job(id={self.id}, type={self.job_type!r}, status={self.status})>"




class EventLog(Base):
    __tablename__ = 'event_log'
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String, nullable=False)
    source = Column(String, nullable=True)
    payload = Column(JSON, nullable=False)
    status = Column(String, default='PENDING')
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=10)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text('now()'))
    updated_at = Column(DateTime(timezone=True), server_default=text('now()'))
    processed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index('ix_event_log_status_created_at', 'status', 'created_at'),
    )

    def __repr__(self):
        return f"<EventLog(id={self.id}, event_type={self.event_type!r}, status={self.status!r})>"


# Table to claim a single FS-scan per external run (e.g. fullsync:<id>)
class FSScanRun(Base):
    __tablename__ = 'fs_scan_run'
    id = Column(Integer, primary_key=True, autoincrement=True)
    # external run identifier (for fullsync runs we use 'fullsync:<uuid>')
    run_id = Column(String, nullable=False, unique=True)
    claimed_by = Column(String, nullable=True)
    claimed_at = Column(DateTime(timezone=True), server_default=text('now()'))

    def __repr__(self):
        return f"<FSScanRun(id={self.id}, run_id={self.run_id!r})>"


class ProcessedJobKey(Base):
    """Idempotency-key registry for side-effecting handlers.

    Phase 4 of the holistic NOTIFY audit: the stale-CLAIMED reaper resets
    rows that may still be running their original handler thread, which
    means the same external action (NFO write, Plex refresh, ARR search
    trigger) can fire twice. Handlers atomically INSERT a deterministic
    key derived from ``(job_type, target_id [, run_id])`` BEFORE doing the
    side effect; the second runner sees the unique-constraint hit and
    skips. Old keys are pruned by a periodic sweep.
    """

    __tablename__ = 'processed_job_key'
    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, nullable=False, unique=True)
    job_type = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text('now()'))

    __table_args__ = (
        Index('ix_processed_job_key_created_at', 'created_at'),
    )

    def __repr__(self):
        return f"<ProcessedJobKey(id={self.id}, key={self.key!r})>"


class ArrState(Base):
    __tablename__ = 'arr_state'

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Instance key examples: radarr_std, radarr_4k, sonarr_std, sonarr_4k
    instance_key = Column(String, nullable=False, unique=True)
    arr_type = Column(String, nullable=False)  # radarr | sonarr
    last_history_id = Column(Integer, nullable=True)
    last_history_checked_at = Column(DateTime(timezone=True), nullable=True)
    first_full_sync_completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text('now()'))
    updated_at = Column(DateTime(timezone=True), server_default=text('now()'))

    __table_args__ = (
        Index('ix_arr_state_arr_type', 'arr_type'),
    )

    def __repr__(self):
        return (
            f"<ArrState(id={self.id}, instance_key={self.instance_key!r}, "
            f"last_history_id={self.last_history_id})>"
        )


class AppConfig(Base):
    __tablename__ = 'app_config'

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, nullable=False, unique=True)
    value = Column(JSON, nullable=True)
    value_type = Column(String, nullable=False, default='string')
    restart_required = Column(Boolean, nullable=False, default=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text('now()'))
    updated_at = Column(DateTime(timezone=True), server_default=text('now()'), onupdate=func.now())

    __table_args__ = (
        Index('ix_app_config_key', 'key', unique=True),
    )

    def __repr__(self):
        return f"<AppConfig(id={self.id}, key={self.key!r})>"

class Series(Base):
    __tablename__ = "series"
    __table_args__ = (
        Index('ix_series_plex_dummy_id', 'plex_dummy_id'),
        Index('ux_series_tvdbid_instance_id', 'tvdbid', 'instance_id', unique=True),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    tvdbid = Column(Integer, nullable=False)
    # Canonical ARR instance identity for this row.
    instance_id = Column(String, nullable=False, default='sonarr:primary')
    # ARR instance key for this row (for example: sonarr_primary, sonarr_secondary)
    instance_key = Column(String, nullable=False, default='sonarr_std')
    # preferred placeholder folder for series-level placeholders
    placeholder_folder = Column(String, nullable=True)
    sonarrpath = Column(String, nullable=True)
    # ARR-provided synopsis/overview for series-level metadata
    sonarr_series_overview = Column(String, nullable=True)
    # Aggregate flags for files under this series
    has_files = Column(Boolean, default=False)
    seriesfile_count = Column(BigInteger, nullable=True)
    # Materialized episode counters for fast library reads (refreshed on episode/season changes).
    episode_total = Column(Integer, nullable=False, default=0)
    episode_files = Column(Integer, nullable=False, default=0)
    episode_placeholders = Column(Integer, nullable=False, default=0)
    episode_future = Column(Integer, nullable=False, default=0)
    episode_missing = Column(Integer, nullable=False, default=0)
    stats_computed_at = Column(DateTime(timezone=True), nullable=True)
    # human-friendly quality label aggregated or representative for the series
    sonarr_quality = Column(String, nullable=True)
    sonarr_status = Column(String, nullable=True)
    sonarrid = Column(Integer, nullable=True)
    # IMDB id reported by Sonarr (if present, e.g. 'tt1234567')
    imdbid = Column(String, nullable=True)
    # Remote poster URL reported by Sonarr (useful for NFO/poster downloads)
    remote_poster = Column(String, nullable=True)
    remote_fanart = Column(String, nullable=True)
    remote_banner = Column(String, nullable=True)
    sonarr_runtime = Column(Integer, nullable=True)
    sonarr_certification = Column(String, nullable=True)
    sonarr_genres = Column(JSON, nullable=True)
    sonarr_network = Column(String, nullable=True)
    sonarr_ratings = Column(JSON, nullable=True)
    sonarr_tmdbid = Column(Integer, nullable=True)
    sonarr_tvmazeid = Column(Integer, nullable=True)
    sonarr_first_aired = Column(Date, nullable=True)
    sonarr_actors = Column(JSON, nullable=True)
    sonarr_payload_raw = Column(JSON, nullable=True)
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
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=text('now()'))
    updated_at = Column(DateTime(timezone=True), server_default=text('now()'))
    # Last time this season metadata was observed in Sonarr
    last_found_in_sonarr = Column(DateTime(timezone=True), nullable=True)

    subflows = relationship('SubFlow', back_populates='series')
    season = relationship('Season', back_populates='series')

    @hybrid_property
    def is_4k(self) -> bool:
        """True when this row's Sonarr instance is configured as the 4K/secondary library."""
        key = str(getattr(self, 'instance_key', '') or '').strip().lower()
        if not key:
            return False
        item = settings.resolve_arr_instance("sonarr", instance_key=key)
        if item is not None:
            return bool(item.get("is_4k", False))
        instance_id = str(getattr(self, 'instance_id', '') or '').strip().lower()
        return ("4k" in key) or key.endswith("_secondary") or instance_id.endswith(":secondary")

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
    # preferred placeholder folder for season-level placeholders
    placeholder_folder = Column(String, nullable=True)
    sonarrpath = Column(String, nullable=True)
    # ARR-provided synopsis/overview for series-level metadata
    sonarr_season_overview = Column(String, nullable=True)
    # Remote season poster URL from Sonarr (seasons[].images when includeSeasonImages is set)
    remote_poster = Column(String, nullable=True)
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
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=text('now()'))
    updated_at = Column(DateTime(timezone=True), server_default=text('now()'), onupdate=func.now())

    subflows = relationship('SubFlow', back_populates='season')
    series = relationship('Series', back_populates='season')
    episode = relationship('Episode', back_populates='season')

    def __repr__(self):
        return f"<Season(id={self.id}, title={self.title!r}, year={self.year})>"
class Episode(Base):
    __tablename__ = "episode"
    __table_args__ = (
        Index('ix_episode_determination', 'determination'),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    season_id = Column(Integer, ForeignKey('season.id'), nullable=False)
    # episode_number must NOT be unique across the whole table — uniqueness applies per season
    episode_number = Column(Integer, nullable=False, index=True)
    title = Column(String, nullable=False)
    year = Column(Integer, nullable=False)
    # preferred placeholder folder for episode-level placeholders
    placeholder_folder = Column(String, nullable=True)
    sonarrpath = Column(String, nullable=True)
    # Standardized episode content file path naming (previously episodefile_filepath)
    # Now named `sonarr_filepath` to make intent explicit (source: Sonarr)
    sonarr_filepath = Column(String, nullable=True)
    episodefile_size = Column(BigInteger, nullable=True)
    sonarr_runtime = Column(Integer, nullable=True)
    # Episode-level overview (if provided by Sonarr)
    sonarr_episode_overview = Column(String, nullable=True)
    sonarr_episode_tvdbid = Column(Integer, nullable=True)
    sonarr_episode_still = Column(String, nullable=True)
    sonarr_episode_directors = Column(JSON, nullable=True)
    sonarr_episode_credits = Column(JSON, nullable=True)
    sonarr_payload_raw = Column(JSON, nullable=True)
    sonarr_episodefile_payload_raw = Column(JSON, nullable=True)
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
    # Boolean to track if placeholder file physically exists (aggregated)
    has_placeholder = Column(Boolean, default=False)
    # Canonical observed placeholder file path for this episode (derived)
    placeholder_filepath = Column(String, nullable=True)
    sonarr_progress = Column(Integer, nullable=True, default=0)
    sonarr_status = Column(String, nullable=True)
    air_date = Column(Date, nullable=True)
    is_deleted = Column(Boolean, default=False)
    determination = Column(String, nullable=True)
    determination_updated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=text('now()'))
    updated_at = Column(DateTime(timezone=True), server_default=text('now()'), onupdate=func.now())
    # Last time this episode was observed in Sonarr
    last_found_in_sonarr = Column(DateTime(timezone=True), nullable=True)

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
            f"tvdbid={self.sonarr_episode_tvdbid})>"
        )
class LibraryRefreshThrottle(Base):
    """Concurrency lock and throttle state for media server library refreshes."""
    __tablename__ = "library_refresh_throttle"

    id = Column(Integer, primary_key=True, autoincrement=True)
    section_id = Column(Integer, nullable=False, unique=True, index=True)
    source = Column(String, nullable=True)
    acquired_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=text('now()'), onupdate=func.now())

    def __repr__(self):
        return f"<LibraryRefreshThrottle(section_id={self.section_id}, source={self.source!r})>"
