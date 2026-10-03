"""Arr TV season-density stubs: one playable file per season that still needs content."""

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
    season_density_stub_path,
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
from services.source_of_truth.placeholder_policy import policy_from_entity
from services.source_of_truth.tv_density import (
    DENSITY_PLACEHOLDER_TITLE,
    PLACEHOLDER_KIND_SEASON_STUB,
    density_uses_season_stub,
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


def _season_policy(season: Season) -> str:
    return policy_from_entity(season)


def _active_season_stub(session, season_id: int) -> Placeholder | None:
    return (
        session.query(Placeholder)
        .filter(
            Placeholder.season_id == int(season_id),
            Placeholder.placeholder_kind == PLACEHOLDER_KIND_SEASON_STUB,
            Placeholder.episode_id.is_(None),
            Placeholder.movie_id.is_(None),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .order_by(Placeholder.id.desc())
        .first()
    )


def _season_stub_rows(session, *, season_id: int | None = None, series_id: int | None = None) -> list[Placeholder]:
    q = session.query(Placeholder).filter(
        Placeholder.placeholder_kind == PLACEHOLDER_KIND_SEASON_STUB,
        Placeholder.episode_id.is_(None),
        Placeholder.movie_id.is_(None),
    )
    if season_id is not None:
        q = q.filter(Placeholder.season_id == int(season_id))
    if series_id is not None:
        q = q.filter(Placeholder.series_id == int(series_id))
    return q.all()


def season_stub_needed(
    session,
    series: Series,
    season: Season,
    *,
    now_date: date | None = None,
) -> bool:
    """Whether this season should have a density stub under retire-when + policy."""
    if bool(getattr(series, "is_deleted", False)) or bool(getattr(season, "is_deleted", False)):
        return False
    if not density_uses_season_stub(series):
        return False

    sn = int(getattr(season, "season_number", 0) or 0)
    if sn == 0 and not _include_specials():
        return False

    series_pol = _series_policy(series)
    if series_pol == "never":
        return False
    season_pol = _season_policy(season)
    if season_pol == "never":
        return False
    if series_pol == "pinned" or season_pol == "pinned":
        # Force stub if the season has any non-deleted episodes.
        count = (
            session.query(Episode.id)
            .filter(Episode.season_id == int(season.id), Episode.is_deleted == False)  # noqa: E712
            .count()
        )
        return bool(count)

    retire = tv_density_retire_when_for_series(series)
    placeholders_enabled = _placeholders_enabled()
    lookahead_days = _lookahead_days()
    today = now_date or date.today()

    episodes = (
        session.query(Episode)
        .filter(Episode.season_id == int(season.id), Episode.is_deleted == False)  # noqa: E712
        .all()
    )
    if not episodes:
        return False

    if retire == "when_any_episode_has_file":
        for ep in episodes:
            if bool(getattr(ep, "has_file", False)):
                return False

    for ep in episodes:
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
            series_policy=series_pol,
        ):
            return True
    return False


def _stub_nfo_proxies(series: Series, season: Season) -> tuple[Any, Any]:
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


def _mark_season_stub_active(
    session,
    *,
    series: Series,
    season: Season,
    path: str,
    activity_reason: str | None = None,
) -> Placeholder:
    row = _active_season_stub(session, int(season.id))
    if not row:
        candidate = (
            session.query(Placeholder)
            .filter(Placeholder.path == path)
            .order_by(Placeholder.id.desc())
            .first()
        )
        if candidate and candidate.movie_id is None and candidate.episode_id is None:
            if candidate.season_id is None or int(candidate.season_id) == int(season.id):
                row = candidate
        if not row:
            for existing in _season_stub_rows(session, season_id=int(season.id)):
                row = existing
                break
        if not row:
            row = Placeholder(
                series_id=int(series.id),
                season_id=int(season.id),
                episode_id=None,
                movie_id=None,
                path=path,
                placeholder_kind=PLACEHOLDER_KIND_SEASON_STUB,
                created_by="source_of_truth.season_density",
            )
            session.add(row)

    old_path = str(getattr(row, "path", "") or "").strip()
    new_path = str(path or "").strip()
    if old_path and new_path and _norm_stub_path(old_path) != _norm_stub_path(new_path):
        # Destination / Library Root moves must not leave a playable stub behind.
        cleanup_episode_placeholder_files(
            session,
            season=season,
            series=series,
            candidate_paths=[old_path],
        )

    row.movie_id = None
    row.episode_id = None
    row.series_id = int(series.id)
    row.season_id = int(season.id)
    row.placeholder_kind = PLACEHOLDER_KIND_SEASON_STUB
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


def _mark_season_stubs_deleted(
    session,
    *,
    season_id: int | None = None,
    series_id: int | None = None,
    activity_reason: str | None = None,
) -> list[str]:
    paths: list[str] = []
    for row in _season_stub_rows(session, season_id=season_id, series_id=series_id):
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
        raise RuntimeError(f"Dummy video missing or invalid for season density stub: {path!r}")
    return path


def apply_season_density_materialization(
    season_id: int,
    session=None,
    activity_reason: str | None = None,
) -> dict[str, Any]:
    owns_session = session is None
    session = session or get_session()
    try:
        season = session.query(Season).filter(Season.id == int(season_id)).first()
        if not season:
            return {"ok": False, "reason": "season_not_found", "season_id": season_id}
        series = session.query(Series).filter(Series.id == int(season.series_id)).first()
        if not series:
            return {"ok": False, "reason": "series_not_found", "season_id": season_id}

        if not density_uses_season_stub(series):
            paths = _mark_season_stubs_deleted(
                session,
                season_id=int(season.id),
                activity_reason=activity_reason or "density_not_season",
            )
            if paths:
                cleanup_episode_placeholder_files(
                    session,
                    season=season,
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
                "season_id": int(season.id),
                "series_id": int(series.id),
                "deleted": bool(paths),
                "paths": paths,
            }

        needed = season_stub_needed(session, series, season)
        if needed:
            target_path = season_density_stub_path(series, season)
            created = ensure_placeholder_file(target_path, dummy_file_path=_dummy_path())
            ep_proxy, season_proxy = _stub_nfo_proxies(series, season)
            nfo_written = False
            try:
                nfo_written = ensure_episode_nfo(target_path, ep_proxy, season_proxy, series)
            except Exception as exc:
                logger.debug(
                    f"Season density stub NFO skipped for season_id={season.id}: {exc}",
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
            if not getattr(season, "placeholder_folder", None):
                season.placeholder_folder = os.path.dirname(target_path)
                session.add(season)

            prior = _active_season_stub(session, int(season.id))
            relocated_from = str(getattr(prior, "path", "") or "").strip() if prior else ""
            if relocated_from and _norm_stub_path(relocated_from) == _norm_stub_path(target_path):
                relocated_from = ""

            row = _mark_season_stub_active(
                session,
                series=series,
                season=season,
                path=target_path,
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
                "season_id": int(season.id),
                "series_id": int(series.id),
                "placeholder_id": int(row.id) if getattr(row, "id", None) else None,
            }

        paths = _mark_season_stubs_deleted(
            session,
            season_id=int(season.id),
            activity_reason=activity_reason or "season_placeholder_retired",
        )
        if paths:
            cleanup_episode_placeholder_files(
                session,
                season=season,
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
            "season_id": int(season.id),
            "series_id": int(series.id),
            "paths": paths,
        }
    except Exception as exc:
        if owns_session:
            session.rollback()
        logger.error(
            f"Season density materialization failed for season_id={season_id}: {exc}",
            extra={"emoji_type": "error"},
        )
        return {"ok": False, "reason": str(exc), "season_id": season_id}
    finally:
        if owns_session:
            session.close()


def apply_season_density_for_series(
    series_id: int,
    session=None,
    activity_reason: str | None = None,
) -> dict[str, Any]:
    """Create/retire season stubs for every season under a series (or clear when density≠season)."""
    owns_session = session is None
    session = session or get_session()
    stats: dict[str, Any] = {
        "seasons_considered": 0,
        "created": 0,
        "retired": 0,
        "skipped": 0,
        "errors": 0,
        "created_paths": [],
        "deleted_paths": [],
    }
    try:
        series = session.query(Series).filter(Series.id == int(series_id)).first()
        if not series:
            return {**stats, "ok": False, "reason": "series_not_found"}

        if not density_uses_season_stub(series):
            paths = _mark_season_stubs_deleted(
                session,
                series_id=int(series.id),
                activity_reason=activity_reason or "density_not_season",
            )
            if paths:
                cleanup_episode_placeholder_files(
                    session,
                    season=None,
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
            stats["retired"] = 1 if paths else 0
            stats["deleted_paths"] = [str(p).strip() for p in paths if str(p).strip()]
            return {**stats, "ok": True, "action": "cleared_series_season_stubs", "deleted": bool(paths)}

        seasons = (
            session.query(Season)
            .filter(Season.series_id == int(series.id), Season.is_deleted == False)  # noqa: E712
            .order_by(Season.season_number.asc())
            .all()
        )
        for season in seasons:
            stats["seasons_considered"] += 1
            out = apply_season_density_materialization(
                int(season.id),
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
        if owns_session:
            session.commit()
        return {**stats, "ok": True}
    except Exception as exc:
        if owns_session:
            session.rollback()
        logger.error(
            f"Season density for series_id={series_id} failed: {exc}",
            extra={"emoji_type": "error"},
        )
        return {**stats, "ok": False, "reason": str(exc)}
    finally:
        if owns_session:
            session.close()


def run_season_density_for_series_ids(
    session,
    series_ids: list[int] | None,
    *,
    activity_reason: str | None = None,
) -> dict[str, Any]:
    ids = sorted({int(sid) for sid in (series_ids or []) if sid is not None})
    stats: dict[str, Any] = {
        "series_considered": len(ids),
        "seasons_considered": 0,
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
        out = apply_season_density_for_series(
            sid,
            session=session,
            activity_reason=activity_reason,
        )
        if not out.get("ok"):
            stats["errors"] += 1
            continue
        stats["seasons_considered"] += int(out.get("seasons_considered") or 0)
        stats["created"] += int(out.get("created") or 0)
        stats["retired"] += int(out.get("retired") or 0)
        stats["skipped"] += int(out.get("skipped") or 0)
        stats["errors"] += int(out.get("errors") or 0)
        stats["created_paths"].extend(
            [str(p).strip() for p in (out.get("created_paths") or []) if str(p).strip()]
        )
        stats["deleted_paths"].extend(
            [str(p).strip() for p in (out.get("deleted_paths") or []) if str(p).strip()]
        )
    return stats


def series_ids_with_season_density_stubs(session) -> list[int]:
    rows = (
        session.query(Placeholder.series_id)
        .filter(
            Placeholder.placeholder_kind == PLACEHOLDER_KIND_SEASON_STUB,
            Placeholder.series_id.isnot(None),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .distinct()
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]
