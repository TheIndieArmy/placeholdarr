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
        # Ensure we preserve the is_4k flag in payload for consumer convenience
        if isinstance(job_payload, dict):
            job_payload.setdefault('is_4k', bool(is_4k))

        # If caller didn't supply run_after, honor ENRICHMENT_DELAY_SECONDS so
        # operators can configure a short scheduling delay to let ARR systems
        # settle before enrichment runs. Default is immediate (0 seconds).
        if run_after:
            default_run_after = run_after
        else:
            delay_seconds = int(getattr(settings, 'ENRICHMENT_DELAY_SECONDS', 0) or 0)
            default_run_after = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        job = Job(job_type='enrichment', payload=job_payload, status='PENDING', run_after=default_run_after, group_id=group)
        session.add(job)
        session.commit()
        logger.debug(f"Enqueued enrichment job {job.id}", extra={'emoji_type': 'queue'})
        # Ensure the job worker is running so this job will be picked up. Import
        # locally to avoid circular imports at module import time.
        try:
            from services.jobs import start_worker_once
            start_worker_once()
        except Exception:
            # Non-fatal; job is persisted and worker may be started elsewhere
            logger.debug("start_worker_once failed in enqueue_enrichment_job; worker may not be running", extra={'emoji_type': 'debug'})
        return job.id
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
