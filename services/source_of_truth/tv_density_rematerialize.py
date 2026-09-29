"""Apply-scope rematerialize after TV placeholder density / retire-when changes."""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import func

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig, Placeholder

PENDING_FLAG_KEY = "TV_DENSITY_REMATERIALIZE_PENDING"


def tv_placeholder_count_for_apply_now() -> int:
    """Active TV placeholders only (movies excluded). Used by the density apply-scope modal."""
    session = get_session()
    try:
        n = (
            session.query(func.count(Placeholder.id))
            .filter(
                Placeholder.has_placeholder == True,  # noqa: E712
                Placeholder.movie_id.is_(None),
            )
            .scalar()
        )
        return int(n or 0)
    except Exception:
        return 0
    finally:
        try:
            session.close()
        except Exception:
            pass

_DENSITY_RANK = {"episode": 0, "season": 1, "series": 2}


def density_rank(value: str | None) -> int:
    raw = str(value or "episode").strip().lower()
    return int(_DENSITY_RANK.get(raw, 0))


def density_change_is_consolidating(before: str | None, after: str | None) -> bool:
    """True when moving to fewer on-disk stubs (episode→season/series or season→series)."""
    return density_rank(after) > density_rank(before)


def _get_pending_row(session):
    return session.query(AppConfig).filter(AppConfig.key == PENDING_FLAG_KEY).first()


def mark_tv_density_rematerialize_pending(pending: bool = True) -> None:
    session = get_session()
    try:
        row = _get_pending_row(session)
        if not row:
            row = AppConfig(
                key=PENDING_FLAG_KEY,
                value=bool(pending),
                value_type="bool",
                restart_required=False,
                description="Internal: rematerialize TV placeholders after density/retire-when change on next full sync.",
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


def is_tv_density_rematerialize_pending() -> bool:
    session = get_session()
    try:
        row = _get_pending_row(session)
        if not row:
            return False
        return bool(row.value) is True or str(row.value).strip().lower() in {"1", "true", "yes"}
    finally:
        session.close()


def clear_tv_density_rematerialize_pending() -> None:
    mark_tv_density_rematerialize_pending(False)


def run_tv_density_rematerialize(*, source: str = "settings_save") -> dict[str, Any]:
    """Run determination + materialization so disk matches the current density setting."""
    from services.source_of_truth.determiner import run_determination_pass
    from services.source_of_truth.materializer import run_materialization_pass

    logger.info(
        f"TV density rematerialize starting ({source})",
        extra={"emoji_type": "gear"},
    )
    determination = run_determination_pass()
    materialization = run_materialization_pass()
    try:
        clear_tv_density_rematerialize_pending()
    except Exception as exc:
        logger.warning(
            f"Could not clear TV density rematerialize pending flag: {exc}",
            extra={"emoji_type": "warning"},
        )
    out = {
        "ok": True,
        "source": source,
        "determination": determination,
        "materialization": materialization,
    }
    logger.info(
        f"TV density rematerialize finished ({source}): "
        f"mat_created={materialization.get('created')} mat_deleted={materialization.get('deleted')}",
        extra={"emoji_type": "success"},
    )
    return out


def start_tv_density_rematerialize_background(*, source: str = "settings_save") -> None:
    def _run() -> None:
        try:
            run_tv_density_rematerialize(source=source)
        except Exception as exc:
            logger.error(
                f"TV density rematerialize background failed ({source}): {exc}",
                extra={"emoji_type": "error"},
            )

    threading.Thread(
        target=_run,
        name="tv-density-rematerialize",
        daemon=True,
    ).start()


def execute_tv_density_rematerialize_apply_scope(
    apply_scope: str,
    *,
    source: str = "settings_save",
) -> dict[str, Any]:
    scope = str(apply_scope or "").strip().lower()
    if scope not in {"now", "next_full_sync"}:
        scope = "next_full_sync"

    if scope == "next_full_sync":
        mark_tv_density_rematerialize_pending(True)
        logger.info(
            f"TV density rematerialize deferred to next full sync ({source})",
            extra={"emoji_type": "info"},
        )
        return {"ok": True, "scope": "next_full_sync", "pending": True, "enqueued": False}

    # Apply now: mark pending cleared after run; start background so settings save returns quickly.
    try:
        mark_tv_density_rematerialize_pending(False)
    except Exception:
        pass
    start_tv_density_rematerialize_background(source=f"{source}:apply_now")
    return {"ok": True, "scope": "now", "pending": False, "enqueued": True}


def run_tv_density_rematerialize_if_pending(*, source: str = "full_sync") -> dict[str, Any]:
    if not is_tv_density_rematerialize_pending():
        return {"ok": True, "skipped": True, "reason": "not_pending"}
    return run_tv_density_rematerialize(source=source)
