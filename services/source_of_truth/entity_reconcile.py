"""Single-title entity reconcile: *arr metadata Refresh (UI-equivalent), forced sync, scoped downstream."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_

from core.config import settings
from core.logger import logger, start_verbose_stall_heartbeat
from services.media_servers.player_metadata_refresh import push_placeholder_batch_player_metadata
from services.media_servers.refresh import refresh_all_paths, refresh_players_after_local_art_rewrite
from services.placeholder_poster_art import (
    ensure_episode_still_art,
    ensure_movie_art,
    ensure_series_art,
)
from services.postgres.db import get_session
from services.postgres.models import Episode, Job, Movie, Placeholder, Season, Series
from services.source_of_truth.arr_commands import (
    ArrCommandError,
    trigger_refresh_movie,
    trigger_refresh_series,
    wait_arr_commands,
)
from services.source_of_truth.determiner import run_determination_for_entities_with_siblings
from services.source_of_truth.job_priority import PRIORITY_INTERACTIVE
from services.source_of_truth.materializer import run_materialization_for_entities
from services.source_of_truth.startup import _refresh_placeholder_presence_for_entities
from services.source_of_truth.status_reconciler import _refresh_episode_nfo, _refresh_movie_nfo
from services.source_of_truth.sync_runner import sync_radarr_movies_by_ids, sync_sonarr_series_by_ids

ENTITY_RECONCILE_JOB_TYPE = "entity_reconcile"
ACTIVE_JOB_STATUSES = frozenset({"PENDING", "CLAIMED", "WORKING"})

STEP_QUEUED = "queued"
STEP_DONE = "done"


@dataclass(frozen=True)
class _ReconcileTargets:
    entity_type: str
    entity_id: int
    display_title: str
    arr_kind: str  # "radarr" | "sonarr"
    arr_id: int
    base_url: str
    api_key: str
    instance_key: str
    movie_row_ids: list[int]
    episode_row_ids: list[int]
    arr_label: str


def _entity_kind_label(entity_type: str) -> str:
    return {"movie": "Movie", "series": "Series", "episode": "Episode"}.get(
        str(entity_type or "").strip().lower(),
        "Title",
    )


def _resolve_display_title(session, entity_type: str, entity_id: int) -> str:
    """Human-readable title for logs and job payload."""
    kind = str(entity_type).strip().lower()
    eid = int(entity_id)

    if kind == "movie":
        movie = session.query(Movie).filter(Movie.id == eid).first()
        if movie:
            title = str(getattr(movie, "title", "") or "").strip()
            if title:
                return title
        return f"Movie #{eid}"

    if kind == "series":
        series = session.query(Series).filter(Series.id == eid).first()
        if series:
            title = str(getattr(series, "title", "") or "").strip()
            if title:
                return title
        return f"Series #{eid}"

    if kind == "episode":
        row = (
            session.query(Episode, Season, Series)
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .filter(Episode.id == eid)
            .first()
        )
        if row:
            episode, season, series = row
            sn = int(getattr(season, "season_number", 0) or 0)
            en = int(getattr(episode, "episode_number", 0) or 0)
            st = str(getattr(series, "title", "") or "").strip() or "Series"
            et = str(getattr(episode, "title", "") or "").strip()
            base = f"{st} S{sn:02d}E{en:02d}"
            if et:
                return f'{base}: "{et}"'
            return base
        return f"Episode #{eid}"

    return f"{kind} #{eid}"


def _reconcile_log_prefix(*, entity_type: str, display_title: str) -> str:
    kind = _entity_kind_label(entity_type)
    title = str(display_title or "").strip() or kind
    return f"Library reconcile · {kind} · {title}"


def _log_reconcile_info(entity_type: str, display_title: str, detail: str, *, job_id: int | None = None) -> None:
    prefix = _reconcile_log_prefix(entity_type=entity_type, display_title=display_title)
    suffix = f" · job {int(job_id)}" if job_id is not None else ""
    logger.info(f"{prefix} · {detail}{suffix}", extra={"emoji_type": "info"})


def _log_reconcile_success(entity_type: str, display_title: str, detail: str, *, job_id: int | None = None) -> None:
    prefix = _reconcile_log_prefix(entity_type=entity_type, display_title=display_title)
    suffix = f" · job {int(job_id)}" if job_id is not None else ""
    logger.info(f"{prefix} · {detail}{suffix}", extra={"emoji_type": "success"})


def _log_reconcile_warning(entity_type: str, display_title: str, detail: str, *, job_id: int | None = None) -> None:
    prefix = _reconcile_log_prefix(entity_type=entity_type, display_title=display_title)
    suffix = f" · job {int(job_id)}" if job_id is not None else ""
    logger.warning(f"{prefix} · {detail}{suffix}", extra={"emoji_type": "warning"})


_STEP_LOG_DETAIL: dict[str, str] = {
    "arr_refresh": "Asking {arr} to refresh metadata",
    "arr_wait_refresh": "Waiting for {arr} metadata refresh to finish",
    "sync_catalog": "Syncing catalog from {arr}",
    "filesystem": "Checking placeholder files on disk",
    "determination": "Updating placeholder status",
    "materialization": "Creating or updating placeholder files",
    "sidecars": "Writing NFO sidecars and poster art",
    "player": "Refreshing Plex, Jellyfin, and Emby for this title",
    STEP_DONE: "Finished",
}


def _step_log_detail(step: str, *, arr_label: str) -> str:
    template = _STEP_LOG_DETAIL.get(step, step.replace("_", " ").capitalize())
    return template.format(arr=arr_label)


def _payload_dict(job: Job) -> dict[str, Any]:
    raw = getattr(job, "payload", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _step_label_for(step: str, *, arr_label: str = "Radarr") -> str:
    labels = {
        STEP_QUEUED: "Queued…",
        "arr_refresh": f"Asking {arr_label} for updates…",
        "arr_wait_refresh": "Waiting for metadata refresh…",
        "sync_catalog": "Syncing catalog…",
        "filesystem": "Checking placeholder files…",
        "determination": "Updating status…",
        "materialization": "Applying placeholders…",
        "sidecars": "Writing artwork & metadata…",
        "player": "Updating Plex/Jellyfin/Emby…",
        STEP_DONE: "Done",
    }
    return labels.get(step, step.replace("_", " ").capitalize())


# Durable sidebar phase bars (queued/done are stage only, not bars).
RECONCILE_LIVE_PHASES: tuple[tuple[str, str], ...] = (
    ("arr_refresh", "Arr refresh"),
    ("arr_wait_refresh", "Wait for Arr"),
    ("sync_catalog", "Catalog sync"),
    ("filesystem", "Filesystem"),
    ("determination", "Status"),
    ("materialization", "Placeholders"),
    ("sidecars", "Artwork & metadata"),
    ("player", "Players"),
)


def _seed_reconcile_live(*, display_title: str, arr_label: str = "Radarr") -> dict[str, Any]:
    phases = [
        {"key": key, "label": label, "done": 0, "total": 1, "status": "pending"}
        for key, label in RECONCILE_LIVE_PHASES
    ]
    return {
        "kind": "entity_reconcile",
        "label": str(display_title or "Title refresh").strip() or "Title refresh",
        "stage": STEP_QUEUED,
        "stage_label": _step_label_for(STEP_QUEUED, arr_label=arr_label),
        "overall_pct": 0,
        "units_done": 0,
        "units_total": len(phases),
        "unit_label": "phases",
        "phases": phases,
        "active_units": [],
    }


def _live_overall_from_phases(phases: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Return (overall_pct, units_done, units_total) from phase bars."""
    total_phases = len(phases) or 1
    done_phases = 0
    weighted_done = 0.0
    for phase in phases:
        status = str(phase.get("status") or "").lower()
        p_done = float(phase.get("done") or 0)
        p_total = float(phase.get("total") or 0)
        if status == "done" or status == "skipped":
            done_phases += 1
            weighted_done += 1.0
        elif status == "working" and p_total > 0:
            weighted_done += max(0.0, min(1.0, p_done / p_total))
        elif status == "working":
            weighted_done += 0.0
    pct = int(round((weighted_done / float(total_phases)) * 100.0))
    return max(0, min(100, pct)), done_phases, total_phases


