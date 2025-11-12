import json
from datetime import datetime, timezone, timedelta
from services.postgres.db import get_session
from services.postgres.models import Job
from core.logger import logger
from core.config import settings


class RateLimiter:
    """Pluggable RateLimiter interface.

    Minimal stub for Option A; default behaviour is no-op (allow all).
    Implementations (in-memory semaphore, Redis token-bucket) can be added
    later and injected into the Enricher.
    """
    def acquire(self, key: str) -> bool:
        return True

    def release(self, key: str) -> None:
        return None


def enqueue_enrichment_job(payload: dict, is_4k: bool = False, run_after: datetime = None, group: str = None):
    """Create a Job row of type 'enrichment' to let the job worker process ARR enrichment.

    Returns the created Job ID on success, or None on failure.
    """
    session = get_session()
    try:
        job_payload = payload or {}
        if isinstance(job_payload, dict):
            job_payload.setdefault('is_4k', bool(is_4k))

        # Scheduling delay
        if run_after:
            default_run_after = run_after
        else:
            delay_seconds = int(getattr(settings, 'ENRICHMENT_DELAY_SECONDS', 0) or 0)
            default_run_after = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)

        # Use group id to coalesce enrichment requests. If caller supplied a group use it,
        # otherwise derive a sensible group id from payload (tvdb/tmdb or generic).
        group_id = group
        if not group_id:
            try:
                if isinstance(job_payload, dict) and job_payload.get('series') and job_payload['series'].get('tvdbId'):
                    group_id = f"enrich:tv:{int(job_payload['series']['tvdbId'])}"
                elif isinstance(job_payload, dict) and job_payload.get('movie') and job_payload['movie'].get('tmdbId'):
                    group_id = f"enrich:movie:{int(job_payload['movie']['tmdbId'])}"
                else:
                    group_id = f"enrich:job:{int(datetime.now().timestamp())}"
            except Exception:
                group_id = f"enrich:job:{int(datetime.now().timestamp())}"

        # Try to find existing pending enrichment job with same group and merge payload/run_after
        existing = None
        try:
            existing = session.query(Job).filter_by(job_type='enrichment', group_id=group_id, status='PENDING').order_by(Job.id.desc()).with_for_update(nowait=True).first()
        except Exception:
            # with_for_update may fail under some engines; fallback to simple lookup
            existing = session.query(Job).filter_by(job_type='enrichment', group_id=group_id, status='PENDING').order_by(Job.id.desc()).first()

        if existing:
            # Merge payloads conservatively: prefer union of series/movie identifiers when present
            try:
                ex_payload = existing.payload or {}
                if isinstance(ex_payload, dict) and isinstance(job_payload, dict):
                    # Merge 'series' or 'movie' keys if present
                    if 'series' in job_payload and 'series' not in ex_payload:
                        ex_payload['series'] = job_payload['series']
                    if 'movie' in job_payload and 'movie' not in ex_payload:
                        ex_payload['movie'] = job_payload['movie']
                    # Merge generic list fields (not common here) by union
                    for k in ('series_tvdb', 'tmdbs'):
                        if k in job_payload:
                            existing_list = list(map(str, ex_payload.get(k, []) or []))
                            incoming = list(map(str, job_payload.get(k, []) or []))
                            merged = list(dict.fromkeys(existing_list + incoming))
                            ex_payload[k] = merged
                    existing.payload = ex_payload
            except Exception:
                existing.payload = job_payload

            # Extend run_after to the earliest of the two times (so we don't delay unnecessarily)
            try:
                if existing.run_after and default_run_after and existing.run_after > default_run_after:
                    existing.run_after = default_run_after
                elif not existing.run_after:
                    existing.run_after = default_run_after
            except Exception:
                existing.run_after = default_run_after

            session.add(existing)
            session.commit()
            job_id = existing.id
            logger.debug(f"Merged into existing enrichment job {job_id}", extra={'emoji_type': 'queue'})
        else:
            job = Job(job_type='enrichment', payload=job_payload, status='PENDING', run_after=default_run_after, group_id=group_id)
            session.add(job)
            session.commit()
            job_id = job.id
            logger.debug(f"Enqueued enrichment job {job.id}", extra={'emoji_type': 'queue'})

        # Ensure worker is running
        try:
            from services.jobs import start_worker_once
            start_worker_once()
        except Exception:
            logger.debug("start_worker_once failed in enqueue_enrichment_job; worker may not be running", extra={'emoji_type': 'debug'})

        return job_id
    except Exception as e:
        logger.error(f"Failed to enqueue enrichment job: {e}", extra={'emoji_type': 'error'})
        try:
            session.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass


class EnricherAdapter:
    """Adapter interface for enrichment sources (RADARR/SONARR).

    Concrete adapters should implement `enrich(payload, is_4k)` and return a
    normalized result dict. This scaffold keeps the codebase ready for
    multiple adapters while leaving the existing `services.integrations`
    functions as the functional implementation for now.
    """
    def enrich(self, payload: dict, is_4k: bool = False) -> dict:
        raise NotImplementedError()
