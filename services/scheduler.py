import logging
import os
import traceback
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Union, Type
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from sqlalchemy import or_, and_, func
from services.postgres.db import get_session, db_session_scope, db_operation_with_retry, db_batch_scope
from services.postgres.models import Movie, Series, Season, Episode, SubFlow
from services.flow_manager import flow_manager
from core.config import settings
from core.handler_logging import end_handler_logging, get_handler_session_for_entity
from importlib import import_module
from sqlalchemy.exc import SQLAlchemyError
from core.logger import logger

# Legacy logger setup for backwards compatibility
tlogger = logging.getLogger('scheduler')
tlogger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
tlogger.addHandler(ch)

class ActionScheduler:
    def __init__(
        self,
        action: str,
        poll_interval: int = 5,
        max_retries: int = 3,
        max_workers: int = 2
    ):
        logger.info(f"Initializing ActionScheduler for action '{action}'", extra={'emoji_type': 'processing'})
        self.action = action
        self.model = None
        self.max_retries = max_retries
        logger.verbose(f"Scheduler settings: poll_interval={poll_interval}s, max_retries={max_retries}, max_workers={max_workers}", extra={'emoji_type': 'debug'})
        
        executors = {'default': ThreadPoolExecutor(max_workers), 'plex': ThreadPoolExecutor(1), 'jellyfin': ThreadPoolExecutor(1)}
        job_defaults = {'max_instances': 1, 'coalesce': True}
        self.scheduler = BackgroundScheduler(executors=executors, job_defaults=job_defaults)
        
        logger.verbose(f"Adding polling job with {poll_interval}s interval", extra={'emoji_type': 'debug'})
        self.scheduler.add_job(
            self.poll_and_enqueue,
            'interval',
            seconds=poll_interval,
            id=f'poll_{action}'
        )
        
        # Daily retry of failed subflows
        if settings.SCHEDULED_TIME_FAILED:
            hh, mm = map(int, settings.SCHEDULED_TIME_FAILED.split(':'))
            logger.verbose(f"Scheduling daily retry at {hh:02d}:{mm:02d} for failed subflows", extra={'emoji_type': 'clock'})
            self.scheduler.add_job(
                self.retry_failed_subflows,
                'cron',
                hour=hh,
                minute=mm,
                id=f'retry_failed_{action}'
            )
        else:
            logger.debug("No scheduled retry time configured", extra={'emoji_type': 'debug'})
        
        logger.verbose(f"Scheduler for '{action}' initialized successfully", extra={'emoji_type': 'success'})

    def start(self):
        logger.info(f"Starting scheduler for action '{self.action}'", extra={'emoji_type': 'start'})
        try:
            self.scheduler.start()
            logger.info(f"Scheduler for '{self.action}' started successfully", extra={'emoji_type': 'success'})
        except Exception as e:
            logger.error(f"Failed to start scheduler for '{self.action}': {e}", extra={'emoji_type': 'error'})

    def poll_and_enqueue(self):
        logger.verbose(f"Polling for subflows - action: {self.action}", extra={'emoji_type': 'search'})
        
        # First, check for stalled progressions due to scheduler congestion
        self._detect_and_fix_stalled_progressions()
        
        def get_pending_subflows():
            with db_session_scope() as session:
                # Process a batch of SubFlows per poll to increase throughput.
                batch_size = getattr(settings, 'SCHEDULER_BATCH_SIZE', 8)
                sfs = (
                    session.query(SubFlow)
                    .with_for_update(skip_locked=True)
                    .filter(
                        SubFlow.status.in_(["PENDING", "FAILED"]),
                        func.coalesce(SubFlow.retry_count, 0) < self.max_retries,
                        SubFlow.steps.isnot(None),
                        SubFlow.steps != '',
                        SubFlow.action == self.action,
                    )
                    .order_by(SubFlow.id)
                    .limit(batch_size)
                    .all()
                )

                if not sfs:
                    logger.verbose(f"No pending/failed subflows found for action '{self.action}'", extra={'emoji_type': 'debug'})
                    return []

                logger.verbose(f"Found {len(sfs)} subflow(s) to process for action '{self.action}'", extra={'emoji_type': 'processing'})

                # Prepare scheduling plan: mark as QUEUED within the transaction, capture next step info
                schedule_plan = []
                for sf in sfs:
                    steps = sf.steps.split(',')
                    if sf.step_index >= len(steps):
                        sf.status = 'DONE'
                        logger.verbose(f"SubFlow {sf.id} marked as complete - all steps finished", extra={'emoji_type': 'success'})
                        continue

                    next_func_name = steps[sf.step_index]
                    logger.verbose(f"Next step for subflow {sf.id}: {next_func_name} (step {sf.step_index + 1}/{len(steps)})", extra={'emoji_type': 'step'})
                    # Don't mark as QUEUED yet - keep as PENDING until job actually starts
                    schedule_plan.append((sf.id, next_func_name, sf.episode_id))

                # Don't bulk update status to QUEUED - leave as PENDING until job execution starts
                
                return schedule_plan

        try:
            # Get subflows to process with database retry
            schedule_plan = db_operation_with_retry(get_pending_subflows)
            
            if not schedule_plan:
                return

            # Schedule each planned subflow outside the transaction
            for sf_id, next_func_name, episode_id in schedule_plan:
                logger.verbose(f"Scheduling subflow {sf_id} step: {next_func_name}", extra={'emoji_type': 'schedule'})
                try:
                    self._schedule_subflow(sf_id, self._get_flow_function(next_func_name), episode_id)
                except Exception as e:
                    logger.error(f"Failed to schedule subflow {sf_id} step {next_func_name}: {e}", extra={'emoji_type': 'error'})

        except Exception as e:
            logger.error(f"poll_and_enqueue error for action '{self.action}': {e}", extra={'emoji_type': 'error'})

    def retry_failed_subflows(self):
        """
        Runs once daily to retry subflows that failed after max_retries.
        """
        logger.info(f"Starting daily retry of failed subflows for action '{self.action}'", extra={'emoji_type': 'retry'})
        session = get_session()
        try:
            failed = session.query(SubFlow).filter(
                SubFlow.status == 'FAILED',
                func.coalesce(SubFlow.retry_count, 0) >= self.max_retries
            ).all()
            
            logger.verbose(f"Found {len(failed)} failed subflows to retry for action '{self.action}'", extra={'emoji_type': 'processing'})
            
            retry_count = 0
            for sf in failed:
                try:
                    logger.verbose(f"Retrying subflow {sf.id} (was failed with {sf.retry_count} retries)", extra={'emoji_type': 'retry'})
                    sf.status = 'PENDING'  # Reset to PENDING, will be QUEUED when execution starts
                    sf.retry_count = 0
                    session.add(sf)
                    
                    # Get the current step to retry
                    steps = sf.steps.split(',')
                    if sf.step_index < len(steps):
                        current_step = steps[sf.step_index]
                        self._schedule_subflow(sf.id, self._get_flow_function(current_step), sf.episode_id)
                        retry_count += 1
                        logger.verbose(f"Rescheduled subflow {sf.id} step: {current_step}", extra={'emoji_type': 'schedule'})
                    else:
                        logger.warning(f"Subflow {sf.id} has invalid step_index {sf.step_index} >= {len(steps)}", extra={'emoji_type': 'warning'})
                        
                except Exception as step_error:
                    logger.error(f"Failed to retry subflow {sf.id}: {step_error}", extra={'emoji_type': 'error'})
                    
            session.commit()
            logger.verbose(f"Successfully retried {retry_count} failed subflows for action '{self.action}'", extra={'emoji_type': 'success'})
            
        except Exception as e:
            logger.error(f"retry_failed_subflows error for action '{self.action}': {e}", extra={'emoji_type': 'error'})
            session.rollback()
        finally:
            session.close()

    def _reset_failed_subflow(self, sf_id: int):
        """
        Reset a failed subflow to retry it from the last step index.
        """
        logger.info(f"Attempting to reset failed SubFlow {sf_id} for retry", extra={'emoji_type': 'retry'})
        session = get_session()
        try:
            sf = session.query(SubFlow).filter(SubFlow.id == sf_id).first()
            if not sf:
                logger.warning(f"SubFlow {sf_id} not found for reset", extra={'emoji_type': 'warning'})
                return
                
            if sf.status != 'FAILED':
                logger.verbose(f"SubFlow {sf_id} status is {sf.status}, not resetting", extra={'emoji_type': 'debug'})
                return
                
            # Reset retry count and status to allow retry
            old_retry_count = sf.retry_count
            sf.retry_count = 0
            sf.status = 'PENDING'
            sf.error_message = None
            
            session.add(sf)
            session.commit()
            
            logger.verbose(f"Reset SubFlow {sf_id}: retry_count {old_retry_count}→0, status FAILED→PENDING", extra={'emoji_type': 'success'})
            
        except Exception as e:
            logger.error(f"Failed to reset SubFlow {sf_id}: {e}", extra={'emoji_type': 'error'})
            session.rollback()
        finally:
            session.close()

    def _detect_and_fix_stalled_progressions(self):
        """
        Detect and fix ALL types of stalled progressions across ALL actions and steps.
        This is a comprehensive solution that prevents entities from getting stuck at any stage.
        
        Stalls can happen when:
        1. SubFlows complete but fail to advance to next step due to scheduler congestion
        2. All SubFlows for an entity complete but entity advancement fails
        3. Entities have mismatched status vs their actual SubFlow completions
        """
        
        try:
            with db_session_scope() as session:
                stall_fixes = 0
                # --- Detect stalled QUEUED subflows ---
                # Get all SubFlows with status QUEUED for this action
                queued_subflows = session.query(SubFlow).filter(
                    SubFlow.action == self.action,
                    SubFlow.status == 'QUEUED'
                ).all()

                # Get all job IDs from the scheduler
                scheduler_job_ids = set(job.id for job in self.scheduler.get_jobs())

                # For each QUEUED SubFlow, check if a corresponding job exists in the scheduler
                import datetime
                stalled_queued = []
                for sf in queued_subflows:
                    job_id_prefix = f"{self.action}_{sf.id}_"
                    found = any(jid.startswith(job_id_prefix) for jid in scheduler_job_ids)
                    # Check if QUEUED for more than 5 minutes
                    time_threshold = datetime.datetime.now() - datetime.timedelta(minutes=5)
                    if sf.created_time and sf.created_time < time_threshold:
                        # Mark as FAILED and remove job if exists
                        sf.status = 'FAILED'
                        sf.error_message = 'SubFlow QUEUED for over 5 minutes, marked as FAILED by scheduler stall detection.'
                        session.add(sf)
                        stall_fixes += 1
                        logger.warning(f"SubFlow {sf.id} QUEUED for over 5 minutes, marking as FAILED", extra={'emoji_type': 'warning'})
                        # Remove job from scheduler if present
                        for job_id in scheduler_job_ids:
                            if job_id.startswith(job_id_prefix):
                                self.scheduler.remove_job(job_id)
                                logger.info(f"Removed stalled job {job_id} from scheduler", extra={'emoji_type': 'repair'})
                    elif not found:
                        stalled_queued.append(sf)

                if stalled_queued:
                    logger.warning(f"Detected {len(stalled_queued)} stalled QUEUED subflows for action '{self.action}' (QUEUED in DB but not in scheduler)", extra={'emoji_type': 'warning'})
                    for sf in stalled_queued:
                        logger.warning(f"Stalled QUEUED SubFlow: id={sf.id}, entity={{'movie_id': sf.movie_id, 'series_id': sf.series_id, 'episode_id': sf.episode_id}}, step_index={sf.step_index}", extra={'emoji_type': 'warning'})
                        # Optionally, reset status to PENDING for retry
                        sf.status = 'PENDING'
                        session.add(sf)
                        stall_fixes += 1
                else:
                    logger.verbose(f"No stalled QUEUED subflows detected for action '{self.action}'", extra={'emoji_type': 'debug'})
                # 1. Find SubFlows that completed all steps but didn't trigger entity advancement
                completed_subflows = session.query(SubFlow).filter(
                    SubFlow.action == self.action,
                    SubFlow.status == 'DONE'
                ).all()
                
                logger.verbose(f"Checking {len(completed_subflows)} completed SubFlows for stalled entity advancement", extra={'emoji_type': 'debug'})
                
                # Group by entity to check if all SubFlows are done for each entity
                entities_to_check = {}
                for sf in completed_subflows:
                    # Determine entity type and ID
                    if sf.movie_id is not None:
                        entity_key = ('Movie', sf.movie_id)
                    elif sf.series_id is not None and sf.episode_id is None:
                        entity_key = ('Series', sf.series_id) 
                    elif sf.episode_id is not None:
                        entity_key = ('Episode', sf.episode_id)
                    else:
                        continue
                        
                    if entity_key not in entities_to_check:
                        entities_to_check[entity_key] = []
                    entities_to_check[entity_key].append(sf)
                
                # Check each entity to see if it needs advancement
                for (entity_type, entity_id), entity_subflows in entities_to_check.items():
                    try:
                        # Get the entity model class
                        if entity_type == 'Movie':
                            from services.postgres.models import Movie
                            entity_model = Movie
                        elif entity_type == 'Series':
                            from services.postgres.models import Series
                            entity_model = Series
                        elif entity_type == 'Episode':
                            from services.postgres.models import Episode
                            entity_model = Episode
                        else:
                            continue
                            
                        # Check if entity exists and get its current status
                        entity = session.query(entity_model).get(entity_id)
                        if not entity:
                            continue
                            
                        # Find ALL SubFlows for this entity and action (including pending/failed)
                        if entity_type == 'Movie':
                            all_subflows = session.query(SubFlow).filter(
                                SubFlow.movie_id == entity_id,
                                SubFlow.action == self.action,
                                SubFlow.status != 'CANCELLED'
                            ).all()
                        elif entity_type == 'Series':
                            all_subflows = session.query(SubFlow).filter(
                                SubFlow.series_id == entity_id,
                                SubFlow.episode_id.is_(None),  # Series-level SubFlows only
                                SubFlow.action == self.action,
                                SubFlow.status != 'CANCELLED'
                            ).all()
                        elif entity_type == 'Episode':
                            all_subflows = session.query(SubFlow).filter(
                                SubFlow.episode_id == entity_id,
                                SubFlow.action == self.action,
                                SubFlow.status != 'CANCELLED'
                            ).all()
                        else:
                            continue
                            
                        # Check if ALL SubFlows are complete
                        pending_subflows = [sf for sf in all_subflows if sf.status in ['PENDING', 'QUEUED', 'FAILED']]
                        
                        if not pending_subflows:
                            # All SubFlows are DONE - entity should be advanced
                            logger.verbose(f"All SubFlows complete for {entity_type} {entity_id}, checking if entity needs advancement", extra={'emoji_type': 'debug'})
                            
                            # Check if entity is still in a processing state when it should be advanced
                            # CRITICAL FIX: Only advance if the entity's current action matches this scheduler's action
                            # This prevents infinite loops where completed entities get re-advanced by wrong schedulers
                            # Note: Removed QUEUED from check since entities in QUEUED are actively executing
                            if (hasattr(entity, 'status') and entity.status in ['PENDING'] and
                                hasattr(entity, 'action') and entity.action == self.action):
                                logger.info(f"Detected stalled {entity_type} {entity_id} - all SubFlows done for action '{self.action}' but entity status is {entity.status}", extra={'emoji_type': 'repair'})
                                
                                # Try to advance the entity to next flow stage
                                try:
                                    self._advance_entity(entity_id, entity_model)
                                    stall_fixes += 1
                                    logger.info(f"Fixed stalled {entity_type} {entity_id} by advancing to next stage", extra={'emoji_type': 'success'})
                                except Exception as advance_error:
                                    logger.warning(f"Failed to advance stalled {entity_type} {entity_id}: {advance_error}", extra={'emoji_type': 'warning'})
                            elif hasattr(entity, 'action') and entity.action != self.action:
                                logger.verbose(f"{entity_type} {entity_id} has completed SubFlows for action '{self.action}' but entity is now on action '{entity.action}' - no advancement needed", extra={'emoji_type': 'debug'})
                            else:
                                logger.verbose(f"{entity_type} {entity_id} has status {entity.status if hasattr(entity, 'status') else 'unknown'} - no advancement needed", extra={'emoji_type': 'debug'})
                        else:
                            # Entity has active SubFlows - it will advance naturally when they complete
                            active_subflows = [sf for sf in pending_subflows if sf.status in ['PENDING', 'QUEUED']]
                            if active_subflows:
                                logger.verbose(f"{entity_type} {entity_id} has {len(active_subflows)} active SubFlows - will advance naturally when they complete", extra={'emoji_type': 'debug'})
                            
                            # Some SubFlows are still pending - check for stuck SubFlows that should have been processed
                            for pending_sf in pending_subflows:
                                # Check if this SubFlow has been stuck for too long (e.g., more than 5 minutes)
                                import datetime
                                time_threshold = datetime.datetime.now() - datetime.timedelta(minutes=5)
                                
                                if pending_sf.created_time and pending_sf.created_time < time_threshold:
                                    if pending_sf.status == 'FAILED' and (pending_sf.retry_count or 0) >= self.max_retries:
                                        # Reset failed SubFlow for retry
                                        logger.info(f"Resetting stuck failed SubFlow {pending_sf.id} for {entity_type} {entity_id}", extra={'emoji_type': 'repair'})
                                        pending_sf.status = 'PENDING'
                                        pending_sf.retry_count = 0
                                        pending_sf.error_message = None
                                        session.add(pending_sf)
                                        stall_fixes += 1
                                    elif pending_sf.status == 'PENDING':
                                        # SubFlow has been pending too long - might be overlooked by scheduler
                                        logger.verbose(f"SubFlow {pending_sf.id} has been PENDING for over 5 minutes - will be picked up in next poll", extra={'emoji_type': 'info'})
                                        
                    except Exception as entity_error:
                        logger.warning(f"Error checking {entity_type} {entity_id} for stalls: {entity_error}", extra={'emoji_type': 'warning'})
                
                # 2. Look for entities that should have SubFlows but don't have any
                # This handles cases where SubFlow creation failed but entity is marked as PENDING/QUEUED
                if self.model:
                    try:
                        # Find entities in PENDING state with no SubFlows (truly stalled)
                        # Note: Exclude QUEUED entities since they're actively being processed
                        if hasattr(self.model, 'status'):
                            entities_without_subflows = session.query(self.model).filter(
                                self.model.status == 'PENDING',  # Only PENDING, not QUEUED (QUEUED means actively processing)
                                hasattr(self.model, 'action') and self.model.action == self.action  # Only entities for this action
                            ).all()
                            
                            for entity in entities_without_subflows:
                                # Check if this entity has any active SubFlows for current action
                                if self.model.__name__ == 'Movie':
                                    existing_active_subflows = session.query(SubFlow).filter(
                                        SubFlow.movie_id == entity.id,
                                        SubFlow.action == self.action,
                                        SubFlow.status.in_(['PENDING', 'QUEUED'])  # Only check for active SubFlows
                                    ).count()
                                elif self.model.__name__ == 'Series':
                                    existing_active_subflows = session.query(SubFlow).filter(
                                        SubFlow.series_id == entity.id,
                                        SubFlow.action == self.action,
                                        SubFlow.status.in_(['PENDING', 'QUEUED'])
                                    ).count()
                                elif self.model.__name__ == 'Episode':
                                    existing_active_subflows = session.query(SubFlow).filter(
                                        SubFlow.episode_id == entity.id,
                                        SubFlow.action == self.action,
                                        SubFlow.status.in_(['PENDING', 'QUEUED'])
                                    ).count()
                                else:
                                    existing_active_subflows = 1  # Skip unknown models
                                    
                                if existing_active_subflows == 0:
                                    # Check if entity is not at the final step of the flow
                                    try:
                                        current_step = entity.current_step_name if hasattr(entity, 'current_step_name') else None
                                        next_entry = flow_manager.next_entry(self.action, current_step) if current_step else flow_manager.get_initial(self.action)
                                        
                                        if next_entry is not None:
                                            # Entity is not at final step and has no active SubFlows - truly stalled
                                            logger.info(f"Detected {self.model.__name__} {entity.id} in PENDING state with no active SubFlows (current_step: {current_step}) - re-enqueueing", extra={'emoji_type': 'repair'})
                                            
                                            # Re-enqueue the entity to create missing SubFlows
                                            try:
                                                result = self.enqueue(entity)
                                                if result:
                                                    stall_fixes += 1
                                                    logger.info(f"Re-enqueued stalled {self.model.__name__} {entity.id}", extra={'emoji_type': 'success'})
                                            except Exception as enqueue_error:
                                                logger.warning(f"Failed to re-enqueue {self.model.__name__} {entity.id}: {enqueue_error}", extra={'emoji_type': 'warning'})
                                        else:
                                            # Entity is at final step - no need to re-enqueue
                                            logger.verbose(f"{self.model.__name__} {entity.id} is PENDING but at final step (current_step: {current_step}) - no re-enqueue needed", extra={'emoji_type': 'debug'})
                                    except Exception as flow_check_error:
                                        logger.warning(f"Failed to check flow status for {self.model.__name__} {entity.id}: {flow_check_error}", extra={'emoji_type': 'warning'})
                                else:
                                    # Entity has active SubFlows - check if they're on the correct step
                                    current_step = entity.current_step_name if hasattr(entity, 'current_step_name') else None
                                    
                                    if current_step:
                                        # Get the active SubFlows to check their steps
                                        if self.model.__name__ == 'Movie':
                                            active_subflows = session.query(SubFlow).filter(
                                                SubFlow.movie_id == entity.id,
                                                SubFlow.action == self.action,
                                                SubFlow.status.in_(['PENDING', 'QUEUED'])
                                            ).all()
                                        elif self.model.__name__ == 'Series':
                                            active_subflows = session.query(SubFlow).filter(
                                                SubFlow.series_id == entity.id,
                                                SubFlow.action == self.action,
                                                SubFlow.status.in_(['PENDING', 'QUEUED'])
                                            ).all()
                                        elif self.model.__name__ == 'Episode':
                                            active_subflows = session.query(SubFlow).filter(
                                                SubFlow.episode_id == entity.id,
                                                SubFlow.action == self.action,
                                                SubFlow.status.in_(['PENDING', 'QUEUED'])
                                            ).all()
                                        else:
                                            active_subflows = []
                                        
                                        # Check if any SubFlow is working on the expected step
                                        correct_step_subflows = []
                                        for sf in active_subflows:
                                            sf_steps = sf.steps.split(',') if sf.steps else []
                                            if sf.step_index < len(sf_steps):
                                                current_sf_step = sf_steps[sf.step_index]
                                                if current_sf_step == current_step:
                                                    correct_step_subflows.append(sf)
                                        
                                        if correct_step_subflows:
                                            logger.verbose(f"{self.model.__name__} {entity.id} has {len(correct_step_subflows)} SubFlows correctly working on step '{current_step}' - will advance naturally", extra={'emoji_type': 'debug'})
                                        else:
                                            # SubFlows exist but none are on the correct step - potential stall
                                            logger.warning(f"{self.model.__name__} {entity.id} has {existing_active_subflows} active SubFlows but none are on expected step '{current_step}' - checking for step mismatch", extra={'emoji_type': 'warning'})
                                            
                                            # Log what steps the SubFlows are actually on
                                            for sf in active_subflows:
                                                sf_steps = sf.steps.split(',') if sf.steps else []
                                                actual_step = sf_steps[sf.step_index] if sf.step_index < len(sf_steps) else 'unknown'
                                                logger.verbose(f"  SubFlow {sf.id}: step_index={sf.step_index}, actual_step='{actual_step}', expected='{current_step}'", extra={'emoji_type': 'debug'})
                                            
                                            # This could indicate a step synchronization issue - consider re-enqueueing
                                            try:
                                                next_entry = flow_manager.next_entry(self.action, current_step)
                                                if next_entry is not None:
                                                    logger.info(f"Step mismatch detected for {self.model.__name__} {entity.id} - re-enqueueing to sync steps", extra={'emoji_type': 'repair'})
                                                    result = self.enqueue(entity)
                                                    if result:
                                                        stall_fixes += 1
                                                        logger.info(f"Re-enqueued {self.model.__name__} {entity.id} due to step mismatch", extra={'emoji_type': 'success'})
                                            except Exception as step_sync_error:
                                                logger.warning(f"Failed to fix step mismatch for {self.model.__name__} {entity.id}: {step_sync_error}", extra={'emoji_type': 'warning'})
                                    else:
                                        logger.verbose(f"{self.model.__name__} {entity.id} has {existing_active_subflows} active SubFlows (no current_step_name set) - will advance naturally", extra={'emoji_type': 'debug'})
                                        
                    except Exception as model_error:
                        logger.warning(f"Error checking {self.model.__name__} entities for missing SubFlows: {model_error}", extra={'emoji_type': 'warning'})
                
                # Commit all stall fixes
                if stall_fixes > 0:
                    session.commit()
                    logger.info(f"Fixed {stall_fixes} stalled progressions for action '{self.action}'", extra={'emoji_type': 'success'})
                else:
                    logger.verbose(f"No stalled progressions detected for action '{self.action}'", extra={'emoji_type': 'debug'})
                        
        except Exception as e:
            logger.error(f"Error detecting stalled progressions for action '{self.action}': {e}", extra={'emoji_type': 'error'})

    def enqueue(self, obj): 
        """
        Enqueue an object for processing.
        
        Args:
            obj (obj Model or int): The object model to process, or its ID
        Returns:
            int: The ID of the enqueued object, or None on failure
        """
        logger.verbose(f"Enqueuing object for processing - action: {self.action}", extra={'emoji_type': 'processing'})
        
        # Handle both model objects and integer IDs
        if isinstance(obj, (Movie, Series, Episode)):
            self.model = obj.__class__
            obj_id = obj.id
            logger.debug(f"Object type: {self.model.__name__}, ID: {obj_id}", extra={'emoji_type': 'debug'})
        elif isinstance(obj, int):
            # Try to determine model type from action if not already set
            if not self.model:
                if 'movie' in self.action.lower():
                    self.model = Movie
                elif 'series' in self.action.lower():
                    self.model = Series
                elif 'episode' in self.action.lower():
                    self.model = Episode
                else:
                    logger.error(f"Cannot determine model type from action '{self.action}' for ID {obj}", extra={'emoji_type': 'error'})
                    return None
            obj_id = obj
            logger.debug(f"ID provided: {obj_id}, inferred model type: {self.model.__name__}", extra={'emoji_type': 'debug'})
        else:
            logger.error(f"Invalid object type {type(obj)} for object {obj} - expected Movie, Series, Episode, or int ID", extra={'emoji_type': 'error'})
            return None
        
        def process_enqueue():
            with db_session_scope() as session:
                ent = session.query(self.model).get(obj_id)
                if not ent:
                    logger.warning(f"No {self.model.__name__} found with ID {obj_id}", extra={'emoji_type': 'warning'})
                    return None

                # Always cancel previous action subflows for this entity if action is changing
                if not hasattr(ent, 'action') or ent.action != self.action:
                    logger.info(f"Entity {self.model.__name__} {obj_id} action changed to '{self.action}' - cancelling previous action subflows", extra={'emoji_type': 'repair'})
                    ent.action = self.action
                    # Find all subflows for this entity with a different action and not CANCELLED
                    if self.model is Movie:
                        prev_subflows = session.query(SubFlow).filter(
                            SubFlow.movie_id == obj_id,
                            SubFlow.action != self.action,
                            SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE', 'FAILED'])
                        ).all()
                    elif self.model is Series:
                        prev_subflows = session.query(SubFlow).filter(
                            SubFlow.series_id == obj_id,
                            SubFlow.action != self.action,
                            SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE', 'FAILED'])
                        ).all()
                    elif self.model is Episode:
                        prev_subflows = session.query(SubFlow).filter(
                            SubFlow.episode_id == obj_id,
                            SubFlow.action != self.action,
                            SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE', 'FAILED'])
                        ).all()
                    else:
                        prev_subflows = []
                    for old_sf in prev_subflows:
                        logger.info(f"Cancelling previous action SubFlow {old_sf.id} (action: {old_sf.action}, status: {old_sf.status})", extra={'emoji_type': 'cancel'})
                        old_sf.status = 'CANCELLED'
                        old_sf.error_message = f"Cancelled due to new action: {self.action}"
                        # Remove scheduled jobs for previous subflows
                        job_id_pattern = f"{old_sf.action}_{old_sf.id}_"
                        try:
                            jobs_to_remove = []
                            for job in self.scheduler.get_jobs():
                                if job.id and job.id.startswith(job_id_pattern):
                                    jobs_to_remove.append(job.id)
                            for job_id in jobs_to_remove:
                                self.scheduler.remove_job(job_id)
                                logger.verbose(f"Cancelled scheduled job: {job_id}", extra={'emoji_type': 'cancel'})
                        except Exception as e:
                            logger.warning(f"Failed to cancel job for SubFlow {old_sf.id}: {e}", extra={'emoji_type': 'warning'})

                # If entity is not PENDING, reset for reprocessing
                if ent.status != 'PENDING':
                    if ent.status in ['DONE', 'QUEUED']:
                        logger.verbose(f"{self.model.__name__} {obj_id} has status '{ent.status}' - resetting to PENDING for new action '{self.action}'", extra={'emoji_type': 'refresh'})
                        ent.status = 'PENDING'
                        ent.current_step_name = None
                    else:
                        logger.warning(f"{self.model.__name__} {obj_id} has status '{ent.status}' (expected PENDING)", extra={'emoji_type': 'warning'})
                        return None

                logger.debug(f"Found PENDING {self.model.__name__} {obj_id} - creating subflows", extra={'emoji_type': 'success'})
                if not hasattr(ent, 'current_step_name') or ent.current_step_name is None:
                    ent.current_step_name = None
                session.add(ent)
                session.commit()

                initial_entry = flow_manager.get_initial(self.action)
                entry_description = (
                    initial_entry.__name__ if callable(initial_entry)
                    else f"list[{len(initial_entry)}]" if isinstance(initial_entry, list)
                    else f"dict[{len(initial_entry)}]" if isinstance(initial_entry, dict)
                    else str(type(initial_entry))
                )
                logger.debug(f"Initial flow entry: {entry_description}", extra={'emoji_type': 'debug'})
                self._create_subflows(obj_id, initial_entry)
                logger.verbose(f"Successfully enqueued {self.model.__name__} {obj_id} for processing", extra={'emoji_type': 'success'})
                return obj_id
        
        try:
            return db_operation_with_retry(process_enqueue)
        except Exception as e:
            logger.error(f"enqueue error for {self.model.__name__ if self.model else 'unknown'} {obj_id}: {e}", extra={'emoji_type': 'error'})
            return None

    def _create_subflows(
        self,
        ent_id: int,
        entry: Union[Callable, List[Callable], Dict[str, List[Callable]]],
    ):
        logger.verbose(f"Creating subflows for {self.model.__name__ if self.model else 'unknown'} {ent_id}", extra={'emoji_type': 'processing'})
        session = get_session()
        try:
            # Initial explosion for Series: entry is first step, not a dict
            if not isinstance(entry, dict):
                logger.verbose(f"Processing single entry/list for {self.model.__name__}", extra={'emoji_type': 'debug'})
                
                if self.model is Series:
                    # For Series workflows, ensure episodes are in PENDING status to match flow criteria
                    episodes_to_reset = (
                            session.query(Episode)
                            .join(Season)
                            .filter(
                                Season.series_id == ent_id,
                                or_(Episode.status.in_(["DONE", "QUEUED"]), Episode.action != self.action)
                            )
                            .all()
                        )
                    
                    # Debug: Log all episodes for this series
                    all_episodes = session.query(Episode).join(Season).filter(Season.series_id == ent_id).all()
                    logger.verbose(f"🐛 DEBUG: Series {ent_id} has {len(all_episodes)} total episodes", extra={'emoji_type': 'debug'})
                    for ep in all_episodes:
                        logger.verbose(f"🐛 DEBUG: Episode {ep.id} - status: {ep.status}, action: {ep.action}", extra={'emoji_type': 'debug'})
                    
                    if episodes_to_reset:
                        logger.verbose(f"➡️ Resetting {len(episodes_to_reset)} episodes to PENDING for {self.action} series {ent_id}", extra={'emoji_type': 'refresh'})

                        reset_session = get_session()
                        try:
                            logger.verbose("🐛 DEBUG: Starting episode reset transaction", extra={'emoji_type': 'debug'})

                            # Re-query and update inside reset_session
                            updated_ids = []
                            for ep in episodes_to_reset:
                                logger.verbose(f"🐛 DEBUG: Preparing to reset episode {ep.id} ({ep.status}, {ep.action})", extra={'emoji_type': 'debug'})
                                reset_ep = reset_session.query(Episode).get(ep.id)
                                if not reset_ep:
                                    logger.error(f"🐛 DEBUG: Could not find episode {ep.id} in reset session", extra={'emoji_type': 'error'})
                                    continue
                                # Only change if different to ensure an UPDATE
                                if reset_ep.status != 'PENDING' or reset_ep.action != self.action:
                                    reset_ep.status = 'PENDING'
                                    reset_ep.action = self.action
                                    reset_session.add(reset_ep)
                                    updated_ids.append(reset_ep.id)
                                else:
                                    logger.verbose(f"🐛 DEBUG: Episode {ep.id} already PENDING with same action, skipping", extra={'emoji_type': 'debug'})

                            logger.verbose(f"🐛 DEBUG: session.dirty={len(reset_session.dirty)}, session.new={len(reset_session.new)}", extra={'emoji_type': 'debug'})

                            # Force flush to catch DB level errors early
                            try:
                                reset_session.flush()
                                logger.verbose("🐛 DEBUG: flush() succeeded", extra={'emoji_type': 'debug'})
                            except SQLAlchemyError:
                                logger.error("🐛 ERROR: flush() failed during episode reset", exc_info=True, extra={'emoji_type': 'error'})
                                reset_session.rollback()
                                raise

                            # Commit and log
                            try:
                                reset_session.commit()
                                logger.verbose("🐛 DEBUG: reset_session.commit() SUCCESS", extra={'emoji_type': 'success'})
                            except SQLAlchemyError:
                                logger.error("🐛 ERROR: commit() failed during episode reset", exc_info=True, extra={'emoji_type': 'error'})
                                reset_session.rollback()
                                raise

                            # Verification: open a fresh session and verify the rows were updated
                            verify_session = get_session()
                            try:
                                if updated_ids:
                                    q = verify_session.query(Episode.id, Episode.status, Episode.action).filter(Episode.id.in_(updated_ids)).all()
                                    logger.verbose(f"🐛 DEBUG: Verification select returned {len(q)} rows: {q}", extra={'emoji_type': 'debug'})
                                else:
                                    logger.verbose("🐛 DEBUG: No IDs needed update (nothing to verify)", extra={'emoji_type': 'debug'})
                            finally:
                                verify_session.close()

                        except Exception as reset_error:
                            logger.error(f"Failed to reset episodes (outer): {reset_error}", exc_info=True, extra={'emoji_type': 'error'})
                            try:
                                reset_session.rollback()
                            except Exception:
                                logger.error("rollback() failed on reset_session", exc_info=True, extra={'emoji_type': 'error'})
                            raise
                        finally:
                            reset_session.close()

                        # Ensure main session sees changes (or use fresh session later)
                        session.expire_all()
                        logger.verbose("🐛 DEBUG: Main session expired_all() after episode reset", extra={'emoji_type': 'debug'})
                    else:
                        logger.verbose(f"🐛 DEBUG: No episodes to reset for series {ent_id} (looking for DONE/QUEUED status)", extra={'emoji_type': 'debug'})
                    
                    logger.verbose(f"🚨 CHECKPOINT: About to get episode criteria for {self.action}", extra={'emoji_type': 'debug'})
                    
                    logger.verbose(f"Getting episode criteria for {self.action}", extra={'emoji_type': 'debug'})
                    episode_criteria = self._get_flow_episode_criteria()
                    logger.verbose(f"Episode criteria for {self.action}: {episode_criteria}", extra={'emoji_type': 'debug'})
                    
                    eps = session.query(Episode.id).join(Season).filter(
                        Season.series_id == ent_id,
                        episode_criteria
                    ).all()
                    logger.verbose(f"Found {len(eps)} episodes matching flow criteria for {self.action} series {ent_id}", extra={'emoji_type': 'tv'})
                    
                    # Debug: Show which episodes matched the criteria
                    if eps:
                        episode_ids = [eid[0] for eid in eps]
                        logger.verbose(f"🐛 DEBUG: Episode IDs that matched criteria: {episode_ids}", extra={'emoji_type': 'debug'})
                        
                        # Show the current status of these episodes
                        matched_episodes = session.query(Episode).filter(Episode.id.in_(episode_ids)).all()
                        for ep in matched_episodes:
                            logger.verbose(f"🐛 DEBUG: Matched Episode {ep.id} - status: {ep.status}, action: {ep.action}, placeholder_exists: {ep.placeholder_exists}", extra={'emoji_type': 'debug'})
                    else:
                        logger.verbose(f"🐛 DEBUG: No episodes matched the criteria, checking all series episodes again...", extra={'emoji_type': 'debug'})
                        all_eps_post_reset = session.query(Episode).join(Season).filter(Season.series_id == ent_id).all()
                        for ep in all_eps_post_reset:
                            logger.verbose(f"🐛 DEBUG: Post-reset Episode {ep.id} - status: {ep.status}, action: {ep.action}, placeholder_exists: {ep.placeholder_exists}", extra={'emoji_type': 'debug'})
                    
                    if eps:
                        logger.verbose(f"Episode IDs to process: {[eid[0] for eid in eps]}", extra={'emoji_type': 'debug'})
                        for (eid,) in eps:
                            logger.verbose(f"Creating subflow for episode {eid}", extra={'emoji_type': 'debug'})
                            try:
                                self._make_or_schedule(
                                    session, eid, branch=str(eid), entry=entry, context=eid, target_model=Episode
                                )
                                logger.verbose(f"Successfully processed episode {eid}", extra={'emoji_type': 'success'})
                            except Exception as e:
                                logger.error(f"Failed to create subflow for episode {eid}: {e}", extra={'emoji_type': 'error'})
                    else:
                        logger.verbose(f"No episodes found for {self.action} series_id: {ent_id}", extra={'emoji_type': 'warning'})
                        return
                        
                elif self.model is Episode:
                    # Validate that ent_id corresponds to an existing episode
                    episode = session.query(Episode).filter(Episode.id == ent_id).first()
                    if not episode:
                        logger.error(f"Invalid episode_id: {ent_id}", extra={'emoji_type': 'error'})
                        return
                    logger.verbose(f"Creating subflow for single episode {ent_id}", extra={'emoji_type': 'tv'})
                    self._make_or_schedule(
                        session, ent_id, branch=str(ent_id), entry=entry, context=ent_id, target_model=Episode
                    )
                    
                elif self.model is Movie:
                    logger.verbose(f"Creating subflow for movie {ent_id}", extra={'emoji_type': 'movie'})
                    self._make_or_schedule(
                        session, ent_id, branch="main", entry=entry, context=None, target_model=Movie
                    )

            # Handle dict branches at any step
            else:
                logger.verbose(f"Processing dict entry with {len(entry)} branches", extra={'emoji_type': 'debug'})
                for branch_key, funcs in entry.items():
                    logger.verbose(f"Processing branch '{branch_key}' with {len(funcs) if isinstance(funcs, list) else 1} functions", extra={'emoji_type': 'branch'})
                    
                    # Determine contexts based on model type
                    if self.model is Series:
                        episode_criteria = self._get_flow_episode_criteria()
                        contexts = [e.id for e in session.query(Episode.id).join(Season).filter(
                            Season.series_id == ent_id,
                            episode_criteria
                        )]
                        logger.verbose(f"Series branch: found {len(contexts)} episode contexts for {self.action}", extra={'emoji_type': 'tv'})
                    elif self.model is Episode:
                        contexts = [ent_id]
                        logger.verbose(f"Episode branch: using single context {ent_id}", extra={'emoji_type': 'tv'})
                    elif self.model is Movie:
                        contexts = [None]
                        logger.verbose(f"Movie branch: using None context", extra={'emoji_type': 'movie'})
    
                    for ctx in contexts:
                        # For Series workflows with episode contexts, use Episode as target model
                        if self.model is Series and ctx is not None:
                            target_model = Episode
                        else:
                            target_model = self.model
                            
                        self._make_or_schedule(
                            session=session,
                            ent_id=ent_id,
                            branch=branch_key,
                            entry=funcs,
                            context=ctx,
                            target_model=target_model
                        )
        finally:
            session.close()

    def _make_or_schedule(
        self,
        session,
        ent_id: int,
        branch: Union[str, int],
        entry: Union[Callable, List[Callable]],
        context: Union[int, None],
        target_model=None,  # Add parameter to explicitly specify the target model
    ):
        # Determine the correct model and entity ID for this specific subflow
        if target_model is None:
            target_model = self.model
            target_ent_id = ent_id
        else:
            # Use the provided target_model and ent_id as the target entity ID
            target_ent_id = ent_id
            
        logger.verbose(f"Making/scheduling subflow for {target_model.__name__} {target_ent_id}, branch: {branch}, context: {context}", extra={'emoji_type': 'debug'})
         
         # Check for existing SubFlows for this entity and cancel them (including completed ones for fresh processing)
        # Implement action priority: seriesadd > seriesdelete to prevent cancellation conflicts
        
        def should_cancel_subflow(existing_action, current_action):
            """Determine if an existing SubFlow should be cancelled by the current action.
            
            Rules:
            - Delete should immediately cancel add (delete is newer, add is older)
            - Same action can cancel DONE/FAILED for fresh processing
            - Different actions always cancel each other
            """
            return True  # All existing SubFlows can be cancelled by new actions
        
        # First, validate that the entity actually exists before creating SubFlows
        if target_model is Movie:
            entity = session.query(target_model).get(target_ent_id)
            if not entity:
                logger.error(f"Cannot create SubFlow: {target_model.__name__} with ID {target_ent_id} does not exist", extra={'emoji_type': 'error'})
                return
            
            existing_subflows = session.query(SubFlow).filter(
                SubFlow.movie_id == target_ent_id,
                SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE', 'FAILED'])
            ).all()
        elif target_model is Series:
            entity = session.query(target_model).get(target_ent_id)
            if not entity:
                logger.error(f"Cannot create SubFlow: {target_model.__name__} with ID {target_ent_id} does not exist", extra={'emoji_type': 'error'})
                return
                
            existing_subflows = session.query(SubFlow).filter(
                SubFlow.series_id == target_ent_id,
                SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE', 'FAILED'])
            ).all()
        elif target_model is Episode:
            entity = session.query(target_model).get(target_ent_id)
            if not entity:
                logger.error(f"Cannot create SubFlow: {target_model.__name__} with ID {target_ent_id} does not exist", extra={'emoji_type': 'error'})
                return
                
            existing_subflows = session.query(SubFlow).filter(
                SubFlow.episode_id == target_ent_id,
                SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE', 'FAILED'])
            ).all()
        else:
            existing_subflows = []
            
        # Check for existing PENDING/QUEUED SubFlows of the same action (prevent duplicates)
        # For Series workflows with episodes, check per-episode to avoid false duplicates
        if self.model is Series and context is not None:
            # For series workflows, check for duplicates based on specific episode context
            existing_pending_same_action = [sf for sf in existing_subflows 
                                           if sf.action == self.action and sf.status in ['PENDING', 'QUEUED'] 
                                           and sf.episode_id == context]
        else:
            # For non-series workflows or series-level workflows, check normally
            existing_pending_same_action = [sf for sf in existing_subflows 
                                           if sf.action == self.action and sf.status in ['PENDING', 'QUEUED']]
        
        if existing_pending_same_action:
            # Better logging message based on context
            if self.model is Series and context is not None:
                target_description = f"Episode {context} (Series {ent_id})"
            else:
                target_description = f"{target_model.__name__} {target_ent_id}"
                
            logger.warning(f"Duplicate request detected: Found {len(existing_pending_same_action)} existing PENDING/QUEUED SubFlows for action {self.action} on {target_description}", extra={'emoji_type': 'warning'})
            for existing_sf in existing_pending_same_action:
                logger.verbose(f"Existing SubFlow {existing_sf.id}: status={existing_sf.status}, created={existing_sf.created_time}", extra={'emoji_type': 'info'})
            
            logger.info(f"Skipping SubFlow creation - already have PENDING/QUEUED SubFlows for same action", extra={'emoji_type': 'skip'})
            return  # Don't create duplicate SubFlows
        
        # Filter subflows that should actually be cancelled
        subflows_to_cancel = []
        for old_sf in existing_subflows:
            # Smart cancellation logic:
            # 1. Never cancel the same action unless it's explicitly failed or stuck
            # 2. Only cancel different actions (e.g., seriesdelete cancels seriesadd)
            # 3. Allow DONE SubFlows to remain unless forcing fresh processing
            
            if old_sf.action != self.action:
                # Different actions cancel each other (e.g., delete cancels add)
                if old_sf.status in ['PENDING', 'QUEUED']:
                    subflows_to_cancel.append(old_sf)
                    logger.verbose(f"Will cancel different action: {old_sf.action} -> {self.action}", extra={'emoji_type': 'warning'})
            elif old_sf.action == self.action and old_sf.status == 'FAILED':
                # Only cancel failed SubFlows of the same action for retry
                subflows_to_cancel.append(old_sf)
                logger.verbose(f"Will cancel failed SubFlow for retry: {old_sf.id}", extra={'emoji_type': 'warning'})
            # Note: Do NOT cancel DONE or PENDING/QUEUED SubFlows of the same action
            
        if subflows_to_cancel:
            logger.verbose(f"Found {len(subflows_to_cancel)} existing subflows for {target_model.__name__} {target_ent_id} to cancel for fresh processing", extra={'emoji_type': 'warning'})
            for old_sf in subflows_to_cancel:
                logger.verbose(f"Cancelling SubFlow {old_sf.id} (action: {old_sf.action}, status: {old_sf.status})", extra={'emoji_type': 'cancel'})
                old_sf.status = 'CANCELLED'
                old_sf.error_message = f"Cancelled for fresh processing by action: {self.action}"
                session.add(old_sf)
                
                # Try to cancel the scheduled job if it exists
                job_id_pattern = f"{old_sf.action}_{old_sf.id}_"
                try:
                    # Get all jobs and find ones that match our pattern
                    jobs_to_remove = []
                    for job in self.scheduler.get_jobs():
                        if job.id and job.id.startswith(job_id_pattern):
                            jobs_to_remove.append(job.id)
                    
                    for job_id in jobs_to_remove:
                        self.scheduler.remove_job(job_id)
                        logger.verbose(f"Cancelled scheduled job: {job_id}", extra={'emoji_type': 'cancel'})
                        
                except Exception as e:
                    logger.warning(f"Failed to cancel job for SubFlow {old_sf.id}: {e}", extra={'emoji_type': 'warning'})
            
            session.commit()
            logger.verbose(f"Successfully cancelled {len(existing_subflows)} conflicting subflows", extra={'emoji_type': 'success'})
        
        # steps string
        steps = (entry.__name__ if callable(entry) else
                ','.join(f.__name__ for f in entry))
        logger.verbose(f"Subflow steps: {steps}", extra={'emoji_type': 'step'})
        
        # lookup by identity - exclude cancelled and completed SubFlows for fresh processing
        filter_kwargs = {'branch': branch, 'steps': steps, 'action': self.action}
        
        # Determine correct IDs based on model type
        series_id_for_subflow = None
        episode_id_for_subflow = None
        movie_id_for_subflow = None
        
        if target_model is Movie:
            movie_id_for_subflow = target_ent_id
            filter_kwargs['movie_id'] = target_ent_id
        elif target_model is Series:
            series_id_for_subflow = target_ent_id
            filter_kwargs['series_id'] = target_ent_id
            filter_kwargs['episode_id'] = context
            episode_id_for_subflow = context
        elif target_model.__name__ == 'Episode':
            # For episodes, we need to get the series_id from the episode's season
            episode_id_for_subflow = target_ent_id
            episode = session.query(target_model).get(target_ent_id)
            if episode and episode.season:
                series_id_for_subflow = episode.season.series_id
            filter_kwargs['series_id'] = series_id_for_subflow
            filter_kwargs['episode_id'] = target_ent_id
        else:
            # Fallback for other models
            filter_kwargs['series_id'] = ent_id
            filter_kwargs['episode_id'] = context
            
        logger.verbose(f"Looking for existing SubFlow with: {filter_kwargs}", extra={'emoji_type': 'debug'})
        sf = session.query(SubFlow).filter_by(**filter_kwargs).filter(
            SubFlow.status.in_(['PENDING', 'QUEUED'])  # Only reuse pending/queued SubFlows, not completed ones
        ).first()
        
        if not sf:
            logger.verbose(f"No existing SubFlow found, creating new one for {target_model.__name__} {target_ent_id}", extra={'emoji_type': 'new'})
            sf = SubFlow(
                movie_id=movie_id_for_subflow,
                series_id=series_id_for_subflow,
                episode_id=episode_id_for_subflow,
                action=self.action,
                branch=branch,
                steps=steps,
                step_index=0,
                status='PENDING',
            )
            session.add(sf)
            session.commit()
            logger.verbose(f"Created SubFlow {sf.id} for {target_model.__name__} {target_ent_id}", extra={'emoji_type': 'success'})
        else:
            logger.verbose(f"SubFlow already exists: {sf.id} (status: {sf.status})", extra={'emoji_type': 'info'})
             
        # schedule first function
        func = entry if callable(entry) else entry[0]
        logger.verbose(f"Scheduling first function: {func.__name__} for SubFlow {sf.id}", extra={'emoji_type': 'schedule'})
        self._schedule_subflow(sf.id, func, context)

    def _schedule_subflow(
        self,
        sf_id: int,
        func: Callable,
        context: Union[int, None] = None,
    ):
        from core.config import settings
        
        job_id = f"{self.action}_{sf_id}_{func.__name__}"
        args = (sf_id, func.__name__)
        if context is not None:
            args += (context,)
            
        # Determine executor based on function name
        lname = func.__name__.lower()
        if 'plex' in lname:
            executor = 'plex'
            # Check if Plex is enabled
            if not settings.plex_enabled:
                logger.verbose(f"Cancelling Plex subflow {sf_id} function '{func.__name__}' - Plex is disabled", extra={'emoji_type': 'skip'})
                self._cancel_subflow(sf_id, "Plex is disabled")
                return
        elif 'jellyfin' in lname:
            executor = 'jellyfin'
            # Check if Jellyfin is enabled
            if not settings.jellyfin_enabled:
                logger.verbose(f"Cancelling Jellyfin subflow {sf_id} function '{func.__name__}' - Jellyfin is disabled", extra={'emoji_type': 'skip'})
                self._cancel_subflow(sf_id, "Jellyfin is disabled")
                return
        else:
            executor = 'default'
            
        logger.verbose(f"Scheduling subflow {sf_id} function '{func.__name__}' on executor '{executor}'", extra={'emoji_type': 'schedule'})
        
        try:
            self.scheduler.add_job(
                self._run_subflow,
                'date',
                run_date=datetime.now(),
                kwargs={
                    'sf_id': sf_id,
                    'step_name': func.__name__,
                    'context': context
                },
                id=job_id,
                replace_existing=True,
                executor=executor,
                max_instances=1
            )
            logger.verbose(f"Successfully scheduled job {job_id} for subflow {sf_id}", extra={'emoji_type': 'success'})
        except Exception as e:
            logger.error(f"Failed to schedule subflow {sf_id} function '{func.__name__}': {e}", extra={'emoji_type': 'error'})

    def _get_flow_episode_criteria(self):
        """
        Determine episode selection criteria based on the flow definition.
        This inspects the actual flow functions to understand what episodes they work with.
        """
        try:
            # Get the flow definition for this action
            initial_entry = flow_manager.get_initial(self.action)
            if not initial_entry:
                logger.verbose(f"No flow definition found for action '{self.action}', using default criteria", extra={'emoji_type': 'debug'})
                return Episode.status == 'PENDING'
            
            # Analyze the flow functions to determine what episodes they operate on
            flow_functions = []
            if callable(initial_entry):
                flow_functions = [initial_entry]
            elif isinstance(initial_entry, list):
                flow_functions = initial_entry
            elif isinstance(initial_entry, dict):
                # For dict entries, get all functions from all branches
                for branch_funcs in initial_entry.values():
                    if callable(branch_funcs):
                        flow_functions.append(branch_funcs)
                    elif isinstance(branch_funcs, list):
                        flow_functions.extend(branch_funcs)
            
            # Analyze function names and types to determine appropriate criteria
            func_names = [f.__name__ for f in flow_functions if callable(f)]
            logger.verbose(f"Analyzing flow functions for {self.action}: {func_names}", extra={'emoji_type': 'debug'})
            
            # If any function deals with deletion, include non-deleted episodes that have placeholders
            if any('delete' in name.lower() for name in func_names):
                logger.verbose(f"Delete operation detected in {self.action}, selecting non-deleted episodes with existing placeholders", extra={'emoji_type': 'debug'})
                # For deletion, we want episodes that either:
                # 1. Have placeholder_exists = True (tracked placeholders)
                # 2. Have status != 'DONE' (might have placeholders not yet tracked)
                # 3. Are not marked as deleted
                return and_(
                    Episode.is_deleted == False,
                    or_(
                        Episode.placeholder_exists == True,
                        Episode.status != 'DONE'
                    )
                )
            
            # If any function deals with file operations, include episodes with files
            if any(term in name.lower() for name in func_names for term in ['file', 'dummy', 'placeholder']):
                logger.verbose(f"File operation detected in {self.action}, selecting episodes with placeholders or pending status", extra={'emoji_type': 'debug'})
                return or_(Episode.status == 'PENDING', Episode.placeholder_exists == True)
            
            # Default: pending episodes
            logger.verbose(f"Using default criteria for {self.action}: PENDING episodes", extra={'emoji_type': 'debug'})
            return Episode.status == 'PENDING'
            
        except Exception as e:
            logger.warning(f"Error analyzing flow criteria for {self.action}: {e}", extra={'emoji_type': 'warning'})
            return Episode.status == 'PENDING'

    def _cancel_subflow(self, sf_id: int, reason: str):
        """Cancel a subflow by marking it as CANCELLED in the database"""
        session = get_session()
        try:
            sf = session.query(SubFlow).get(sf_id)
            if sf:
                sf.status = 'CANCELLED'
                sf.error_message = reason
                session.add(sf)
                session.commit()
                logger.debug(f"SubFlow {sf_id} marked as CANCELLED: {reason}", extra={'emoji_type': 'cancel'})
            else:
                logger.warning(f"SubFlow {sf_id} not found when trying to cancel", extra={'emoji_type': 'warning'})
        except Exception as e:
            logger.error(f"Error cancelling subflow {sf_id}: {e}", extra={'emoji_type': 'error'})
            session.rollback()
        finally:
            session.close()

    def _run_subflow(
        self,
        sf_id: int,
        step_name: str,
        context: Union[int, None] = None
    ):
        # Import here to avoid circular imports
        from core.logger import set_subflow_context, clear_subflow_context
        
        logger.info(f"Starting subflow {sf_id} step '{step_name}' with context {context}", extra={'emoji_type': 'processing'})
        session = get_session()
        try:
            sf = session.query(SubFlow).get(sf_id)
            if not sf:
                logger.error(f"SubFlow {sf_id} not found in database", extra={'emoji_type': 'error'})
                return
            
            # Set SubFlow context for all log messages in this thread
            entity_id = sf.movie_id or sf.episode_id or sf.series_id
            entity_type = 'movie' if sf.movie_id else ('episode' if sf.episode_id else 'series')
            set_subflow_context(
                subflow_id=sf_id,
                entity_id=entity_id,
                entity_type=entity_type
            )
                
            # Check if SubFlow was cancelled before execution
            if sf.status == 'CANCELLED':
                logger.info(f"SubFlow {sf_id} was cancelled, skipping execution: {sf.error_message}", extra={'emoji_type': 'cancel'})
                return
                
            logger.verbose(f"SubFlow {sf_id} details: action={sf.action}, status={sf.status}, step_index={sf.step_index}", extra={'emoji_type': 'debug'})
            
            steps = sf.steps.split(',')
            retries = sf.retry_count or 0
            success = False
            error = None

            if sf.status != "DONE":
                logger.debug(f"Executing step '{step_name}' for subflow {sf_id} (attempt 1)", extra={'emoji_type': 'step'})
                
                # Determine model type for dynamic actions like playback
                current_model = self.model
                # Prefer deriving model type from SubFlow's entity IDs so per-episode subflows run with Episode model.
                if sf.movie_id is not None:
                    current_model = Movie
                elif sf.episode_id is not None:
                    current_model = Episode
                elif sf.series_id is not None:
                    current_model = Series
                else:
                    # Fall back to scheduler's configured model
                    current_model = self.model
                    if current_model is None:
                        logger.error(f"SubFlow {sf_id} has no entity ID set and scheduler has no default model", extra={'emoji_type': 'error'})
                        return
                logger.verbose(f"Determined model type for SubFlow {sf_id}: {current_model.__name__}", extra={'emoji_type': 'debug'})
                
                # Mark SubFlow as QUEUED and update entity status to QUEUED when execution starts
                sf.status = 'QUEUED'
                
                # Update the entity status to QUEUED to indicate it's actively being processed
                try:
                    # Determine which entity to update based on SubFlow type
                    if sf.movie_id is not None:
                        entity = session.query(Movie).get(sf.movie_id)
                        entity_desc = f"Movie {sf.movie_id}"
                    elif sf.episode_id is not None:
                        entity = session.query(Episode).get(sf.episode_id)
                        entity_desc = f"Episode {sf.episode_id}"
                    elif sf.series_id is not None:
                        entity = session.query(Series).get(sf.series_id)
                        entity_desc = f"Series {sf.series_id}"
                    else:
                        entity = None
                        entity_desc = "Unknown entity"
                    
                    if entity and hasattr(entity, 'status'):
                        if entity.status != 'QUEUED':
                            logger.verbose(f"Updating {entity_desc} status from '{entity.status}' to 'QUEUED' (execution started)", extra={'emoji_type': 'processing'})
                            entity.status = 'QUEUED'
                            # Update current_step_name to the step that's about to execute
                            if hasattr(entity, 'current_step_name'):
                                entity.current_step_name = step_name
                                logger.verbose(f"Updated {entity_desc} current_step_name to '{step_name}'", extra={'emoji_type': 'debug'})
                            session.add(entity)
                        else:
                            logger.verbose(f"{entity_desc} already has QUEUED status", extra={'emoji_type': 'debug'})
                    else:
                        logger.verbose(f"Could not update entity status for {entity_desc} - entity not found or no status field", extra={'emoji_type': 'debug'})
                        
                except Exception as entity_update_error:
                    logger.warning(f"Failed to update entity status to QUEUED: {entity_update_error}", extra={'emoji_type': 'warning'})
                
                # Commit both SubFlow and entity status updates
                session.commit()
                
                for attempt in range(self.max_retries):
                    try:
                        # Determine the correct argument based on SubFlow entity type
                        if context is not None:
                            arg = context
                        elif sf.movie_id is not None:
                            arg = sf.movie_id
                        elif sf.episode_id is not None:
                            arg = sf.episode_id
                        elif sf.series_id is not None:
                            arg = sf.series_id
                        else:
                            arg = None
                        logger.verbose(f"Calling {step_name} with arg={arg}, model={current_model}, action={self.action}", extra={'emoji_type': 'debug'})
                        
                        flow_func = self._get_flow_function(step_name)
                        result = flow_func(session, arg, current_model, self.action)
                        success = bool(result)

                        if success:
                            logger.verbose(f"Step '{step_name}' succeeded for subflow {sf_id} on attempt {attempt + 1}", extra={'emoji_type': 'success'})
                            break
                        else:
                            # Non-exceptional failure: count as a retry so we don't loop forever
                            retries += 1
                            logger.warning(f"Step '{step_name}' returned False for subflow {sf_id} on attempt {attempt + 1}", extra={'emoji_type': 'warning'})
                            error = Exception('Step returned False')
                            
                    except Exception as e:
                        error = e
                        retries += 1
                        tb = traceback.format_exc()
                        logger.warning(f"Step '{step_name}' subflow {sf_id} attempt {attempt + 1} failed: {e}", extra={'emoji_type': 'warning'})
                        logger.verbose(f"Full traceback for subflow {sf_id} attempt {attempt + 1}:\n{tb}", extra={'emoji_type': 'debug'})
                        
                        if attempt < self.max_retries - 1:
                            logger.verbose(f"Will retry step '{step_name}' for subflow {sf_id}", extra={'emoji_type': 'retry'})
                        else:
                            logger.error(f"All {self.max_retries} attempts failed for step '{step_name}' subflow {sf_id}", extra={'emoji_type': 'error'})
            else:
                logger.debug(f"SubFlow {sf_id} already marked as DONE, skipping execution", extra={'emoji_type': 'info'})
                success = True
                
            # Create log directory for detailed logging
            log_dir = os.path.join('/logs', self.action, f"{sf.movie_id}_{sf.branch}")
            os.makedirs(log_dir, exist_ok=True)
            
            # Update subflow status
            sf.retry_count = retries

            if success:
                logger.info(f"SubFlow {sf_id} step '{step_name}' completed successfully", extra={'emoji_type': 'success'})

                if sf.step_index + 1 < len(steps):
                    sf.step_index += 1
                    next_name = steps[sf.step_index]
                    logger.verbose(f"SubFlow {sf_id} advancing to next step: {next_name} (step {sf.step_index + 1}/{len(steps)})", extra={'emoji_type': 'step'})
                    # Update status but don't mark as DONE yet since there are more steps
                    session.add(sf)
                    session.commit()
                    self._schedule_subflow(sf_id, self._get_flow_function(next_name), context)
                else:
                    logger.verbose(f"SubFlow {sf_id} completed all steps, marking as DONE and checking for remaining subflows", extra={'emoji_type': 'success'})
                    
                    # Mark this SubFlow as DONE first, then check for remaining
                    sf.status = 'DONE'
                    session.add(sf)
                    session.commit()
                    
                    # Initialize variables to prevent UnboundLocalError
                    still_pending = None
                    advance_id = None
                    
                    # Check if all subflows for this entity AND ACTION are complete
                    # Use dynamically determined model if available
                    check_model = current_model if 'current_model' in locals() else self.model
                    
                    if check_model is Movie:
                        all_subflows = session.query(SubFlow).filter(
                            SubFlow.movie_id == sf.movie_id,
                            SubFlow.action == self.action
                        ).all()
                        
                        logger.debug(f"Movie {sf.movie_id} action {self.action}: Found {len(all_subflows)} total subflows", extra={'emoji_type': 'debug'})
                        for subflow in all_subflows:
                            logger.debug(f"  SubFlow {subflow.id}: status={subflow.status}, action={subflow.action}, steps={subflow.steps}, branch={subflow.branch}", extra={'emoji_type': 'debug'})
                        
                        # Only check the latest non-cancelled SubFlows for pending work
                        # Get the maximum SubFlow ID for each branch to find the latest SubFlows
                        latest_subflows = (
                            session.query(SubFlow)
                            .filter(
                                SubFlow.movie_id == sf.movie_id,
                                SubFlow.action == self.action,
                                SubFlow.status != 'CANCELLED'  # Exclude cancelled ones entirely
                            ).all()
                        )
                        
                        still_pending = None
                        for latest_sf in latest_subflows:
                            if latest_sf.status in ['PENDING', 'QUEUED', 'FAILED']:
                                still_pending = latest_sf
                                break
                                
                        advance_id = sf.movie_id
                        logger.debug(f"Movie {sf.movie_id} action {self.action}: still_pending = {still_pending.id if still_pending else None}", extra={'emoji_type': 'debug'})
                        
                    elif check_model is Series:
                        # for Series flows, look at all SubFlows whose episodes belong to this series
                        episode = session.query(Episode).get(sf.episode_id)
                        if episode:
                            season = session.query(Season).get(episode.season_id)
                            series_id = season.series_id if season else None
                        else:
                            series_id = None
                            
                        if series_id:
                            # Only check the latest non-cancelled SubFlows for pending work
                            latest_subflows = (
                                session.query(SubFlow)
                                .join(Episode, SubFlow.episode_id == Episode.id)
                                .filter(
                                    Episode.season.has(series_id=series_id),
                                    SubFlow.action == self.action,
                                    SubFlow.status != 'CANCELLED'  # Exclude cancelled ones entirely
                                ).all()
                            )
                            
                            still_pending = None
                            for latest_sf in latest_subflows:
                                if latest_sf.status in ['PENDING', 'QUEUED', 'FAILED']:
                                    still_pending = latest_sf
                                    break
                                    
                            advance_id = series_id
                            logger.debug(f"Series {series_id} action {self.action}: still_pending = {still_pending.id if still_pending else None}", extra={'emoji_type': 'debug'})
                        else:
                            still_pending = True  # Don't advance if we can't determine series
                            advance_id = None
                            
                    elif check_model is Episode:
                        # Only check the latest non-cancelled SubFlows for pending work
                        latest_subflows = session.query(SubFlow).filter(
                            SubFlow.episode_id == sf.episode_id,
                            SubFlow.action == self.action,
                            SubFlow.status != 'CANCELLED'  # Exclude cancelled ones entirely
                        ).all()
                        
                        still_pending = None
                        for latest_sf in latest_subflows:
                            if latest_sf.status in ['PENDING', 'QUEUED', 'FAILED']:
                                still_pending = latest_sf
                                break
                                
                        advance_id = sf.episode_id
                        logger.debug(f"Episode {sf.episode_id} action {self.action}: still_pending = {still_pending.id if still_pending else None}", extra={'emoji_type': 'debug'})
                    
                    else:
                        # Unknown model type - don't advance
                        logger.warning(f"Unknown model type for SubFlow advancement: {check_model}", extra={'emoji_type': 'warning'})
                        still_pending = True  # Prevent advancement
                        advance_id = None
                        
                    if not still_pending and advance_id:
                        logger.info(f"All subflows complete for {check_model.__name__} {advance_id} action {self.action}, checking if advancement needed", extra={'emoji_type': 'success'})
                        
                        # Update entity's current_step_name to reflect the completed step
                        entity = session.query(check_model).get(advance_id)
                        if entity:
                            # CRITICAL FIX: Only advance if entity's current action matches this scheduler's action
                            # This prevents infinite loops where SubFlow completion triggers advancement by wrong schedulers
                            if hasattr(entity, 'action') and entity.action == self.action:
                                # FIXED: Set current_step_name to the step that just completed
                                # This will be used by flow_manager.next_entry() to determine the next step
                                completed_step_name = step_name  # This is the step that just finished
                                logger.verbose(f"Updating {check_model.__name__} {advance_id} current_step_name from '{entity.current_step_name}' to '{completed_step_name}'", extra={'emoji_type': 'debug'})
                                entity.current_step_name = completed_step_name
                                session.add(entity)
                                session.commit()
                                
                                logger.verbose(f"Advancing {check_model.__name__} {advance_id} - entity action '{entity.action}' matches scheduler action '{self.action}'", extra={'emoji_type': 'debug'})
                                self._advance_entity(advance_id, check_model)
                            else:
                                entity_action = entity.action if hasattr(entity, 'action') else 'unknown'
                                logger.verbose(f"{check_model.__name__} {advance_id} has completed SubFlows for action '{self.action}' but entity is now on action '{entity_action}' - no advancement needed", extra={'emoji_type': 'debug'})
                        else:
                            logger.warning(f"{check_model.__name__} {advance_id} not found when trying to advance", extra={'emoji_type': 'warning'})
                    elif still_pending:
                        logger.verbose(f"Still have pending subflows for {check_model.__name__ if check_model else 'unknown'} action {self.action}, not advancing yet", extra={'emoji_type': 'info'})
                    else:
                        logger.warning(f"Could not determine advance_id for {check_model.__name__ if check_model else 'unknown'}", extra={'emoji_type': 'warning'})

            else:
                sf.status = 'FAILED'
                sf.error_message = str(error) if error else "Unknown error"
                logger.error(f"SubFlow {sf_id} step '{step_name}' failed permanently: {sf.error_message}", extra={'emoji_type': 'error'})
                
                # Write detailed error log
                try:
                    with open(os.path.join(log_dir, f"error_{step_name}.log"), 'w') as f:
                        f.write(f"SubFlow ID: {sf_id}\n")
                        f.write(f"Step: {step_name}\n")
                        f.write(f"Context: {context}\n")
                        f.write(f"Error: {error}\n\n")
                        f.write(traceback.format_exc())
                    logger.verbose(f"Error details written to {log_dir}/error_{step_name}.log", extra={'emoji_type': 'debug'})
                except Exception as log_error:
                    logger.error(f"Failed to write error log: {log_error}", extra={'emoji_type': 'error'})
                
                # Commit the failed status
                session.add(sf)
                session.commit()
                
                # Schedule a retry after 10 seconds to reset retry_count and try again
                logger.verbose(f"Scheduling retry for failed SubFlow {sf_id} in 10 seconds", extra={'emoji_type': 'retry'})
                run_at = datetime.now() + timedelta(seconds=10)
                self.scheduler.add_job(
                    func=self._reset_failed_subflow,
                    trigger='date',
                    run_date=run_at,
                    args=[sf_id],
                    id=f'retry_failed_{sf_id}',
                    replace_existing=True
                )
            
        except Exception as outer_error:
            logger.error(f"Critical error in _run_subflow for {sf_id}: {outer_error}", extra={'emoji_type': 'error'})
            logger.verbose(f"Critical error traceback:\n{traceback.format_exc()}", extra={'emoji_type': 'debug'})
        finally:
            # Clear SubFlow context when execution is complete
            clear_subflow_context()
            session.close()

    def _advance_entity(self, ent_id: int, model_type=None):
        logger.verbose(f"Advancing entity {ent_id} to next flow stage", extra={'emoji_type': 'processing'})
        session = get_session()
        try:
            # Use provided model type or fall back to scheduler's model
            entity_model = model_type or self.model
            if not entity_model:
                logger.error(f"No model type available for advancing entity {ent_id}", extra={'emoji_type': 'error'})
                return
            
            ent = session.query(entity_model).get(ent_id)
            if not ent:
                logger.error(f"{entity_model.__name__} {ent_id} not found", extra={'emoji_type': 'error'})
                return
                
            logger.debug(f"Current entity status: {ent.status}, current_step_name: {getattr(ent, 'current_step_name', 'None')}", extra={'emoji_type': 'debug'})
            
            # Determine the correct branch from completed SubFlows
            current_branch = self._get_entity_branch(session, ent_id, entity_model)
            logger.verbose(f"Determined branch for {entity_model.__name__} {ent_id}: {current_branch}", extra={'emoji_type': 'debug'})
            
            entry = flow_manager.next_entry(self.action, current_branch, ent.current_step_name)
            
            if entry:
                new_step_name = flow_manager.get_entry_id(self.action, entry)
                logger.verbose(f"Advancing {entity_model.__name__} {ent_id} from '{ent.current_step_name}' to '{new_step_name}'", extra={'emoji_type': 'step'})
                
                ent.current_step_name = new_step_name
                ent.status = 'QUEUED'                    
                session.add(ent)
                session.commit()
                
                # Temporarily set the model for subflow creation
                old_model = self.model
                self.model = entity_model
                
                logger.debug(f"Creating subflows for next entry: {type(entry)}", extra={'emoji_type': 'debug'})
                # now create SubFlows for the next entry
                self._create_subflows(ent_id, entry)
                logger.info(f"Successfully advanced {entity_model.__name__} {ent_id} to next stage", extra={'emoji_type': 'success'})
                
                # Restore original model
                self.model = old_model
                
            else:
                logger.info(f"No more entries for {entity_model.__name__} {ent_id} - marking as DONE", extra={'emoji_type': 'success'})
                ent.status = 'DONE'
                session.add(ent)
                session.commit()
                logger.info(f"{entity_model.__name__} {ent_id} processing complete", extra={'emoji_type': 'success'})
                
                # End handler logging session if this completes the handler
                self._check_and_end_handler_session(ent_id, entity_model, ent)
                
        except Exception as e:
            logger.error(f"Error advancing entity {ent_id}: {e}", extra={'emoji_type': 'error'})
            session.rollback()
        finally:
            session.close()
    
    def _check_and_end_handler_session(self, ent_id: int, entity_model: Type, entity):
        """
        Check if handler processing is complete and end the logging session.
        For series: Check if all episodes are DONE
        For movies: Entity completion means handler is done
        """
        try:
            # Find active logging session for this handler and entity
            session_id = get_handler_session_for_entity(self.action, ent_id)
            if not session_id:
                return  # No active logging session
            
            if entity_model.__name__ == 'Series':
                # For series, check if all episodes are DONE
                session = get_session()
                try:
                    # Count episodes that are not DONE for this series and action
                    pending_episodes = session.query(Episode).join(Season).filter(
                        Season.series_id == ent_id,
                        Episode.action == self.action,
                        Episode.status != 'DONE'
                    ).count()
                    
                    if pending_episodes == 0:
                        # All episodes are done, end the handler session
                        total_episodes = session.query(Episode).join(Season).filter(
                            Season.series_id == ent_id,
                            Episode.action == self.action
                        ).count()
                        
                        end_handler_logging(
                            session_id, 
                            success=True,
                            summary=f"Series processing complete - {total_episodes} episodes processed"
                        )
                        logger.info(f"🏁 Handler session ended for series {ent_id} - all {total_episodes} episodes complete", extra={'emoji_type': 'success'})
                    else:
                        logger.debug(f"Series {ent_id} still has {pending_episodes} pending episodes", extra={'emoji_type': 'debug'})
                        
                finally:
                    session.close()
                    
            elif entity_model.__name__ == 'Movie':
                # For movies, entity completion means handler is done
                end_handler_logging(
                    session_id,
                    success=(entity.status == 'DONE'),
                    summary=f"Movie processing {'complete' if entity.status == 'DONE' else 'failed'}"
                )
                logger.info(f"🏁 Handler session ended for movie {ent_id}", extra={'emoji_type': 'success'})
                
        except Exception as e:
            logger.error(f"Error checking/ending handler session for {entity_model.__name__} {ent_id}: {e}", extra={'emoji_type': 'error'})

    def _get_entity_branch(self, session, ent_id: int, entity_model):
        """
        Determine the current branch for an entity based on its completed SubFlows.
        Returns the branch name from the most recently completed SubFlow, or None if no branch info available.
        """
        try:
            # Query for the most recent completed SubFlow for this entity and action
            if entity_model is Movie:
                latest_subflows = session.query(SubFlow).filter(
                    SubFlow.movie_id == ent_id,
                    SubFlow.action == self.action,
                    SubFlow.status == 'DONE'
                ).order_by(SubFlow.id.desc()).limit(5).all()
            elif entity_model is Series:
                # For series, look at episode-level SubFlows
                latest_subflows = session.query(SubFlow).join(Episode).join(Season).filter(
                    Season.series_id == ent_id,
                    SubFlow.action == self.action,
                    SubFlow.status == 'DONE'
                ).order_by(SubFlow.id.desc()).limit(5).all()
            elif entity_model is Episode:
                latest_subflows = session.query(SubFlow).filter(
                    SubFlow.episode_id == ent_id,
                    SubFlow.action == self.action,
                    SubFlow.status == 'DONE'
                ).order_by(SubFlow.id.desc()).limit(5).all()
            else:
                logger.warning(f"Unknown entity model for branch detection: {entity_model.__name__}", extra={'emoji_type': 'warning'})
                return None
            
            # Look for all valid branch names: "main", episode IDs, "jellyfin", "plex"
            # Branch types: main | episode_id | jellyfin | plex
            all_branches = []
            for sf in latest_subflows:
                if sf.branch:
                    all_branches.append(sf.branch)
                    logger.verbose(f"Found branch '{sf.branch}' from SubFlow {sf.id}", extra={'emoji_type': 'debug'})
            
            if all_branches:
                # Return the most recent branch (prioritize service branches over episode IDs)
                # Priority: jellyfin/plex > main > episode_id
                service_branches = [b for b in all_branches if b in ['jellyfin', 'plex']]
                main_branches = [b for b in all_branches if b == 'main']
                
                if service_branches:
                    branch = service_branches[0]  # Most recent service branch
                    logger.verbose(f"Selected service branch '{branch}' for {entity_model.__name__} {ent_id}", extra={'emoji_type': 'debug'})
                    return branch
                elif main_branches:
                    branch = main_branches[0]  # Most recent main branch
                    logger.verbose(f"Selected main branch '{branch}' for {entity_model.__name__} {ent_id}", extra={'emoji_type': 'debug'})
                    return branch
                else:
                    # Use the most recent branch (likely episode ID)
                    branch = all_branches[0]
                    logger.verbose(f"Selected episode/other branch '{branch}' for {entity_model.__name__} {ent_id}", extra={'emoji_type': 'debug'})
                    return branch
            
            # If no meaningful branch found, check if we need to determine branch from flow structure
            # This handles cases where we're about to enter a branching step
            return self._determine_branch_from_flow(session, ent_id, entity_model)
            
        except Exception as e:
            logger.warning(f"Error determining branch for {entity_model.__name__} {ent_id}: {e}", extra={'emoji_type': 'warning'})
            return None

    def _determine_branch_from_flow(self, session, ent_id: int, entity_model):
        """
        Determine which branch to use when entering a branching step.
        This checks system configuration (Plex/Jellyfin enabled) to decide the branch.
        """
        try:
            from core.config import settings
            
            # Default branch selection based on enabled services
            if hasattr(settings, 'jellyfin_enabled') and settings.jellyfin_enabled:
                logger.verbose(f"Jellyfin is enabled, selecting 'jellyfin' branch for {entity_model.__name__} {ent_id}", extra={'emoji_type': 'debug'})
                return "jellyfin"
            elif hasattr(settings, 'plex_enabled') and settings.plex_enabled:
                logger.verbose(f"Plex is enabled, selecting 'plex' branch for {entity_model.__name__} {ent_id}", extra={'emoji_type': 'debug'})
                return "plex"
            else:
                # If neither is enabled or we can't determine, let flow_manager handle it with None
                logger.verbose(f"No specific service enabled, using None branch for {entity_model.__name__} {ent_id}", extra={'emoji_type': 'debug'})
                return None
                
        except Exception as e:
            logger.warning(f"Error determining branch from flow for {entity_model.__name__} {ent_id}: {e}", extra={'emoji_type': 'warning'})
            return None

    def check_entity_advancement(self, ent_id: int):
        """
        Manually check if an entity should be advanced based on completed SubFlows.
        This can be used to fix entities that got stuck due to timing issues.
        """
        logger.info(f"Manually checking advancement for {self.model.__name__} {ent_id}", extra={'emoji_type': 'check'})
        session = get_session()
        try:
            # Get all non-cancelled SubFlows for this entity and action
            if self.model is Movie:
                active_subflows = session.query(SubFlow).filter(
                    SubFlow.movie_id == ent_id,
                    SubFlow.action == self.action,
                    SubFlow.status != 'CANCELLED'
                ).all()
            elif self.model is Episode:
                active_subflows = session.query(SubFlow).filter(
                    SubFlow.episode_id == ent_id,
                    SubFlow.action == self.action,
                    SubFlow.status != 'CANCELLED'
                ).all()
            else:
                logger.warning(f"Manual advancement check not implemented for {self.model.__name__}", extra={'emoji_type': 'warning'})
                return
            
            logger.verbose(f"Found {len(active_subflows)} active SubFlows for {self.model.__name__} {ent_id}", extra={'emoji_type': 'info'})
            
            # Check if any are still pending
            pending = [sf for sf in active_subflows if sf.status in ['PENDING', 'QUEUED', 'FAILED']]
            
            if not pending:
                logger.info(f"No pending SubFlows found, advancing {self.model.__name__} {ent_id}", extra={'emoji_type': 'success'})
                self._advance_entity(ent_id)
            else:
                logger.verbose(f"Still have {len(pending)} pending SubFlows, not advancing yet", extra={'emoji_type': 'info'})
                for sf in pending:
                    logger.verbose(f"  Pending SubFlow {sf.id}: {sf.status} - {sf.steps}", extra={'emoji_type': 'debug'})

        except Exception as e:
            logger.error(f"Error checking entity advancement: {e}", extra={'emoji_type': 'error'})
        finally:
            session.close()
            
    def _get_flow_function(self, func_name: str) -> Callable:
        logger.verbose(f"Getting flow function '{func_name}' for action '{self.action}'", extra={'emoji_type': 'debug'})
        try:
            module_name = f'services.actions.{self.action}_flow'
            module = import_module(module_name)
            func = getattr(module, func_name)
            logger.debug(f"Successfully loaded function '{func_name}' from {module_name}", extra={'emoji_type': 'success'})
            return func
        except ModuleNotFoundError as e:
            logger.error(f"Module not found: {module_name} - {e}", extra={'emoji_type': 'error'})
            raise
        except AttributeError as e:
            logger.error(f"Function '{func_name}' not found in {module_name} - {e}", extra={'emoji_type': 'error'})
            raise
        except Exception as e:
            logger.error(f"Unexpected error loading {func_name} from {self.action}_flow: {e}", extra={'emoji_type': 'error'})
            raise


