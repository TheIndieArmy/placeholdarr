import time
from services.jobs import claim_jobs, requeue_job, job_done
from services.enrich import process_enrich_base_subflow, enrich_episode
from services.enrich_and_merge import process_enrich_and_merge
from services.list_capture import create_episode_subflows_for_series
from services.postgres.db import get_session
from services.postgres.models import Job as JobModel
from services.postgres.models import SubFlow
from core.logger import logger
from services.jobs import insert_job, insert_job_with_session
from services.fs_scan import scan_once_if_needed, scan_placeholder_roots


def _handle_claimed_job(job):
    job_id = job['id']
    job_type = job['job_type']
    # Backwards-compatibility: map legacy phase job types to the new combined phase
    if job_type in ('subjob:merge_scan', 'subjob:enrich_files'):
        job_type = 'subjob:enrich_and_merge'
    payload = job['payload'] or {}
    # Quick pre-check: if the Job row or referenced SubFlow has already been cancelled
    session = get_session()
    try:
        try:
            jrow = session.query(JobModel).filter(JobModel.id == job_id).first()
        except Exception:
            jrow = None
        if jrow and getattr(jrow, 'status', None) == 'CANCELLED':
            logger.info(f"Job {job_id} is CANCELLED; marking done")
            job_done(job_id, success=True)
            try:
                session.close()
            except Exception:
                pass
            return True

        # If the job references a SubFlow that has been cancelled, abort early
        try:
            sfid = payload.get('subflow_id')
            if sfid:
                try:
                    sfrow = session.query(SubFlow).filter(SubFlow.id == int(sfid)).first()
                except Exception:
                    sfrow = None
                if sfrow and getattr(sfrow, 'status', None) == 'CANCELLED':
                    logger.info(f"SubFlow {sfid} is CANCELLED; marking job {job_id} done")
                    job_done(job_id, success=True)
                    try:
                        session.close()
                    except Exception:
                        pass
                    return True
        except Exception:
            # best-effort pre-check — proceed if anything goes wrong here
            pass
    finally:
        try:
            session.close()
        except Exception:
            pass
    try:
        if job_type == 'subjob:enrich_base':
            subflow_id = payload.get('subflow_id')
            if not subflow_id:
                logger.info(f"Job {job_id} missing subflow_id; marking FAILED")
                job_done(job_id, success=False, error_message='missing_subflow_id')
                return False
            # Guard: ensure the SubFlow is at the expected step_index and phase
            session = get_session()
            try:
                sf = session.query(SubFlow).filter(SubFlow.id == int(subflow_id)).first()
            finally:
                try:
                    session.close()
                except Exception:
                    pass

            if not sf:
                logger.info(f"Job {job_id} references missing SubFlow {subflow_id}; marking DONE to avoid retries")
                job_done(job_id, success=True)
                return True

            # Parse expected phase from SubFlow.steps using step_index
            try:
                steps = (sf.steps or '').split(',')
                expected_step_index = int(payload.get('step_index', 0))
                expected_phase = payload.get('phase')
                actual_phase = steps[sf.step_index] if 0 <= sf.step_index < len(steps) else None
            except Exception:
                actual_phase = None
                expected_phase = payload.get('phase')

            # If the job is for a later/earlier phase than the SubFlow currently is, requeue briefly
            if actual_phase != expected_phase or sf.step_index != expected_step_index:
                logger.info(f"Job {job_id} (phase={expected_phase} idx={expected_step_index}) is not ready: subflow {sf.id} is at phase={actual_phase} idx={sf.step_index}; requeueing")
                requeue_job(job_id, delay_seconds=3)
                return False

            # Execute the current phase handler
            ok = process_enrich_base_subflow(subflow_id)
            if ok:
                # Advance the SubFlow.step_index and enqueue next-phase job if any
                session = get_session()
                try:
                        sfrow = session.query(SubFlow).filter(SubFlow.id == int(subflow_id)).with_for_update().first()
                        if sfrow:
                            try:
                                steps = (sfrow.steps or '').split(',')
                                prev_idx = int(sfrow.step_index or 0)
                                # compute next index; only advance if we are not already at the final step
                                if prev_idx + 1 < len(steps):
                                    new_idx = prev_idx + 1
                                    sfrow.step_index = new_idx
                                    # Prepare next job payload and insert it in the same transaction
                                    next_phase = steps[new_idx]
                                    payload_next = {'run_id': payload.get('run_id'), 'phase': next_phase, 'subflow_id': sfrow.id, 'step_index': new_idx}
                                    group_id = f"subflow:{sfrow.id}:{next_phase}"
                                    # Add the SubFlow update and insert the next job before committing so both are atomic
                                    session.add(sfrow)
                                    insert_job_with_session(session, f'subjob:{next_phase}', payload_next, group_id=group_id)
                                    session.commit()
                                else:
                                    # already at last step; nothing to enqueue
                                    session.commit()
                            except Exception:
                                session.rollback()
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass

                job_done(job_id, success=True)
                return True
            else:
                # transient failure; requeue with backoff
                # try to read max_attempts from the Job row
                try:
                    session = get_session()
                    jrow = session.query(JobModel).filter(JobModel.id == job_id).first()
                    max_attempts = jrow.max_attempts if jrow else 5
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
                requeue_job(job_id, delay_seconds=10)
                return False
        elif job_type == 'reenrich:episode' or payload.get('episode_id'):
            episode_id = payload.get('episode_id')
            if not episode_id:
                logger.info(f"Job {job_id} missing episode_id; marking FAILED")
                job_done(job_id, success=False, error_message='missing_episode_id')
                return False
            ok = enrich_episode(int(episode_id))
            if ok:
                job_done(job_id, success=True)
                return True
            else:
                # transient failure; requeue
                try:
                    session = get_session()
                    jrow = session.query(JobModel).filter(JobModel.id == job_id).first()
                    max_attempts = jrow.max_attempts if jrow else 5
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
                requeue_job(job_id, delay_seconds=15)
                return False
        elif job_type == 'subjob:create_episode_subflows':
            # Expect payload to contain subflow_id and run_id
            subflow_id = payload.get('subflow_id')
            if not subflow_id:
                logger.info(f"Job {job_id} missing subflow_id; marking FAILED")
                job_done(job_id, success=False, error_message='missing_subflow_id')
                return False
            # Guard: ensure the SubFlow is at the expected step_index and phase
            session = get_session()
            try:
                sf = session.query(SubFlow).filter(SubFlow.id == int(subflow_id)).first()
            finally:
                try:
                    session.close()
                except Exception:
                    pass

            if not sf:
                logger.info(f"Job {job_id} references missing SubFlow {subflow_id}; marking DONE to avoid retries")
                job_done(job_id, success=True)
                return True

            # Parse expected phase/index
            try:
                steps = (sf.steps or '').split(',')
                expected_step_index = int(payload.get('step_index', 0))
                expected_phase = payload.get('phase')
                actual_phase = steps[sf.step_index] if 0 <= sf.step_index < len(steps) else None
            except Exception:
                actual_phase = None
                expected_phase = payload.get('phase')

            if actual_phase != expected_phase or sf.step_index != expected_step_index:
                logger.info(f"Job {job_id} (phase={expected_phase} idx={expected_step_index}) is not ready: subflow {sf.id} is at phase={actual_phase} idx={sf.step_index}; requeueing")
                requeue_job(job_id, delay_seconds=3)
                return False

            # Call the list-capture helper to create episode subflows
            try:
                created = create_episode_subflows_for_series(sf.series_id, payload.get('run_id'))
                ok = created is not False
            except Exception as ex:
                logger.exception(f"Failed to create episode subflows for series {getattr(sf, 'series_id', None)}: {ex}")
                return False

            if ok:
                # Advance the SubFlow.step_index and enqueue next-phase job if any (transactional)
                session = get_session()
                try:
                    sfrow = session.query(SubFlow).filter(SubFlow.id == int(subflow_id)).with_for_update().first()
                    if sfrow:
                        try:
                            steps = (sfrow.steps or '').split(',')
                            prev_idx = int(sfrow.step_index or 0)
                            if prev_idx + 1 < len(steps):
                                new_idx = prev_idx + 1
                                sfrow.step_index = new_idx
                                next_phase = steps[new_idx]
                                payload_next = {'run_id': payload.get('run_id'), 'phase': next_phase, 'subflow_id': sfrow.id, 'step_index': new_idx}
                                group_id = f"subflow:{sfrow.id}:{next_phase}"
                                session.add(sfrow)
                                insert_job_with_session(session, f'subjob:{next_phase}', payload_next, group_id=group_id)
                                session.commit()
                            else:
                                session.commit()
                        except Exception:
                            session.rollback()
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass

                job_done(job_id, success=True)
                return True
            else:
                # transient failure; requeue
                try:
                    session = get_session()
                    jrow = session.query(JobModel).filter(JobModel.id == job_id).first()
                    max_attempts = jrow.max_attempts if jrow else 5
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
                requeue_job(job_id, delay_seconds=15)
                return False
        elif job_type == 'subjob:fs_scan':
            # Expect payload to include run_id and optionally content identifiers or paths
            subflow_id = payload.get('subflow_id')
            if not subflow_id:
                logger.info(f"Job {job_id} missing subflow_id; marking FAILED")
                job_done(job_id, success=False, error_message='missing_subflow_id')
                return False

            # Guard: ensure the SubFlow is at the expected step_index and phase
            session = get_session()
            try:
                sf = session.query(SubFlow).filter(SubFlow.id == int(subflow_id)).first()
            finally:
                try:
                    session.close()
                except Exception:
                    pass

            if not sf:
                logger.info(f"Job {job_id} references missing SubFlow {subflow_id}; marking DONE to avoid retries")
                job_done(job_id, success=True)
                return True

            try:
                steps = (sf.steps or '').split(',')
                expected_step_index = int(payload.get('step_index', 0))
                expected_phase = payload.get('phase')
                actual_phase = steps[sf.step_index] if 0 <= sf.step_index < len(steps) else None
            except Exception:
                actual_phase = None
                expected_phase = payload.get('phase')

            if actual_phase != expected_phase or sf.step_index != expected_step_index:
                logger.info(f"Job {job_id} (phase={expected_phase} idx={expected_step_index}) is not ready: subflow {sf.id} is at phase={actual_phase} idx={sf.step_index}; requeueing")
                requeue_job(job_id, delay_seconds=3)
                return False

            # Decide whether this is part of a fullsync run.
            run_id = payload.get('run_id') or ''
            is_fullsync = str(run_id).startswith('fullsync:')

            try:
                if is_fullsync:
                    # Fullsync: call the global idempotent scanner (it will no-op if already run)
                    logger.info(f"Job {job_id}: triggering idempotent fullsync FS-scan (no-op if already run)")
                    # Request observed paths so we can run centralized enrichment exactly once for the run
                    observed = None
                    try:
                        observed = scan_once_if_needed(run_id)
                    except Exception:
                        # In case older/newer signatures raise unexpected errors, keep trying to call it in a best-effort way
                        try:
                            observed = scan_once_if_needed(run_id)
                        except Exception:
                            logger.exception('scan_once_if_needed raised an unexpected exception')

                    # Normalize and handle the scanner's possible return shapes:
                    # - list of paths -> run process_placeholders(paths=...)
                    # - int (legacy) -> nothing to enrich here
                    # - (count, info) tuple -> may indicate a skipped scan with info
                    try:
                        # Case: list of paths
                        if isinstance(observed, list) and observed:
                            from services.enrich_and_merge import process_placeholders
                            try:
                                res = process_placeholders(paths=observed)
                                logger.info(f"Triggered enrichment for {res.get('processed',0)} placeholders after FS-scan (fullsync)", extra={'emoji_type': 'placeholder'})
                            except Exception:
                                logger.exception('Failed to trigger process_placeholders after fullsync fs_scan')
                        # Case: tuple (count, info)
                        elif isinstance(observed, tuple) and len(observed) == 2:
                            count, info = observed
                            # If the scanner explicitly skipped due to time guard, log that at INFO
                            try:
                                reason = info.get('reason') if isinstance(info, dict) else None
                            except Exception:
                                reason = None
                            if reason == 'time_guard':
                                delta = info.get('delta') if isinstance(info, dict) else None
                                threshold = info.get('threshold') if isinstance(info, dict) else None
                                logger.info(f"FS-scan (fullsync) skipped by time guard: last observation {delta}s (<{threshold}s); run_id={run_id}")
                            else:
                                logger.info(f"FS-scan (fullsync) completed with count={count}; info={info}; run_id={run_id}")
                        # Case: legacy int or None -> nothing to do
                        else:
                            # observed may be an int (legacy) or None; log debug for visibility
                            logger.debug(f"FS-scan returned: {observed} (no placeholders paths to trigger centralized enrichment)")
                    except Exception:
                        logger.exception('Unexpected error handling scan_once_if_needed result')
                else:
                    # Non-fullsync: attempt a targeted scan
                    # Prefer explicit 'paths' in the payload
                    paths = payload.get('paths')
                    if paths and isinstance(paths, list) and paths:
                        logger.info(f"Job {job_id}: running targeted FS-scan for provided paths")
                        scan_placeholder_roots(paths)
                    else:
                        # If the SubFlow references a specific movie/episode, scan its placeholder folder if configured
                        session = get_session()
                        try:
                            movie_id = payload.get('movie_id') or getattr(sf, 'movie_id', None)
                            episode_id = payload.get('episode_id') or getattr(sf, 'episode_id', None)
                            paths_to_scan = []
                            if movie_id:
                                from services.postgres.models import Movie
                                mv = session.query(Movie).filter(Movie.id == int(movie_id)).first()
                                if mv and mv.placeholder_folder:
                                    paths_to_scan.append(mv.placeholder_folder)
                            if episode_id:
                                from services.postgres.models import Episode
                                ep = session.query(Episode).filter(Episode.id == int(episode_id)).first()
                                if ep and ep.placeholder_folder:
                                    paths_to_scan.append(ep.placeholder_folder)
                        finally:
                            try:
                                session.close()
                            except Exception:
                                pass

                        if paths_to_scan:
                            logger.info(f"Job {job_id}: running targeted FS-scan for content folders: {paths_to_scan}")
                            observed = scan_placeholder_roots(paths_to_scan, return_paths=True)
                            try:
                                if isinstance(observed, list) and observed:
                                    from services.enrich_and_merge import process_placeholders
                                    try:
                                        res = process_placeholders(paths=observed)
                                        logger.info(f"Triggered enrichment for {res.get('processed',0)} placeholders after targeted FS-scan", extra={'emoji_type': 'placeholder'})
                                    except Exception:
                                        logger.exception('Failed to trigger process_placeholders after targeted fs_scan')
                            except Exception:
                                pass
                        else:
                            # Fallback: run global idempotent scanner
                            # Only pass run_id when this is an explicit fullsync run.
                            logger.info(f"Job {job_id}: no explicit targets; falling back to idempotent global FS-scan")
                            if is_fullsync:
                                scan_once_if_needed(run_id)
                            else:
                                # Non-fullsync fallback should not attempt to claim the fs_scan_run table
                                scan_once_if_needed()
            except Exception as ex:
                logger.exception(f"FS-scan job {job_id} failed: {ex}")
                requeue_job(job_id, delay_seconds=15)
                return False

            # Advance SubFlow and enqueue next-phase job if any (transactional)
            session = get_session()
            try:
                sfrow = session.query(SubFlow).filter(SubFlow.id == int(subflow_id)).with_for_update().first()
                if sfrow:
                    try:
                        steps = (sfrow.steps or '').split(',')
                        prev_idx = int(sfrow.step_index or 0)
                        if prev_idx + 1 < len(steps):
                            new_idx = prev_idx + 1
                            sfrow.step_index = new_idx
                            next_phase = steps[new_idx]
                            payload_next = {'run_id': payload.get('run_id'), 'phase': next_phase, 'subflow_id': sfrow.id, 'step_index': new_idx}
                            group_id = f"subflow:{sfrow.id}:{next_phase}"
                            session.add(sfrow)
                            insert_job_with_session(session, f'subjob:{next_phase}', payload_next, group_id=group_id)
                            session.commit()
                        else:
                            session.commit()
                    except Exception:
                        session.rollback()
            finally:
                try:
                    session.close()
                except Exception:
                    pass

            job_done(job_id, success=True)
            return True
        elif job_type == 'subjob:enrich_and_merge':
            # Expect payload to include subflow_id (and optionally placeholder_ids or paths)
            subflow_id = payload.get('subflow_id')
            if not subflow_id:
                logger.info(f"Job {job_id} missing subflow_id; marking FAILED")
                job_done(job_id, success=False, error_message='missing_subflow_id')
                return False

            # Guard: ensure the SubFlow is at the expected step_index and phase
            session = get_session()
            try:
                sf = session.query(SubFlow).filter(SubFlow.id == int(subflow_id)).first()
            finally:
                try:
                    session.close()
                except Exception:
                    pass

            if not sf:
                logger.info(f"Job {job_id} references missing SubFlow {subflow_id}; marking DONE to avoid retries")
                job_done(job_id, success=True)
                return True

            try:
                steps = (sf.steps or '').split(',')
                expected_step_index = int(payload.get('step_index', 0))
                expected_phase = payload.get('phase')
                actual_phase = steps[sf.step_index] if 0 <= sf.step_index < len(steps) else None
            except Exception:
                actual_phase = None
                expected_phase = payload.get('phase')

            if actual_phase != expected_phase or sf.step_index != expected_step_index:
                logger.info(f"Job {job_id} (phase={expected_phase} idx={expected_step_index}) is not ready: subflow {sf.id} is at phase={actual_phase} idx={sf.step_index}; requeueing")
                requeue_job(job_id, delay_seconds=3)
                return False

            # Call the enrichment/merge processor
            ok = process_enrich_and_merge(subflow_id, payload)
            if ok:
                # Advance the SubFlow.step_index and enqueue next-phase job if any
                session = get_session()
                try:
                        sfrow = session.query(SubFlow).filter(SubFlow.id == int(subflow_id)).with_for_update().first()
                        if sfrow:
                            try:
                                steps = (sfrow.steps or '').split(',')
                                prev_idx = int(sfrow.step_index or 0)
                                # compute next index; only advance if we are not already at the final step
                                if prev_idx + 1 < len(steps):
                                    new_idx = prev_idx + 1
                                    sfrow.step_index = new_idx
                                    # Prepare next job payload and insert it in the same transaction
                                    next_phase = steps[new_idx]
                                    payload_next = {'run_id': payload.get('run_id'), 'phase': next_phase, 'subflow_id': sfrow.id, 'step_index': new_idx}
                                    group_id = f"subflow:{sfrow.id}:{next_phase}"
                                    session.add(sfrow)
                                    insert_job_with_session(session, f'subjob:{next_phase}', payload_next, group_id=group_id)
                                    session.commit()
                                else:
                                    session.commit()
                            except Exception:
                                session.rollback()
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass

                job_done(job_id, success=True)
                return True
            else:
                # transient failure; requeue
                requeue_job(job_id, delay_seconds=15)
                return False
        else:
            logger.info(f"Unhandled job_type: {job_type}")
            job_done(job_id, success=False, error_message=f'unhandled_job_type:{job_type}')
            return False
    except Exception as exc:
        logger.exception(f"Exception while processing job {job_id}: {exc}")
        try:
            requeue_job(job_id, delay_seconds=30)
        except Exception:
            pass
        job_done(job_id, success=False, error_message=str(exc))
        return False


def run_once(limit: int = 10):
    claimed = claim_jobs(limit=limit)
    if not claimed:
        return 0
    processed = 0
    for job in claimed:
        ok = _handle_claimed_job(job)
        if ok:
            processed += 1
    return processed


def run_loop(poll_interval: float = 1.0):
    logger.info('Worker loop starting')
    try:
        while True:
            processed = run_once(limit=10)
            if processed == 0:
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info('Worker loop stopped by KeyboardInterrupt')