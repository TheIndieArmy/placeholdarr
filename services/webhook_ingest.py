"""Webhook persist: durable EventLog + Job before HTTP 200 (off the event loop via to_thread)."""

from __future__ import annotations

from typing import Any

from core.logger import logger


def persist_webhook_before_accept(payload: dict[str, Any], instance: str | None) -> dict[str, Any]:
    """Sync persist used from ``asyncio.to_thread`` so ARR callers get 200 only after enqueue."""
    from services.handlers import handle_webhook

    result = handle_webhook(payload, instance)
    status = str(result.get("status") or "").strip().lower()
    if status != "accepted":
        reason = str(result.get("reason") or "webhook_rejected")
        logger.warning(
            f"Webhook persist rejected instance={instance or 'unknown'} reason={reason}",
            extra={"emoji_type": "warning"},
        )
        raise ValueError(reason)
    return result


def enqueue_webhook_persist(payload: dict[str, Any], instance: str | None) -> None:
    """Compatibility shim; prefer ``persist_webhook_before_accept`` from the HTTP route."""
    persist_webhook_before_accept(payload, instance)
