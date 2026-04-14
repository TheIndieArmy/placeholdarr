"""
Status Intent: A formalized request to change placeholder display status.

StatusIntent represents a request to update Placeholder.display_status and related fields.
It is the unit of work passed between status-compute phases and the status-apply orchestrator.
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
from datetime import datetime


class StatusSource(Enum):
    """Where did this status change request originate?"""
    INITIAL_CREATION = "initial_creation"              # Materializer: new placeholder created
    CALENDAR_RELEASE_WINDOW = "calendar_release_window"  # CalendarPhase: countdown or release-day transition
    QUEUE_MONITOR = "queue_monitor_progress"           # QueueMonitorPhase: search/download progress
    EVENT_IMPORT_CLEANUP = "event_import_cleanup"       # ImportHandler: import completed
    EVENT_DELETE_FILE = "event_delete_file"             # DeleteHandler: file was deleted, recreate placeholder
    EVENT_DELETE_ENTITY = "event_delete_entity"         # DeleteHandler: entity deleted from ARR
    EVENT_PLAYBACK_STARTED = "event_playback_started"   # PlaybackHandler: user started playback
    MANUAL_OVERRIDE = "manual_override"                 # Admin/CLI: manual status change
    LIFECYCLE_DELETION = "lifecycle_deletion"           # Materializer: placeholder deleted (soft delete)


class DisplayStatus(Enum):
    """Canonical user-facing status values."""
    # Lifecycle-initial states
    REQUEST = "REQUEST"                           # User added content, no release date yet
    COMING_SOON = "COMING_SOON"                   # Known release date in future (generic)
    
    # Calendar countdown variants
    COMING_SOON_30 = "COMING_SOON_30"            # 30+ days until release
    COMING_SOON_14 = "COMING_SOON_14"            # 14-29 days until release
    COMING_SOON_7 = "COMING_SOON_7"              # 7-13 days until release
    COMING_SOON_1 = "COMING_SOON_1"              # 1-6 days until release
    COMING_SOON_TODAY = "COMING_SOON_TODAY"      # Release date is today
    
    # Action states
    SEARCHING = "SEARCHING"                       # Queue monitor: actively searching for media
    DOWNLOADING = "DOWNLOADING"                   # Queue monitor: download in progress
    IMPORT_IN_PROGRESS = "IMPORT_IN_PROGRESS"    # ImportHandler: file being imported to media server
    
    # Resolved states
    AVAILABLE = "AVAILABLE"                       # File exists in media server
    CLEANUP = "CLEANUP"                           # File marked for cleanup (transitional)
    
    # Terminal states
    DELETED = "DELETED"                           # Content removed from ARR
    ARCHIVED = "ARCHIVED"                         # User archived/ignored content


@dataclass
class StatusIntent:
    """
    A request to update a placeholder's display status.
    
    This is the canonical unit of work for the status orchestrator.
    All status changes must flow through this dataclass to enable:
    - Traceability (source field)
    - Idempotency (reason field for deduplication)
    - Conditional application (conditions field)
    - Optional cascading operations (trigger_* fields)
    """
    
    placeholder_id: int
    new_status: str  # DisplayStatus enum value (or string for flexibility)
    reason: str      # Human-readable explanation for audit logs
    source: StatusSource
    
    # Optional: descriptive progress indicator (e.g., "2/5 episodes downloading", "24 hours until release")
    progress: Optional[str] = None
    
    # Optional: if True, placeholder NFO should be rewritten to reflect new status
    trigger_nfo_refresh: bool = False
    
    # Optional: if specified, only apply this intent if DB value matches this condition
    # Used to prevent race conditions (e.g., only apply if current_status == "REQUEST")
    condition_current_status: Optional[str] = None
    
    # Optional: arbitrary metadata for debugging/observability
    metadata: dict = field(default_factory=dict)
    
    # Timestamp when this intent was created (for ordering/deduplication)
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    def __repr__(self) -> str:
        return (
            f"StatusIntent(placeholder_id={self.placeholder_id}, "
            f"new_status={self.new_status!r}, source={self.source.value!r})"
        )
    
    def is_terminal(self) -> bool:
        """Check if this status represents a terminal state."""
        terminal_statuses = {
            DisplayStatus.DELETED.value,
            DisplayStatus.ARCHIVED.value,
            DisplayStatus.AVAILABLE.value,
        }
        return self.new_status in terminal_statuses
    
    def should_skip_nfo_refresh(self) -> bool:
        """
        Most status changes need NFO refresh for [REQUEST] markers etc.
        Some statuses (like AVAILABLE or DELETED) don't need NFO updates.
        """
        if self.new_status in {DisplayStatus.AVAILABLE.value, DisplayStatus.DELETED.value}:
            return True
        # Caller can override with trigger_nfo_refresh=True if needed
        return False

    def wants_player_metadata_refresh_after_nfo(self) -> bool:
        """Whether to run post-NFO player status projection.

        Initial materialization that settles on plain REQUEST already wrote a
        REQUEST-flavored NFO; a broad library/path refresh handles discovery.
        All other NFO-driving status changes benefit from direct title/summary
        projection so bracketed status text updates quickly in client UIs.
        """
        if not self.trigger_nfo_refresh:
            return False
        if self.source == StatusSource.INITIAL_CREATION and self.new_status == DisplayStatus.REQUEST.value:
            return False
        return True
