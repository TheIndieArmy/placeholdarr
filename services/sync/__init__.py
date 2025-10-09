"""Canonical sync scheduling for ARR services.

This module provides a single `schedule_all_syncs()` entrypoint used by
`main.py` to schedule startup and cron-based full-syncs for Sonarr/Radarr.

We intentionally keep movie sync behavior in the canonical `services.syncer`
so movies are treated the same as TV: the syncer enqueues per-item enrichment
jobs and reuses the same attach/enrichment logic.
"""
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from core.config import settings
from services.syncer import run_full_sync

logger = logging.getLogger('services.sync')


def schedule_all_syncs():
    """Schedule startup and cron syncs for ARR services.

    For movies we call the canonical `run_full_sync(types=('movie',))` so
    the same enrichment/attach flow is used as for TV.
    """
    # Startup syncs
    try:
        # Radarr movie startup syncs
        if getattr(settings, 'RADARR_SYNC_ON_STARTUP', False):
            logger.info('Running Radarr (standard) movie full-sync on startup...')
            try:
                run_full_sync(dry_run=False, batch_size=50, types=('movie',), is_4k=False)
            except Exception:
                logger.exception('Startup Radarr (standard) full-sync failed')
    except Exception:
        logger.exception('Failed running RADARR standard startup sync')

    try:
        if getattr(settings, 'RADARR_4K_SYNC_ON_STARTUP', False):
            logger.info('Running Radarr (4K) movie full-sync on startup...')
            try:
                run_full_sync(dry_run=False, batch_size=50, types=('movie',), is_4k=True)
            except Exception:
                logger.exception('Startup Radarr (4K) full-sync failed')
    except Exception:
        logger.exception('Failed running RADARR 4K startup sync')

    # Sonarr TV startup syncs
    try:
        if getattr(settings, 'SONARR_SYNC_ON_STARTUP', False):
            logger.info('Running Sonarr TV full-sync on startup...')
            try:
                run_full_sync(dry_run=False, batch_size=50, types=('tv',), is_4k=False)
            except Exception:
                logger.exception('Startup Sonarr (standard) full-sync failed')
    except Exception:
        logger.exception('Failed running SONARR standard startup sync')

    try:
        if getattr(settings, 'SONARR_4K_SYNC_ON_STARTUP', False):
            logger.info('Running Sonarr (4K) TV full-sync on startup...')
            try:
                run_full_sync(dry_run=False, batch_size=50, types=('tv',), is_4k=True)
            except Exception:
                logger.exception('Startup Sonarr (4K) full-sync failed')
    except Exception:
        logger.exception('Failed running SONARR 4K startup sync')

    # Cron scheduling – parse simple cron-like strings split by whitespace into minute hour day month day_of_week
    def _start_cron(cron_str, target, label, is_4k=False):
        if not cron_str:
            return
        try:
            parts = cron_str.replace('"', '').split()
            cron_kwargs = {k: v for k, v in zip(['minute', 'hour', 'day', 'month', 'day_of_week'], parts) if v != '*'}
            sched = BackgroundScheduler()
            sched.add_job(lambda: target(is_4k=is_4k), 'cron', **cron_kwargs)
            sched.start()
            logger.info(f"{label} scheduler started with cron: {cron_str}")
        except Exception:
            logger.exception(f"Failed to start scheduler for {label}")

    # Target wrapper to call run_full_sync with appropriate is_4k
    def _run_movie_sync(is_4k=False):
        try:
            run_full_sync(dry_run=False, batch_size=50, types=('movie',), is_4k=is_4k)
        except Exception:
            logger.exception('Scheduled movie full-sync failed')

    _start_cron(getattr(settings, 'RADARR_SYNC_CRON', None), _run_movie_sync, 'Radarr (standard)', is_4k=False)
    _start_cron(getattr(settings, 'RADARR_4K_SYNC_CRON', None), _run_movie_sync, 'Radarr (4K)', is_4k=True)
# Makes 'sync' a Python package for imports
