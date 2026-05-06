"""startup_sync_runner durable job handler (Phase 4).

Replaces daemon threading.Thread launchers for:
- App lifespan startup sync (run_startup_source_of_truth)
- Post-onboarding first-run sync (start_runtime_background_services + startup sync)
- ARR configuration change full sync (per-instance run_full_sync loop)

Phase 1 of the holistic NOTIFY audit: previously this handler used
``pg_try_advisory_lock`` on the worker's session, which is SESSION-scoped in
Postgres. That meant the handler PINNED its DB connection for the entire
duration of the full sync (often many minutes / hours), starving the pool.

The replacement uses a row-based "sync_running" flag in ``app_config`` plus a
stale-heartbeat timeout. Acquire is a tiny SELECT FOR UPDATE + UPSERT in its
own short transaction; the connection is then RELEASED before the actual sync
work begins. Release is another tiny UPDATE in its own short transaction. If
the worker crashes mid-sync the heartbeat stops being updated and a follow-up
attempt sees the row as expired and takes over.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig, Job


STARTUP_SYNC_RUNNER_JOB_TYPE = "startup_sync_runner"

# Row key used in ``app_config`` to coordinate exclusive startup-sync runs
# without pinning a DB connection.
_STARTUP_SYNC_GATE_KEY = "startup_sync_running"

# Connection-released advisory gate: how stale the gate row may be before a
# new attempt steals it. Must comfortably exceed the heartbeat interval.
_STALE_GATE_SECONDS = 600

# Heartbeat interval — how often the running handler bumps the gate row's
# updated_at so concurrent attempts see a fresh occupant.
_GATE_HEARTBEAT_SECONDS = 60


def use_job_driven_startup_sync() -> bool:
    return bool(getattr(settings, "USE_JOB_DRIVEN_STARTUP_SYNC", True))


def enqueue_startup_sync_runner_job(session, *, reason: str, payload: dict[str, Any] | None = None) -> Job:
    """Insert a startup_sync_runner Job. Caller commits."""
    from services.source_of_truth.job_priority import default_priority_for

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
        priority=default_priority_for(STARTUP_SYNC_RUNNER_JOB_TYPE),
    )
    session.add(job)
    return job


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _try_acquire_sync_gate(*, owner: str) -> bool:
    """Acquire the row-based startup-sync gate in a tiny short-lived transaction.

    Uses an UPSERT-with-staleness-check so we don't pin a connection. The
    runner heartbeats the row from a daemon thread to keep it fresh; if the
    runner crashes, the row goes stale within ``_STALE_GATE_SECONDS`` and a
    later attempt takes over.
    """
    session = get_session()
    try:
        cutoff = _now_utc() - timedelta(seconds=_STALE_GATE_SECONDS)
        row = (
            session.query(AppConfig)
            .filter(AppConfig.key == _STARTUP_SYNC_GATE_KEY)
            .with_for_update(skip_locked=False)
            .first()
        )
        if row is None:
            row = AppConfig(
                key=_STARTUP_SYNC_GATE_KEY,
                value={"active": True, "owner": owner, "started_at": _now_utc().isoformat()},
                value_type="json",
                description="Startup-sync exclusive gate (row-based; not session-pinning).",
            )
            session.add(row)
            session.commit()
            return True

        existing = row.value if isinstance(row.value, dict) else {}
        active = bool(existing.get("active"))
        updated_at = row.updated_at
        is_stale = bool(updated_at is None or updated_at < cutoff)

        if active and not is_stale:
            session.rollback()
            return False

        # Either inactive or stale -> we may take it.
        row.value = {"active": True, "owner": owner, "started_at": _now_utc().isoformat()}
        row.updated_at = _now_utc()
        session.add(row)
        session.commit()
        return True
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(
            f"startup_sync_runner: gate acquisition query failed (treating as lock denied): {exc}",
            extra={"emoji_type": "warning"},
        )
        return False
    finally:
        try:
            session.close()
        except Exception:
            pass


def _release_sync_gate(*, owner: str) -> None:
    """Release the gate in a tiny short-lived transaction. Idempotent."""
    session = get_session()
    try:
        row = (
            session.query(AppConfig)
            .filter(AppConfig.key == _STARTUP_SYNC_GATE_KEY)
            .with_for_update(skip_locked=False)
            .first()
        )
        if row is None:
            session.rollback()
            return
        existing = row.value if isinstance(row.value, dict) else {}
        # Only flip if we still own it; otherwise leave alone for the new owner.
        if existing.get("owner") and existing.get("owner") != owner:
            session.rollback()
            return
        row.value = {"active": False, "owner": owner, "released_at": _now_utc().isoformat()}
        row.updated_at = _now_utc()
        session.add(row)
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        try:
            session.close()
        except Exception:
            pass


def _heartbeat_sync_gate(*, owner: str) -> None:
    """Bump the gate row's updated_at so concurrent acquirers see a fresh owner."""
    session = get_session()
    try:
        row = (
            session.query(AppConfig)
            .filter(AppConfig.key == _STARTUP_SYNC_GATE_KEY)
            .first()
        )
        if row is None:
            session.rollback()
            return
        existing = row.value if isinstance(row.value, dict) else {}
        if existing.get("owner") != owner or not existing.get("active"):
            session.rollback()
            return
        row.updated_at = _now_utc()
        session.add(row)
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        try:
            session.close()
        except Exception:
            pass


