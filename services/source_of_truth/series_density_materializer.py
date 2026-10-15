"""Arr TV series-density stubs: one playable file per series that still needs content."""

from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.messages.context import build_projection_context_from_session
from services.placeholder_poster_art import ensure_series_art
from services.placeholders import (
    ensure_episode_nfo,
    ensure_placeholder_file,
    ensure_series_nfo,
    series_density_stub_path,
)
from services.postgres.db import get_session
from services.postgres.models import Episode, Placeholder, Season, Series
from services.source_of_truth.determiner import (
    DETERMINATION_NEEDS,
    DETERMINATION_NOT_NEEDED,
    episode_contributes_to_density_stub,
)
from services.source_of_truth.placeholder_cleanup import (
    cleanup_episode_placeholder_files,
)
from services.source_of_truth.tv_density import (
    DENSITY_PLACEHOLDER_TITLE,
    PLACEHOLDER_KIND_SERIES_STUB,
    density_uses_series_stub,
    tv_density_retire_when_for_series,
    tv_placeholder_density,
)
from services.status_projection import projected_status_display

REQUEST_STATUS = "REQUEST"
REQUEST_REASON = "placeholder_request"


def _norm_stub_path(path: str | None) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text))


def _placeholders_enabled() -> bool:
    return bool(getattr(settings, "coming_soon_placeholders_enabled", True))


def _lookahead_days() -> int:
    try:
        return int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    except (TypeError, ValueError):
        return 30


def _include_specials() -> bool:
    return bool(getattr(settings, "INCLUDE_SPECIALS", False))


def _series_policy(series: Series) -> str:
    if bool(getattr(series, "block_placeholder", False)):
        return "never"
    if bool(getattr(series, "force_placeholder", False)):
        return "pinned"
    return "auto"


