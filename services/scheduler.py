import logging
import os
import traceback
from datetime import datetime
from typing import Callable, Dict, List, Union, Type
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from sqlalchemy import or_, and_
from services.postgres.db import get_session
from services.postgres.models import Movie, Series, Season, Episode, SubFlow
from services.flow_manager import flow_manager
from core.config import settings
from importlib import import_module
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
        logger.debug(f"Scheduler settings: poll_interval={poll_interval}s, max_retries={max_retries}, max_workers={max_workers}", extra={'emoji_type': 'debug'})
        
        executors = {'default': ThreadPoolExecutor(max_workers), 'plex': ThreadPoolExecutor(1), 'jellyfin': ThreadPoolExecutor(1)}
        job_defaults = {'max_instances': 1, 'coalesce': True}
        self.scheduler = BackgroundScheduler(executors=executors, job_defaults=job_defaults)
        
        logger.debug(f"Adding polling job with {poll_interval}s interval", extra={'emoji_type': 'debug'})
        self.scheduler.add_job(
            self.poll_and_enqueue,
            'interval',
            seconds=poll_interval,
            id=f'poll_{action}'
        )
        
        # Daily retry of failed subflows
        if settings.SCHEDULED_TIME_FAILED:
            hh, mm = map(int, settings.SCHEDULED_TIME_FAILED.split(':'))
            logger.info(f"Scheduling daily retry at {hh:02d}:{mm:02d} for failed subflows", extra={'emoji_type': 'clock'})
            self.scheduler.add_job(
                self.retry_failed_subflows,
                'cron',
                hour=hh,
                minute=mm,
                id=f'retry_failed_{action}'
            )
        else:
            logger.debug("No scheduled retry time configured", extra={'emoji_type': 'debug'})
        
        logger.info(f"Scheduler for '{action}' initialized successfully", extra={'emoji_type': 'success'})

    def start(self):
        logger.info(f"Starting scheduler for action '{self.action}'", extra={'emoji_type': 'start'})
        try:
            self.scheduler.start()
            logger.info(f"Scheduler for '{self.action}' started successfully", extra={'emoji_type': 'success'})
        except Exception as e:
            logger.error(f"Failed to start scheduler for '{self.action}': {e}", extra={'emoji_type': 'error'})

    def poll_and_enqueue(self):
        logger.debug(f"Polling for subflows - action: {self.action}", extra={'emoji_type': 'search'})
        session = get_session()
        try:
            with session.begin():
                # Find a PENDING or FAILED subflow for this action/model
                sf = (
                    session.query(SubFlow)
                    .with_for_update(skip_locked=True)
                    .filter(
                        SubFlow.status.in_(["PENDING", "FAILED"]),
                        SubFlow.retry_count < self.max_retries,
                        SubFlow.steps.isnot(None),
                        SubFlow.steps != '',
                        SubFlow.action == self.action,
                    )
                    .order_by(SubFlow.id)
                    .first()
                )
                if not sf:
                    logger.debug(f"No pending/failed subflows found for action '{self.action}'", extra={'emoji_type': 'debug'})
                    return
                    
                logger.info(f"Found subflow {sf.id} to process (status: {sf.status}, retry: {sf.retry_count})", extra={'emoji_type': 'processing'})
                
                # Get the next step to run
                steps = sf.steps.split(',')
                if sf.step_index >= len(steps):
                    sf.status = 'DONE'
                    session.add(sf)
                    logger.info(f"SubFlow {sf.id} marked as complete - all steps finished", extra={'emoji_type': 'success'})
                    return
                    
                next_func_name = steps[sf.step_index]
                logger.debug(f"Next step for subflow {sf.id}: {next_func_name} (step {sf.step_index + 1}/{len(steps)})", extra={'emoji_type': 'step'})
                
                sf.status = 'QUEUED'
                session.add(sf)
                
            # Schedule the next step outside transaction
            logger.debug(f"Scheduling subflow {sf.id} step: {next_func_name}", extra={'emoji_type': 'schedule'})
            self._schedule_subflow(sf.id, self._get_flow_function(next_func_name), sf.episode_id)
            
        except Exception as e:
            logger.error(f"poll_and_enqueue error for action '{self.action}': {e}", extra={'emoji_type': 'error'})
        finally:
            session.close()

    def retry_failed_subflows(self):
        """
        Runs once daily to retry subflows that failed after max_retries.
        """
        logger.info(f"Starting daily retry of failed subflows for action '{self.action}'", extra={'emoji_type': 'retry'})
        session = get_session()
        try:
            failed = session.query(SubFlow).filter(
                SubFlow.status == 'FAILED',
                SubFlow.retry_count >= self.max_retries
            ).all()
            
            logger.info(f"Found {len(failed)} failed subflows to retry for action '{self.action}'", extra={'emoji_type': 'processing'})
            
            retry_count = 0
            for sf in failed:
                try:
                    logger.debug(f"Retrying subflow {sf.id} (was failed with {sf.retry_count} retries)", extra={'emoji_type': 'retry'})
                    sf.status = 'QUEUED'
                    sf.retry_count = 0
                    session.add(sf)
                    
                    # Get the current step to retry
                    steps = sf.steps.split(',')
                    if sf.step_index < len(steps):
                        current_step = steps[sf.step_index]
                        self._schedule_subflow(sf.id, self._get_flow_function(current_step), sf.episode_id)
                        retry_count += 1
                        logger.debug(f"Rescheduled subflow {sf.id} step: {current_step}", extra={'emoji_type': 'schedule'})
                    else:
                        logger.warning(f"Subflow {sf.id} has invalid step_index {sf.step_index} >= {len(steps)}", extra={'emoji_type': 'warning'})
                        
                except Exception as step_error:
                    logger.error(f"Failed to retry subflow {sf.id}: {step_error}", extra={'emoji_type': 'error'})
                    
            session.commit()
            logger.info(f"Successfully retried {retry_count} failed subflows for action '{self.action}'", extra={'emoji_type': 'success'})
            
        except Exception as e:
            logger.error(f"retry_failed_subflows error for action '{self.action}': {e}", extra={'emoji_type': 'error'})
            session.rollback()
        finally:
            session.close()

    def enqueue(self, obj): 
        """
        Enqueue an object for processing.
        
        Args:
            obj (obj Model): The object model of the object to process
        Returns:
            int: The ID of the enqueued object, or None on failure
        """
        logger.info(f"Enqueuing object for processing - action: {self.action}", extra={'emoji_type': 'processing'})
        session = get_session()
        
        if isinstance(obj, (Movie, Series, Episode)):
            self.model = obj.__class__
            logger.debug(f"Object type: {self.model.__name__}, ID: {obj.id}", extra={'emoji_type': 'debug'})
        else:
            logger.error(f"Invalid model type for object {obj} - expected Movie, Series, or Episode", extra={'emoji_type': 'error'})
            return None
            
        obj_id = obj.id if isinstance(obj, (Movie, Series, Episode)) else obj
        
        try:
            ent = session.query(self.model).get(obj_id)
            if not ent:
                logger.warning(f"No {self.model.__name__} found with ID {obj_id}", extra={'emoji_type': 'warning'})
                return None
                
            if ent.status != 'PENDING':
                # Check if this is a reprocessing request (entity is DONE but we want to restart)
                if ent.status == 'DONE':
                    logger.info(f"{self.model.__name__} {obj_id} has status 'DONE' - resetting to PENDING for reprocessing", extra={'emoji_type': 'refresh'})
                    ent.status = 'PENDING'
                    ent.current_step_name = None  # Reset the step to start from beginning
                    session.add(ent)
                    
                    # Cancel any existing SubFlows for this entity (including same action)
                    if self.model is Movie:
                        existing_subflows = session.query(SubFlow).filter(
                            SubFlow.movie_id == obj_id,
                            SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE'])  # Include DONE to reset completed flows
                        ).all()
                    elif self.model is Series:
                        existing_subflows = session.query(SubFlow).filter(
                            SubFlow.series_id == obj_id,
                            SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE'])
                        ).all()
                    elif self.model is Episode:
                        existing_subflows = session.query(SubFlow).filter(
                            SubFlow.episode_id == obj_id,
                            SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE'])
                        ).all()
                    else:
                        existing_subflows = []
                        
                    if existing_subflows:
                        logger.info(f"Found {len(existing_subflows)} existing subflows for {self.model.__name__} {obj_id}, cancelling for reprocessing", extra={'emoji_type': 'refresh'})
                        for old_sf in existing_subflows:
                            logger.debug(f"Cancelling SubFlow {old_sf.id} (action: {old_sf.action}, status: {old_sf.status})", extra={'emoji_type': 'cancel'})
                            old_sf.status = 'CANCELLED'
                            old_sf.error_message = f"Cancelled for reprocessing by action: {self.action}"
                            session.add(old_sf)
                            
                            # Try to cancel scheduled jobs
                            job_id_pattern = f"{old_sf.action}_{old_sf.id}_"
                            try:
                                jobs_to_remove = []
                                for job in self.scheduler.get_jobs():
                                    if job.id and job.id.startswith(job_id_pattern):
                                        jobs_to_remove.append(job.id)
                                
                                for job_id in jobs_to_remove:
                                    self.scheduler.remove_job(job_id)
                                    logger.debug(f"Cancelled scheduled job: {job_id}", extra={'emoji_type': 'cancel'})
                                    
                            except Exception as e:
                                logger.warning(f"Failed to cancel job for SubFlow {old_sf.id}: {e}", extra={'emoji_type': 'warning'})
                        
                        session.commit()
                        logger.info(f"Successfully reset {self.model.__name__} {obj_id} for reprocessing", extra={'emoji_type': 'success'})
                else:
                    logger.warning(f"{self.model.__name__} {obj_id} has status '{ent.status}' (expected PENDING)", extra={'emoji_type': 'warning'})
                    return None
                
            logger.debug(f"Found PENDING {self.model.__name__} {obj_id} - creating subflows", extra={'emoji_type': 'success'})
            
            # Call _create_subflows with the initial flow entry
            initial_entry = flow_manager.get_initial(self.action)
            logger.debug(f"Initial flow entry type: {type(initial_entry)}", extra={'emoji_type': 'debug'})
            
            self._create_subflows(obj_id, initial_entry)
            
            session.commit()
            logger.info(f"Successfully enqueued {self.model.__name__} {obj_id} for processing", extra={'emoji_type': 'success'})
            return obj_id
            
        except Exception as e:
            logger.error(f"enqueue error for {self.model.__name__ if self.model else 'unknown'} {obj_id}: {e}", extra={'emoji_type': 'error'})
            session.rollback()
            return None
        finally:
            session.close()

    def _create_subflows(
        self,
        ent_id: int,
        entry: Union[Callable, List[Callable], Dict[str, List[Callable]]],
    ):
        logger.debug(f"Creating subflows for {self.model.__name__ if self.model else 'unknown'} {ent_id}", extra={'emoji_type': 'processing'})
        session = get_session()
        try:
            # Initial explosion for Series: entry is first step, not a dict
            if not isinstance(entry, dict):
                logger.debug(f"Processing single entry/list for {self.model.__name__}", extra={'emoji_type': 'debug'})
                
                if self.model is Series:
                    eps = session.query(Episode.id).join(Season).filter(
                        Season.series_id == ent_id,
                        Episode.status == 'PENDING'
                    ).all()
                    
                    logger.info(f"Found {len(eps)} pending episodes for series {ent_id}", extra={'emoji_type': 'tv'})
                    
                    if eps:
                        for (eid,) in eps:
                            logger.debug(f"Creating subflow for episode {eid}", extra={'emoji_type': 'debug'})
                            self._make_or_schedule(
                                session, ent_id, branch=str(eid), entry=entry, context=eid
                            )
                    else:
                        logger.warning(f"No pending episodes found for series_id: {ent_id}", extra={'emoji_type': 'warning'})
                        return
                        
                elif self.model is Episode:
                    # Validate that ent_id corresponds to an existing episode
                    episode = session.query(Episode).filter(Episode.id == ent_id).first()
                    if not episode:
                        logger.error(f"Invalid episode_id: {ent_id}", extra={'emoji_type': 'error'})
                        return
                    logger.debug(f"Creating subflow for single episode {ent_id}", extra={'emoji_type': 'tv'})
                    self._make_or_schedule(
                        session, ent_id, branch=str(ent_id), entry=entry, context=ent_id
                    )
                    
                elif self.model is Movie:
                    logger.debug(f"Creating subflow for movie {ent_id}", extra={'emoji_type': 'movie'})
                    self._make_or_schedule(
                        session, ent_id, branch="main", entry=entry, context=None
                    )

            # Handle dict branches at any step
            else:
                logger.debug(f"Processing dict entry with {len(entry)} branches", extra={'emoji_type': 'debug'})
                for branch_key, funcs in entry.items():
                    logger.debug(f"Processing branch '{branch_key}' with {len(funcs) if isinstance(funcs, list) else 1} functions", extra={'emoji_type': 'branch'})
                    
                    # Determine contexts based on model type
                    if self.model is Series:
                        contexts = [e.id for e in session.query(Episode.id).join(Season).filter(Season.series_id == ent_id)]
                        logger.debug(f"Series branch: found {len(contexts)} episode contexts", extra={'emoji_type': 'tv'})
                    elif self.model is Episode:
                        contexts = [ent_id]
                        logger.debug(f"Episode branch: using single context {ent_id}", extra={'emoji_type': 'tv'})
                    elif self.model is Movie:
                        contexts = [None]
                        logger.debug(f"Movie branch: using None context", extra={'emoji_type': 'movie'})
    
                    for ctx in contexts:
                        self._make_or_schedule(
                            session=session,
                            ent_id=ent_id,
                            branch=branch_key,
                            entry=funcs,
                            context=ctx
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
    ):
        logger.debug(f"Making/scheduling subflow for {self.model.__name__} {ent_id}, branch: {branch}, context: {context}", extra={'emoji_type': 'debug'})
        
        # Check for existing SubFlows for this entity and cancel them (including completed ones for fresh processing)
        if self.model is Movie:
            existing_subflows = session.query(SubFlow).filter(
                SubFlow.movie_id == ent_id,
                or_(
                    SubFlow.action != self.action,  # Different action
                    and_(
                        SubFlow.action == self.action,
                        SubFlow.status.in_(['DONE', 'FAILED'])  # Same action but completed/failed - cancel for fresh processing
                    )
                ),
                SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE', 'FAILED'])
            ).all()
        elif self.model is Series:
            existing_subflows = session.query(SubFlow).filter(
                SubFlow.series_id == ent_id,
                or_(
                    SubFlow.action != self.action,  # Different action
                    and_(
                        SubFlow.action == self.action,
                        SubFlow.status.in_(['DONE', 'FAILED'])  # Same action but completed/failed - cancel for fresh processing
                    )
                ),
                SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE', 'FAILED'])
            ).all()
        elif self.model is Episode:
            existing_subflows = session.query(SubFlow).filter(
                SubFlow.episode_id == ent_id,
                or_(
                    SubFlow.action != self.action,  # Different action
                    and_(
                        SubFlow.action == self.action,
                        SubFlow.status.in_(['DONE', 'FAILED'])  # Same action but completed/failed - cancel for fresh processing
                    )
                ),
                SubFlow.status.in_(['PENDING', 'QUEUED', 'DONE', 'FAILED'])
            ).all()
        else:
            existing_subflows = []
            
        if existing_subflows:
            logger.info(f"Found {len(existing_subflows)} existing subflows for {self.model.__name__} {ent_id} to cancel for fresh processing", extra={'emoji_type': 'warning'})
            for old_sf in existing_subflows:
                logger.debug(f"Cancelling SubFlow {old_sf.id} (action: {old_sf.action}, status: {old_sf.status})", extra={'emoji_type': 'cancel'})
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
                        logger.debug(f"Cancelled scheduled job: {job_id}", extra={'emoji_type': 'cancel'})
                        
                except Exception as e:
                    logger.warning(f"Failed to cancel job for SubFlow {old_sf.id}: {e}", extra={'emoji_type': 'warning'})
            
            session.commit()
            logger.info(f"Successfully cancelled {len(existing_subflows)} conflicting subflows", extra={'emoji_type': 'success'})
        
        # steps string
        steps = (entry.__name__ if callable(entry) else
                ','.join(f.__name__ for f in entry))
        logger.debug(f"Subflow steps: {steps}", extra={'emoji_type': 'step'})
        
        # lookup by identity - exclude cancelled and completed SubFlows for fresh processing
        filter_kwargs = {'branch': branch, 'steps': steps, 'action': self.action}
        if self.model is Movie:
            filter_kwargs['movie_id'] = ent_id
        else:
            filter_kwargs['series_id'] = ent_id
            filter_kwargs['episode_id'] = context
            
        logger.debug(f"Looking for existing SubFlow with: {filter_kwargs}", extra={'emoji_type': 'debug'})
        sf = session.query(SubFlow).filter_by(**filter_kwargs).filter(
            SubFlow.status.in_(['PENDING', 'QUEUED'])  # Only reuse pending/queued SubFlows, not completed ones
        ).first()
        
        if not sf:
            logger.debug(f"No existing SubFlow found, creating new one for {self.model.__name__} {ent_id}", extra={'emoji_type': 'new'})
            sf = SubFlow(
                movie_id=ent_id if self.model is Movie else None,
                series_id=ent_id if self.model is Series else None,
                episode_id=context,
                action=self.action,
                branch=branch,
                steps=steps,
                step_index=0,
                status='PENDING',
            )
            session.add(sf)
            session.commit()
            logger.info(f"Created SubFlow {sf.id} for {self.model.__name__} {ent_id}", extra={'emoji_type': 'success'})
        else:
            logger.debug(f"SubFlow already exists: {sf.id} (status: {sf.status})", extra={'emoji_type': 'info'})
            
        # schedule first function
        func = entry if callable(entry) else entry[0]
        logger.debug(f"Scheduling first function: {func.__name__} for SubFlow {sf.id}", extra={'emoji_type': 'schedule'})
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
                logger.info(f"Cancelling Plex subflow {sf_id} function '{func.__name__}' - Plex is disabled", extra={'emoji_type': 'skip'})
                self._cancel_subflow(sf_id, "Plex is disabled")
                return
        elif 'jellyfin' in lname:
            executor = 'jellyfin'
            # Check if Jellyfin is enabled
            if not settings.jellyfin_enabled:
                logger.info(f"Cancelling Jellyfin subflow {sf_id} function '{func.__name__}' - Jellyfin is disabled", extra={'emoji_type': 'skip'})
                self._cancel_subflow(sf_id, "Jellyfin is disabled")
                return
        else:
            executor = 'default'
            
        logger.debug(f"Scheduling subflow {sf_id} function '{func.__name__}' on executor '{executor}'", extra={'emoji_type': 'schedule'})
        
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
            logger.debug(f"Successfully scheduled job {job_id} for subflow {sf_id}", extra={'emoji_type': 'success'})
        except Exception as e:
            logger.error(f"Failed to schedule subflow {sf_id} function '{func.__name__}': {e}", extra={'emoji_type': 'error'})

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
        logger.info(f"Starting subflow {sf_id} step '{step_name}' with context {context}", extra={'emoji_type': 'processing'})
        session = get_session()
        try:
            sf = session.query(SubFlow).get(sf_id)
            if not sf:
                logger.error(f"SubFlow {sf_id} not found in database", extra={'emoji_type': 'error'})
                return
                
            # Check if SubFlow was cancelled before execution
            if sf.status == 'CANCELLED':
                logger.info(f"SubFlow {sf_id} was cancelled, skipping execution: {sf.error_message}", extra={'emoji_type': 'cancel'})
                return
                
            logger.debug(f"SubFlow {sf_id} details: action={sf.action}, status={sf.status}, step_index={sf.step_index}", extra={'emoji_type': 'debug'})
            
            steps = sf.steps.split(',')
            retries = sf.retry_count or 0
            success = False
            error = None

            if sf.status != "DONE":
                logger.debug(f"Executing step '{step_name}' for subflow {sf_id} (attempt 1)", extra={'emoji_type': 'step'})
                
                for attempt in range(self.max_retries):
                    try:
                        arg = context if context is not None else sf.movie_id
                        logger.debug(f"Calling {step_name} with arg={arg}, model={self.model}, action={self.action}", extra={'emoji_type': 'debug'})
                        
                        flow_func = self._get_flow_function(step_name)
                        result = flow_func(session, arg, self.model, self.action)
                        success = bool(result)

                        if success:
                            logger.info(f"Step '{step_name}' succeeded for subflow {sf_id} on attempt {attempt + 1}", extra={'emoji_type': 'success'})
                            break
                        else:
                            logger.warning(f"Step '{step_name}' returned False for subflow {sf_id} on attempt {attempt + 1}", extra={'emoji_type': 'warning'})
                            
                    except Exception as e:
                        error = e
                        retries += 1
                        tb = traceback.format_exc()
                        logger.warning(f"Step '{step_name}' subflow {sf_id} attempt {attempt + 1} failed: {e}", extra={'emoji_type': 'warning'})
                        logger.debug(f"Full traceback for subflow {sf_id} attempt {attempt + 1}:\n{tb}", extra={'emoji_type': 'debug'})
                        
                        if attempt < self.max_retries - 1:
                            logger.debug(f"Will retry step '{step_name}' for subflow {sf_id}", extra={'emoji_type': 'retry'})
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
                    logger.info(f"SubFlow {sf_id} advancing to next step: {next_name} (step {sf.step_index + 1}/{len(steps)})", extra={'emoji_type': 'step'})
                    # Update status but don't mark as DONE yet since there are more steps
                    session.add(sf)
                    session.commit()
                    self._schedule_subflow(sf_id, self._get_flow_function(next_name), context)
                else:
                    logger.info(f"SubFlow {sf_id} completed all steps, marking as DONE and checking for remaining subflows", extra={'emoji_type': 'success'})
                    
                    # Mark this SubFlow as DONE first, then check for remaining
                    sf.status = 'DONE'
                    session.add(sf)
                    session.commit()
                    
                    # Initialize variables to prevent UnboundLocalError
                    still_pending = None
                    advance_id = None
                    
                    # Check if all subflows for this entity AND ACTION are complete
                    if self.model is Movie:
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
                        
                    elif self.model is Series:
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
                            
                    elif self.model is Episode:
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
                        logger.warning(f"Unknown model type for SubFlow advancement: {self.model}", extra={'emoji_type': 'warning'})
                        still_pending = True  # Prevent advancement
                        advance_id = None
                        
                    if not still_pending and advance_id:
                        logger.info(f"All subflows complete for {self.model.__name__} {advance_id} action {self.action}, advancing entity", extra={'emoji_type': 'success'})
                        
                        # Update entity's current_step_name to reflect the completed step
                        entity = session.query(self.model).get(advance_id)
                        if entity:
                            # Get the step name that was just completed
                            completed_step_name = step_name  # This is the step that just finished
                            logger.debug(f"Updating {self.model.__name__} {advance_id} current_step_name from '{entity.current_step_name}' to '{completed_step_name}'", extra={'emoji_type': 'debug'})
                            entity.current_step_name = completed_step_name
                            session.add(entity)
                            session.commit()
                        
                        self._advance_entity(advance_id)
                    elif still_pending:
                        logger.debug(f"Still have pending subflows for {self.model.__name__} action {self.action}, not advancing yet", extra={'emoji_type': 'info'})
                    else:
                        logger.warning(f"Could not determine advance_id for {self.model.__name__}", extra={'emoji_type': 'warning'})

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
                    logger.debug(f"Error details written to {log_dir}/error_{step_name}.log", extra={'emoji_type': 'debug'})
                except Exception as log_error:
                    logger.error(f"Failed to write error log: {log_error}", extra={'emoji_type': 'error'})
                
                # Commit the failed status
                session.add(sf)
                session.commit()
            
        except Exception as outer_error:
            logger.error(f"Critical error in _run_subflow for {sf_id}: {outer_error}", extra={'emoji_type': 'error'})
            logger.debug(f"Critical error traceback:\n{traceback.format_exc()}", extra={'emoji_type': 'debug'})
        finally:
            session.close()

    def _advance_entity(self, ent_id: int):
        logger.info(f"Advancing {self.model.__name__} {ent_id} to next flow stage", extra={'emoji_type': 'processing'})
        session = get_session()
        try:
            ent = session.query(self.model).get(ent_id)
            if not ent:
                logger.error(f"{self.model.__name__} {ent_id} not found", extra={'emoji_type': 'error'})
                return
                
            logger.debug(f"Current entity status: {ent.status}, current_step_name: {getattr(ent, 'current_step_name', 'None')}", extra={'emoji_type': 'debug'})
            
            entry = flow_manager.next_entry(self.action, None, ent.current_step_name)
            
            if entry:
                new_step_name = flow_manager.get_entry_id(self.action, entry)
                logger.info(f"Advancing {self.model.__name__} {ent_id} from '{ent.current_step_name}' to '{new_step_name}'", extra={'emoji_type': 'step'})
                
                ent.current_step_name = new_step_name
                ent.status = 'QUEUED'                    
                session.add(ent)
                session.commit()
                
                logger.debug(f"Creating subflows for next entry: {type(entry)}", extra={'emoji_type': 'debug'})
                # now create SubFlows for the next entry
                self._create_subflows(ent_id, entry)
                logger.info(f"Successfully advanced {self.model.__name__} {ent_id} to next stage", extra={'emoji_type': 'success'})
                
            else:
                logger.info(f"No more entries for {self.model.__name__} {ent_id} - marking as DONE", extra={'emoji_type': 'success'})
                ent.status = 'DONE'
                session.add(ent)
                session.commit()
                logger.info(f"{self.model.__name__} {ent_id} processing complete", extra={'emoji_type': 'success'})
                
        except Exception as e:
            logger.error(f"Error advancing {self.model.__name__} {ent_id}: {e}", extra={'emoji_type': 'error'})
            session.rollback()
        finally:
            session.close()

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
            
            logger.info(f"Found {len(active_subflows)} active SubFlows for {self.model.__name__} {ent_id}", extra={'emoji_type': 'info'})
            
            # Check if any are still pending
            pending = [sf for sf in active_subflows if sf.status in ['PENDING', 'QUEUED', 'FAILED']]
            
            if not pending:
                logger.info(f"No pending SubFlows found, advancing {self.model.__name__} {ent_id}", extra={'emoji_type': 'success'})
                self._advance_entity(ent_id)
            else:
                logger.info(f"Still have {len(pending)} pending SubFlows, not advancing yet", extra={'emoji_type': 'info'})
                for sf in pending:
                    logger.debug(f"  Pending SubFlow {sf.id}: {sf.status} - {sf.steps}", extra={'emoji_type': 'debug'})
        except Exception as e:
            logger.error(f"Error checking entity advancement: {e}", extra={'emoji_type': 'error'})
        finally:
            session.close()
            
    def _get_flow_function(self, func_name: str) -> Callable:
        logger.debug(f"Getting flow function '{func_name}' for action '{self.action}'", extra={'emoji_type': 'debug'})
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
    else:
        logger.warning(f"Unknown model type for action '{action}', defaulting to Movie", extra={'emoji_type': 'warning'})
        return Movie

# Instantiate schedulers
logger.info("Initializing ActionSchedulers for all configured flows", extra={'emoji_type': 'start'})
actions = list(flow_manager.flows.keys())
logger.debug(f"Available actions: {actions}", extra={'emoji_type': 'debug'})

for action in actions:
    try:
        logger.debug(f"Creating scheduler for action '{action}'", extra={'emoji_type': 'processing'})
        scheduler = ActionScheduler(action)
        
        # Set the model type based on action name
        scheduler.model = get_model_for_action(action)
        logger.debug(f"Set model type for {action}_scheduler: {scheduler.model.__name__}", extra={'emoji_type': 'debug'})
        
        globals()[f"{action}_scheduler"] = scheduler
        logger.info(f"Successfully created scheduler: {action}_scheduler with model {scheduler.model.__name__}", extra={'emoji_type': 'success'})
    except Exception as e:
        logger.error(f"Failed to create scheduler for action '{action}': {e}", extra={'emoji_type': 'error'})

logger.info(f"Scheduler initialization complete - created {len(actions)} schedulers", extra={'emoji_type': 'success'})
