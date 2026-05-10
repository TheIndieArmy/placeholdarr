"""
Status Orchestrator: Central point for computing and applying display status changes.

The orchestrator decouples status computation from materialization, enabling:
- Calendar countdowns independent of file creation
- Queue monitor progress tracking without file recreation
- Event-driven status cleanup (import, delete, playback)
- Independent feature gates for each status source

Design principle:
  Lifecycle (file creation/deletion) is separate from status (user-facing text).
  The orchestrator receives status intents from multiple sources:
    1. Materializer: initial REQUEST status on new placeholders
    2. CalendarPhase: COMING_SOON countdowns and release transitions
    3. QueueMonitor: SEARCHING/DOWNLOADING/AVAILABLE transitions
    4. Event handlers: cleanup status after imports, deletes, playback
  
  All sources write to Placeholder.display_status via the same apply() pipeline.
"""

import logging
from typing import List, Dict, Optional
from datetime import date, datetime, timezone
from sqlalchemy.orm import Session

from core.config import settings
from services.source_of_truth.status_intent import StatusIntent, StatusSource, DisplayStatus
from services.postgres.models import Placeholder, Movie, Series, Season, Episode, EventLog
from services.postgres.db import get_session
from services.messages.context import build_projection_context_from_session
from services.status_projection import projected_status_display

logger = logging.getLogger(__name__)


def _is_coming_soon_status(value: str | None) -> bool:
    text = str(value or "").strip().upper()
    return text.startswith("COMING_SOON")


def _should_persist_status_history_event(*, source: str, old_status: str, new_status: str) -> bool:
    """Decide whether a status transition is user-meaningful for history UX."""
    if old_status == new_status:
        return False

    src = str(source or "").strip().lower()
    # Suppress calendar countdown churn; keep only release-day milestone.
    if src == StatusSource.CALENDAR_RELEASE_WINDOW.value:
        return _is_coming_soon_status(old_status) and new_status == DisplayStatus.REQUEST.value

    return True


def _runtime_minutes_for_placeholder(session: Session, placeholder: Placeholder) -> int | None:
    movie_id = getattr(placeholder, "movie_id", None)
    if movie_id is not None:
        movie = session.query(Movie).filter(Movie.id == int(movie_id)).first()
        if movie:
            try:
                runtime = int(getattr(movie, "radarr_runtime", 0) or 0)
                return runtime if runtime > 0 else None
            except Exception:
                return None
    episode_id = getattr(placeholder, "episode_id", None)
    if episode_id is not None:
        episode = session.query(Episode).filter(Episode.id == int(episode_id)).first()
        if episode:
            try:
                runtime = int(getattr(episode, "sonarr_runtime", 0) or 0)
                if runtime > 0:
                    return runtime
            except Exception:
                pass
            season = session.query(Season).filter(Season.id == episode.season_id).first()
            series = session.query(Series).filter(Series.id == season.series_id).first() if season else None
            if series:
                try:
                    runtime = int(getattr(series, "sonarr_runtime", 0) or 0)
                    return runtime if runtime > 0 else None
                except Exception:
                    return None
    return None


