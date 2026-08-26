"""Bounded webhook persist so ARR import-list bursts cannot exhaust the DB pool.

The HTTP handler returns 200 immediately. Persist (EventLog + Job) runs on a
small worker pool instead of unbounded FastAPI background tasks.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from core.config import settings
from core.logger import logger

_ingest_queue: queue.Queue[tuple[dict[str, Any], str | None, int]] = queue.Queue()
_started = False
_start_lock = threading.Lock()
_MAX_ATTEMPTS = 4
_RETRY_SLEEP_SECONDS = 0.25


def _concurrency() -> int:
    try:
        return max(1, int(getattr(settings, "WEBHOOK_INGEST_CONCURRENCY", 1) or 1))
    except Exception:
        return 1


def _ensure_started() -> None:
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        workers = _concurrency()
        for index in range(workers):
            thread = threading.Thread(
                target=_ingest_loop,
                name=f"webhook-ingest-{index}",
                daemon=True,
            )
            thread.start()
        _started = True
        logger.info(
            f"Webhook ingest queue started concurrency={workers}",
            extra={"emoji_type": "gear"},
        )


def enqueue_webhook_persist(payload: dict[str, Any], instance: str | None) -> None:
    """Queue validated webhook persist. HTTP should already have returned 200."""
    _ensure_started()
    depth = _ingest_queue.qsize()
    if depth >= 200:
        logger.warning(
            f"Webhook ingest queue depth={depth} instance={instance or 'unknown'}",
            extra={"emoji_type": "warning"},
        )
    _ingest_queue.put((payload, instance, 0))


def _ingest_loop() -> None:
    from services.handlers import handle_webhook

    while True:
        payload, instance, attempt = _ingest_queue.get()
        try:
            handle_webhook(payload, instance)
        except Exception as exc:
            next_attempt = attempt + 1
            if next_attempt < _MAX_ATTEMPTS:
                logger.warning(
                    f"Webhook ingest retry {next_attempt}/{_MAX_ATTEMPTS - 1} "
                    f"instance={instance or 'unknown'}: {exc}",
                    extra={"emoji_type": "warning"},
                )
                time.sleep(_RETRY_SLEEP_SECONDS * next_attempt)
                _ingest_queue.put((payload, instance, next_attempt))
            else:
                logger.error(
                    f"Webhook ingest dropped after {_MAX_ATTEMPTS} attempts "
                    f"instance={instance or 'unknown'}: {exc}",
                    extra={"emoji_type": "error"},
                )
        finally:
            _ingest_queue.task_done()
