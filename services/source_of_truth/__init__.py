"""Canonical source-of-truth sync package.

Modules in this package own ARR ingestion, DB reconcile/materialization,
filesystem scan, startup execution, and interval scheduling.
"""

from services.source_of_truth.scheduler import schedule_all_syncs
from services.source_of_truth.startup import run_startup_source_of_truth
from services.source_of_truth.determiner import run_determination_pass, run_placeholder_link_reconcile
from services.source_of_truth.materializer import run_materialization_pass
from services.source_of_truth.sync_runner import run_full_sync

__all__ = [
    'run_full_sync',
    'run_placeholder_link_reconcile',
    'run_determination_pass',
    'run_materialization_pass',
    'schedule_all_syncs',
    'run_startup_source_of_truth',
]
