from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.config import settings
from services.messages import render as render_message
from services.placeholders import episode_placeholder_path, movie_placeholder_path
from services.placeholder_activity_log import (
    append_placeholder_activity_status,
    materialization_stats_dict,
    outcome_reason_and_status_from_materialization,
)
from services.postgres.models import Episode, Job, Movie, Placeholder, Season, Series
from services.source_of_truth.arr_api import fetch_radarr_movie, fetch_sonarr_episode_item
from services.source_of_truth.arr_share_guard import expand_determination_entity_ids
from services.source_of_truth.determiner import run_determination_for_entities_in_session
from services.source_of_truth.live_catalog_touch import mark_live_catalog_touch
from services.source_of_truth.materializer import run_materialization_for_entities
from services.source_of_truth.status_intent import DisplayStatus, StatusIntent, StatusSource
from services.source_of_truth.status_orchestrator import StatusOrchestrator
from services.source_of_truth.status_reconciler import NFO_REFRESH_JOB_TYPE
from services.status_projection import projected_status_display

_PG_LOCK_NOT_AVAILABLE = "55P03"


def _is_lock_timeout_error(exc: Exception) -> bool:
    pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
    if str(pgcode or "") == _PG_LOCK_NOT_AVAILABLE:
        return True
    text = str(exc or "").lower()
    return "lock timeout" in text or "locknotavailable" in text.replace(" ", "")

IMPORT_GRACE_JOB_TYPE = "import_grace"
IMPORT_GRACE_REASON = "import_grace_countdown"
IMPORT_GRACE_CANCEL_ERROR = "Cancelled: file deleted during import grace"
IMPORT_GRACE_INTERRUPT_REASON = "import_grace_interrupted"
# Coalesced season/series density rematerialize after pack imports (#199).
DENSITY_IMPORT_FINALIZE_JOB_TYPE = "density_import_finalize"
_DENSITY_IMPORT_DEBOUNCE_SECONDS = 15.0
_DENSITY_IMPORT_MAX_DEBOUNCE_SECONDS = 120.0
# Number of countdown ticks before the final "less than a minute" tick.
# Historically: 5, 4, 3, 2, 1 minutes remaining, plus a "less than a minute" final.
COUNTDOWN_MINUTES_DECREASING = (5, 4, 3, 2, 1)


def _load_arr_has_file_probe(session, *, content_type: str, entity_id: int) -> dict[str, Any] | None:
    """Return Arr probe args for hasFile, or None when the check should keep grace alive."""
    ct = str(content_type or "").strip().lower()
    try:
        eid = int(entity_id)
    except (TypeError, ValueError):
        return None
    if ct not in {"movie", "episode"} or eid <= 0:
        return None

    if ct == "movie":
        movie = session.query(Movie).filter(Movie.id == eid).first()
        if not movie:
            return None
        radarr_id = getattr(movie, "radarrid", None)
        if not radarr_id:
            return None
        base_url, api_key = settings.resolve_arr_endpoint(
            "radarr",
            instance_key=str(getattr(movie, "instance_key", "") or "") or None,
            instance_id=str(getattr(movie, "instance_id", "") or "") or None,
        )
        if not base_url or not api_key:
            return None
        return {
            "kind": "movie",
            "arr_id": int(radarr_id),
            "base_url": base_url,
            "api_key": api_key,
        }

    episode = session.query(Episode).filter(Episode.id == eid).first()
    if not episode:
        return None
    sonarr_id = getattr(episode, "sonarrid", None)
    if not sonarr_id:
        return None
    season = session.query(Season).filter(Season.id == int(episode.season_id)).first()
    if not season:
        return None
    series = session.query(Series).filter(Series.id == int(season.series_id)).first()
    if not series:
        return None
    base_url, api_key = settings.resolve_arr_endpoint(
        "sonarr",
        instance_key=str(getattr(series, "instance_key", "") or "") or None,
        instance_id=str(getattr(series, "instance_id", "") or "") or None,
    )
    if not base_url or not api_key:
        return None
    return {
        "kind": "episode",
        "arr_id": int(sonarr_id),
        "base_url": base_url,
        "api_key": api_key,
    }


def _arr_has_file_from_probe(probe: dict[str, Any] | None) -> bool:
    """HTTP-only hasFile check. ``None`` probe means keep grace (cannot verify)."""
    if not probe:
        return True
    try:
        if probe.get("kind") == "movie":
            item = fetch_radarr_movie(
                int(probe["arr_id"]),
                probe["base_url"],
                probe["api_key"],
                bypass_cache=True,
            )
            if not isinstance(item, dict):
                return True
            return bool(item.get("hasFile") or (item.get("movieFile") or {}).get("path"))
        item = fetch_sonarr_episode_item(
            int(probe["arr_id"]),
            url=probe["base_url"],
            api_key=probe["api_key"],
            bypass_cache=True,
        )
        if not isinstance(item, dict):
            return True
        return bool(item.get("hasFile") or (item.get("episodeFile") or {}).get("path"))
    except Exception:
        return True


