"""Apply-scope wiring for poster overlay (art) backfill after settings save.

Mirrors ``template_backfill`` for NFO/status projection:

- ``now``: enqueue batched ``placeholder_art_refresh`` for every active placeholder.
- ``next_full_sync``: set a pending AppConfig flag (no immediate library-wide art pass).
- ``future``: clear the pending flag; no retroactive work (new overlay applies on create / stage moves).
"""

from __future__ import annotations

from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig

PENDING_FLAG_KEY = "PLACEHOLDER_ART_BACKFILL_PENDING"


def _set_pending_flag(session, pending: bool) -> None:
    row = session.query(AppConfig).filter(AppConfig.key == PENDING_FLAG_KEY).first()
    if row is None:
        if pending:
            session.add(
                AppConfig(
                    key=PENDING_FLAG_KEY,
                    value=True,
                    value_type="bool",
                    restart_required=False,
                    description=(
                        "Internal flag: a poster overlay save asked for art backfill on the next full sync."
                    ),
                )
            )
    else:
        row.value = bool(pending)
        row.value_type = "bool"
        session.add(row)


def _read_pending_flag(session) -> bool:
    row = session.query(AppConfig).filter(AppConfig.key == PENDING_FLAG_KEY).first()
    return bool(row and row.value)


def is_art_backfill_pending() -> bool:
    try:
        from services.source_of_truth.placeholder_refresh import get_pending_intent

        return bool(get_pending_intent().get("art"))
    except Exception:
        pass
    session = get_session()
    try:
        return _read_pending_flag(session)
    except Exception:
        return False
    finally:
        try:
            session.close()
        except Exception:
            pass


def mark_art_backfill_pending() -> dict[str, Any]:
    session = get_session()
    try:
        _set_pending_flag(session, True)
        session.commit()
        logger.info(
            "Poster art backfill scheduled for the next full sync (overlay settings save).",
            extra={"emoji_type": "calendar"},
        )
        return {"ok": True, "pending": True}
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(f"Failed to mark art backfill pending: {exc}", extra={"emoji_type": "warning"})
        return {"ok": False, "reason": str(exc)}
    finally:
        try:
            session.close()
        except Exception:
            pass


def clear_pending_art_backfill() -> dict[str, Any]:
    session = get_session()
    try:
        _set_pending_flag(session, False)
        session.commit()
        return {"ok": True, "pending": False}
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(f"Failed to clear art backfill flag: {exc}", extra={"emoji_type": "warning"})
        return {"ok": False, "reason": str(exc)}
    finally:
        try:
            session.close()
        except Exception:
            pass


def execute_art_backfill_apply_scope(apply_scope: str) -> dict[str, Any]:
    """Materialize the user's apply policy after poster overlay settings change."""
    from services.source_of_truth.placeholder_refresh import execute_placeholder_refresh_apply_scope

    out = execute_placeholder_refresh_apply_scope(
        apply_scope=apply_scope,
        art=True,
        source="settings_save",
    )
    scope = str(out.get("scope") or "").strip().lower()
    try:
        if scope == "next_full_sync":
            mark_art_backfill_pending()
        elif scope in {"future", "now"}:
            clear_pending_art_backfill()
    except Exception:
        pass
    art = out.get("art_backfill") if isinstance(out.get("art_backfill"), dict) else {}
    merged = dict(art) if art else {"ok": bool(out.get("ok", True))}
    merged.setdefault("scope", out.get("scope"))
    merged.setdefault("enqueued", bool(out.get("enqueued")))
    if out.get("pending") is not None:
        merged["pending"] = bool(out.get("pending"))
    return merged


def clear_pending_art_backfill_if_set() -> None:
    """Best-effort clear after a full sync consumes overlay backfill intent."""
    if not is_art_backfill_pending():
        return
    clear_pending_art_backfill()
