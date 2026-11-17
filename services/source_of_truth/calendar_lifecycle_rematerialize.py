"""Apply-scope rematerialize after Calendar Coming Soon window / movie date-type changes.

Re-runs determination + materialization (and calendar status) so create/retire and
Library Future labeling match the saved lookahead / preferred movie date type.
"""

from __future__ import annotations

import threading
from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig

PENDING_FLAG_KEY = "CALENDAR_LIFECYCLE_REMATERIALIZE_PENDING"


def _get_pending_row(session):
    return session.query(AppConfig).filter(AppConfig.key == PENDING_FLAG_KEY).first()


def mark_calendar_lifecycle_rematerialize_pending(pending: bool = True) -> None:
    session = get_session()
    try:
        row = _get_pending_row(session)
        if not row:
            row = AppConfig(
                key=PENDING_FLAG_KEY,
                value=bool(pending),
                value_type="bool",
                restart_required=False,
                description=(
                    "Internal: re-run determination/materialization after Calendar "
                    "lookahead or movie release date type change on next full sync."
                ),
            )
            session.add(row)
        else:
            row.value = bool(pending)
            row.value_type = "bool"
            session.add(row)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def is_calendar_lifecycle_rematerialize_pending() -> bool:
    session = get_session()
    try:
        row = _get_pending_row(session)
        if not row:
            return False
        return bool(row.value) is True or str(row.value).strip().lower() in {"1", "true", "yes"}
    finally:
        session.close()


def clear_calendar_lifecycle_rematerialize_pending() -> None:
    mark_calendar_lifecycle_rematerialize_pending(False)


def run_calendar_lifecycle_rematerialize(*, source: str = "settings_save") -> dict[str, Any]:
    """Re-evaluate Coming Soon window membership and create/retire placeholders.

    Holds the shared pipeline lock so Apply now cannot race a scheduled full/lite
    sync materialization on the same catalog and filesystem.
    """
    from services.source_of_truth.calendar_phase import run_calendar_phase
    from services.source_of_truth.determiner import run_determination_pass
    from services.source_of_truth.materializer import run_materialization_pass
    from services.source_of_truth.sync_coordinator import (
        is_full_sync_active,
        release_pipeline,
        try_acquire_pipeline,
    )

    if is_full_sync_active():
        logger.info(
            f"Calendar lifecycle rematerialize deferred: full sync active ({source})",
            extra={"emoji_type": "info"},
        )
        mark_calendar_lifecycle_rematerialize_pending(True)
        return {"ok": True, "deferred": True, "reason": "full_sync_active", "source": source}

    if not try_acquire_pipeline():
        logger.info(
            f"Calendar lifecycle rematerialize deferred: pipeline busy ({source})",
            extra={"emoji_type": "info"},
        )
        mark_calendar_lifecycle_rematerialize_pending(True)
        return {"ok": True, "deferred": True, "reason": "pipeline_busy", "source": source}

    logger.info(
        f"Calendar lifecycle rematerialize starting ({source})",
        extra={"emoji_type": "gear"},
    )
    try:
        determination = run_determination_pass()
        materialization = run_materialization_pass()
        calendar = run_calendar_phase()
        try:
            from services.series_episode_stats_hooks import schedule_full_series_stats_refresh

            schedule_full_series_stats_refresh()
        except Exception as stats_exc:
            logger.warning(
                f"Could not schedule series stats refresh after calendar lifecycle rematerialize: {stats_exc}",
                extra={"emoji_type": "warning"},
            )
        try:
            clear_calendar_lifecycle_rematerialize_pending()
        except Exception as exc:
            logger.warning(
                f"Could not clear calendar lifecycle rematerialize pending flag: {exc}",
                extra={"emoji_type": "warning"},
            )
        out = {
            "ok": True,
            "source": source,
            "determination": determination,
            "materialization": materialization,
            "calendar": calendar,
        }
        logger.info(
            f"Calendar lifecycle rematerialize finished ({source}): "
            f"mat_created={materialization.get('created')} mat_deleted={materialization.get('deleted')}",
            extra={"emoji_type": "success"},
        )
        return out
    finally:
        release_pipeline()


def start_calendar_lifecycle_rematerialize_background(*, source: str = "settings_save") -> None:
    def _run() -> None:
        from services.postgres.db import bind_worker_pool_role

        bind_worker_pool_role()
        try:
            run_calendar_lifecycle_rematerialize(source=source)
        except Exception as exc:
            logger.error(
                f"Calendar lifecycle rematerialize background failed ({source}): {exc}",
                extra={"emoji_type": "error"},
            )

    threading.Thread(
        target=_run,
        name="calendar-lifecycle-rematerialize",
        daemon=True,
    ).start()


def execute_calendar_lifecycle_rematerialize_apply_scope(
    apply_scope: str,
    *,
    source: str = "settings_save",
) -> dict[str, Any]:
    scope = str(apply_scope or "").strip().lower()
    if scope not in {"now", "next_full_sync"}:
        scope = "next_full_sync"

    if scope == "next_full_sync":
        mark_calendar_lifecycle_rematerialize_pending(True)
        logger.info(
            f"Calendar lifecycle rematerialize deferred to next full sync ({source})",
            extra={"emoji_type": "info"},
        )
        return {"ok": True, "scope": "next_full_sync", "pending": True, "enqueued": False}

    # Apply now: keep pending until the background rematerialize finishes successfully.
    # If the process dies mid-run, the next full sync still sees pending.
    try:
        mark_calendar_lifecycle_rematerialize_pending(True)
    except Exception as exc:
        logger.warning(
            f"Could not mark calendar lifecycle rematerialize pending before apply now: {exc}",
            extra={"emoji_type": "warning"},
        )
    start_calendar_lifecycle_rematerialize_background(source=f"{source}:apply_now")
    return {"ok": True, "scope": "now", "pending": True, "enqueued": True}


def run_calendar_lifecycle_rematerialize_if_pending(*, source: str = "full_sync") -> dict[str, Any]:
    if not is_calendar_lifecycle_rematerialize_pending():
        return {"ok": True, "skipped": True, "reason": "not_pending"}
    return run_calendar_lifecycle_rematerialize(source=source)