def arr_import_still_has_file(session, *, content_type: str, entity_id: int) -> bool:
    """True when Arr still reports hasFile, or the Arr check cannot be completed.

    Placeholdarr often cannot see Arr download/import paths (unmounted volumes), so
    grace validity uses Radarr/Sonarr hasFile rather than a local filesystem check.
    API failure or missing ids keep grace running (cannot verify).
    """
    try:
        probe = _load_arr_has_file_probe(session, content_type=content_type, entity_id=entity_id)
        return _arr_has_file_from_probe(probe)
    except Exception:
        return True


def restore_request_after_import_grace_interrupt(
    session,
    *,
    content_type: str,
    entity_id: int,
    placeholder_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Project REQUEST (with player metadata push) after grace is interrupted.

    Used when Arr no longer reports a file mid-grace. EVENT_DELETE_FILE source
    ensures post-NFO player title/summary projection runs (unlike INITIAL_CREATION).
    """
    ct = str(content_type or "").strip().lower()
    try:
        eid = int(entity_id)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "bad_entity_id", "applied": 0}

    ids = [int(pid) for pid in (placeholder_ids or []) if pid is not None]
    if not ids:
        q = session.query(Placeholder.id).filter(Placeholder.has_placeholder == True)  # noqa: E712
        if ct == "movie":
            q = q.filter(Placeholder.movie_id == eid)
        elif ct == "episode":
            q = q.filter(Placeholder.episode_id == eid)
        else:
            return {"ok": False, "reason": "bad_content_type", "applied": 0}
        ids = [int(pid) for (pid,) in q.all()]

    if not ids:
        return {"ok": True, "applied": 0, "placeholder_ids": []}

    intents = [
        StatusIntent(
            placeholder_id=pid,
            new_status=DisplayStatus.REQUEST.value,
            reason="Import grace interrupted: Arr no longer reports a file",
            source=StatusSource.EVENT_DELETE_FILE,
            trigger_nfo_refresh=True,
            metadata={
                "content_type": ct,
                "entity_id": eid,
                "reason": IMPORT_GRACE_INTERRUPT_REASON,
            },
        )
        for pid in ids
    ]
    orchestrator = StatusOrchestrator(session=session)
    applied = orchestrator.apply_and_project_statuses(intents)
    return {"ok": True, "applied": int(applied), "placeholder_ids": ids}


def cancel_import_grace_for_entity(
    session,
    *,
    content_type: str,
    entity_id: int,
) -> int:
    """Fail queued/in-flight import_grace jobs for one movie or episode row.

    File-delete handlers call this so countdown ticks and finalize cannot overwrite
    a mid-grace interrupt (placeholder kept, status restored to REQUEST).
    Returns how many jobs were cancelled.
    """
    ct = str(content_type or "").strip().lower()
    try:
        eid = int(entity_id)
    except (TypeError, ValueError):
        return 0
    if ct not in {"movie", "episode"} or eid <= 0:
        return 0

    rows = (
        session.query(Job)
        .filter(
            Job.job_type == IMPORT_GRACE_JOB_TYPE,
            Job.status.in_(("PENDING", "CLAIMED", "WORKING")),
        )
        .all()
    )
    cancelled = 0
    for job in rows:
        payload = job.payload if isinstance(job.payload, dict) else {}
        if str(payload.get("content_type") or "").strip().lower() != ct:
            continue
        try:
            if int(payload.get("entity_id")) != eid:
                continue
        except (TypeError, ValueError):
            continue
        job.status = "FAILED"
        job.error_message = IMPORT_GRACE_CANCEL_ERROR
        session.add(job)
        cancelled += 1
    return cancelled


def interrupt_import_grace_keep_placeholder(
    session,
    *,
    content_type: str,
    entity_id: int,
) -> dict[str, Any]:
    """Cancel active grace for an entity and restore REQUEST without rematerializing.

    Used when Arr reports file-delete while import grace is still running: the
    placeholder file/row was never removed, so clearing ``has_placeholder`` and
    rematerializing is unnecessary (and races the countdown UI).
    """
    cancelled = cancel_import_grace_for_entity(
        session,
        content_type=content_type,
        entity_id=entity_id,
    )
    if cancelled <= 0:
        return {"interrupted": False, "cancelled_jobs": 0, "restore": None}
    restore = restore_request_after_import_grace_interrupt(
        session,
        content_type=content_type,
        entity_id=entity_id,
    )
    return {
        "interrupted": True,
        "cancelled_jobs": int(cancelled),
        "restore": restore,
    }


def _countdown_status_text(minutes_remaining: int) -> str:
    """Render the customizable countdown status text for a given minutes-remaining tick."""
    return render_message("import_grace.countdown", {"MinutesRemaining": str(int(minutes_remaining))})


def _countdown_lt_1_min_status_text() -> str:
    return render_message("import_grace.countdown_lt_1_min", {})


def _all_countdown_status_texts(step_seconds: int = 60) -> list[str]:
    """Render all countdown status strings in display order.

    Accelerated cadence still uses the configured 5/4/3/2/1-minute templates so
    each tick is a distinct projected status. The step interval only changes
    how quickly those labels advance, not the label text.
    """
    del step_seconds  # Cadence is applied in build_import_grace_schedule, not labels.
    return [_countdown_status_text(m) for m in COUNTDOWN_MINUTES_DECREASING] + [_countdown_lt_1_min_status_text()]


# Backwards-compatible shim for any consumer still importing the static list.
# Generated lazily so it picks up template overrides at scheduling time.
def __getattr__(name: str):  # pragma: no cover - simple compatibility shim
    if name == "COUNTDOWN_STATUSES":
        return _all_countdown_status_texts()
    raise AttributeError(name)


def _import_grace_step_seconds() -> int:
    is_accelerated = bool(getattr(settings, "ENABLE_IMPORT_GRACE_ACCELERATED", False))
    if is_accelerated:
        step = max(1, int(getattr(settings, "IMPORT_GRACE_ACCELERATED_STEP_SECONDS", 10)))
        from core.logger import logger
        logger.info(f"Import grace using accelerated cadence: {step}s per step", extra={'emoji_type': 'info'})
        return step
    return max(1, int(getattr(settings, "IMPORT_GRACE_STEP_SECONDS", 60)))


def build_import_grace_schedule(base_time: datetime | None = None, step_seconds: int | None = None) -> list[dict[str, Any]]:
    now = base_time or datetime.now(timezone.utc)
    step = max(1, int(step_seconds or _import_grace_step_seconds()))

    countdown_texts = _all_countdown_status_texts(step)
    scheduled: list[dict[str, Any]] = []
    for step_index, status_text in enumerate(countdown_texts):
        scheduled.append(
            {
                "step_index": step_index,
                "run_after": now + timedelta(seconds=step_index * step),
                "status_text": status_text,
                "finalize": False,
            }
        )

    scheduled.append(
        {
            "step_index": len(countdown_texts),
            "run_after": now + timedelta(seconds=len(countdown_texts) * step),
            "status_text": None,
            "finalize": True,
        }
    )
    return scheduled




def _enqueue_nfo_refresh_job(session, placeholder_ids: list[int]) -> None:
    if not placeholder_ids:
        return
    from services.source_of_truth.job_priority import default_priority_for

    session.add(
        Job(
            job_type=NFO_REFRESH_JOB_TYPE,
            payload={"placeholder_ids": placeholder_ids},
            status="PENDING",
            run_after=datetime.now(timezone.utc),
            priority=default_priority_for(NFO_REFRESH_JOB_TYPE),
        )
    )


def _set_countdown_status(session, placeholder_ids: list[int], status_text: str) -> list[int]:
    if not placeholder_ids:
        return []

    rows = (
        session.query(Placeholder)
        .filter(
            Placeholder.id.in_(placeholder_ids),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .all()
    )

    active_ids: list[int] = []
    for row in rows:
        row.display_status = status_text
        row.display_reason = IMPORT_GRACE_REASON
        row.display_status_projected = projected_status_display(status_text, reason=IMPORT_GRACE_REASON)
        # Import grace is past Arr queue tracking; stop RefreshMonitoredDownloads polling.
        row.queue_monitor_active = False
        row.queue_monitor_active_set_at = None
        session.add(row)
        active_ids.append(int(row.id))
    return active_ids


def _schedule_import_grace(
    session,
    *,
    content_type: str,
    entity_id: int,
    placeholder_ids: list[int],
    file_path: str | None,
) -> dict[str, Any]:
    schedule = build_import_grace_schedule()

    initial_status = str(schedule[0]["status_text"])
    active_ids = _set_countdown_status(session, placeholder_ids, initial_status)
    _enqueue_nfo_refresh_job(session, active_ids)

    from services.source_of_truth.job_priority import default_priority_for

    grace_priority = default_priority_for(IMPORT_GRACE_JOB_TYPE)
    for item in schedule[1:]:
        session.add(
            Job(
                job_type=IMPORT_GRACE_JOB_TYPE,
                payload={
                    "content_type": content_type,
                    "entity_id": int(entity_id),
                    "placeholder_ids": placeholder_ids,
                    "file_path": file_path,
                    "step_index": int(item["step_index"]),
                    "status_text": item["status_text"],
                    "finalize": bool(item["finalize"]),
                },
                status="PENDING",
                run_after=item["run_after"],
                priority=grace_priority,
            )
        )

    return {
        "ok": True,
        "content_type": content_type,
        "entity_id": int(entity_id),
        "placeholder_count": len(placeholder_ids),
        "active_placeholder_count": len(active_ids),
        "step_seconds": _import_grace_step_seconds(),
        "scheduled_jobs": len(schedule) - 1,
        "initial_status": initial_status,
    }


def schedule_movie_import_grace(session, movie_row_id: int, file_path: str | None = None) -> dict[str, Any]:
    raw_ids = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.movie_id == int(movie_row_id),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .all()
    )
    placeholder_ids = [int(pid) for (pid,) in raw_ids]
    return _schedule_import_grace(
        session,
        content_type="movie",
        entity_id=int(movie_row_id),
        placeholder_ids=placeholder_ids,
        file_path=file_path,
    )


def _density_import_max_debounce_seconds() -> float:
    try:
        return max(
            float(_DENSITY_IMPORT_DEBOUNCE_SECONDS),
            float(
                getattr(
                    settings,
                    "DENSITY_IMPORT_COALESCE_MAX_DEBOUNCE_SECONDS",
                    _DENSITY_IMPORT_MAX_DEBOUNCE_SECONDS,
                )
                or _DENSITY_IMPORT_MAX_DEBOUNCE_SECONDS
            ),
        )
    except Exception:
        return float(_DENSITY_IMPORT_MAX_DEBOUNCE_SECONDS)


def schedule_coalesced_density_import_finalize(
    session,
    *,
    series_id: int,
    episode_id: int | None = None,
    season_id: int | None = None,
) -> dict[str, Any]:
    """Debounce season/series density rematerialize across pack episode imports (#199)."""
    from sqlalchemy.orm.attributes import flag_modified

    from services.source_of_truth.job_priority import default_priority_for

    sid = int(series_id)
    group_id = f"density_import:{sid}"
    debounce = float(_DENSITY_IMPORT_DEBOUNCE_SECONDS)
    run_after = datetime.now(timezone.utc) + timedelta(seconds=debounce)
    incoming_eps = [int(episode_id)] if episode_id is not None else []
    incoming_seasons = [int(season_id)] if season_id is not None else []

    existing = (
        session.query(Job)
        .filter(
            Job.job_type == DENSITY_IMPORT_FINALIZE_JOB_TYPE,
            Job.status == "PENDING",
            Job.group_id == group_id,
        )
        .order_by(Job.id.desc())
        .with_for_update()
        .first()
    )
    if existing is not None:
        old = dict(existing.payload or {})
        ep_ids = sorted(
            {
                int(x)
                for x in list(old.get("episode_ids") or []) + incoming_eps
                if x is not None
            }
        )
        season_ids = sorted(
            {
                int(x)
                for x in list(old.get("season_ids") or []) + incoming_seasons
                if x is not None
            }
        )
        existing.payload = {
            "series_id": sid,
            "episode_ids": ep_ids,
            "season_ids": season_ids,
        }
        flag_modified(existing, "payload")
        prev = existing.run_after
        if prev is None or prev < run_after:
            existing.run_after = run_after
        debounce_cap = datetime.now(timezone.utc) + timedelta(
            seconds=_density_import_max_debounce_seconds()
        )
        if existing.run_after is not None and existing.run_after > debounce_cap:
            existing.run_after = debounce_cap
        existing.updated_at = datetime.now(timezone.utc)
        session.add(existing)
        return {
            "ok": True,
            "coalesced": True,
            "job_id": int(existing.id) if getattr(existing, "id", None) else None,
            "series_id": sid,
            "episode_ids": ep_ids,
            "season_ids": season_ids,
        }

    job = Job(
        job_type=DENSITY_IMPORT_FINALIZE_JOB_TYPE,
        payload={
            "series_id": sid,
            "episode_ids": incoming_eps,
            "season_ids": incoming_seasons,
        },
        status="PENDING",
        run_after=run_after,
        group_id=group_id,
        priority=default_priority_for(DENSITY_IMPORT_FINALIZE_JOB_TYPE),
    )
    session.add(job)
    return {
        "ok": True,
        "coalesced": False,
        "job_id": None,
        "series_id": sid,
        "episode_ids": incoming_eps,
        "season_ids": incoming_seasons,
    }


def process_density_import_finalize_job(session, job: Job) -> dict[str, Any]:
    """Rematerialize season/series density stubs once for a coalesced import pack (#199)."""
    from core.logger import logger
    from services.postgres.db import release_handler_session
    from services.source_of_truth.materializer import schedule_scoped_placeholder_media_refresh
    from services.source_of_truth.season_density_materializer import (
        apply_season_density_for_series,
    )
    from services.source_of_truth.series_density_materializer import (
        apply_series_density_materialization,
    )
    from services.source_of_truth.tv_density import tv_placeholder_density

    payload = job.payload or {}
    try:
        series_id = int(payload.get("series_id"))
    except Exception:
        return {"done": True, "ok": False, "reason": "missing_series_id"}

    episode_ids = sorted(
        {int(x) for x in (payload.get("episode_ids") or []) if x is not None}
    )
    if episode_ids:
        rows = (
            session.query(Episode)
            .filter(Episode.id.in_(episode_ids), Episode.is_deleted == False)  # noqa: E712
            .all()
        )
        touched = False
        for ep in rows:
            if not bool(getattr(ep, "has_file", False)):
                ep.has_file = True
                session.add(ep)
                touched = True
        if touched:
            mark_live_catalog_touch(
                session,
                episode_ids=episode_ids,
                series_ids=[series_id],
            )

    # Persist has_file, then density mat opens its own short sessions (#199).
    release_handler_session(session)

    density = tv_placeholder_density()
    created_paths: list[str] = []
    deleted_paths: list[str] = []
    density_out: dict[str, Any] = {"density": density}
    if density == "series":
        out = apply_series_density_materialization(
            int(series_id),
            activity_reason="density_import_finalize",
            ensure_art=False,
        )
        density_out["series"] = out
        action = str(out.get("action") or "")
        path = str(out.get("path") or "").strip()
        relocated = str(out.get("relocated_from") or "").strip()
        paths = [str(p).strip() for p in (out.get("paths") or []) if str(p).strip()]
        if action == "created_or_exists" and path:
            created_paths.append(path)
        if relocated:
            deleted_paths.append(relocated)
        deleted_paths.extend(paths)
    elif density == "season":
        out = apply_season_density_for_series(
            int(series_id),
            activity_reason="density_import_finalize",
            ensure_art=False,
        )
        density_out["season"] = out
        created_paths.extend(
            [str(p).strip() for p in (out.get("created_paths") or []) if str(p).strip()]
        )
        deleted_paths.extend(
            [str(p).strip() for p in (out.get("deleted_paths") or []) if str(p).strip()]
        )
    else:
        return {
            "done": True,
            "ok": True,
            "phase": "density_import_finalize",
            "skipped": "not_stub_density",
            "series_id": series_id,
        }

    if created_paths or deleted_paths:
        schedule_scoped_placeholder_media_refresh(
            created_paths=created_paths,
            delete_paths=deleted_paths,
            has_movies=False,
            has_episodes=True,
        )

    logger.info(
        f"Density import finalize series_id={series_id} density={density} "
        f"episodes={len(episode_ids)} created_paths={len(created_paths)} "
        f"deleted_paths={len(deleted_paths)}",
        extra={"emoji_type": "success"},
    )
    return {
        "done": True,
        "ok": True,
        "phase": "density_import_finalize",
        "series_id": series_id,
        "episode_count": len(episode_ids),
        "created_paths": created_paths,
        "deleted_paths": deleted_paths,
        "density": density_out,
    }


def schedule_episode_import_grace(session, episode_row_id: int, file_path: str | None = None) -> dict[str, Any]:
    raw_ids = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.episode_id == int(episode_row_id),
            Placeholder.has_placeholder == True,  # noqa: E712
        )
        .all()
    )
    placeholder_ids = [int(pid) for (pid,) in raw_ids]

    # Season/series density stubs are not episode-linked. Skip the per-episode
    # countdown timeline when there is nothing to count down on; mark has_file
    # and coalesce one density rematerialize for the series (#199).
    if not placeholder_ids:
        from services.source_of_truth.tv_density import density_suppresses_episode_files

        episode = session.query(Episode).filter(Episode.id == int(episode_row_id)).first()
        series = None
        season = None
        if episode is not None and getattr(episode, "season_id", None) is not None:
            season = session.query(Season).filter(Season.id == int(episode.season_id)).first()
            if season is not None and getattr(season, "series_id", None) is not None:
                series = session.query(Series).filter(Series.id == int(season.series_id)).first()
        if series is not None and density_suppresses_episode_files(series):
            episode.has_file = True
            if file_path:
                episode.sonarr_filepath = str(file_path)
            session.add(episode)
            mark_live_catalog_touch(
                session,
                episode_ids=[int(episode_row_id)],
                series_ids=[int(series.id)],
            )
            coalesce = schedule_coalesced_density_import_finalize(
                session,
                series_id=int(series.id),
                episode_id=int(episode_row_id),
                season_id=int(season.id) if season is not None else None,
            )
            return {
                "ok": True,
                "content_type": "episode",
                "entity_id": int(episode_row_id),
                "placeholder_count": 0,
                "active_placeholder_count": 0,
                "scheduled_jobs": 0,
                "skipped_grace": True,
                "density_coalesced": True,
                "coalesce": coalesce,
            }

    return _schedule_import_grace(
        session,
        content_type="episode",
        entity_id=int(episode_row_id),
        placeholder_ids=placeholder_ids,
        file_path=file_path,
    )


