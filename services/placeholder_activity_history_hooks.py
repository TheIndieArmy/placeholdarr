"""Append-only `placeholder_activity_history` rows via SQLAlchemy events (materialize + status projection)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.attributes import get_history

from core.logger import logger
from services.postgres.models import (
    ArrMovieOverlay,
    ArrSeriesOverlay,
    Episode,
    EventLog,
    Movie,
    Placeholder,
    PlaceholderActivityHistory,
    Season,
    Series,
    TmdbMovie,
    TmdbSeries,
)
from services.source_of_truth.status_intent import StatusSource

_hooks_registered = False


def _utc(dt: datetime | None) -> datetime:
    if dt is None or not isinstance(dt, datetime):
        return datetime.now(timezone.utc)
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _action_occurred_at() -> datetime:
    """Wall-clock time for create/delete activity (never trust ORM updated_at during flush)."""
    return datetime.now(timezone.utc)


def _trunc(value: Any, max_len: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes"}:
        return True
    if text in {"0", "false", "f", "no"}:
        return False
    return bool(value)


def _instance_and_season_for_placeholder(session, ph: Placeholder) -> tuple[str | None, str | None, int | None]:
    if ph.movie_id:
        mv = session.query(Movie).filter(Movie.id == ph.movie_id).first()
        if mv:
            return getattr(mv, "instance_key", None), getattr(mv, "instance_id", None), None
        return None, None, None
    if getattr(ph, "tmdb_movie_id", None):
        try:
            overlay = (
                session.query(ArrMovieOverlay)
                .filter(ArrMovieOverlay.tmdb_id == int(ph.tmdb_movie_id))
                .order_by(ArrMovieOverlay.id.asc())
                .first()
            )
            if overlay:
                return (
                    getattr(overlay, "instance_key", None),
                    getattr(overlay, "instance_id", None),
                    None,
                )
        except Exception:
            pass
        return None, None, None
    if getattr(ph, "tmdb_series_id", None):
        try:
            overlay = (
                session.query(ArrSeriesOverlay)
                .filter(ArrSeriesOverlay.tmdb_id == int(ph.tmdb_series_id))
                .order_by(ArrSeriesOverlay.id.asc())
                .first()
            )
            if overlay:
                return (
                    getattr(overlay, "instance_key", None),
                    getattr(overlay, "instance_id", None),
                    1,
                )
        except Exception:
            pass
        return None, None, 1
    inst_key: str | None = None
    inst_id: str | None = None
    if ph.series_id:
        s = session.query(Series).filter(Series.id == ph.series_id).first()
        if s:
            inst_key = getattr(s, "instance_key", None)
            inst_id = getattr(s, "instance_id", None)
    season_number: int | None = None
    if ph.episode_id:
        ep = session.query(Episode).filter(Episode.id == ph.episode_id).first()
        if ep and getattr(ep, "season_id", None):
            season = session.query(Season).filter(Season.id == ep.season_id).first()
            if season:
                season_number = getattr(season, "season_number", None)
    elif ph.season_id:
        season = session.query(Season).filter(Season.id == ph.season_id).first()
        if season:
            season_number = getattr(season, "season_number", None)
    return inst_key, inst_id, season_number


def _item_type_for_placeholder(ph: Placeholder) -> str:
    if ph.movie_id or getattr(ph, "tmdb_movie_id", None):
        return "movie"
    if getattr(ph, "tmdb_series_id", None):
        return "episode"
    if ph.series_id and not ph.episode_id:
        return "series"
    return "episode"


def _title_for_placeholder(session, ph: Placeholder) -> str:
    """Best-effort display title at write time (Discover rows have no Arr movie_id)."""
    if ph.movie_id:
        mv = session.query(Movie).filter(Movie.id == ph.movie_id).first()
        if mv and getattr(mv, "title", None):
            return _trunc(mv.title, 512)
    tid = getattr(ph, "tmdb_movie_id", None)
    if tid:
        tm = session.query(TmdbMovie).filter(TmdbMovie.tmdb_id == int(tid)).first()
        if tm and getattr(tm, "title", None):
            title = str(tm.title).strip()
            year = getattr(tm, "year", None)
            if year:
                title = f"{title} ({int(year)})"
            return _trunc(title, 512)
    sid = getattr(ph, "tmdb_series_id", None)
    if sid:
        ts = session.query(TmdbSeries).filter(TmdbSeries.tmdb_id == int(sid)).first()
        if ts and getattr(ts, "title", None):
            title = str(ts.title).strip()
            year = getattr(ts, "year", None)
            if year:
                title = f"{title} ({int(year)})"
            return _trunc(f"{title} S01E01", 512)
    return ""


def _queue_history(session, values: dict[str, Any]) -> None:
    """Queue a history insert for after_flush.

    Mapper after_insert/after_update run mid-flush; ``session.add`` there is unsupported and
    can silently drop Created rows on placeholder revive.
    """
    if session is None:
        return
    pending = session.info.setdefault("placeholder_activity_pending", [])
    pending.append(values)


def _drain_pending_history(session: Session, _flush_context=None) -> None:
    pending = session.info.pop("placeholder_activity_pending", None)
    if not pending:
        return
    try:
        from sqlalchemy import insert

        session.execute(insert(PlaceholderActivityHistory.__table__), pending)
    except Exception as exc:
        logger.warning(
            "placeholder_activity_history insert skipped: %s",
            exc,
            extra={"emoji_type": "warning"},
        )


def _created_history_values(session, target: Placeholder, *, occurred: datetime) -> dict[str, Any]:
    extra = target.extra if isinstance(target.extra, dict) else {}
    item_type = _item_type_for_placeholder(target)
    create_reason = extra.get("create_reason")
    reason = _trunc(str(create_reason) if create_reason is not None else "", 4000)
    lifecycle = str(target.lifecycle_status or "").strip() or "Created"
    status_label = _trunc(lifecycle, 512)
    inst_key, inst_id, season_num = _instance_and_season_for_placeholder(session, target)
    return {
        "occurred_at": occurred,
        "action": "Created",
        "item_type": item_type,
        "placeholder_id": target.id,
        "movie_id": target.movie_id,
        "episode_id": target.episode_id,
        "series_id": target.series_id,
        "season_id": getattr(target, "season_id", None),
        "season_number": season_num,
        "instance_key": inst_key,
        "instance_id": inst_id,
        "event_type": "placeholder_created",
        "path": str(target.path or ""),
        "item_title": _title_for_placeholder(session, target),
        "series_title": None,
        "reason": reason,
        "status_label": status_label,
        "source": None,
        "event_log_id": None,
        "extra_snapshot": extra if extra else None,
    }


def _deleted_history_values(session, target: Placeholder, *, occurred: datetime) -> dict[str, Any]:
    extra = target.extra if isinstance(target.extra, dict) else {}
    item_type = _item_type_for_placeholder(target)
    del_reason = extra.get("delete_reason")
    reason = _trunc(str(del_reason) if del_reason is not None else "", 4000)
    lifecycle = str(target.lifecycle_status or "").strip() or "Deleted"
    status_label = _trunc(lifecycle, 512)
    inst_key, inst_id, season_num = _instance_and_season_for_placeholder(session, target)
    return {
        "occurred_at": occurred,
        "action": "Deleted",
        "item_type": item_type,
        "placeholder_id": target.id,
        "movie_id": target.movie_id,
        "episode_id": target.episode_id,
        "series_id": target.series_id,
        "season_id": getattr(target, "season_id", None),
        "season_number": season_num,
        "instance_key": inst_key,
        "instance_id": inst_id,
        "event_type": "placeholder_deleted",
        "path": str(target.path or ""),
        "item_title": _title_for_placeholder(session, target),
        "series_title": None,
        "reason": reason,
        "status_label": status_label,
        "source": None,
        "event_log_id": None,
        "extra_snapshot": extra if extra else None,
    }


def _on_placeholder_after_insert(_mapper, _connection, target: Placeholder) -> None:
    session = object_session(target)
    if session is None:
        return
    _queue_history(
        session,
        _created_history_values(session, target, occurred=_utc(getattr(target, "created_at", None))),
    )


def _placeholder_deleted_like(ph: Placeholder) -> bool:
    lifecycle = str(ph.lifecycle_status or "").strip().lower()
    path = getattr(ph, "path", None) or ""
    try:
        file_exists = bool(path and os.path.exists(path))
    except OSError:
        file_exists = False
    return (not bool(ph.has_placeholder)) and (not file_exists) and lifecycle in {
        "deleted",
        "missing",
        "obsolete",
        "replaced",
        "",
    }


def _should_record_placeholder_deleted(ph: Placeholder) -> bool:
    if not _placeholder_deleted_like(ph):
        return False
    hp = get_history(ph, "has_placeholder")
    if hp.has_changes():
        prev = _as_bool(hp.deleted[0] if hp.deleted else None)
        curr = _as_bool(hp.added[0] if hp.added else ph.has_placeholder)
        if prev is True and curr is False:
            return True
    lc = get_history(ph, "lifecycle_status")
    if lc.has_changes() and lc.added:
        new_lc = str(lc.added[0] or "").strip().lower()
        if new_lc in {"deleted", "missing", "obsolete", "replaced"}:
            return True
    return False


def _should_record_placeholder_created_on_update(ph: Placeholder) -> bool:
    """True when an existing Placeholder row is revived (reuse path, not INSERT)."""
    if not bool(getattr(ph, "has_placeholder", False)):
        return False
    lifecycle = str(getattr(ph, "lifecycle_status", None) or "").strip().lower()
    if lifecycle in {"deleted", "missing", "obsolete", "replaced"}:
        return False

    hp = get_history(ph, "has_placeholder")
    if hp.has_changes():
        prev = _as_bool(hp.deleted[0] if hp.deleted else None)
        curr = _as_bool(hp.added[0] if hp.added else ph.has_placeholder)
        if prev is False and curr is True:
            return True

    lc = get_history(ph, "lifecycle_status")
    if lc.has_changes() and lc.added:
        new_lc = str(lc.added[0] or "").strip().lower()
        old_lc = str(lc.deleted[0] or "").strip().lower() if lc.deleted else ""
        if new_lc in {"active", "created"} and old_lc in {"deleted", "missing", "obsolete", "replaced"}:
            return True
    return False


def _on_placeholder_after_update(_mapper, _connection, target: Placeholder) -> None:
    session = object_session(target)
    if session is None:
        return
    if _should_record_placeholder_created_on_update(target):
        _queue_history(
            session,
            _created_history_values(session, target, occurred=_action_occurred_at()),
        )
        return
    if not _should_record_placeholder_deleted(target):
        return
    _queue_history(
        session,
        _deleted_history_values(session, target, occurred=_action_occurred_at()),
    )


def _on_event_log_after_insert(_mapper, _connection, target: EventLog) -> None:
    if str(target.event_type or "").strip() != "placeholder_status_changed":
        return
    session = object_session(target)
    if session is None:
        return
    payload = target.payload if isinstance(target.payload, dict) else {}
    try:
        ph_id = int(payload.get("placeholder_id"))
    except (TypeError, ValueError):
        return
    ph = session.query(Placeholder).filter(Placeholder.id == ph_id).first()
    if not ph:
        return
    new_status = str(payload.get("new_status") or "").strip()
    old_status = str(payload.get("old_status") or "").strip()
    reason_part = str(payload.get("reason") or "").strip()
    detail = f"{old_status or '--'} → {new_status or '--'}"
    if reason_part:
        detail = f"{detail} • {reason_part}"
    src = str(target.source or "").strip()
    if src and src != StatusSource.CALENDAR_RELEASE_WINDOW.value:
        detail = f"{detail} • {src}"
    reason = _trunc(detail, 8000)
    status_label = _trunc(new_status, 512)
    item_type = _item_type_for_placeholder(ph)
    inst_key, inst_id, season_num = _instance_and_season_for_placeholder(session, ph)
    _queue_history(
        session,
        {
            "occurred_at": _utc(getattr(target, "created_at", None)),
            "action": "Status",
            "item_type": item_type,
            "placeholder_id": ph.id,
            "movie_id": ph.movie_id,
            "episode_id": ph.episode_id,
            "series_id": ph.series_id,
            "season_id": getattr(ph, "season_id", None),
            "season_number": season_num,
            "instance_key": inst_key,
            "instance_id": inst_id,
            "event_type": "placeholder_status_changed",
            "path": str(ph.path or ""),
            "item_title": _title_for_placeholder(session, ph),
            "series_title": None,
            "reason": reason,
            "status_label": status_label,
            "source": _trunc(src, 128) or None,
            "event_log_id": target.id,
            "extra_snapshot": payload if payload else None,
        },
    )


def register_placeholder_activity_history_hooks() -> None:
    global _hooks_registered
    if _hooks_registered:
        return
    _hooks_registered = True
    event.listen(Placeholder, "after_insert", _on_placeholder_after_insert)
    event.listen(Placeholder, "after_update", _on_placeholder_after_update)
    event.listen(EventLog, "after_insert", _on_event_log_after_insert)
    event.listen(Session, "after_flush", _drain_pending_history)
    logger.info("Registered placeholder_activity_history ORM hooks", extra={"emoji_type": "success"})