def _start_gate_heartbeat(*, owner: str, stop_event: threading.Event) -> threading.Thread:
    def _loop():
        while not stop_event.wait(_GATE_HEARTBEAT_SECONDS):
            _heartbeat_sync_gate(owner=owner)

    t = threading.Thread(target=_loop, name=f"startup-sync-gate-hb-{owner[:8]}", daemon=True)
    t.start()
    return t


def _opens_worker_gate(reason: str) -> bool:
    """ARR-change jobs must not flip startup_sync_complete; startup paths must."""
    r = str(reason or "").strip().lower()
    return r not in {"arr_change", "arr_endpoint_changed"}


def process_startup_sync_runner_job(session, job: Job) -> dict[str, Any]:
    """Worker handler: runs one of the startup / ARR-change pipelines based on payload.reason.

    Phase 1 connection-lifecycle: this handler RELEASES the worker session
    immediately after acquiring the row-based sync gate. The actual sync
    work uses its own short-lived sessions internally. The gate is
    refreshed by a heartbeat thread and released in a tiny finalize
    transaction.
    """
    payload = dict(job.payload or {})
    reason = str(payload.get("reason") or "lifespan").strip().lower()
    job_id_repr = getattr(job, "id", "?")
    owner = f"job:{job_id_repr}"

    # Release the worker's claim session BEFORE we even attempt to acquire
    # the gate; gate acquisition uses its own short-lived session.
    try:
        session.close()
    except Exception:
        pass

    if not _try_acquire_sync_gate(owner=owner):
        logger.info(
            f"startup_sync_runner job_id={job_id_repr} skipped — sync gate busy (reason={reason})",
            extra={"emoji_type": "info"},
        )
        return {"ok": True, "skipped": "sync_gate_busy", "reason": reason}

    from services.startup_gate import startup_sync_complete

    stop_hb = threading.Event()
    _start_gate_heartbeat(owner=owner, stop_event=stop_hb)
    try:
        if reason in {"arr_change", "arr_endpoint_changed"}:
            from services.source_of_truth.sync_runner import run_full_sync

            logger.info(
                f"startup_sync_runner job_id={job_id_repr} ARR-change full sync starting reason={reason}",
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
                f"startup_sync_runner job_id={job_id_repr} ARR-change full sync completed runs={run_count}",
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

        from services.source_of_truth.startup import run_startup_source_of_truth

        logger.info(
            f"startup_sync_runner job_id={job_id_repr} running startup source-of-truth (reason={reason})",
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
        stop_hb.set()
        _release_sync_gate(owner=owner)
        if _opens_worker_gate(reason):
            startup_sync_complete.set()


__all__ = [
    "STARTUP_SYNC_RUNNER_JOB_TYPE",
    "use_job_driven_startup_sync",
    "enqueue_startup_sync_runner_job",
    "process_startup_sync_runner_job",
]