def _apply_live_step(
    live: dict[str, Any],
    step: str,
    *,
    arr_label: str = "Radarr",
    phase_done: int | None = None,
    phase_total: int | None = None,
) -> dict[str, Any]:
    """Advance phase bars for ``step`` (or mark all done)."""
    out = dict(live) if isinstance(live, dict) else {}
    phases_in = out.get("phases") if isinstance(out.get("phases"), list) else []
    by_key = {
        str(p.get("key") or "").strip(): dict(p)
        for p in phases_in
        if isinstance(p, dict) and str(p.get("key") or "").strip()
    }
    order = [key for key, _label in RECONCILE_LIVE_PHASES]
    for key, label in RECONCILE_LIVE_PHASES:
        if key not in by_key:
            by_key[key] = {"key": key, "label": label, "done": 0, "total": 1, "status": "pending"}

    if step == STEP_DONE:
        for key in order:
            row = by_key[key]
            total = max(1, int(row.get("total") or 1))
            row["status"] = "done"
            row["done"] = total
            row["total"] = total
        stage = STEP_DONE
        stage_label = _step_label_for(STEP_DONE, arr_label=arr_label)
    elif step == STEP_QUEUED:
        stage = STEP_QUEUED
        stage_label = _step_label_for(STEP_QUEUED, arr_label=arr_label)
    else:
        reached = False
        for key in order:
            row = by_key[key]
            if key == step:
                reached = True
                total = int(phase_total) if phase_total is not None else max(1, int(row.get("total") or 1))
                done = int(phase_done) if phase_done is not None else int(row.get("done") or 0)
                row["total"] = max(1, total)
                row["done"] = max(0, min(row["total"], done))
                row["status"] = "working"
                continue
            if not reached:
                total = max(1, int(row.get("total") or 1))
                row["status"] = "done"
                row["done"] = total
                row["total"] = total
            else:
                if str(row.get("status") or "").lower() == "working":
                    row["status"] = "pending"
                    row["done"] = 0
        stage = step
        stage_label = _step_label_for(step, arr_label=arr_label)

    phases = [by_key[key] for key in order if key in by_key]
    pct, units_done, units_total = _live_overall_from_phases(phases)
    out["stage"] = stage
    out["stage_label"] = stage_label
    out["overall_pct"] = pct
    out["units_done"] = units_done
    out["units_total"] = units_total
    out["unit_label"] = "phases"
    out["phases"] = phases
    out["kind"] = "entity_reconcile"
    if not out.get("label"):
        out["label"] = "Title refresh"
    return out


