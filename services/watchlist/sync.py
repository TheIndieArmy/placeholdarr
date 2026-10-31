"""Poll Plex owner watchlist, upsert rows, optional auto Arr add."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from core.config import settings
from core.logger import logger
from services.media_servers.plex_watchlist import fetch_owner_watchlist, plex_watchlist_token
from services.postgres.db import get_session
from services.postgres.models import Movie, PlexWatchlistItem, Series
from services.task_run_history import begin_task_run, finish_task_run


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def identity_key_for(item: dict[str, Any]) -> str:
    guid = str(item.get("plex_guid") or "").strip()
    if guid:
        return guid[:640]
    rk = str(item.get("rating_key") or "").strip()
    if rk:
        return f"rating_key:{rk}"[:640]
    title = str(item.get("title") or "").strip().lower()
    year = item.get("year")
    media = str(item.get("media_type") or "movie")
    return f"titlekey:{media}:{title}:{year or ''}"[:640]


def tv_scope_to_sonarr_monitor(scope: str | None) -> str:
    s = str(scope or "").strip().lower()
    if s == "season":
        return "firstSeason"
    if s == "episode":
        return "firstSeason"
    return "all"


def default_tv_request_scope() -> str:
    configured = str(getattr(settings, "WATCHLIST_TV_REQUEST_SCOPE", "") or "").strip().lower()
    if configured in {"episode", "season", "series"}:
        return configured
    density = str(getattr(settings, "TV_PLACEHOLDER_DENSITY", "episode") or "episode").strip().lower()
    if density in {"episode", "season", "series"}:
        return density
    return "episode"


def watchlist_action_mode() -> str:
    mode = str(getattr(settings, "WATCHLIST_ON_NEW", "manual") or "manual").strip().lower()
    if mode in {"manual", "monitor_only", "add_only", "add_and_search"}:
        return mode
    return "manual"


def _in_arr_library(session, *, media_type: str, tmdb_id: int | None, tvdb_id: int | None, imdb_id: str | None) -> bool:
    if media_type == "movie":
        if tmdb_id and session.query(Movie.id).filter(Movie.tmdbid == int(tmdb_id)).first():
            return True
        if imdb_id and session.query(Movie.id).filter(Movie.imdbid == str(imdb_id)).first():
            return True
        return False
    if tvdb_id and session.query(Series.id).filter(Series.tvdbid == int(tvdb_id)).first():
        return True
    if tmdb_id and session.query(Series.id).filter(Series.sonarr_tmdbid == int(tmdb_id)).first():
        return True
    if imdb_id and session.query(Series.id).filter(Series.imdbid == str(imdb_id)).first():
        return True
    return False


def _resolve_status_for_row(session, row: PlexWatchlistItem) -> str:
    if row.excluded:
        return "excluded"
    if row.status == "added":
        return "added"
    if row.status == "failed" and row.last_error:
        # Keep failed until next successful add or clear
        pass
    if _in_arr_library(
        session,
        media_type=str(row.media_type),
        tmdb_id=row.tmdb_id,
        tvdb_id=row.tvdb_id,
        imdb_id=row.imdb_id,
    ):
        return "in_arr" if row.status != "added" else "added"
    if row.status == "failed":
        return "failed"
    return "new"


def upsert_watchlist_from_plex(remote: list[dict[str, Any]]) -> dict[str, Any]:
    now = _utcnow()
    seen_keys: set[str] = set()
    created = updated = 0
    session = get_session()
    try:
        existing = {r.identity_key: r for r in session.query(PlexWatchlistItem).all()}
        for item in remote:
            key = identity_key_for(item)
            seen_keys.add(key)
            row = existing.get(key)
            if row is None:
                row = PlexWatchlistItem(
                    identity_key=key,
                    plex_guid=item.get("plex_guid"),
                    rating_key=item.get("rating_key"),
                    title=str(item.get("title") or ""),
                    year=item.get("year"),
                    media_type=str(item.get("media_type") or "movie"),
                    tmdb_id=item.get("tmdb_id"),
                    tvdb_id=item.get("tvdb_id"),
                    imdb_id=item.get("imdb_id"),
                    status="new",
                    excluded=False,
                    first_seen_at=now,
                    last_seen_at=now,
                    updated_at=now,
                    extra={"thumb": item.get("thumb")},
                )
                session.add(row)
                existing[key] = row
                created += 1
            else:
                row.title = str(item.get("title") or row.title)
                row.year = item.get("year") if item.get("year") is not None else row.year
                row.media_type = str(item.get("media_type") or row.media_type)
                row.plex_guid = item.get("plex_guid") or row.plex_guid
                row.rating_key = item.get("rating_key") or row.rating_key
                row.tmdb_id = item.get("tmdb_id") or row.tmdb_id
                row.tvdb_id = item.get("tvdb_id") or row.tvdb_id
                row.imdb_id = item.get("imdb_id") or row.imdb_id
                row.last_seen_at = now
                row.updated_at = now
                updated += 1
            row.status = _resolve_status_for_row(session, row)

        # Items no longer on the remote list: leave rows (history), but do not delete in v1.
        session.commit()
    finally:
        session.close()
    return {"fetched": len(remote), "created": created, "updated": updated, "seen": len(seen_keys)}


def normalize_watchlist_target(raw: Any) -> dict[str, Any] | None:
    """Normalize one target dict; returns None when instance_key is missing."""
    if not isinstance(raw, dict):
        return None
    key = str(raw.get("instance_key") or "").strip()
    if not key:
        return None
    qp_raw = raw.get("quality_profile_id")
    try:
        qp_i = int(qp_raw) if qp_raw not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        qp_i = None
    root = str(raw.get("root_folder_path") or raw.get("root_folder") or "").strip() or None
    return {"instance_key": key, "quality_profile_id": qp_i, "root_folder_path": root}


def parse_watchlist_targets_json(raw: Any) -> list[dict[str, Any]]:
    """Parse a JSON list (or already-decoded list) of Arr targets."""
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            import json

            data = json.loads(text)
        except Exception:
            return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        target = normalize_watchlist_target(item)
        if not target:
            continue
        key = str(target["instance_key"]).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(target)
    return out


def _legacy_single_target(media_type: str) -> dict[str, Any] | None:
    if media_type == "movie":
        key = str(getattr(settings, "WATCHLIST_RADARR_INSTANCE_KEY", "") or "").strip()
        qp = getattr(settings, "WATCHLIST_RADARR_QUALITY_PROFILE_ID", None)
        root = str(getattr(settings, "WATCHLIST_RADARR_ROOT_FOLDER", "") or "").strip()
    else:
        key = str(getattr(settings, "WATCHLIST_SONARR_INSTANCE_KEY", "") or "").strip()
        qp = getattr(settings, "WATCHLIST_SONARR_QUALITY_PROFILE_ID", None)
        root = str(getattr(settings, "WATCHLIST_SONARR_ROOT_FOLDER", "") or "").strip()
    if not key:
        return None
    return normalize_watchlist_target(
        {"instance_key": key, "quality_profile_id": qp, "root_folder_path": root}
    )


def arr_targets_for_media(media_type: str) -> list[dict[str, Any]]:
    """Enabled Arr targets for watchlist fan-out (movies → Radarr, shows → Sonarr)."""
    json_key = (
        "WATCHLIST_RADARR_TARGETS_JSON" if media_type == "movie" else "WATCHLIST_SONARR_TARGETS_JSON"
    )
    targets = parse_watchlist_targets_json(getattr(settings, json_key, None))
    if targets:
        return targets
    legacy = _legacy_single_target(media_type)
    return [legacy] if legacy else []


def _ensure_profile_and_root(instance: dict[str, Any], defaults: dict[str, Any]) -> tuple[int, str]:
    """Require an explicit quality profile and root folder (no first-item fallback)."""
    _ = instance  # kept for call-site compatibility / future URL checks
    qp = defaults.get("quality_profile_id")
    root = defaults.get("root_folder_path")
    try:
        qp_i = int(qp) if qp not in (None, "", 0, "0") else 0
    except (TypeError, ValueError):
        qp_i = 0
    root_s = str(root or "").strip()
    key = str(defaults.get("instance_key") or instance.get("instance_key") or "Arr").strip()
    if qp_i <= 0:
        raise RuntimeError(f"Quality profile is required for {key}")
    if not root_s:
        raise RuntimeError(f"Root folder is required for {key}")
    return qp_i, root_s


def add_watchlist_items_to_arr(
    item_ids: Iterable[int],
    *,
    search: bool,
    monitored: bool = True,
    tags: list[str] | None = None,
    tv_scope: str | None = None,
    targets: list[dict[str, Any]] | None = None,
    instance_key: str | None = None,
    quality_profile_id: int | None = None,
    root_folder_path: str | None = None,
) -> dict[str, Any]:
    """Add selected watchlist rows to Arr (fan-out to every enabled target). Returns summary counts."""
    from types import SimpleNamespace

    from services.source_of_truth import arr_api

    ids = [int(i) for i in item_ids]
    tag_labels = []
    for raw in tags or []:
        label = arr_api.normalize_arr_tag_label(str(raw))
        if label:
            tag_labels.append(label)
    if not tag_labels:
        default_tag = str(getattr(settings, "WATCHLIST_ARR_TAGS", "") or "placeholdarr-watchlist")
        for part in default_tag.replace(",", " ").split():
            label = arr_api.normalize_arr_tag_label(part)
            if label:
                tag_labels.append(label)

    scope = (tv_scope or default_tv_request_scope()).strip().lower()
    sonarr_monitor = tv_scope_to_sonarr_monitor(scope)
    added = skipped = failed = 0
    session = get_session()
    try:
        rows = session.query(PlexWatchlistItem).filter(PlexWatchlistItem.id.in_(ids)).all()
        by_media: dict[str, list[PlexWatchlistItem]] = {"movie": [], "show": []}
        for row in rows:
            if row.excluded:
                continue
            by_media.setdefault(str(row.media_type), []).append(row)

        override_targets = parse_watchlist_targets_json(targets) if targets is not None else None
        # Cheap shim: single instance_key from older clients → one-target list.
        if override_targets is None and (instance_key or "").strip():
            shim = normalize_watchlist_target(
                {
                    "instance_key": instance_key,
                    "quality_profile_id": quality_profile_id,
                    "root_folder_path": root_folder_path,
                }
            )
            override_targets = [shim] if shim else []

        for media_type, media_rows in by_media.items():
            if not media_rows:
                continue
            arr_type = "radarr" if media_type == "movie" else "sonarr"
            if override_targets is not None:
                media_targets = [
                    t
                    for t in override_targets
                    if settings.resolve_arr_instance(arr_type, instance_key=str(t.get("instance_key") or ""))
                ]
            else:
                media_targets = arr_targets_for_media(media_type)

            if not media_targets:
                for row in media_rows:
                    row.status = "failed"
                    row.last_error = (
                        f"No {arr_type} watchlist targets configured. "
                        "Enable at least one instance under Settings → Watching → Arr targets."
                    )
                    failed += 1
                continue

            # Per-row accumulators across fan-out targets.
            row_ok: dict[int, bool] = {int(r.id): False for r in media_rows}
            row_had_new: dict[int, bool] = {int(r.id): False for r in media_rows}
            row_keys: dict[int, list[str]] = {int(r.id): [] for r in media_rows}
            row_errors: dict[int, list[str]] = {int(r.id): [] for r in media_rows}

            items = [
                SimpleNamespace(
                    title=r.title,
                    year=r.year,
                    tmdb_id=r.tmdb_id,
                    tvdb_id=r.tvdb_id,
                    imdb_id=r.imdb_id,
                )
                for r in media_rows
            ]

            for target in media_targets:
                key = str(target.get("instance_key") or "").strip()
                instance = settings.resolve_arr_instance(arr_type, instance_key=key)
                if not instance:
                    for row in media_rows:
                        row_errors[int(row.id)].append(f"Unknown Arr instance {key!r}")
                    continue
                try:
                    qp, root = _ensure_profile_and_root(instance, target)
                except Exception as exc:
                    for row in media_rows:
                        row_errors[int(row.id)].append(str(exc))
                    continue

                tag_ids: list[int] = []
                url = str(instance.get("url") or "")
                api_key = str(instance.get("api_key") or "")
                for label in tag_labels:
                    try:
                        tag_id = arr_api.ensure_arr_tag(url=url, api_key=api_key, label=label)
                        if tag_id is not None and tag_id not in tag_ids:
                            tag_ids.append(int(tag_id))
                    except Exception as exc:
                        logger.warning(
                            f"Watchlist Arr tag {label!r} failed on {key}: {exc}",
                            extra={"emoji_type": "warning"},
                        )

                results = arr_api.add_missing_titles(
                    media_type=media_type,
                    url=url,
                    api_key=api_key,
                    items=items,
                    quality_profile_id=qp,
                    root_folder_path=root,
                    monitored=monitored,
                    search=bool(search),
                    tag_ids=tag_ids or None,
                    instance_key=key,
                    sonarr_monitor=sonarr_monitor if media_type == "show" else None,
                )
                for row, result in zip(media_rows, results):
                    rid = int(row.id)
                    status = str(result.get("status") or "")
                    if status in {"ok", "skipped"}:
                        row_ok[rid] = True
                        if key not in row_keys[rid]:
                            row_keys[rid].append(key)
                        if status == "ok":
                            row_had_new[rid] = True
                    else:
                        row_errors[rid].append(
                            f"{key}: {result.get('error') or 'Arr add failed'}"
                        )

            now = _utcnow()
            for row in media_rows:
                rid = int(row.id)
                if row_ok[rid]:
                    row.status = "added"
                    row.added_at = now
                    row.arr_instance_key = ",".join(row_keys[rid]) or None
                    err_parts = row_errors[rid]
                    row.last_error = "; ".join(err_parts) if err_parts else None
                    if row_had_new[rid]:
                        added += 1
                    else:
                        skipped += 1
                else:
                    row.status = "failed"
                    row.last_error = "; ".join(row_errors[rid]) or "Arr add failed"
                    failed += 1
        session.commit()
    finally:
        session.close()
    return {"added": added, "skipped": skipped, "failed": failed}


def auto_add_new_items() -> dict[str, Any]:
    mode = watchlist_action_mode()
    if mode == "manual":
        return {"mode": mode, "attempted": 0}
    search = mode == "add_and_search"
    monitored = mode != "add_only"
    session = get_session()
    try:
        ids = [
            int(r.id)
            for r in session.query(PlexWatchlistItem.id)
            .filter(
                PlexWatchlistItem.excluded.is_(False),
                PlexWatchlistItem.status == "new",
            )
            .all()
        ]
    finally:
        session.close()
    if not ids:
        return {"mode": mode, "attempted": 0}
    summary = add_watchlist_items_to_arr(ids, search=search, monitored=monitored)
    summary["mode"] = mode
    summary["attempted"] = len(ids)
    return summary


def set_excluded(item_ids: Iterable[int], excluded: bool) -> int:
    ids = [int(i) for i in item_ids]
    session = get_session()
    try:
        rows = session.query(PlexWatchlistItem).filter(PlexWatchlistItem.id.in_(ids)).all()
        for row in rows:
            row.excluded = bool(excluded)
            row.status = "excluded" if excluded else _resolve_status_for_row(session, row)
            row.updated_at = _utcnow()
            if excluded:
                row.last_error = None
        session.commit()
        return len(rows)
    finally:
        session.close()


def list_watchlist_items(*, status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    session = get_session()
    try:
        q = session.query(PlexWatchlistItem).order_by(PlexWatchlistItem.last_seen_at.desc())
        if status and status != "all":
            if status == "excluded":
                q = q.filter(PlexWatchlistItem.excluded.is_(True))
            else:
                q = q.filter(PlexWatchlistItem.status == status, PlexWatchlistItem.excluded.is_(False))
        rows = q.limit(max(1, min(int(limit or 500), 2000))).all()
        return [_serialize_item(r) for r in rows]
    finally:
        session.close()


def watchlist_summary() -> dict[str, Any]:
    session = get_session()
    try:
        rows = session.query(PlexWatchlistItem).all()
    finally:
        session.close()
    counts = {"new": 0, "in_arr": 0, "added": 0, "excluded": 0, "failed": 0, "total": len(rows)}
    for r in rows:
        if r.excluded or r.status == "excluded":
            counts["excluded"] += 1
        elif r.status in counts:
            counts[r.status] += 1
    enabled = bool(getattr(settings, "ENABLE_PLEX_WATCHLIST_SYNC", False))
    last = getattr(settings, "WATCHLIST_LAST_POLL_AT", None)
    last_error = getattr(settings, "WATCHLIST_LAST_ERROR", None)
    return {
        "enabled": enabled,
        "has_token": bool(plex_watchlist_token()),
        "on_new": watchlist_action_mode(),
        "tv_request_scope": default_tv_request_scope(),
        "arr_tags": str(getattr(settings, "WATCHLIST_ARR_TAGS", "") or "placeholdarr-watchlist"),
        "radarr_targets": arr_targets_for_media("movie"),
        "sonarr_targets": arr_targets_for_media("show"),
        "last_poll_at": last,
        "last_error": last_error or None,
        "counts": counts,
    }


def _serialize_item(r: PlexWatchlistItem) -> dict[str, Any]:
    return {
        "id": r.id,
        "identity_key": r.identity_key,
        "plex_guid": r.plex_guid,
        "rating_key": r.rating_key,
        "title": r.title,
        "year": r.year,
        "media_type": r.media_type,
        "tmdb_id": r.tmdb_id,
        "tvdb_id": r.tvdb_id,
        "imdb_id": r.imdb_id,
        "status": "excluded" if r.excluded else r.status,
        "excluded": bool(r.excluded),
        "last_error": r.last_error,
        "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
        "added_at": r.added_at.isoformat() if r.added_at else None,
        "arr_instance_key": r.arr_instance_key,
    }


def _persist_poll_meta(*, at: str | None, error: str | None) -> None:
    """Best-effort write of last poll metadata into AppConfig + runtime settings."""
    try:
        from services.postgres.models import AppConfig

        session = get_session()
        try:
            for key, value in (
                ("WATCHLIST_LAST_POLL_AT", at or ""),
                ("WATCHLIST_LAST_ERROR", error or ""),
            ):
                if key == "WATCHLIST_LAST_POLL_AT" and at is None:
                    continue
                row = session.query(AppConfig).filter(AppConfig.key == key).first()
                if row is None:
                    session.add(AppConfig(key=key, value=value))
                else:
                    row.value = value
            session.commit()
        finally:
            session.close()
        if at is not None:
            try:
                setattr(settings, "WATCHLIST_LAST_POLL_AT", at)
            except Exception:
                pass
        try:
            setattr(settings, "WATCHLIST_LAST_ERROR", error or "")
        except Exception:
            pass
    except Exception as exc:
        logger.debug(f"watchlist poll meta persist skipped: {exc}", extra={"emoji_type": "debug"})


def run_watchlist_sync(*, trigger: str = "manual") -> dict[str, Any]:
    """Full poll (+ optional auto-add). Records a scheduled task run when possible."""
    task_run_id = None
    try:
        task_run_id = begin_task_run(task_key="watchlist_sync", trigger=trigger)
    except Exception as exc:
        logger.debug(f"watchlist begin_task_run skipped: {exc}", extra={"emoji_type": "debug"})

    summary: dict[str, Any] = {"ok": False, "trigger": trigger}
    try:
        if not bool(getattr(settings, "ENABLE_PLEX_WATCHLIST_SYNC", False)) and trigger == "scheduled":
            summary["skipped"] = "disabled"
            if task_run_id:
                finish_task_run(task_run_id, status="SKIPPED", summary=summary, skip_reason="disabled")
            return summary
        if not plex_watchlist_token():
            raise RuntimeError("No Plex token for watchlist (configure PLEX_TOKEN or sign in with Plex)")

        remote = fetch_owner_watchlist(limit=1000)
        upsert = upsert_watchlist_from_plex(remote)
        auto = auto_add_new_items()
        summary.update({"ok": True, "upsert": upsert, "auto_add": auto})
        _persist_poll_meta(at=_utcnow().isoformat(), error=None)
        if task_run_id:
            finish_task_run(task_run_id, status="DONE", summary=summary)
        return summary
    except Exception as exc:
        logger.error(f"Watchlist sync failed: {exc}", extra={"emoji_type": "error"})
        summary["error"] = str(exc)
        _persist_poll_meta(at=_utcnow().isoformat(), error=str(exc))
        if task_run_id:
            finish_task_run(task_run_id, status="FAILED", summary=summary, error_message=str(exc))
        return summary