class StatusOrchestrator:
    """Orchestrates computation and application of status changes across all sources."""
    
    def __init__(self, session: Optional[Session] = None):
        """
        Initialize orchestrator with optional DB session.
        If not provided, a new session will be created for each operation.
        """
        self.session = session
    
    def _get_session(self) -> Session:
        """Get or create a DB session."""
        if self.session is None:
            return get_session().__enter__()
        return self.session

    def _compute_initial_creation_status(
        self,
        session: Session,
        placeholder: Placeholder,
    ) -> tuple[str, str, dict]:
        """Compute initial lifecycle status with calendar semantics.

        This keeps event-driven materialization aligned with calendar-phase logic so
        newly created future items are not stuck as REQUEST until a later calendar pass.
        """
        lookahead_days = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
        placeholders_enabled = bool(settings.coming_soon_placeholders_enabled)
        countdown_enabled = bool(getattr(settings, "ENABLE_COMING_SOON_COUNTDOWN", True))
        now_date = datetime.now(timezone.utc).date()

        media_type = None
        target_date = None
        has_file = False

        if getattr(placeholder, "movie_id", None):
            movie = session.get(Movie, int(placeholder.movie_id))
            if movie:
                media_type = "movie"
                has_file = bool(getattr(movie, "has_file", False))
                preferred = str(getattr(settings, "PREFERRED_MOVIE_DATE_TYPE", "inCinemas") or "inCinemas").strip()
                release_map = {
                    "inCinemas": "theater_release_date",
                    "digitalRelease": "digital_release_date",
                    "physicalRelease": "physical_release_date",
                }
                target_date = getattr(movie, release_map.get(preferred, "theater_release_date"), None)

        elif getattr(placeholder, "episode_id", None):
            episode = session.get(Episode, int(placeholder.episode_id))
            if episode:
                media_type = "episode"
                has_file = bool(getattr(episode, "has_file", False))
                target_date = getattr(episode, "air_date", None)

        if not media_type:
            return (
                DisplayStatus.REQUEST.value,
                "Initial status on placeholder creation",
                {"event_type": "creation", "reason": "unlinked_placeholder"},
            )

        if has_file:
            return (
                DisplayStatus.AVAILABLE.value,
                "Media file is available",
                {"event_type": "creation", "media_type": media_type, "days_until_release": None},
            )

        if not target_date:
            return (
                DisplayStatus.REQUEST.value,
                "No release date available",
                {"event_type": "creation", "media_type": media_type, "days_until_release": None},
            )

        # Ensure target_date is a date object (not datetime) for safe subtraction
        if hasattr(target_date, "date") and not isinstance(target_date, date):
            target_date = target_date.date()

        days_until = (target_date - now_date).days
        logger.debug(
            f"Computing initial creation status for Placeholder[{placeholder.id}]: "
            f"target_date={target_date} ({type(target_date)}), "
            f"now_date={now_date} ({type(now_date)}), "
            f"days_until={days_until}, "
            f"lookahead_days={lookahead_days}, "
            f"placeholders_enabled={placeholders_enabled}"
        )
        if not placeholders_enabled:
            return (
                DisplayStatus.REQUEST.value,
                "Calendar placeholders disabled",
                {"event_type": "creation", "media_type": media_type, "days_until_release": days_until},
            )

        if lookahead_days == 0:
            return (
                DisplayStatus.REQUEST.value,
                "Calendar lookahead disabled",
                {"event_type": "creation", "media_type": media_type, "days_until_release": days_until},
            )

        if days_until < 0:
            return (
                DisplayStatus.REQUEST.value,
                "Release date passed; waiting for import",
                {"event_type": "creation", "media_type": media_type, "days_until_release": days_until},
            )

        if lookahead_days > 0 and days_until > lookahead_days:
            return (
                DisplayStatus.REQUEST.value,
                f"Outside lookahead window ({lookahead_days} days)",
                {"event_type": "creation", "media_type": media_type, "days_until_release": days_until},
            )

        if not countdown_enabled:
            label = "Airing soon" if media_type == "episode" else "Coming Soon"
            return (
                DisplayStatus.COMING_SOON.value,
                label,
                {"event_type": "creation", "media_type": media_type, "days_until_release": days_until},
            )

        if days_until == 0:
            return (
                DisplayStatus.COMING_SOON_TODAY.value,
                "Airing today" if media_type == "episode" else "Coming Soon (Today)",
                {"event_type": "creation", "media_type": media_type, "days_until_release": days_until},
            )
        if days_until <= 6:
            status = DisplayStatus.COMING_SOON_1.value
        elif days_until <= 13:
            status = DisplayStatus.COMING_SOON_7.value
        elif days_until <= 29:
            status = DisplayStatus.COMING_SOON_14.value
        else:
            status = DisplayStatus.COMING_SOON_30.value

        reason = "Airing in 1 day" if media_type == "episode" and days_until == 1 else (
            f"Airing in {days_until} days" if media_type == "episode" else (
                "Coming Soon (1 day)" if days_until == 1 else f"Coming Soon ({days_until} days)"
            )
        )
        return (
            status,
            reason,
            {"event_type": "creation", "media_type": media_type, "days_until_release": days_until},
        )
    
    # =========================================================================
    # COMPUTE PHASES: Various sources compute status intents
    # =========================================================================
    
    def compute_status_for_lifecycle_event(
        self,
        placeholder_id: int,
        event_type: str = "creation",
    ) -> Optional[StatusIntent]:
        """
        Materializer calls this after creating or updating a placeholder file.
        
        Args:
            placeholder_id: Placeholder row ID
            event_type: "creation" | "update" | "deletion"
        
        Returns:
            StatusIntent or None if no status change needed
        """
        session = self._get_session()
        
        if event_type == "creation":
            placeholder = session.query(Placeholder).filter_by(id=placeholder_id).first()
            if not placeholder:
                return None

            initial_status, reason, metadata = self._compute_initial_creation_status(session, placeholder)
            return StatusIntent(
                placeholder_id=placeholder_id,
                new_status=initial_status,
                reason=reason,
                source=StatusSource.INITIAL_CREATION,
                # Materializer writes NFO before status is finalized; refresh so
                # sidecars reflect the computed initial lifecycle status.
                trigger_nfo_refresh=True,
                metadata=metadata,
            )
        
        elif event_type == "deletion":
            # Placeholder file deleted: mark as deleted in DB, stop displaying
            return StatusIntent(
                placeholder_id=placeholder_id,
                new_status=DisplayStatus.DELETED.value,
                reason="Placeholder file deleted",
                source=StatusSource.LIFECYCLE_DELETION,
                trigger_nfo_refresh=False,  # No NFO for deleted items
                metadata={"event_type": "deletion"},
            )
        
        elif event_type == "update":
            # Update after materialization: no status change needed (keep existing)
            return None
        
        return None
    
    def compute_status_for_calendar_phase(self) -> Dict[int, StatusIntent]:
        """
        CalendarPhase calls this to compute countdown statuses for all placeholders with air/release dates.
        
        Returns:
            Dict mapping Placeholder.id -> StatusIntent for changed statuses only
        """
        session = self._get_session()
        intents: Dict[int, StatusIntent] = {}
        
        # Query all placeholders with air/release dates and non-terminal statuses
        now = datetime.now(timezone.utc)
        
        placeholders = session.query(Placeholder).filter(
            Placeholder.has_placeholder == True,
            Placeholder.display_status.notin_([
                DisplayStatus.AVAILABLE.value,
                DisplayStatus.DELETED.value,
                DisplayStatus.ARCHIVED.value,
            ]),
        ).all()
        
        for ph in placeholders:
            # Compute countdown based on air_date or release_date
            release_date = None
            if hasattr(ph, 'air_date') and ph.air_date:
                release_date = ph.air_date
            elif hasattr(ph, 'release_date') and ph.release_date:
                release_date = ph.release_date
            
            if not release_date:
                continue  # No date known, can't compute countdown
            
            # Normalize to naive or aware consistently
            if release_date.tzinfo is None:
                release_date = release_date.replace(tzinfo=timezone.utc)
            
            days_until = (release_date.date() - now.date()).days
            
            # Compute new status based on days until release
            if days_until < 0:
                # Release date has passed; check if content became available
                # If available_on_media_server, status should be AVAILABLE
                # Otherwise, revert to REQUEST for user to search
                new_status = DisplayStatus.AVAILABLE.value  # Placeholder for real logic
                reason = "Release date passed"
            elif days_until == 0:
                new_status = DisplayStatus.COMING_SOON_TODAY.value
                reason = "Release date is today"
            elif days_until == 1:
                new_status = DisplayStatus.COMING_SOON_1.value
                reason = f"Releasing in {days_until} day"
            elif days_until <= 6:
                new_status = DisplayStatus.COMING_SOON_1.value
                reason = f"Releasing in {days_until} days"
            elif days_until <= 13:
                new_status = DisplayStatus.COMING_SOON_7.value
                reason = f"Releasing in {days_until} days"
            elif days_until <= 29:
                new_status = DisplayStatus.COMING_SOON_14.value
                reason = f"Releasing in {days_until} days"
            else:
                new_status = DisplayStatus.COMING_SOON_30.value
                reason = f"Releasing in {days_until} days"
            
            # Only create intent if status would actually change
            if new_status != ph.display_status:
                intents[ph.id] = StatusIntent(
                    placeholder_id=ph.id,
                    new_status=new_status,
                    reason=reason,
                    source=StatusSource.CALENDAR_RELEASE_WINDOW,
                    trigger_nfo_refresh=True,  # Countdown changes should update NFO
                    metadata={
                        "days_until_release": days_until,
                        "release_date": release_date.isoformat(),
                    },
                )
        
        return intents
    
    def compute_status_for_queue_progress(
        self,
        placeholder_id: int,
        queue_status: str,
        progress_info: Optional[Dict] = None,
    ) -> Optional[StatusIntent]:
        """
        QueueMonitor calls this to report search/download progress.
        
        Args:
            placeholder_id: Placeholder row ID
            queue_status: "searching" | "downloading" | "available" | "failed"
            progress_info: Optional dict with {episode_count, total_episodes, eta_hours, etc.}
        
        Returns:
            StatusIntent to update progress or status, or None if no change needed
        """
        session = self._get_session()
        ph = session.query(Placeholder).filter_by(id=placeholder_id).first()
        
        if not ph:
            logger.warning(f"Placeholder {placeholder_id} not found for queue progress update")
            return None
        
        # Map queue status to display status
        status_map = {
            "searching": DisplayStatus.SEARCHING.value,
            "downloading": DisplayStatus.DOWNLOADING.value,
            "available": DisplayStatus.AVAILABLE.value,
            "failed": DisplayStatus.REQUEST.value,  # Revert to REQUEST if search/download failed
        }
        
        new_status = status_map.get(queue_status, ph.display_status)
        
        if new_status == ph.display_status:
            return None  # No status change, but could update progress
        
        return StatusIntent(
            placeholder_id=placeholder_id,
            new_status=new_status,
            reason=f"Queue monitor: {queue_status}",
            source=StatusSource.QUEUE_MONITOR,
            progress=progress_info.get("progress_text") if progress_info else None,
            # Keep Emby/Jellyfin sidecars in sync for all queue-driven transitions.
            trigger_nfo_refresh=True,
            metadata=progress_info or {},
        )
    
    def compute_status_for_import_cleanup(
        self,
        entity_type: str,  # "movie" | "series" | "season" | "episode"
        entity_id: int,
    ) -> List[StatusIntent]:
        """
        ImportHandler calls this after a file import completes.
        
        Updates status for the imported entity and potentially related entities.
        """
        session = self._get_session()
        intents: List[StatusIntent] = []
        
        # Find all placeholders related to this entity
        placeholders = session.query(Placeholder).filter(
            getattr(Placeholder, f"{entity_type}_id") == entity_id
        ).all()
        
        for ph in placeholders:
            # If content became available (file imported), mark as AVAILABLE
            # Clear queue-like or "not found" status from before the import
            if ph.display_status in {
                DisplayStatus.SEARCHING.value,
                DisplayStatus.DOWNLOADING.value,
                DisplayStatus.NOT_FOUND.value,
            }:
                intents.append(StatusIntent(
                    placeholder_id=ph.id,
                    new_status=DisplayStatus.AVAILABLE.value,
                    reason=f"File imported for {entity_type}",
                    source=StatusSource.EVENT_IMPORT_CLEANUP,
                    trigger_nfo_refresh=True,
                    metadata={"entity_type": entity_type, "entity_id": entity_id},
                ))
        
        return intents
    
    def compute_status_for_delete_event(
        self,
        delete_type: str,  # "file_deleted" | "entity_deleted"
        entity_type: str,  # "movie" | "series" | "season" | "episode"
        entity_id: int,
    ) -> List[StatusIntent]:
        """
        DeleteHandler calls this to handle file or entity deletion.
        """
        session = self._get_session()
        intents: List[StatusIntent] = []
        
        if delete_type == "file_deleted":
            # File was deleted but entity still exists in ARR
            # Placeholder should be recreated, status should revert to REQUEST/COMING_SOON
            placeholders = session.query(Placeholder).filter(
                getattr(Placeholder, f"{entity_type}_id") == entity_id
            ).all()
            
            for ph in placeholders:
                intents.append(StatusIntent(
                    placeholder_id=ph.id,
                    new_status=DisplayStatus.REQUEST.value,
                    reason=f"File deleted, {entity_type} still in ARR",
                    source=StatusSource.EVENT_DELETE_FILE,
                    trigger_nfo_refresh=True,
                    metadata={"entity_type": entity_type, "entity_id": entity_id},
                ))
        
        elif delete_type == "entity_deleted":
            # Entity removed from ARR entirely
            # Mark placeholder as deleted, prevent rehydration
            placeholders = session.query(Placeholder).filter(
                getattr(Placeholder, f"{entity_type}_id") == entity_id
            ).all()
            
            for ph in placeholders:
                intents.append(StatusIntent(
                    placeholder_id=ph.id,
                    new_status=DisplayStatus.DELETED.value,
                    reason=f"{entity_type} deleted from ARR",
                    source=StatusSource.EVENT_DELETE_ENTITY,
                    trigger_nfo_refresh=False,
                    metadata={"entity_type": entity_type, "entity_id": entity_id},
                ))
        
        return intents
    
    # =========================================================================
    # APPLY PHASE: Write intents to DB, NFO, and media servers
    # =========================================================================
    
    def apply_status_intent(self, intent: StatusIntent) -> bool:
        """
        Apply a single status intent to the database.
        
        Returns True if successfully applied, False if skipped/failed.
        """
        session = self._get_session()
        
        try:
            ph = session.query(Placeholder).filter_by(id=intent.placeholder_id).first()
            if not ph:
                logger.warning(f"Placeholder {intent.placeholder_id} not found, skipping status intent")
                return False
            
            # Check precondition if specified
            if intent.condition_current_status and ph.display_status != intent.condition_current_status:
                logger.debug(
                    f"Skipping status intent for {intent.placeholder_id}: "
                    f"condition not met (current={ph.display_status!r}, "
                    f"expected={intent.condition_current_status!r})"
                )
                return False
            
            # Update DB
            old_status = ph.display_status
            ph.display_status = intent.new_status
            ph.display_reason = intent.reason
            _rm = _runtime_minutes_for_placeholder(session, ph)
            _media_ctx = build_projection_context_from_session(
                session,
                movie_id=getattr(ph, "movie_id", None),
                episode_id=getattr(ph, "episode_id", None),
                runtime_minutes=_rm,
            )
            ph.display_status_projected = projected_status_display(
                intent.new_status,
                reason=intent.reason,
                runtime_minutes=_rm,
                media_context=_media_ctx,
            )
            if intent.progress is not None:
                try:
                    ph.display_progress = int(intent.progress)
                except Exception:
                    ph.display_progress = None
            elif intent.new_status != DisplayStatus.DOWNLOADING.value:
                ph.display_progress = None
            ph.updated_at = datetime.now(timezone.utc)

            old_status_text = str(old_status or "")
            new_status_text = str(intent.new_status or "")
            source_text = str(intent.source.value)

            # Persist only user-meaningful transitions in placeholder history.
            if _should_persist_status_history_event(
                source=source_text,
                old_status=old_status_text,
                new_status=new_status_text,
            ):
                history_reason = str(intent.reason or "")
                if source_text == StatusSource.CALENDAR_RELEASE_WINDOW.value:
                    history_reason = "Reached release day"
                session.add(
                    EventLog(
                        event_type="placeholder_status_changed",
                        source=source_text,
                        payload={
                            "placeholder_id": int(ph.id),
                            "old_status": old_status_text,
                            "new_status": new_status_text,
                            "reason": history_reason,
                        },
                        status="DONE",
                        attempts=0,
                        max_attempts=0,
                        updated_at=ph.updated_at,
                        processed_at=ph.updated_at,
                    )
                )
            
            session.commit()
            
            logger.info(
                f"Applied status intent: Placeholder[{intent.placeholder_id}] "
                f"{old_status!r} -> {intent.new_status!r} "
                f"(source={intent.source.value}, reason={intent.reason!r})"
            )
            
            return True
        
        except Exception as e:
            logger.error(f"Failed to apply status intent {intent}: {e}", exc_info=True)
            session.rollback()
            return False
    
    def apply_status_intents(self, intents: List[StatusIntent]) -> int:
        """
        Apply multiple status intents in batch.
        
        Returns: Number of intents successfully applied.
        """
        session = self._get_session()
        applied = 0
        
        for intent in intents:
            if self.apply_status_intent(intent):
                applied += 1
        
        return applied
    
    def apply_and_project_statuses(self, intents: List[StatusIntent]) -> int:
        """
        Apply status intents AND trigger downstream projection to media servers.
        
        This is the full three-stage pipeline:
          1. DB write (apply_status_intents)
          2. NFO refresh (external trigger)
          3. Media server projection (enqueue projection job)
        
        Returns: Number of intents successfully applied.
        """
        session = self._get_session()
        
        # Stage 1: Apply to DB
        applied = self.apply_status_intents(intents)
        
        if applied == 0:
            return 0
        
        # Stage 2: Trigger NFO refresh for intents that request it
        nfo_refresh_ids = [
            intent.placeholder_id
            for intent in intents
            if intent.trigger_nfo_refresh
        ]
        nfo_refresh_ids = list(dict.fromkeys(int(pid) for pid in nfo_refresh_ids if pid is not None))
        
        if nfo_refresh_ids:
            logger.debug(f"Triggering NFO refresh for {len(nfo_refresh_ids)} placeholders")
            from services.source_of_truth.status_reconciler import enqueue_nfo_refresh

            player_refresh: dict[int, bool] = {}
            for intent in intents:
                if not intent.trigger_nfo_refresh:
                    continue
                if intent.placeholder_id is None:
                    continue
                pid = int(intent.placeholder_id)
                want = intent.wants_player_metadata_refresh_after_nfo()
                player_refresh[pid] = player_refresh.get(pid, False) or want
            try:
                enqueue_nfo_refresh(nfo_refresh_ids, session=session, player_metadata_refresh=player_refresh)
            except Exception as e:
                logger.warning(f"Failed to enqueue NFO refresh: {e}", exc_info=True)
        
        return applied
