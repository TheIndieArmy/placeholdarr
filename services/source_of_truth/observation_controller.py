from __future__ import annotations

from typing import Any

from core.config import settings
from core.logger import logger
from services.source_of_truth.observation_hybrid import enqueue_hybrid_observation_slice
from services.source_of_truth.observation_selection import rank_placeholder_ids_for_observation
from services.source_of_truth.observation_trail import enqueue_observation_trail


def _conditional_trail_enabled() -> bool:
    return bool(getattr(settings, "OBSERVATION_CONTINUATION_TRAIL_CONDITIONAL_ENABLED", True))


def _trail_max_candidates() -> int:
    try:
        return max(1, int(getattr(settings, "OBSERVATION_CONTINUATION_TRAIL_MAX_CANDIDATES", 150)))
    except Exception:
        return 150


def _should_enqueue_trail(*, hybrid_enabled: bool, hybrid_result: dict[str, Any], candidate_count: int) -> tuple[bool, str]:
    if not hybrid_enabled:
        return True, "hybrid_disabled"

    if not bool(hybrid_result.get("enqueued")):
        return True, "hybrid_not_enqueued"

    if not _conditional_trail_enabled():
        return True, "conditional_policy_disabled"

    max_candidates = _trail_max_candidates()
    if int(candidate_count) <= max_candidates:
        return True, "within_trail_candidate_limit"

    return False, f"candidate_count_exceeds_limit:{max_candidates}"


def enqueue_observation_continuation(
    session,
    *,
    placeholder_ids: list[int],
    source: str,
    trigger_reason: str,
    delay_seconds: int = 0,
) -> dict[str, Any]:
    """Enqueue deferred observation continuation using one canonical contract.

    Contract:
    - Prefer hybrid slice when enabled.
    - Conditionally enqueue trail fallback to avoid duplicate giant continuation lanes.
    - Emit one INFO summary for operators and one DEBUG branch detail.
    """
    raw_ids = [int(x) for x in (placeholder_ids or []) if x is not None]
    ids = rank_placeholder_ids_for_observation(session, raw_ids)
    if not ids:
        logger.debug(
            "Observation continuation skipped: no placeholder ids",
            extra={"emoji_type": "debug"},
        )
        return {
            "ok": False,
            "reason": "no_placeholder_ids",
            "hybrid_enqueued": False,
            "trail_enqueued": False,
        }

    hybrid_enabled = bool(getattr(settings, "HYBRID_OBSERVATION_SLICES_ENABLED", False))
    hybrid_result: dict[str, Any] = {"enqueued": False}
    trail_result: dict[str, Any] = {"enqueued": False}
    trail_reason = "not_evaluated"

    if hybrid_enabled:
        hybrid_result = enqueue_hybrid_observation_slice(
            session,
            placeholder_ids=ids,
            source=source,
            trigger_reason=trigger_reason,
            delay_seconds=delay_seconds,
        )
        logger.debug(
            "Observation continuation hybrid enqueue attempted "
            f"source={source} reason={trigger_reason} enqueued={bool(hybrid_result.get('enqueued'))} "
            f"coalesced={bool(hybrid_result.get('coalesced'))} candidates={len(ids)}",
            extra={"emoji_type": "debug"},
        )

    should_enqueue_trail, trail_reason = _should_enqueue_trail(
        hybrid_enabled=hybrid_enabled,
        hybrid_result=hybrid_result,
        candidate_count=len(ids),
    )
    if should_enqueue_trail:
        trail_result = enqueue_observation_trail(
            session,
            placeholder_ids=ids,
            source=source,
            delay_seconds=max(0, int(delay_seconds or 0)),
        )

    logger.info(
        "Observation continuation enqueued "
        f"source={source} reason={trigger_reason} candidates={len(ids)} "
        f"hybrid_enabled={hybrid_enabled} hybrid_enqueued={bool(hybrid_result.get('enqueued'))} "
        f"hybrid_coalesced={bool(hybrid_result.get('coalesced'))} hybrid_job_id={hybrid_result.get('job_id')} "
        f"trail_policy={trail_reason} trail_enqueued={bool(trail_result.get('enqueued'))} "
        f"trail_job_id={trail_result.get('job_id')}",
        extra={"emoji_type": "info"},
    )

    return {
        "ok": bool(hybrid_result.get("enqueued") or trail_result.get("enqueued")),
        "reason": "queued",
        "hybrid_enqueued": bool(hybrid_result.get("enqueued")),
        "hybrid_coalesced": bool(hybrid_result.get("coalesced")),
        "hybrid_job_id": hybrid_result.get("job_id"),
        "trail_enqueued": bool(trail_result.get("enqueued")),
        "trail_job_id": trail_result.get("job_id"),
        "trail_group_id": trail_result.get("group_id"),
        "trail_policy": trail_reason,
        "candidate_count": len(ids),
    }
