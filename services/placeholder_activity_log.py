"""Shared helpers for append-only PlaceholderActivityHistory rows (webhooks, grace jobs, etc.)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.postgres.models import PlaceholderActivityHistory


def materialization_stats_dict(stats: Any) -> dict[str, Any]:
    return stats if isinstance(stats, dict) else {}


def outcome_reason_and_status_from_materialization(prefix: str, mat: dict[str, Any]) -> tuple[str, str]:
    """Map aggregate materialization stats to reason + status_label (priority: created > deleted > noop)."""
    created_n = int(mat.get("created", 0) or 0)
    deleted_n = int(mat.get("deleted", 0) or 0)
    noop_n = int(mat.get("noop", 0) or 0)
    if created_n > 0:
        return (f"{prefix} - placeholder created.", "Created")
    if deleted_n > 0:
        return (f"{prefix} - placeholder cleanup applied ({deleted_n} removed).", "Deleted")
    if noop_n > 0:
        return (f"{prefix} - placeholder already matched disk; no new file was written.", "Already Exists")
    return (f"{prefix} - no placeholder filesystem change.", "No Action")


def append_placeholder_activity_status(
    session,
    *,
    item_type: str,
    movie_id: int | None,
    episode_id: int | None,
    series_id: int | None,
    season_id: int | None,
    season_number: int | None,
    instance_key: str | None,
    instance_id: str | None,
    event_type: str,
    path: str,
    item_title: str,
    series_title: str | None,
    reason: str,
    status_label: str,
    source: str,
    extra_snapshot: dict[str, Any],
) -> None:
    session.add(
        PlaceholderActivityHistory(
            occurred_at=datetime.now(timezone.utc),
            action="Status",
            item_type=item_type,
            placeholder_id=None,
            movie_id=movie_id,
            episode_id=episode_id,
            series_id=series_id,
            season_id=season_id,
            season_number=season_number,
            instance_key=instance_key,
            instance_id=instance_id,
            event_type=event_type,
            path=str(path or ""),
            item_title=str(item_title or ""),
            series_title=series_title,
            reason=str(reason or ""),
            status_label=str(status_label or ""),
            source=source,
            event_log_id=None,
            extra_snapshot=extra_snapshot or None,
        )
    )