def _import_grace_job_was_cancelled(session, job: Job) -> bool:
    """True when file-delete (or Arr abort) already failed this grace job row.

    Cancel commits in another session while a worker may still hold a CLAIMED
    descriptor. Re-read error_message so we do not overwrite REQUEST with a
    stale countdown tick after interrupt.
    """
    raw_id = getattr(job, "id", None)
    try:
        jid = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        jid = None
    try:
        session.refresh(job)
    except Exception:
        pass
    err = str(getattr(job, "error_message", "") or "")
    if err == IMPORT_GRACE_CANCEL_ERROR:
        return True
    if jid is None:
        return False
    try:
        from sqlalchemy import text

        row = session.execute(
            text("SELECT error_message FROM job WHERE id = :id"),
            {"id": jid},
        ).first()
        if row and str(row[0] or "") == IMPORT_GRACE_CANCEL_ERROR:
            return True
    except Exception:
        pass
    return False


def process_import_grace_job(session, job: Job) -> dict[str, Any]:
    from core.logger import logger
    
    payload = job.payload or {}
    content_type = str(payload.get("content_type") or "").strip().lower()

    entity_id_val = payload.get("entity_id")
    try:
        entity_id = int(entity_id_val)
    except Exception:
        logger.debug(f"Import grace job missing entity_id: {entity_id_val}", extra={'emoji_type': 'debug'})
        return {"done": True, "ok": False, "reason": "missing_entity_id"}

    placeholder_ids = [int(pid) for pid in (payload.get("placeholder_ids") or []) if pid is not None]
    file_path = payload.get("file_path")
    is_finalize = bool(payload.get("finalize"))
    step_index = int(payload.get("step_index", -1))

    logger.debug(
        f"Import grace job processing: step_index={step_index}, finalize={is_finalize}, content_type={content_type}, entity_id={entity_id}, placeholder_ids={placeholder_ids}",
        extra={'emoji_type': 'debug'}
    )

    if _import_grace_job_was_cancelled(session, job):
        logger.info(
            f"Import grace skipped (job cancelled): step_index={step_index} finalize={is_finalize} "
            f"content_type={content_type} entity_id={entity_id}",
            extra={"emoji_type": "info"},
        )
        return {
            "done": True,
            "ok": True,
            "phase": "cancelled",
            "content_type": content_type,
            "entity_id": entity_id,
            "reason": "cancelled",
        }

    # Arr hasFile: DB probe → release handler session → HTTP → reopen for cancel/restore.
    from services.postgres.db import get_session, release_handler_session

    job_id_int = int(job.id) if getattr(job, "id", None) is not None else None
    try:
        probe = _load_arr_has_file_probe(session, content_type=content_type, entity_id=entity_id)
    except Exception:
        probe = None
    try:
        session.commit()
    except Exception as commit_exc:
        logger.warning(
            f"Import grace Arr probe commit failed before release: {commit_exc}",
            extra={"emoji_type": "warning"},
        )
        try:
            session.rollback()
        except Exception:
            pass
        release_handler_session(session)
        return {
            "done": False,
            "ok": False,
            "phase": "arr_probe_commit_failed",
            "content_type": content_type,
            "entity_id": entity_id,
            "reason": str(commit_exc),
        }
    release_handler_session(session)
    still_has = _arr_has_file_from_probe(probe)
    session = get_session()
    try:
        if job_id_int is not None:
            job = session.get(Job, job_id_int) or job

        if not still_has:
            phase = "finalize_aborted_arr_no_file" if is_finalize else "tick_aborted_arr_no_file"
            logger.info(
                f"Import grace aborted ({phase}): Arr no longer reports hasFile "
                f"content_type={content_type} entity_id={entity_id} file_path={file_path!r}",
                extra={"emoji_type": "info"},
            )
            cancelled = cancel_import_grace_for_entity(
                session,
                content_type=content_type,
                entity_id=entity_id,
            )
            restore = restore_request_after_import_grace_interrupt(
                session,
                content_type=content_type,
                entity_id=entity_id,
                placeholder_ids=placeholder_ids,
            )
            return {
                "done": True,
                "ok": True,
                "phase": phase,
                "content_type": content_type,
                "entity_id": entity_id,
                "reason": "arr_no_file",
                "file_path": file_path,
                "cancelled_jobs": cancelled,
                "restore": restore,
            }

        materialization: dict[str, Any] = {}

        if is_finalize:
            if _import_grace_job_was_cancelled(session, job):
                logger.info(
                    f"Import grace finalize skipped (job cancelled): content_type={content_type} entity_id={entity_id}",
                    extra={"emoji_type": "info"},
                )
                return {
                    "done": True,
                    "ok": True,
                    "phase": "cancelled",
                    "content_type": content_type,
                    "entity_id": entity_id,
                    "reason": "cancelled",
                }
            logger.info(
                f"Import grace finalization starting: content_type={content_type}, entity_id={entity_id}, placeholder_ids={placeholder_ids}",
                extra={'emoji_type': 'info'}
            )
            if content_type == "movie":
                movie_row = session.query(Movie).filter(Movie.id == entity_id).first()
                if not movie_row:
                    logger.warning(f"Finalize: Movie not found with id={entity_id}", extra={'emoji_type': 'warning'})
                    return {"done": True, "ok": False, "reason": f"movie_not_found:{entity_id}"}
                movie_row.has_file = True
                if file_path:
                    movie_row.radarr_filepath = str(file_path)
                session.add(movie_row)
                mark_live_catalog_touch(session, movie_ids=[entity_id])
                session.flush()
                # Persist has_file before mat so a short lock-busy retry does not lose it.
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                mat_movie_ids, _mat_episode_ids = expand_determination_entity_ids(
                    session,
                    movie_ids=[entity_id],
                )
                determination = run_determination_for_entities_in_session(
                    session,
                    movie_ids=mat_movie_ids,
                )
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                # Mat opens its own short sessions; do not keep this handler session open.
                release_handler_session(session)
                session = None
                try:
                    materialization = run_materialization_for_entities(
                        movie_ids=mat_movie_ids,
                        observation_source="event_movie_imported_grace_finalize",
                    )
                except Exception as mat_exc:
                    if _is_lock_timeout_error(mat_exc):
                        logger.info(
                            f"Import grace finalize deferred (row lock busy): movie_id={entity_id}",
                            extra={"emoji_type": "info"},
                        )
                        return {
                            "done": False,
                            "ok": True,
                            "defer": True,
                            "defer_seconds": 5,
                            "skipped": "lock_busy",
                            "phase": "finalize_deferred",
                            "content_type": content_type,
                            "entity_id": entity_id,
                        }
                    raise
                session = get_session()
                movie_ref = session.query(Movie).filter(Movie.id == entity_id).first()
                mat = materialization_stats_dict(materialization)
                result_reason, status_label = outcome_reason_and_status_from_materialization(
                    "Import grace finished (movie)", mat
                )
                result_path = (
                    str(getattr(movie_ref, "placeholder_filepath", "") or "").strip()
                    if movie_ref
                    else ""
                ) or (movie_placeholder_path(movie_ref) if movie_ref else "")
                append_placeholder_activity_status(
                    session,
                    item_type="movie",
                    movie_id=int(entity_id),
                    episode_id=None,
                    series_id=None,
                    season_id=None,
                    season_number=None,
                    instance_key=getattr(movie_ref, "instance_key", None) if movie_ref else None,
                    instance_id=getattr(movie_ref, "instance_id", None) if movie_ref else None,
                    event_type="placeholder_event_import_grace_movie_finalize",
                    path=str(result_path or ""),
                    item_title=str(getattr(movie_ref, "title", "") or "Unknown Movie") if movie_ref else "Unknown Movie",
                    series_title=None,
                    reason=result_reason,
                    status_label=status_label,
                    source="import_grace_finalize",
                    extra_snapshot={
                        "determination": determination if isinstance(determination, dict) else {},
                        "materialization": mat,
                        "movie_id": int(entity_id),
                        "placeholder_ids": placeholder_ids,
                    },
                )
            elif content_type == "episode":
                episode_row = session.query(Episode).filter(Episode.id == entity_id).first()
                if not episode_row:
                    logger.warning(f"Finalize: Episode not found with id={entity_id}", extra={'emoji_type': 'warning'})
                    return {"done": True, "ok": False, "reason": f"episode_not_found:{entity_id}"}
                episode_row.has_file = True
                if file_path:
                    episode_row.sonarr_filepath = str(file_path)
                session.add(episode_row)
                season_id = int(getattr(episode_row, "season_id", 0) or 0)
                series_id_for_touch = None
                if season_id:
                    season_row = session.query(Season).filter(Season.id == season_id).first()
                    if season_row is not None:
                        series_id_for_touch = int(getattr(season_row, "series_id", 0) or 0) or None
                mark_live_catalog_touch(
                    session,
                    episode_ids=[entity_id],
                    series_ids=[series_id_for_touch] if series_id_for_touch else None,
                )
                session.flush()
                from services.source_of_truth.tv_density import density_suppresses_episode_files

                series_for_density = None
                if series_id_for_touch:
                    series_for_density = (
                        session.query(Series).filter(Series.id == int(series_id_for_touch)).first()
                    )
                coalesce_density = bool(
                    series_for_density is not None
                    and density_suppresses_episode_files(series_for_density)
                )
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                _mat_movie_ids, mat_episode_ids = expand_determination_entity_ids(
                    session,
                    episode_ids=[entity_id],
                )
                determination = run_determination_for_entities_in_session(
                    session,
                    episode_ids=mat_episode_ids,
                )
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                # Mat opens its own short sessions; do not keep this handler session open.
                release_handler_session(session)
                session = None
                try:
                    # Under season/series density, skip per-episode density rematerialize
                    # and coalesce one pack finalize for the series (#199).
                    materialization = run_materialization_for_entities(
                        episode_ids=mat_episode_ids,
                        observation_source="event_episode_imported_grace_finalize",
                        skip_density=coalesce_density,
                    )
                except Exception as mat_exc:
                    if _is_lock_timeout_error(mat_exc):
                        logger.info(
                            f"Import grace finalize deferred (row lock busy): episode_id={entity_id}",
                            extra={"emoji_type": "info"},
                        )
                        return {
                            "done": False,
                            "ok": True,
                            "defer": True,
                            "defer_seconds": 5,
                            "skipped": "lock_busy",
                            "phase": "finalize_deferred",
                            "content_type": content_type,
                            "entity_id": entity_id,
                        }
                    raise
                session = get_session()
                if coalesce_density and series_id_for_touch:
                    schedule_coalesced_density_import_finalize(
                        session,
                        series_id=int(series_id_for_touch),
                        episode_id=int(entity_id),
                        season_id=int(season_id) if season_id else None,
                    )
                ctx = (
                    session.query(Episode, Season, Series)
                    .join(Season, Episode.season_id == Season.id)
                    .join(Series, Season.series_id == Series.id)
                    .filter(Episode.id == entity_id)
                    .first()
                )
                mat = materialization_stats_dict(materialization)
                result_reason, status_label = outcome_reason_and_status_from_materialization(
                    "Import grace finished (episode)", mat
                )
                if ctx:
                    ep, season, series = ctx
                    sn = int(season.season_number or 0)
                    en = int(ep.episode_number or 0)
                    ep_label = f"S{sn:02d}E{en:02d} - {ep.title}"
                    result_path = str(getattr(ep, "placeholder_filepath", "") or "").strip() or episode_placeholder_path(
                        ep, season, series
                    )
                    append_placeholder_activity_status(
                        session,
                        item_type="episode",
                        movie_id=None,
                        episode_id=int(entity_id),
                        series_id=int(series.id),
                        season_id=int(season.id),
                        season_number=sn,
                        instance_key=getattr(series, "instance_key", None),
                        instance_id=getattr(series, "instance_id", None),
                        event_type="placeholder_event_import_grace_episode_finalize",
                        path=str(result_path or ""),
                        item_title=ep_label,
                        series_title=str(getattr(series, "title", "") or "") or None,
                        reason=result_reason,
                        status_label=status_label,
                        source="import_grace_finalize",
                        extra_snapshot={
                            "determination": determination if isinstance(determination, dict) else {},
                            "materialization": mat,
                            "episode_id": int(entity_id),
                            "placeholder_ids": placeholder_ids,
                        },
                    )
                    try:
                        from services.source_of_truth.placeholder_art_reconciler import (
                            enqueue_art_refresh_for_calendar_date_changes,
                        )

                        enqueue_art_refresh_for_calendar_date_changes(
                            session,
                            series_ids=[int(series.id)],
                        )
                    except Exception as art_exc:
                        logger.debug(
                            f"Import grace New Ep. art enqueue skipped episode_id={entity_id}: {art_exc}",
                            extra={"emoji_type": "debug"},
                            exc_info=True,
                        )
                else:
                    append_placeholder_activity_status(
                        session,
                        item_type="episode",
                        movie_id=None,
                        episode_id=int(entity_id),
                        series_id=None,
                        season_id=None,
                        season_number=None,
                        instance_key=None,
                        instance_id=None,
                        event_type="placeholder_event_import_grace_episode_finalize",
                        path="",
                        item_title="Unknown episode",
                        series_title=None,
                        reason=result_reason,
                        status_label=status_label,
                        source="import_grace_finalize",
                        extra_snapshot={
                            "determination": determination if isinstance(determination, dict) else {},
                            "materialization": mat,
                            "episode_id": int(entity_id),
                            "placeholder_ids": placeholder_ids,
                        },
                    )
            else:
                logger.warning(f"Finalize: Unsupported content_type={content_type}", extra={'emoji_type': 'warning'})
                return {"done": True, "ok": False, "reason": f"unsupported_content_type:{content_type}"}

            remaining_ids: list[int] = []
            if placeholder_ids:
                logger.debug(f"Finalize: Checking remaining placeholders: {placeholder_ids}", extra={'emoji_type': 'debug'})
                rows = (
                    session.query(Placeholder)
                    .filter(
                        Placeholder.id.in_(placeholder_ids),
                        Placeholder.has_placeholder == True,  # noqa: E712
                    )
                    .all()
                )
                remaining_ids = [int(row.id) for row in rows]
                logger.info(f"Finalize: Found {len(remaining_ids)} remaining placeholders to clean up", extra={'emoji_type': 'info'})
                # Status projection removed
                pass

            logger.info(
                f"Import grace finalization complete: {len(remaining_ids)} remaining placeholders queued for projection",
                extra={'emoji_type': 'success'}
            )
            return {
                "done": True,
                "ok": True,
                "phase": "finalize",
                "content_type": content_type,
                "entity_id": entity_id,
                "remaining_placeholders": len(remaining_ids),
                "materialization": materialization,
            }

        status_text = str(payload.get("status_text") or "").strip()
        if not status_text:
            logger.debug(f"Import grace tick: missing status_text at step_index={step_index}", extra={'emoji_type': 'debug'})
            return {"done": True, "ok": False, "reason": "missing_status_text"}

        # Re-check cancel immediately before writing countdown (delete may have raced us).
        if _import_grace_job_was_cancelled(session, job):
            logger.info(
                f"Import grace tick skipped (job cancelled): step_index={step_index} "
                f"content_type={content_type} entity_id={entity_id}",
                extra={"emoji_type": "info"},
            )
            return {
                "done": True,
                "ok": True,
                "phase": "cancelled",
                "content_type": content_type,
                "entity_id": entity_id,
                "reason": "cancelled",
            }

        logger.debug(
            f"Import grace tick: step_index={step_index}, setting status_text='{status_text}' for {len(placeholder_ids)} placeholder(s)",
            extra={'emoji_type': 'debug'}
        )
        active_ids = _set_countdown_status(session, placeholder_ids, status_text)
        _enqueue_nfo_refresh_job(session, active_ids)

        logger.debug(
            f"Import grace tick complete: step_index={step_index}, updated {len(active_ids)} placeholder(s)",
            extra={'emoji_type': 'debug'}
        )
        return {
            "done": True,
            "ok": True,
            "phase": "tick",
            "content_type": content_type,
            "entity_id": entity_id,
            "updated_placeholders": len(active_ids),
        }
    finally:
        # Owned replacement session(s): worker cannot commit after the original
        # handler session was released above.
        if session is not None:
            release_handler_session(session)