def _set_job_step(
    session,
    job: Job,
    step: str,
    *,
    arr_label: str = "Radarr",
    phase_done: int | None = None,
    phase_total: int | None = None,
) -> None:
    payload = _payload_dict(job)
    payload["step"] = step
    payload["step_label"] = _step_label_for(step, arr_label=arr_label)
    live = payload.get("live") if isinstance(payload.get("live"), dict) else None
    if live is None:
        live = _seed_reconcile_live(
            display_title=str(payload.get("display_title") or "Title refresh"),
            arr_label=arr_label,
        )
    payload["live"] = _apply_live_step(
        live,
        step,
        arr_label=arr_label,
        phase_done=phase_done,
        phase_total=phase_total,
    )
    job.payload = payload
    session.add(job)
    session.commit()


def _find_active_reconcile_job(session, entity_type: str, entity_id: int) -> Job | None:
    rows = (
        session.query(Job)
        .filter(
            Job.job_type == ENTITY_RECONCILE_JOB_TYPE,
            Job.status.in_(tuple(ACTIVE_JOB_STATUSES)),
        )
        .order_by(Job.id.desc())
        .limit(50)
        .all()
    )
    want_type = str(entity_type).strip().lower()
    want_id = int(entity_id)
    for row in rows:
        payload = _payload_dict(row)
        if str(payload.get("entity_type") or "").strip().lower() != want_type:
            continue
        try:
            if int(payload.get("entity_id")) != want_id:
                continue
        except (TypeError, ValueError):
            continue
        return row
    return None


def _find_pending_reconcile_job(session, entity_type: str, entity_id: int) -> Job | None:
    rows = (
        session.query(Job)
        .filter(
            Job.job_type == ENTITY_RECONCILE_JOB_TYPE,
            Job.status == "PENDING",
        )
        .order_by(Job.id.desc())
        .limit(50)
        .all()
    )
    want_type = str(entity_type).strip().lower()
    want_id = int(entity_id)
    for row in rows:
        payload = _payload_dict(row)
        if str(payload.get("entity_type") or "").strip().lower() != want_type:
            continue
        try:
            if int(payload.get("entity_id")) != want_id:
                continue
        except (TypeError, ValueError):
            continue
        return row
    return None


