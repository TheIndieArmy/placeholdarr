"""Minimal enricher API shim

Simple, robust behavior aligned with the "single workflow" design:
- Always create a Job row via the ORM (duplicates allowed).
- Worker will claim and run jobs sequentially; we ensure the worker
  selection avoids running two jobs with the same group_id at the same time.
"""
from datetime import datetime
import json
from typing import Optional
from services.postgres.db import get_session
from sqlalchemy import text
from services.postgres.models import Job
from core.logger import logger


def _determine_group_id(pl: dict) -> Optional[str]:
    try:
        if pl.get('series') and (pl.get('series').get('id') or pl.get('series').get('tvdbId')):
            gid = pl.get('series').get('id') or pl.get('series').get('tvdbId')
            return f"enrich:tv:{gid}"
        if pl.get('movie') and (pl.get('movie').get('id') or pl.get('movie').get('tmdbId') or pl.get('movie').get('tmdb')):
            gid = pl.get('movie').get('id') or pl.get('movie').get('tmdbId') or pl.get('movie').get('tmdb')
            return f"enrich:movie:{gid}"
        if pl.get('type') == 'movie' and (pl.get('tmdb') or pl.get('id')):
            gid = pl.get('tmdb') or pl.get('id')
            return f"enrich:movie:{gid}"
        if pl.get('type') == 'tv' and (pl.get('tvdb') or pl.get('id')):
            gid = pl.get('tvdb') or pl.get('id')
            return f"enrich:tv:{gid}"
        if pl.get('tmdb'):
            return f"enrich:movie:{pl.get('tmdb')}"
        if pl.get('tvdb'):
            return f"enrich:tv:{pl.get('tvdb')}"
    except Exception:
        return None
    return None


def enqueue_enrichment_job(payload: dict, is_4k: bool = False, start_worker: bool = True) -> Optional[int]:
    """Enqueue an enrichment job using a plain ORM insert and optionally start the worker.

    Returns the created Job.id or None on error.
    """
    pl = payload or {}
    group_id = _determine_group_id(pl)
    try:
        with get_session() as sess:
            # If we can derive a group_id, prefer to return an existing active
            # job for that group rather than creating a duplicate from the same
            # logical entry. To make this safe under concurrent callers we acquire
            # a Postgres advisory lock per-group so the check+insert is atomic.
            if group_id:
                try:
                    import zlib
                    key = int(zlib.adler32(group_id.encode('utf-8')))
                except Exception:
                    key = None

                if key is not None:
                    try:
                        # Blocking lock: wait until we can serialize this group
                        sess.execute(text("SELECT pg_advisory_lock(:k)"), {'k': key})
                    except Exception:
                        # If advisory lock fails for any reason, proceed without it
                        logger.debug(f"advisory lock failed for group_id={group_id}", extra={'emoji_type': 'debug'})

                try:
                    existing = None
                    try:
                        existing = (
                            sess.query(Job)
                            .filter(Job.group_id == group_id)
                            .filter(Job.status.in_(['PENDING', 'CLAIMED', 'WORKING']))
                            .order_by(Job.id)
                            .first()
                        )
                    except Exception:
                        existing = None

                    if existing:
                        logger.debug(f"Found existing active job id={existing.id} for group_id={group_id}", extra={'emoji_type': 'debug'})
                        return existing.id

                    # use local server time so DB now() comparisons align with run_after
                    job = Job(job_type='enrichment', payload=pl, status='PENDING', group_id=group_id, run_after=datetime.now())
                    sess.add(job)
                    sess.commit()
                    jid = job.id
                finally:
                    # Always try to release the advisory lock if we acquired one
                    if group_id and key is not None:
                        try:
                            sess.execute(text("SELECT pg_advisory_unlock(:k)"), {'k': key})
                        except Exception:
                            try:
                                sess.rollback()
                            except Exception:
                                pass
            else:
                # No group_id: fall back to plain insert
                # use local server time so DB now() comparisons align with run_after
                job = Job(job_type='enrichment', payload=pl, status='PENDING', group_id=group_id, run_after=datetime.now())
                sess.add(job)
                sess.commit()
                jid = job.id

        if start_worker:
            try:
                from services.jobs import start_worker_once
                start_worker_once()
            except Exception:
                logger.debug("Failed to start job worker from enqueue_enrichment_job", extra={'emoji_type':'debug'})

        return jid
    except Exception as e:
        logger.error(f"Failed to enqueue enrichment job: {e}", extra={'emoji_type': 'error'})
        return None