def _active_series_stub(session, series_id: int) -> Placeholder | None:
    return (
        session.query(Placeholder)
        .filter(
            Placeholder.series_id == int(series_id),
            Placeholder.placeholder_kind == PLACEHOLDER_KIND_SERIES_STUB,
            Placeholder.episode_id.is_(None),
            Placeholder.movie_id.is_(None),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .order_by(Placeholder.id.desc())
        .first()
    )


def _series_stub_rows(session, series_id: int) -> list[Placeholder]:
    return (
        session.query(Placeholder)
        .filter(
            Placeholder.series_id == int(series_id),
            Placeholder.placeholder_kind == PLACEHOLDER_KIND_SERIES_STUB,
            Placeholder.episode_id.is_(None),
            Placeholder.movie_id.is_(None),
        )
        .all()
    )


def series_stub_needed(session, series: Series, *, now_date: date | None = None) -> bool:
    """Whether this series should have a density stub under current retire-when + policy."""
    if bool(getattr(series, "is_deleted", False)):
        return False
    if not density_uses_series_stub(series):
        return False

    policy = _series_policy(series)
    if policy == "never":
        return False
    if policy == "pinned":
        return True

    retire = tv_density_retire_when_for_series(series)
    include_specials = _include_specials()
    placeholders_enabled = _placeholders_enabled()
    lookahead_days = _lookahead_days()
    today = now_date or date.today()

    episodes = (
        session.query(Episode)
        .join(Season, Episode.season_id == Season.id)
        .filter(Season.series_id == int(series.id), Episode.is_deleted == False)  # noqa: E712
        .all()
    )
    if not episodes:
        return False

    season_by_id = {
        int(s.id): s
        for s in session.query(Season).filter(Season.series_id == int(series.id)).all()
        if getattr(s, "id", None) is not None
    }

    if retire == "when_any_episode_has_file":
        for ep in episodes:
            season = season_by_id.get(int(ep.season_id)) if getattr(ep, "season_id", None) else None
            sn = int(getattr(season, "season_number", 0) or 0) if season else 0
            if not include_specials and sn == 0:
                continue
            if bool(getattr(ep, "has_file", False)):
                return False

    any_needs = False
    for ep in episodes:
        season = season_by_id.get(int(ep.season_id)) if getattr(ep, "season_id", None) else None
        sn = int(getattr(season, "season_number", 0) or 0) if season else 0
        if not include_specials and sn == 0:
            from services.source_of_truth.placeholder_policy import (
                policy_from_entity,
                resolve_episode_effective_policy,
            )

            effective = resolve_episode_effective_policy(
                series_policy=policy,  # type: ignore[arg-type]
                episode_policy=policy_from_entity(ep),
            )
            if effective != "pinned":
                continue
        meta = (
            int(series.id),
            sn,
            int(getattr(ep, "episode_number", 0) or 0),
        )
        if episode_contributes_to_density_stub(
            session,
            ep,
            placeholders_enabled=placeholders_enabled,
            lookahead_days=lookahead_days,
            now_date=today,
            episode_order_meta=meta,
            series_policy=policy,
        ):
            any_needs = True
            break

    return any_needs


def _stub_nfo_proxies(series: Series, season01: Season | None) -> tuple[Any, Any]:
    season = season01 or SimpleNamespace(
        id=None,
        series_id=series.id,
        season_number=1,
        title=f"{series.title} Season 01",
        year=getattr(series, "year", None) or 0,
    )
    episode = SimpleNamespace(
        id=None,
        season_id=getattr(season, "id", None),
        episode_number=1,
        title=DENSITY_PLACEHOLDER_TITLE,
        air_date=None,
        overview=None,
        sonarr_episode_overview=None,
        runtime=getattr(series, "sonarr_runtime", None),
        absolute_episode_number=None,
    )
    return episode, season


def _mark_series_stub_active(
    session,
    *,
    series: Series,
    path: str,
    season_id: int | None,
    activity_reason: str | None = None,
) -> Placeholder:
    row = _active_series_stub(session, int(series.id))
    if not row:
        candidate = (
            session.query(Placeholder)
            .filter(Placeholder.path == path)
            .order_by(Placeholder.id.desc())
            .first()
        )
        if candidate and candidate.movie_id is None and candidate.episode_id is None:
            if candidate.series_id is None or int(candidate.series_id) == int(series.id):
                row = candidate
        if not row:
            for existing in _series_stub_rows(session, int(series.id)):
                row = existing
                break
        if not row:
            row = Placeholder(
                series_id=int(series.id),
                season_id=season_id,
                episode_id=None,
                movie_id=None,
                path=path,
                placeholder_kind=PLACEHOLDER_KIND_SERIES_STUB,
                created_by="source_of_truth.series_density",
            )
            session.add(row)

    old_path = str(getattr(row, "path", "") or "").strip()
    new_path = str(path or "").strip()
    if old_path and new_path and _norm_stub_path(old_path) != _norm_stub_path(new_path):
        # Destination / Library Root moves must not leave a playable stub behind.
        season = None
        if season_id is not None:
            season = session.query(Season).filter(Season.id == int(season_id)).first()
        cleanup_episode_placeholder_files(
            session,
            season=season,
            series=series,
            candidate_paths=[old_path],
        )

    row.movie_id = None
    row.episode_id = None
    row.series_id = int(series.id)
    row.season_id = season_id
    row.placeholder_kind = PLACEHOLDER_KIND_SERIES_STUB
    row.path = path
    row.has_placeholder = True
    row.lifecycle_status = "ACTIVE"
    row.display_status = REQUEST_STATUS
    row.display_reason = REQUEST_REASON
    row.plex_placeholder_id = None
    row.jellyfin_placeholder_id = None
    row.emby_placeholder_id = None
    row.plex_id_observed_at = None
    row.jellyfin_id_observed_at = None
    row.emby_id_observed_at = None
    row.media_lookup_error = None
    row.media_lookup_last_attempt_at = None
    _rm = getattr(series, "sonarr_runtime", None)
    try:
        runtime_minutes = int(_rm) if _rm is not None else None
    except (TypeError, ValueError):
        runtime_minutes = None
    _media_ctx = build_projection_context_from_session(
        session,
        movie_id=None,
        episode_id=None,
        runtime_minutes=runtime_minutes,
    )
    row.display_status_projected = projected_status_display(
        REQUEST_STATUS,
        reason=REQUEST_REASON,
        runtime_minutes=runtime_minutes,
        media_context=_media_ctx,
    )
    row.display_progress = 0
    extra = dict(getattr(row, "extra", {}) or {})
    extra.pop("plex_metadata_ready_seen", None)
    extra.pop("delete_reason", None)
    if activity_reason:
        extra["create_reason"] = str(activity_reason)
        extra["last_action_reason"] = str(activity_reason)
        extra["last_action"] = "created"
    row.extra = extra
    row.last_observed_at = func.now()
    row.updated_at = func.now()
    row.determination = DETERMINATION_NEEDS
    row.determination_updated_at = func.now()
    session.add(row)
    return row


def _mark_series_stub_deleted(
    session,
    *,
    series_id: int,
    activity_reason: str | None = None,
) -> list[str]:
    paths: list[str] = []
    for row in _series_stub_rows(session, series_id):
        if getattr(row, "path", None):
            paths.append(str(row.path))
        row.has_placeholder = False
        row.lifecycle_status = "DELETED"
        row.path = ""
        row.display_status = None
        row.display_status_projected = None
        row.display_reason = None
        row.display_progress = None
        row.queue_monitor_active = False
        row.queue_monitor_active_set_at = None
        row.determination = DETERMINATION_NOT_NEEDED
        row.determination_updated_at = func.now()
        extra = dict(getattr(row, "extra", {}) or {})
        if activity_reason:
            extra["delete_reason"] = str(activity_reason)
            extra["last_action_reason"] = str(activity_reason)
            extra["last_action"] = "deleted"
        row.extra = extra
        row.updated_at = func.now()
        session.add(row)
    return paths


def _dummy_path() -> str:
    from services.placeholders import resolve_calendar_variant_dummy_path

    path = str(resolve_calendar_variant_dummy_path("primary") or "").strip()
    if not path or not os.path.isfile(path):
        raise RuntimeError(f"Dummy video missing or invalid for series density stub: {path!r}")
    return path


def apply_series_density_materialization(
    series_id: int,
    session=None,
    activity_reason: str | None = None,
) -> dict[str, Any]:
    owns_session = session is None
    session = session or get_session()
    try:
        series = session.query(Series).filter(Series.id == int(series_id)).first()
        if not series:
            return {"ok": False, "reason": "series_not_found", "series_id": series_id}

        if not density_uses_series_stub(series):
            paths = _mark_series_stub_deleted(
                session,
                series_id=int(series.id),
                activity_reason=activity_reason or "density_not_series",
            )
            if paths:
                season01 = (
                    session.query(Season)
                    .filter(Season.series_id == int(series.id), Season.season_number == 1)
                    .first()
                )
                cleanup_episode_placeholder_files(
                    session,
                    season=season01,
                    series=series,
                    candidate_paths=paths,
                )
            try:
                from services.series_episode_stats_hooks import refresh_series_stats_after_bulk

                refresh_series_stats_after_bulk(session, series_ids={int(series.id)})
            except Exception:
                pass
            if owns_session:
                session.commit()
            return {
                "ok": True,
                "action": "skipped_density",
                "series_id": int(series.id),
                "deleted": bool(paths),
                "paths": paths,
            }

        needed = series_stub_needed(session, series)
        season01 = (
            session.query(Season)
            .filter(Season.series_id == int(series.id), Season.season_number == 1)
            .first()
        )
        season_id = int(season01.id) if season01 is not None else None

        if needed:
            target_path = series_density_stub_path(series)
            created = ensure_placeholder_file(target_path, dummy_file_path=_dummy_path())
            ep_proxy, season_proxy = _stub_nfo_proxies(series, season01)
            nfo_written = False
            try:
                nfo_written = ensure_episode_nfo(target_path, ep_proxy, season_proxy, series)
            except Exception as exc:
                logger.debug(
                    f"Series density stub NFO skipped for series_id={series.id}: {exc}",
                    extra={"emoji_type": "debug"},
                )
            series_folder = getattr(series, "placeholder_folder", None)
            try:
                series_nfo_written = ensure_series_nfo(series, folder=series_folder)
            except Exception:
                series_nfo_written = False
            try:
                ensure_series_art(series, series_folder=series_folder)
            except Exception:
                pass

            if not series_folder:
                series.placeholder_folder = os.path.dirname(os.path.dirname(target_path))
                session.add(series)

            prior = _active_series_stub(session, int(series.id))
            relocated_from = str(getattr(prior, "path", "") or "").strip() if prior else ""
            if relocated_from and _norm_stub_path(relocated_from) == _norm_stub_path(target_path):
                relocated_from = ""

            row = _mark_series_stub_active(
                session,
                series=series,
                path=target_path,
                season_id=season_id,
                activity_reason=activity_reason,
            )
            try:
                from services.series_episode_stats_hooks import refresh_series_stats_after_bulk

                refresh_series_stats_after_bulk(session, series_ids={int(series.id)})
            except Exception:
                pass
            if owns_session:
                session.commit()
            return {
                "ok": True,
                "action": "created_or_exists",
                "created": created,
                "nfo_written": nfo_written,
                "series_nfo_written": series_nfo_written,
                "path": target_path,
                "relocated_from": relocated_from or None,
                "series_id": int(series.id),
                "placeholder_id": int(row.id) if getattr(row, "id", None) else None,
            }

        paths = _mark_series_stub_deleted(
            session,
            series_id=int(series.id),
            activity_reason=activity_reason or "series_placeholder_retired",
        )
        if paths:
            cleanup_episode_placeholder_files(
                session,
                season=season01,
                series=series,
                candidate_paths=paths,
            )
        try:
            from services.series_episode_stats_hooks import refresh_series_stats_after_bulk

            refresh_series_stats_after_bulk(session, series_ids={int(series.id)})
        except Exception:
            pass
        if owns_session:
            session.commit()
        return {
            "ok": True,
            "action": "retired" if paths else "noop",
            "deleted": bool(paths),
            "series_id": int(series.id),
            "paths": paths,
        }
    except Exception as exc:
        if owns_session:
            session.rollback()
        logger.error(
            f"Series density materialization failed for series_id={series_id}: {exc}",
            extra={"emoji_type": "error"},
        )
        return {"ok": False, "reason": str(exc), "series_id": series_id}
    finally:
        if owns_session:
            session.close()


def run_series_density_for_series_ids(
    session,
    series_ids: list[int] | None,
    *,
    activity_reason: str | None = None,
) -> dict[str, Any]:
    ids = sorted({int(sid) for sid in (series_ids or []) if sid is not None})
    stats: dict[str, Any] = {
        "series_considered": len(ids),
        "created": 0,
        "retired": 0,
        "skipped": 0,
        "errors": 0,
        "created_paths": [],
        "deleted_paths": [],
    }
    if not ids:
        return stats

    density = tv_placeholder_density()
    if density not in ("series", "episode", "season"):
        return stats

    for sid in ids:
        out = apply_series_density_materialization(
            sid,
            session=session,
            activity_reason=activity_reason,
        )
        if not out.get("ok"):
            stats["errors"] += 1
            continue
        action = str(out.get("action") or "")
        path = str(out.get("path") or "").strip()
        relocated_from = str(out.get("relocated_from") or "").strip()
        deleted_paths = [str(p).strip() for p in (out.get("paths") or []) if str(p).strip()]
        if action == "created_or_exists":
            if out.get("created"):
                stats["created"] += 1
            else:
                stats["skipped"] += 1
            if path:
                stats["created_paths"].append(path)
            if relocated_from:
                stats["deleted_paths"].append(relocated_from)
        elif action == "retired" or (action == "skipped_density" and out.get("deleted")):
            stats["retired"] += 1
            stats["deleted_paths"].extend(deleted_paths)
        else:
            stats["skipped"] += 1
            if deleted_paths:
                stats["deleted_paths"].extend(deleted_paths)
    return stats


def series_ids_for_episode_ids(session, episode_ids: list[int]) -> list[int]:
    ids = [int(eid) for eid in (episode_ids or []) if eid is not None]
    if not ids:
        return []
    rows = (
        session.query(Season.series_id)
        .join(Episode, Episode.season_id == Season.id)
        .filter(Episode.id.in_(ids))
        .distinct()
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def all_active_series_ids(session) -> list[int]:
    rows = (
        session.query(Series.id)
        .filter(Series.is_deleted == False)  # noqa: E712
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def series_ids_with_density_stubs(session) -> list[int]:
    rows = (
        session.query(Placeholder.series_id)
        .filter(
            Placeholder.placeholder_kind == PLACEHOLDER_KIND_SERIES_STUB,
            Placeholder.series_id.isnot(None),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .distinct()
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]
