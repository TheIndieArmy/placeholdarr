"""startup_sync_runner durable job handler (Phase 4).

Replaces daemon threading.Thread launchers for:
- App lifespan startup sync (run_startup_source_of_truth)
- Post-onboarding first-run sync (start_runtime_background_services + startup sync)
- ARR configuration change full sync (per-instance run_full_sync loop)

Uses a Postgres advisory lock so only one startup sync runs at a time (FM-20).
If the lock cannot be acquired, the handler logs and marks the Job DONE without
re-running — another invocation is already active or just finished.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from core.config import settings
from core.logger import logger
from services.postgres.models import Job


STARTUP_SYNC_RUNNER_JOB_TYPE = "startup_sync_runner"

_ADVISORY_LOCK_KEY = 0x504C484F5F53594E


def use_job_driven_startup_sync() -> bool:
    return bool(getattr(settings, "USE_JOB_DRIVEN_STARTUP_SYNC", True))


def enqueue_startup_sync_runner_job(session, *, reason: str, payload: dict[str, Any] | None = None) -> Job:
    """Insert a startup_sync_runner Job. Caller commits."""
    job_payload: dict[str, Any] = {"reason": str(reason)}
    if payload:
        for k, v in payload.items():
            if k == "reason":
                continue
            job_payload[k] = v
    job = Job(
        job_type=STARTUP_SYNC_RUNNER_JOB_TYPE,
        payload=job_payload,
        status="PENDING",
        max_attempts=3,
    )
    session.add(job)
    return job


def _try_acquire_startup_advisory_lock(session) -> bool:
    try:
        row = session.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _ADVISORY_LOCK_KEY},
        ).scalar()
        return bool(row)
    except Exception as exc:
        logger.warning(
            f"startup_sync_runner: advisory lock query failed (treating as lock denied): {exc}",
            extra={"emoji_type": "warning"},
        )
        return False


def _release_startup_advisory_lock(session) -> None:
    try:
        session.execute(
            text("SELECT pg_advisory_unlock(:k)"),
            {"k": _ADVISORY_LOCK_KEY},
        )
    except Exception:
        pass


def _opens_worker_gate(reason: str) -> bool:
    """ARR-change jobs must not flip startup_sync_complete; startup paths must."""
    r = str(reason or "").strip().lower()
    return r not in {"arr_change", "arr_endpoint_changed"}


def process_startup_sync_runner_job(session, job: Job) -> dict[str, Any]:
    """Worker handler: runs one of the startup / ARR-change pipelines based on payload.reason."""
    payload = job.payload or {}
    reason = str(payload.get("reason") or "lifespan").strip().lower()

    if not _try_acquire_startup_advisory_lock(session):
        logger.info(
            f"startup_sync_runner job_id={job.id} skipped — advisory lock busy (reason={reason})",
            extra={"emoji_type": "info"},
        )
        return {"ok": True, "skipped": "advisory_lock_busy", "reason": reason}

    from services.startup_gate import startup_sync_complete

    try:
        if reason in {"arr_change", "arr_endpoint_changed"}:
            from services.source_of_truth.sync_runner import run_full_sync

            logger.info(
                f"startup_sync_runner job_id={job.id} ARR-change full sync starting reason={reason}",
                extra={"emoji_type": "gear"},
            )
            run_count = 0
            for item in (getattr(settings, "configured_arr_instances", []) or []):
                arr_type = str(item.get("arr_type") or "").strip().lower()
                if arr_type not in {"radarr", "sonarr"}:
                    continue
                run_full_sync(
                    dry_run=False,
                    batch_size=50,
                    types=("movie",) if arr_type == "radarr" else ("series",),
                    instance_key=str(item.get("instance_key") or "").strip().lower() or None,
                )
                run_count += 1
            logger.info(
                f"startup_sync_runner job_id={job.id} ARR-change full sync completed runs={run_count}",
                extra={"emoji_type": "success"},
            )
            return {"ok": True, "reason": reason, "runs": run_count}

        if reason == "post_onboarding":
            try:
                from main import start_runtime_background_services
                from services.source_of_truth.startup import run_startup_source_of_truth

                start_runtime_background_services(reason="post_onboarding_completion")

                logger.info(
                    "startup_sync_runner: first-run startup sync after onboarding",
                    extra={"emoji_type": "gear"},
                )
                result = run_startup_source_of_truth()
                logger.info(
                    f"Post-onboarding startup sync completed mode={result.get('startup_sync_mode')} "
                    f"run_ids={result.get('run_ids') or []}",
                    extra={"emoji_type": "success"},
                )
                return {"ok": True, "reason": reason, "result": result}
            except Exception as exc:
                logger.error(f"Post-onboarding startup sync failed: {exc}", extra={"emoji_type": "error"})
                return {"ok": False, "reason": str(exc)}

        # lifespan / app_startup / default — mirrors main.py daemon thread
        from services.source_of_truth.startup import run_startup_source_of_truth

        logger.info(
            f"startup_sync_runner job_id={job.id} running startup source-of-truth (reason={reason})",
            extra={"emoji_type": "gear"},
        )
        try:
            startup_result = run_startup_source_of_truth()
        except Exception as exc:
            logger.error(f"Startup source-of-truth failed: {exc}", extra={"emoji_type": "error"})
            return {"ok": False, "reason": str(exc), "reason_tag": reason}

        if startup_result.get("ran"):
            logger.info(
                f"Startup source-of-truth completed run_ids={startup_result.get('run_ids') or []}",
                extra={"emoji_type": "info"},
            )
        else:
            logger.debug("Startup source-of-truth disabled by config flags", extra={"emoji_type": "debug"})
        return {"ok": True, "reason": reason, "result": startup_result}

    finally:
        _release_startup_advisory_lock(session)
        if _opens_worker_gate(reason):
            startup_sync_complete.set()


__all__ = [
    "STARTUP_SYNC_RUNNER_JOB_TYPE",
    "use_job_driven_startup_sync",
    "enqueue_startup_sync_runner_job",
    "process_startup_sync_runner_job",
]