def _enqueue_followup_reconcile_if_needed(
    session,
    *,
    entity_type: str,
    entity_id: int,
    display_title: str,
    source: str,
) -> int | None:
    """Queue a follow-up reconcile when policy changes during an in-flight job."""
    if _find_pending_reconcile_job(session, entity_type, entity_id) is not None:
        return None
    kind = str(entity_type).strip().lower()
    eid = int(entity_id)
    body: dict[str, Any] = {
        "entity_type": kind,
        "entity_id": eid,
        "source": f"followup:{source}",
        "display_title": display_title,
        "step": STEP_QUEUED,
        "step_label": _step_label_for(STEP_QUEUED),
        "live": _seed_reconcile_live(display_title=display_title),
    }
    job = Job(
        job_type=ENTITY_RECONCILE_JOB_TYPE,
        payload=body,
        status="PENDING",
        max_attempts=3,
        priority=PRIORITY_INTERACTIVE,
        group_id=f"entity_reconcile:{body['entity_type']}:{body['entity_id']}",
    )
    session.add(job)
    session.commit()
    _log_reconcile_info(
        kind,
        display_title,
        "Queued follow-up reconcile after in-flight job",
        job_id=int(job.id),
    )
    return int(job.id)


def _episode_row_ids_for_series(session, series_id: int) -> list[int]:
    rows = (
        session.query(Episode.id)
        .join(Season, Episode.season_id == Season.id)
        .filter(Season.series_id == int(series_id), Episode.is_deleted == False)  # noqa: E712
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def _resolve_targets(session, entity_type: str, entity_id: int) -> _ReconcileTargets:
    kind = str(entity_type).strip().lower()
    eid = int(entity_id)

    if kind == "movie":
        movie = session.query(Movie).filter(Movie.id == eid, Movie.is_deleted == False).first()  # noqa: E712
        if not movie:
            raise ValueError("movie_not_found")
        radarr_id = getattr(movie, "radarrid", None)
        if radarr_id is None:
            raise ValueError("movie_missing_radarr_id")
        instance_key = str(getattr(movie, "instance_key", "") or "").strip()
        resolved = settings.resolve_arr_instance("radarr", instance_key=instance_key) or {}
        base_url = str(resolved.get("base_url") or resolved.get("url") or "").strip()
        api_key = str(resolved.get("api_key") or "").strip()
        if not base_url or not api_key:
            raise ValueError("radarr_not_configured")
        display_title = str(getattr(movie, "title", "") or "").strip() or f"Movie #{eid}"
        return _ReconcileTargets(
            entity_type=kind,
            entity_id=eid,
            display_title=display_title,
            arr_kind="radarr",
            arr_id=int(radarr_id),
            base_url=base_url,
            api_key=api_key,
            instance_key=instance_key,
            movie_row_ids=[int(movie.id)],
            episode_row_ids=[],
            arr_label="Radarr",
        )

    if kind == "series":
        series = session.query(Series).filter(Series.id == eid, Series.is_deleted == False).first()  # noqa: E712
        if not series:
            raise ValueError("series_not_found")
        sonarr_id = getattr(series, "sonarrid", None)
        if sonarr_id is None:
            raise ValueError("series_missing_sonarr_id")
        instance_key = str(getattr(series, "instance_key", "") or "").strip()
        resolved = settings.resolve_arr_instance("sonarr", instance_key=instance_key) or {}
        base_url = str(resolved.get("base_url") or resolved.get("url") or "").strip()
        api_key = str(resolved.get("api_key") or "").strip()
        if not base_url or not api_key:
            raise ValueError("sonarr_not_configured")
        display_title = str(getattr(series, "title", "") or "").strip() or f"Series #{eid}"
        return _ReconcileTargets(
            entity_type=kind,
            entity_id=eid,
            display_title=display_title,
            arr_kind="sonarr",
            arr_id=int(sonarr_id),
            base_url=base_url,
            api_key=api_key,
            instance_key=instance_key,
            movie_row_ids=[],
            episode_row_ids=_episode_row_ids_for_series(session, eid),
            arr_label="Sonarr",
        )

    if kind == "episode":
        episode = session.query(Episode).filter(Episode.id == eid, Episode.is_deleted == False).first()  # noqa: E712
        if not episode:
            raise ValueError("episode_not_found")
        season = session.query(Season).get(episode.season_id) if episode.season_id else None
        series = session.query(Series).get(season.series_id) if season and season.series_id else None
        if not season or not series:
            raise ValueError("episode_series_not_found")
        sonarr_id = getattr(series, "sonarrid", None)
        if sonarr_id is None:
            raise ValueError("series_missing_sonarr_id")
        instance_key = str(getattr(series, "instance_key", "") or "").strip()
        resolved = settings.resolve_arr_instance("sonarr", instance_key=instance_key) or {}
        base_url = str(resolved.get("base_url") or resolved.get("url") or "").strip()
        api_key = str(resolved.get("api_key") or "").strip()
        if not base_url or not api_key:
            raise ValueError("sonarr_not_configured")
        display_title = _resolve_display_title(session, kind, eid)
        return _ReconcileTargets(
            entity_type=kind,
            entity_id=eid,
            display_title=display_title,
            arr_kind="sonarr",
            arr_id=int(sonarr_id),
            base_url=base_url,
            api_key=api_key,
            instance_key=instance_key,
            movie_row_ids=[],
            episode_row_ids=[int(episode.id)],
            arr_label="Sonarr",
        )

    raise ValueError(f"unsupported_entity_type:{kind}")


def _placeholders_for_scope(session, *, movie_row_ids: list[int], episode_row_ids: list[int]) -> list[Placeholder]:
    mids = [int(x) for x in movie_row_ids if x is not None]
    eids = [int(x) for x in episode_row_ids if x is not None]
    if not mids and not eids:
        return []
    q = session.query(Placeholder)
    if mids and eids:
        q = q.filter(or_(Placeholder.movie_id.in_(mids), Placeholder.episode_id.in_(eids)))
    elif mids:
        q = q.filter(Placeholder.movie_id.in_(mids))
    else:
        q = q.filter(Placeholder.episode_id.in_(eids))
    return q.all()


@dataclass
class _SidecarPassResult:
    refresh_paths: set[str]
    rewritten_movies: list[Any]
    rewritten_series: list[Any]


def _run_sidecars_and_collect_paths(
    session,
    placeholders: list[Placeholder],
    *,
    entity_type: str,
    display_title: str,
    job_id: int | None = None,
    on_progress: Any | None = None,
) -> _SidecarPassResult:
    refresh_paths: set[str] = set()
    rewritten_movies: list[Any] = []
    rewritten_series: list[Any] = []
    processed_movies: set[int] = set()
    processed_series: set[int] = set()
    player_push: list[Placeholder] = []
    total = len(placeholders)
    processed = 0

    progress_every = max(5, total // 20) if total > 0 else 5

    def _tick_sidecar_progress() -> None:
        nonlocal processed
        processed += 1
        if callable(on_progress) and (
            processed == 1 or processed == total or processed % progress_every == 0
        ):
            try:
                on_progress(processed, total)
            except Exception:
                pass

    for placeholder in placeholders:
        movie = session.query(Movie).get(placeholder.movie_id) if placeholder.movie_id else None
        episode = session.query(Episode).get(placeholder.episode_id) if placeholder.episode_id else None
        effective = bool(getattr(placeholder, "has_placeholder", False))
        if movie and bool(getattr(movie, "has_placeholder", False)):
            effective = True
        if episode and bool(getattr(episode, "has_placeholder", False)):
            effective = True
        if not effective:
            _tick_sidecar_progress()
            continue

        if movie:
            if _refresh_movie_nfo(placeholder, movie):
                pass
            mid = int(movie.id)
            path = str(getattr(placeholder, "path", "") or getattr(movie, "placeholder_filepath", "") or "").strip()
            if path and mid not in processed_movies:
                processed_movies.add(mid)
                result = ensure_movie_art(movie, path)
                if result.wrote_any:
                    refresh_paths.add(os.path.dirname(os.path.abspath(path)))
                    rewritten_movies.append(movie)
            if getattr(placeholder, "has_placeholder", False) or getattr(movie, "has_placeholder", False):
                player_push.append(placeholder)
            _tick_sidecar_progress()
            continue

        if not episode:
            _tick_sidecar_progress()
            continue
        season = session.query(Season).get(episode.season_id) if episode.season_id else None
        series = session.query(Series).get(season.series_id) if season and season.series_id else None
        if not season or not series:
            _tick_sidecar_progress()
            continue

        _refresh_episode_nfo(session, placeholder, episode)
        path = str(
            getattr(placeholder, "path", "") or getattr(episode, "placeholder_filepath", "") or ""
        ).strip()
        if not path:
            _tick_sidecar_progress()
            continue

        sid = int(series.id)
        series_folder = getattr(series, "placeholder_folder", None) or os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(path)))
        )
        if sid not in processed_series:
            processed_series.add(sid)
            series_result = ensure_series_art(series, series_folder=series_folder)
            if series_result.wrote_any and series_folder:
                refresh_paths.add(os.path.abspath(series_folder))
                rewritten_series.append(series)
        still_result = ensure_episode_still_art(episode, season, series, path)
        if still_result.wrote_any:
            refresh_paths.add(os.path.dirname(os.path.abspath(path)))
        else:
            refresh_paths.add(os.path.dirname(os.path.abspath(path)))

        if getattr(placeholder, "has_placeholder", False) or getattr(episode, "has_placeholder", False):
            player_push.append(placeholder)

        _tick_sidecar_progress()

    if player_push:
        seen: set[tuple[str, int]] = set()
        unique: list[Placeholder] = []
        for ph in player_push:
            if ph.movie_id:
                key = ("movie", int(ph.movie_id))
            elif ph.episode_id:
                key = ("episode", int(ph.episode_id))
            else:
                continue
            if key in seen:
                continue
            seen.add(key)
            unique.append(ph)
        try:
            _log_reconcile_info(
                entity_type,
                display_title,
                f"Pushing status to media servers for {len(unique)} placeholder(s)",
                job_id=job_id,
            )
            push_placeholder_batch_player_metadata(session, unique)
        except Exception as exc:
            _log_reconcile_warning(
                entity_type,
                display_title,
                f"Media server status update failed: {exc}",
                job_id=job_id,
            )

    if callable(on_progress) and total > 0:
        try:
            on_progress(total, total)
        except Exception:
            pass

    return _SidecarPassResult(
        refresh_paths=refresh_paths,
        rewritten_movies=rewritten_movies,
        rewritten_series=rewritten_series,
    )


