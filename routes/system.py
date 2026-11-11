"""System-level routes: restarting the running process from the Settings UI.

Placeholdarr runs as a single long-lived process inside its container. There is
no in-process "reload config" for restart-required settings (env-derived at
import time), so the practical way to apply them is to exit the process and
let Docker's restart policy (`restart: unless-stopped` in docker-compose.yml)
bring it back up with the new configuration.
"""

from __future__ import annotations

import asyncio
import os
import signal

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.logger import logger

router = APIRouter(prefix="/api/system", tags=["system"])

_RESTART_DELAY_SECONDS = 0.75


async def _delayed_self_terminate() -> None:
    await asyncio.sleep(_RESTART_DELAY_SECONDS)
    logger.warning("Restart requested from Settings UI; terminating process now.", extra={"emoji_type": "restart"})
    os.kill(os.getpid(), signal.SIGTERM)


@router.post("/restart")
async def system_restart():
    """Schedule a graceful self-restart shortly after responding.

    Requires the container to run under a restart policy that brings it back
    up after a clean exit (already the case in the bundled docker-compose.yml).
    Without such a policy, this stops the app instead of restarting it.
    """
    asyncio.create_task(_delayed_self_terminate())
    return JSONResponse(content={"status": "restarting", "delay_seconds": _RESTART_DELAY_SECONDS})


@router.post("/jobs/fail-active")
async def system_fail_active_jobs():
    """Fail all PENDING/CLAIMED/WORKING worker jobs, then restart the process.

    Cancels queued imports, webhooks, reconcile, and other job-queue work.
    A hung handler mid-syscall is not aborted in-process; the follow-up restart
    drops wedged threads and any DB connections they still hold.
    """
    from services.job_fail_active import FAIL_ACTIVE_REASON, fail_active_jobs

    result = await asyncio.to_thread(fail_active_jobs, reason=FAIL_ACTIVE_REASON)
    failed_count = int(result.get("failed_count") or 0)
    logger.warning(
        f"Fail-active recovery complete (failed_count={failed_count}); scheduling restart "
        f"in {_RESTART_DELAY_SECONDS}s to release any wedged handler threads.",
        extra={"emoji_type": "restart"},
    )
    asyncio.create_task(_delayed_self_terminate())
    return JSONResponse(
        content={
            "ok": True,
            "failed_count": failed_count,
            "job_ids": list(result.get("job_ids") or []),
            "reason": str(result.get("reason") or FAIL_ACTIVE_REASON),
            "status": "restarting",
            "delay_seconds": _RESTART_DELAY_SECONDS,
        }
    )