# Function to determine model type from action name
def get_model_for_action(action: str):
    """Map action names to their corresponding model classes."""
    from services.postgres.models import Movie, Series, Episode
    
    if 'movie' in action.lower():
        return Movie
    elif 'series' in action.lower():
        return Series  
    elif 'episode' in action.lower():
        return Episode
    elif action.lower() == 'playback':
        # For playback actions, model type is determined dynamically when objects are enqueued
        return None
    else:
        logger.warning(f"Unknown model type for action '{action}', defaulting to Movie", extra={'emoji_type': 'warning'})
        return Movie

# Instantiate schedulers
logger.verbose("Initializing ActionSchedulers for all configured flows", extra={'emoji_type': 'start'})
actions = list(flow_manager.flows.keys())
logger.verbose(f"Available actions: {actions}", extra={'emoji_type': 'debug'})

for action in actions:
    try:
        logger.debug(f"Creating scheduler for action '{action}'", extra={'emoji_type': 'processing'})
        scheduler = ActionScheduler(action)
        
        # Set the model type based on action name
        scheduler.model = get_model_for_action(action)
        if scheduler.model:
            logger.debug(f"Set model type for {action}_scheduler: {scheduler.model.__name__}", extra={'emoji_type': 'debug'})
            log_message = f"Successfully created scheduler: {action}_scheduler with model {scheduler.model.__name__}"
        else:
            logger.debug(f"Set dynamic model type for {action}_scheduler (determined at runtime)", extra={'emoji_type': 'debug'})
            log_message = f"Successfully created scheduler: {action}_scheduler with dynamic model type"
        
        globals()[f"{action}_scheduler"] = scheduler
        logger.info(log_message, extra={'emoji_type': 'success'})
    except Exception as e:
        logger.error(f"Failed to create scheduler for action '{action}': {e}", extra={'emoji_type': 'error'})

logger.verbose(f"Scheduler initialization complete - created {len(actions)} schedulers", extra={'emoji_type': 'success'})