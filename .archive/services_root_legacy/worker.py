import time
from datetime import datetime
from services.jobs import claim_jobs, requeue_job, job_done
from services.enrich import process_enrich_base_subflow, enrich_episode, process_determine_subflow
from services.enrich_and_merge import process_enrich_and_merge, process_placeholders
# episode subflow discovery is now performed inline during the series
# `enrich_base` handler; keep the list_capture helper for reuse but do not
# import it at module top-level here.
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
            ok = process_enrich_base_subflow(subflow_id, payload.get('run_id'))
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
        # NOTE: the dedicated 'subjob:create_episode_subflows' phase has been
        # removed: episode subflows are created inline during the series
        # `enrich_base` handler so their initial `subjob:enrich_base` jobs are
        # claimable immediately in the same phase. Any older code paths that
        # still enqueue 'subjob:create_episode_subflows' will be ignored here.
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
        elif job_type == 'subjob:determine':
            # Expect payload to include subflow_id
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

            ok = process_determine_subflow(subflow_id, payload.get('run_id'))
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
                requeue_job(job_id, delay_seconds=15)
                return False
        elif job_type == 'subjob:enrich_and_merge':
            # Expect payload to include subflow_id (and optionally placeholder_ids or paths)
            subflow_id = payload.get('subflow_id')
            if not subflow_id:
                logger.info(f"Job {job_id} missing subflow_id; marking FAILED")
                job_done(job_id, success=False, error_message='missing_subflow_id')
                return False
            # Guard: ensure the SubFlow exists and is at the expected phase/index
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

            # Call the enrichment/merge processor (instrument with debug logging)
            logger.debug(f"Job {job_id}: calling process_enrich_and_merge for subflow={subflow_id} payload_keys={list(payload.keys())}")
            ok = process_enrich_and_merge(subflow_id, payload)
            logger.debug(f"Job {job_id}: process_enrich_and_merge returned {ok}")
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
        elif job_type == 'subjob:materialize':
            # The materialize phase is orchestrated by enqueued 'materialize:placeholder'
            # jobs. At the SubFlow-level we perform a readiness check and then advance
            # the SubFlow step (no-op here) so the overall flow continues.
            subflow_id = payload.get('subflow_id')
            if not subflow_id:
                logger.info(f"Job {job_id} missing subflow_id; marking FAILED")
                job_done(job_id, success=False, error_message='missing_subflow_id')
                return False

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

            # Nothing else required here — materialize:placeholder jobs will perform concrete work.
            # Advance the SubFlow.step_index and enqueue next-phase job if any
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

        elif job_type == 'subjob:process_placeholders' or job_type == 'placeholders:process':
            # Dedicated placeholder-first merging job. Payload may include 'placeholder_ids' or 'paths' or 'limit'.
            try:
                from services.enrich_and_merge import process_placeholders
            except Exception:
                logger.exception('process_placeholders module not available')
                job_done(job_id, success=False, error_message='modules_missing')
                return False

            ph_ids = payload.get('placeholder_ids')
            paths = payload.get('paths')
            limit = payload.get('limit')
            try:
                res = process_placeholders(placeholder_ids=ph_ids, paths=paths, limit=limit)
                ok = bool(res and res.get('ok', False))
            except Exception:
                logger.exception(f"Job {job_id}: process_placeholders raised")
                ok = False

            if ok:
                job_done(job_id, success=True)
                return True
            else:
                requeue_job(job_id, delay_seconds=15)
                return False

        elif job_type == 'materialize:placeholder':
            # Payload: {'content_type': 'movie|movie4k|tv|tv4k', 'content_id': id, 'decision': 'create'|'delete'}
            try:
                from services.materialize import apply_placeholder_decision
                from services.postgres.models import Movie as MovieModel, Episode as EpisodeModel, Season as SeasonModel, Series as SeriesModel
            except Exception:
                logger.exception('materialize module not available')
                job_done(job_id, success=False, error_message='modules_missing')
                return False

            content_type = payload.get('content_type')
            content_id = payload.get('content_id')
            decision = payload.get('decision')
            is_4k = False
            if content_type and content_type.endswith('4k'):
                is_4k = True

            session = get_session()
            try:
                if content_type and content_type.startswith('movie'):
                    mv = session.query(MovieModel).filter(MovieModel.id == int(content_id)).first()
                    if not mv:
                        logger.info(f"materialize: movie {content_id} not found; marking done")
                        job_done(job_id, success=True)
                        return True
                    dec = 'REQUEST_CREATE' if decision == 'create' else 'REQUEST_DELETE' if decision == 'delete' else 'NOOP'
                    apply_placeholder_decision(session=session, media_type='movie', movie=mv, decision=dec, is_4k=is_4k, enqueue=True, commit=True)
                    job_done(job_id, success=True)
                    return True

                if content_type and content_type.startswith('tv'):
                    ep = session.query(EpisodeModel).filter(EpisodeModel.id == int(content_id)).first()
                    if not ep:
                        logger.info(f"materialize: episode {content_id} not found; marking done")
                        job_done(job_id, success=True)
                        return True
                    season = session.query(SeasonModel).filter(SeasonModel.id == int(ep.season_id)).first() if getattr(ep, 'season_id', None) else None
                    series = session.query(SeriesModel).filter(SeriesModel.id == int(season.series_id)).first() if season and getattr(season, 'series_id', None) else None
                    dec = 'REQUEST_CREATE' if decision == 'create' else 'REQUEST_DELETE' if decision == 'delete' else 'NOOP'
                    apply_placeholder_decision(session=session, media_type='tv', series=series, season=season, episode=ep, decision=dec, is_4k=is_4k, enqueue=True, commit=True)
                    job_done(job_id, success=True)
                    return True

                logger.info(f"materialize: unsupported content_type {content_type}; marking done")
                job_done(job_id, success=True)
                return True
            finally:
                try:
                    session.close()
                except Exception:
                    pass

            # materialize handled above; nothing further to do here
        elif job_type == 'placeholder:create':
            # Expect payload to include placeholder_id and enough info to place the dummy
            placeholder_id = payload.get('placeholder_id')
            if not placeholder_id:
                logger.info(f"Job {job_id} missing placeholder_id; marking FAILED")
                job_done(job_id, success=False, error_message='missing_placeholder_id')
                return False

            try:
                from services.postgres.models import Placeholder as PlaceholderModel
                from services.integrations import place_dummy_file
                from services.placeholders import mark_exists, compute_fingerprint, set_lifecycle_status
            except Exception:
                logger.exception('Required modules for placeholder:create not available')
                job_done(job_id, success=False, error_message='modules_missing')
                return False

            session = get_session()
            try:
                ph = session.query(PlaceholderModel).filter(PlaceholderModel.id == int(placeholder_id)).with_for_update().first()
                if not ph:
                    logger.info(f"Job {job_id}: placeholder {placeholder_id} not found; marking done")
                    job_done(job_id, success=True)
                    return True

                # If job provided concrete placement info, attempt to create file
                lib = payload.get('library_root')
                title = payload.get('title')
                year = payload.get('year')
                media_id = payload.get('media_id')
                media_type = payload.get('media_type', 'movie')

                # Refresh session-managed ph in case of detached state
                try:
                    session.expunge(ph)
                    session.add(ph)
                except Exception:
                    pass

                placed = None
                placed = None
                try:
                    # For TV we should pass season/episode details to get per-episode files
                    season_number = payload.get('season_number')
                    episode_number = payload.get('episode_number')
                    episode_title = payload.get('episode_title')
                    # If payload is missing library/title/media id, attempt to derive them from DB rows
                    if media_type and media_type.startswith('tv') and (not lib or not title or not media_id):
                        try:
                            from core.config import settings as _settings
                            from services.postgres.models import Series as SeriesModel, Season as SeasonModel, Episode as EpisodeModel
                            # Prefer placeholder's series_id or episode->season->series
                            sid = getattr(ph, 'season_id', None)
                            ser = None
                            if getattr(ph, 'series_id', None):
                                ser = session.query(SeriesModel).filter(SeriesModel.id == int(getattr(ph, 'series_id'))).first()
                            elif getattr(ph, 'episode_id', None):
                                ep_tmp = session.query(EpisodeModel).filter(EpisodeModel.id == int(getattr(ph, 'episode_id'))).first()
                                if ep_tmp and getattr(ep_tmp, 'season_id', None):
                                    s_tmp = session.query(SeasonModel).filter(SeasonModel.id == int(getattr(ep_tmp, 'season_id'))).first()
                                    if s_tmp and getattr(s_tmp, 'series_id', None):
                                        ser = session.query(SeriesModel).filter(SeriesModel.id == int(getattr(s_tmp, 'series_id'))).first()
                            if ser:
                                # derive media_id (tvdbid or fallback to id)
                                media_id = media_id or getattr(ser, 'tvdbid', None) or getattr(ser, 'id', None)
                                title = title or getattr(ser, 'title', None)
                                # derive library root from settings depending on 4k flags
                                try:
                                    is_4k_flag = getattr(ser, 'is_4k', False)
                                    lib = lib or (_settings.TV_LIBRARY_FOLDER_4K if is_4k_flag else _settings.TV_LIBRARY_FOLDER)
                                except Exception:
                                    lib = lib or None
                        except Exception:
                            # best-effort; non-fatal
                            pass
                    # If not present in payload, try resolving from provided ids
                    if media_type and media_type.startswith('tv') and (season_number is None or episode_number is None):
                        try:
                            from services.postgres.models import Season as SeasonModel, Episode as EpisodeModel
                            # Prefer episode_id if available
                            epid = payload.get('episode_id') or getattr(ph, 'episode_id', None)
                            if epid:
                                ep_row = session.query(EpisodeModel).filter(EpisodeModel.id == int(epid)).first()
                                if ep_row:
                                    episode_number = episode_number or getattr(ep_row, 'episode_number', None)
                                    episode_title = episode_title or getattr(ep_row, 'title', None)
                                    sid = getattr(ep_row, 'season_id', None)
                                    if sid:
                                        srow = session.query(SeasonModel).filter(SeasonModel.id == int(sid)).first()
                                        if srow:
                                            season_number = season_number or getattr(srow, 'season_number', None)
                            else:
                                # Fall back to season_id -> season_number
                                sid = payload.get('season_id')
                                if sid:
                                    srow = session.query(SeasonModel).filter(SeasonModel.id == int(sid)).first()
                                    if srow:
                                        season_number = season_number or getattr(srow, 'season_number', None)
                        except Exception:
                            # non-fatal; we'll proceed with whatever we have
                            pass

                    if media_type and media_type.startswith('tv'):
                        # require season+episode to create episode file; otherwise skip
                        # Add debug logging to capture why we might skip in production runs
                        # Diagnostic prints to stdout to aid interactive debugging in CI
                        # debug: payload and resolved values are logged via logger.debug/info above
                        if lib and title and media_id and (season_number is not None) and (episode_number is not None):
                            logger.debug(f"Job {job_id}: attempting to place tv dummy file: series={title!r} s={season_number} e={episode_number} media_id={media_id}")
                            placed = place_dummy_file(media_type, title, year or 0, media_id, lib, season_number=int(season_number), episode_number=int(episode_number), episode_title=episode_title)
                            logger.debug(f"Job {job_id}: place_dummy_file returned: {placed!r}")
                        else:
                            # Log a richer message to diagnose why resolution failed
                            logger.info(f"Job {job_id}: insufficient placement info for TV; skipping create. payload_keys={list(payload.keys())} lib={lib!r} title={title!r} media_id={media_id!r} season_number={season_number!r} episode_number={episode_number!r} ph_episode_id={getattr(ph, 'episode_id', None)!r}")
                            job_done(job_id, success=True)
                            return True
                    else:
                        # movie path: existing behavior
                        if lib and title and media_id:
                            placed = place_dummy_file(media_type, title, year or 0, media_id, lib)
                        else:
                            # Missing structured info: attempt to create folder from ph.path
                            # Do not attempt destructive operations if insufficient info
                            logger.info(f"Job {job_id}: insufficient placement info; skipping create")
                            job_done(job_id, success=True)
                            return True
                except Exception:
                    logger.exception(f"Job {job_id}: place_dummy_file failed")
                    placed = None

                logger.debug(f"Job {job_id}: place_dummy_file returned: {placed!r}")
                if not placed:
                    # mark failure on placeholder for operator visibility
                    try:
                        ph.display_status = 'FAILED'
                        ph.display_reason = 'creation_failed'
                        ph.lifecycle_status = 'FAILED'
                        session.add(ph)
                        session.commit()
                    except Exception:
                        try:
                            session.rollback()
                        except Exception:
                            pass
                    job_done(job_id, success=False, error_message='creation_failed')
                    return False

                # Update placeholder row to reflect created file
                try:
                    logger.debug(f"Job {job_id}: updating placeholder row {ph.id} with path {placed}")
                    ph.path = placed
                    mark_exists(session, ph, True, commit=False)
                    # compute fingerprint and store in extra
                    try:
                        fp = compute_fingerprint(placed) or {}
                        extra = ph.extra or {}
                        if not isinstance(extra, dict):
                            extra = {}
                        extra.update({'fingerprint': fp})
                        ph.extra = extra
                    except Exception:
                        pass
                    ph.lifecycle_status = 'ACTIVE'
                    ph.last_observed_at = datetime.now()
                    session.add(ph)
                    session.commit()
                    logger.debug(f"Job {job_id}: placeholder {ph.id} updated to ACTIVE")
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    logger.exception(f"Job {job_id}: failed to update placeholder after create")
                    job_done(job_id, success=False, error_message='update_failed')
                    return False

                job_done(job_id, success=True)
                return True
            finally:
                try:
                    session.close()
                except Exception:
                    pass
        elif job_type == 'placeholder:delete':
            placeholder_id = payload.get('placeholder_id')
            path = payload.get('path')
            try:
                from services.integrations import delete_dummy_file, delete_dummy_files
                from services.postgres.models import Placeholder as PlaceholderModel, Movie as MovieModel, Episode as EpisodeModel, Season as SeasonModel, Series as SeriesModel
                from core.config import settings as _settings
            except Exception:
                logger.exception('Required modules for placeholder:delete not available')
                job_done(job_id, success=False, error_message='modules_missing')
                return False

            session = get_session()
            try:
                ph = None
                if placeholder_id:
                    ph = session.query(PlaceholderModel).filter(PlaceholderModel.id == int(placeholder_id)).with_for_update().first()
                if not ph and path:
                    ph = session.query(PlaceholderModel).filter(PlaceholderModel.path == path).with_for_update().first()

                # If we have a concrete target path, prefer a straight file delete
                target = path or (ph.path if ph else None)
                if not target:
                    logger.info(f"Job {job_id}: no path for delete; attempting metadata-driven delete if placeholder row exists")
                    # If we have a placeholder row but no path, attempt legacy metadata-driven delete
                    if not ph:
                        job_done(job_id, success=True)
                        return True

                ok = False
                # If we have a path, remove that specific file/folder
                if target:
                    try:
                        ok = delete_dummy_file(target)
                    except Exception:
                        logger.exception(f"Job {job_id}: delete_dummy_file raised")
                        ok = False

                # If we didn't have a path but we do have a placeholder row, attempt the legacy recursive delete
                if not target and ph:
                    try:
                        # Derive metadata and library path from related rows
                        if getattr(ph, 'movie_id', None):
                            mv = session.query(MovieModel).filter(MovieModel.id == int(ph.movie_id)).first()
                            lib = _settings.MOVIE_LIBRARY_FOLDER_4K if getattr(mv, 'is_4k', False) else _settings.MOVIE_LIBRARY_FOLDER
                            ok = delete_dummy_files(media_type='movie', title=getattr(mv, 'title', None), year=getattr(mv, 'year', None), tvdb_id=getattr(mv, 'tmdbid', None), library_path=lib, session=session)
                        elif getattr(ph, 'episode_id', None):
                            ep = session.query(EpisodeModel).filter(EpisodeModel.id == int(ph.episode_id)).first()
                            if ep:
                                season = session.query(SeasonModel).filter(SeasonModel.id == int(ep.season_id)).first() if getattr(ep, 'season_id', None) else None
                                series = session.query(SeriesModel).filter(SeriesModel.id == int(season.series_id)).first() if season and getattr(season, 'series_id', None) else None
                                lib = _settings.TV_LIBRARY_FOLDER_4K if (getattr(ep, 'is_4k', False) or (series and getattr(series, 'is_4k', False))) else _settings.TV_LIBRARY_FOLDER
                                ok = delete_dummy_files(media_type='tv', title=getattr(series, 'title', None) if series else None, year=getattr(series, 'year', None) if series else None, tvdb_id=getattr(series, 'tvdbid', None) if series else None, library_path=lib, season_number=getattr(season, 'season_number', None) if season else None, episode_number=getattr(ep, 'episode_number', None), session=session)
                    except Exception:
                        logger.exception(f"Job {job_id}: metadata-driven delete raised")

                # After deletion, update/delete placeholder DB row
                try:
                    if ph:
                        if ok:
                            try:
                                session.delete(ph)
                                session.commit()
                            except Exception:
                                try:
                                    session.rollback()
                                except Exception:
                                    pass
                        else:
                            ph.lifecycle_status = 'FAILED'
                            ph.display_status = 'DELETE_FAILED'
                            session.add(ph)
                            session.commit()
                except Exception:
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    logger.exception(f"Job {job_id}: failed to update placeholder after delete")
                    job_done(job_id, success=False, error_message='update_failed')
                    return False

                job_done(job_id, success=True)
                return True
            finally:
                try:
                    session.close()
                except Exception:
                    pass
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