def enqueue_entity_reconcile(
    *,
    entity_type: str,
    entity_id: int,
    source: str,
) -> dict[str, Any]:
    """Enqueue (or reuse) a single-title entity reconcile job."""
    session = get_session()
    try:
        kind = str(entity_type).strip().lower()
        eid = int(entity_id)
        display_title = _resolve_display_title(session, kind, eid)
        existing = _find_active_reconcile_job(session, kind, eid)
        if existing is not None:
            payload = _payload_dict(existing)
            _log_reconcile_info(
                kind,
                str(payload.get("display_title") or display_title),
                "Already running — reusing in-flight job",
                job_id=int(existing.id),
            )
            followup_job_id = _enqueue_followup_reconcile_if_needed(
                session,
                entity_type=kind,
                entity_id=eid,
                display_title=display_title,
                source=str(source or "").strip(),
            )
            return {
                "ok": True,
                "job_id": int(existing.id),
                "reused": True,
                "followup_job_id": followup_job_id,
                "step": payload.get("step") or STEP_QUEUED,
                "step_label": payload.get("step_label") or _step_label_for(STEP_QUEUED),
                "display_title": str(payload.get("display_title") or display_title),
                "live": payload.get("live") if isinstance(payload.get("live"), dict) else None,
            }

        body: dict[str, Any] = {
            "entity_type": kind,
            "entity_id": eid,
            "source": str(source or "").strip(),
            "display_title": display_title,
            "step": STEP_QUEUED,
            "step_label": _step_label_for(STEP_QUEUED),
            "live": _seed_reconcile_live(display_title=display_title),
        }
        job = Job(
            job_type=ENTITY_RECONCILE_JOB_TYPE,
            payload=body,
            status="PENDING",
            max_attempts=3,
            priority=PRIORITY_INTERACTIVE,
            group_id=f"entity_reconcile:{body['entity_type']}:{body['entity_id']}",
        )
        session.add(job)
        session.commit()
        _log_reconcile_info(kind, display_title, "Queued from library", job_id=int(job.id))
        return {
            "ok": True,
            "job_id": int(job.id),
            "reused": False,
            "step": STEP_QUEUED,
            "step_label": body["step_label"],
            "display_title": display_title,
            "live": body["live"],
        }
    finally:
        session.close()


