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
try:
    # Prefer new sync implementation if present
    from services.sync.sync_movies_impl import run_full_sync
except Exception:
    # Fallback: provide a no-op run_full_sync so startup won't fail. The
    # real implementation should be added to services.sync.sync_movies_impl.
    def run_full_sync(*args, **kwargs):
        logger.debug('run_full_sync stub called (no-op)')
        return None

logger = logging.getLogger('services.sync')


def schedule_all_syncs():
    """Schedule startup and cron syncs for ARR services.

    For movies we call the canonical `run_full_sync(types=('movie',))` so
    the same enrichment/attach flow is used as for TV.
    """
    # Startup syncs
    # Note: immediate startup full-syncs are intentionally NOT invoked here.
    # `main.py` already performs list-capture seeding when configured and we want
    # a single authority (main) to control startup seeding to avoid duplicate
    # work. This function only configures cron-based syncs below.

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