def reconcile_job_status_view(job: Job) -> dict[str, Any]:
    """API-facing status for a reconcile job."""
    payload = _payload_dict(job)
    raw_status = str(getattr(job, "status", "") or "").strip().upper()
    if raw_status == "DONE":
        status = "done"
    elif raw_status == "FAILED":
        status = "failed"
    else:
        status = "working"

    live = payload.get("live") if isinstance(payload.get("live"), dict) else None
    if live is None and status == "working":
        live = _seed_reconcile_live(
            display_title=str(payload.get("display_title") or "Title refresh"),
        )
        step = str(payload.get("step") or STEP_QUEUED)
        if step and step != STEP_QUEUED:
            live = _apply_live_step(live, step)

    return {
        "ok": status != "failed",
        "status": status,
        "step": payload.get("step"),
        "step_label": payload.get("step_label"),
        "display_title": payload.get("display_title"),
        "error_message": getattr(job, "error_message", None),
        "entity_type": payload.get("entity_type"),
        "entity_id": payload.get("entity_id"),
        "live": live,
    }


def process_entity_reconcile_job(session, job: Job) -> dict[str, Any]:
    payload = _payload_dict(job)
    entity_type = str(payload.get("entity_type") or "").strip().lower()
    try:
        entity_id = int(payload.get("entity_id"))
    except (TypeError, ValueError) as exc:
        raise ValueError("entity_reconcile_missing_entity_id") from exc

    targets = _resolve_targets(session, entity_type, entity_id)
    title = targets.display_title
    payload = _payload_dict(job)
    payload["display_title"] = title
    job.payload = payload
    session.add(job)
    session.commit()

    def _step(step: str) -> None:
        _set_job_step(session, job, step, arr_label=targets.arr_label)
        _log_reconcile_info(entity_type, title, _step_log_detail(step, arr_label=targets.arr_label), job_id=int(job.id))

    stop_v = start_verbose_stall_heartbeat(
        f"Library reconcile · {_entity_kind_label(entity_type)} · {title} · job {job.id}",
    )
    refresh_paths: set[str] = set()
    placeholders: list[Placeholder] = []
    try:
        _log_reconcile_info(entity_type, title, "Started scan & refresh", job_id=int(job.id))
        if targets.arr_kind == "radarr":
            _step("arr_refresh")
            refresh_cmd = trigger_refresh_movie(
                base_url=targets.base_url, api_key=targets.api_key, movie_id=targets.arr_id
            )
            _step("arr_wait_refresh")

            def _on_refresh_tick(_statuses: dict[int, str]) -> None:
                _set_job_step(session, job, "arr_wait_refresh", arr_label=targets.arr_label)

            wait_arr_commands(
                base_url=targets.base_url,
                api_key=targets.api_key,
                command_ids=[refresh_cmd],
                on_tick=_on_refresh_tick,
            )

            _step("sync_catalog")
            sync_radarr_movies_by_ids(
                [targets.arr_id],
                base_url=targets.base_url,
                api_key=targets.api_key,
                instance_key=targets.instance_key,
            )
        else:
            _step("arr_refresh")
            refresh_cmd = trigger_refresh_series(
                base_url=targets.base_url, api_key=targets.api_key, series_id=targets.arr_id
            )
            _step("arr_wait_refresh")
            wait_arr_commands(
                base_url=targets.base_url,
                api_key=targets.api_key,
                command_ids=[refresh_cmd],
            )
            _step("sync_catalog")
            sync_sonarr_series_by_ids(
                [targets.arr_id],
                base_url=targets.base_url,
                api_key=targets.api_key,
                instance_key=targets.instance_key,
            )
            if entity_type == "series":
                targets = _ReconcileTargets(
                    entity_type=targets.entity_type,
                    entity_id=targets.entity_id,
                    display_title=targets.display_title,
                    arr_kind=targets.arr_kind,
                    arr_id=targets.arr_id,
                    base_url=targets.base_url,
                    api_key=targets.api_key,
                    instance_key=targets.instance_key,
                    movie_row_ids=[],
                    episode_row_ids=_episode_row_ids_for_series(session, entity_id),
                    arr_label=targets.arr_label,
                )

        # Tags may have changed during Arr refresh/sync; apply Never/Pinned before
        # determination so a concurrent lite sync cannot leave policy on Auto.
        try:
            from services.source_of_truth.tag_placeholder_policy import apply_arr_tag_placeholder_policies

            tag_movie_ids = list(targets.movie_row_ids or [])
            tag_series_ids: list[int] = []
            if targets.arr_kind == "sonarr":
                if entity_type == "series":
                    tag_series_ids = [int(entity_id)]
                elif entity_type == "episode":
                    ep = session.query(Episode).filter(Episode.id == int(entity_id)).first()
                    season = session.query(Season).get(ep.season_id) if ep and ep.season_id else None
                    if season and season.series_id is not None:
                        tag_series_ids = [int(season.series_id)]
            if tag_movie_ids or tag_series_ids:
                apply_arr_tag_placeholder_policies(
                    movie_row_ids=tag_movie_ids,
                    series_row_ids=tag_series_ids,
                )
        except Exception as tag_exc:
            _log_reconcile_warning(
                entity_type,
                title,
                f"Arr tag policy apply after catalog sync failed: {tag_exc}",
                job_id=int(job.id),
            )

        _step("filesystem")
        _refresh_placeholder_presence_for_entities(
            movie_row_ids=targets.movie_row_ids,
            episode_row_ids=targets.episode_row_ids,
        )

        scope_total = max(1, len(targets.movie_row_ids or []) + len(targets.episode_row_ids or []))
        _set_job_step(
            session,
            job,
            "determination",
            arr_label=targets.arr_label,
            phase_done=0,
            phase_total=scope_total,
        )
        _log_reconcile_info(
            entity_type,
            title,
            _step_log_detail("determination", arr_label=targets.arr_label),
            job_id=int(job.id),
        )
        run_determination_for_entities_with_siblings(
            movie_ids=targets.movie_row_ids or None,
            episode_ids=targets.episode_row_ids or None,
            log_subject=title,
        )
        _set_job_step(
            session,
            job,
            "determination",
            arr_label=targets.arr_label,
            phase_done=scope_total,
            phase_total=scope_total,
        )

        _set_job_step(
            session,
            job,
            "materialization",
            arr_label=targets.arr_label,
            phase_done=0,
            phase_total=scope_total,
        )
        _log_reconcile_info(
            entity_type,
            title,
            _step_log_detail("materialization", arr_label=targets.arr_label),
            job_id=int(job.id),
        )
        run_materialization_for_entities(
            movie_ids=targets.movie_row_ids or None,
            episode_ids=targets.episode_row_ids or None,
            observation_source="entity_reconcile",
            log_subject=title,
        )
        _set_job_step(
            session,
            job,
            "materialization",
            arr_label=targets.arr_label,
            phase_done=scope_total,
            phase_total=scope_total,
        )

        placeholders = _placeholders_for_scope(
            session,
            movie_row_ids=targets.movie_row_ids,
            episode_row_ids=targets.episode_row_ids,
        )
        sidecar_total = max(1, len(placeholders))
        _set_job_step(
            session,
            job,
            "sidecars",
            arr_label=targets.arr_label,
            phase_done=0,
            phase_total=sidecar_total,
        )
        _log_reconcile_info(
            entity_type,
            title,
            _step_log_detail("sidecars", arr_label=targets.arr_label),
            job_id=int(job.id),
        )

        def _on_sidecar_progress(done: int, total: int) -> None:
            _set_job_step(
                session,
                job,
                "sidecars",
                arr_label=targets.arr_label,
                phase_done=int(done),
                phase_total=max(1, int(total)),
            )

        sidecar = _run_sidecars_and_collect_paths(
            session,
            placeholders,
            entity_type=entity_type,
            display_title=title,
            job_id=int(job.id),
            on_progress=_on_sidecar_progress,
        )
        refresh_paths = sidecar.refresh_paths

        _step("player")
        if refresh_paths:
            _log_reconcile_info(
                entity_type,
                title,
                f"Scanning {len(refresh_paths)} library folder(s) on media servers",
                job_id=int(job.id),
            )
            refresh_all_paths(
                refresh_paths,
                update_type="Modified",
                include_plex=True,
                include_jellyfin=True,
                include_emby=True,
            )
        # Path scans do not update Plex posters; match bulk art refresh.
        if sidecar.rewritten_movies or sidecar.rewritten_series:
            try:
                _log_reconcile_info(
                    entity_type,
                    title,
                    "Refreshing player posters for rewritten art",
                    job_id=int(job.id),
                )
                refresh_players_after_local_art_rewrite(
                    movies=sidecar.rewritten_movies,
                    series_rows=sidecar.rewritten_series,
                )
            except Exception as exc:
                _log_reconcile_warning(
                    entity_type,
                    title,
                    f"Player poster refresh after art rewrite failed: {exc}",
                    job_id=int(job.id),
                )

        _set_job_step(session, job, STEP_DONE, arr_label=targets.arr_label)
        folder_word = "folder" if len(refresh_paths) == 1 else "folders"
        ph_word = "placeholder" if len(placeholders) == 1 else "placeholders"
        _log_reconcile_success(
            entity_type,
            title,
            f"Finished · {len(placeholders)} {ph_word} · {len(refresh_paths)} library {folder_word} refreshed",
            job_id=int(job.id),
        )
        return {"ok": True}
    except ArrCommandError as exc:
        _log_reconcile_warning(
            entity_type,
            title,
            f"{targets.arr_label} step failed: {exc}",
            job_id=int(job.id),
        )
        raise ValueError(str(exc)) from exc
    finally:
        stop_v.set()


__all__ = [
    "ENTITY_RECONCILE_JOB_TYPE",
    "enqueue_entity_reconcile",
    "process_entity_reconcile_job",
    "reconcile_job_status_view",
]
