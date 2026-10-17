"""Batched placeholder local art refresh (decoupled from NFO writes)."""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from core.logger import logger
from services.media_servers.refresh import (
    refresh_all_paths,
    refresh_all_sections,
    refresh_players_after_local_art_rewrite,
)
from services.placeholder_poster_art import (
    ensure_episode_still_art,
    ensure_movie_art,
    ensure_series_art,
    episode_still_art_needs_refresh,
    movie_art_needs_refresh,
    series_art_needs_refresh,
)
from services.postgres.db import get_session
from services.postgres.models import Episode, Job, Movie, Placeholder, Season, Series
from services.source_of_truth.idempotency import (
    placeholder_art_refresh_idempotency_key,
    try_claim_processed_key,
)
from services.source_of_truth.placeholder_job_enqueue import enqueue_batched_placeholder_job
from services.task_run_phases import FULL_SYNC_TASK_RUN_ID_KEY


PLACEHOLDER_ART_REFRESH_JOB_TYPE = "placeholder_art_refresh"
ART_BACKFILL_RUN_ID_KEY = "art_backfill_run_id"
ART_BACKFILL_REFRESH_ON_COMPLETION_KEY = "art_backfill_refresh_on_completion"
ART_BACKFILL_TASK_RUN_ID_KEY = "art_backfill_task_run_id"

# Narrow settings toggles: path-scoped media refresh only (no full-library metadata scan).
NARROW_ART_SCOPES = frozenset({"coming_soon", "tv_new_ep", "coming_soon_and_tv"})


def art_scope_uses_section_completion_refresh(art_scope: str | None) -> bool:
    """Full overlay/language backfills use section refresh; banner toggles use path refresh."""
    scope = str(art_scope or "all").strip().lower() or "all"
    return scope not in NARROW_ART_SCOPES


def collect_active_placeholder_ids(session) -> list[int]:
    rows = (
        session.query(Placeholder.id)
        .filter(Placeholder.has_placeholder == True)  # noqa: E712
        .order_by(Placeholder.id.asc())
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def collect_coming_soon_placeholder_ids(session) -> list[int]:
    """Active placeholders currently in a Coming Soon display status (movies + episodes)."""
    from services.source_of_truth.calendar_phase import _COMING_SOON_STATUS_VALUES

    rows = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.has_placeholder == True,  # noqa: E712
            Placeholder.display_status.in_(_COMING_SOON_STATUS_VALUES),
        )
        .order_by(Placeholder.id.asc())
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def collect_tv_placeholder_ids(session) -> list[int]:
    """One active placeholder per series for series/season poster refreshes (New Ep. banner)."""
    seen_series: set[int] = set()
    out: list[int] = []

    stub_rows = (
        session.query(Placeholder.id, Placeholder.series_id)
        .filter(
            Placeholder.has_placeholder == True,  # noqa: E712
            Placeholder.series_id.isnot(None),
        )
        .order_by(Placeholder.id.asc())
        .all()
    )
    for pid, sid in stub_rows:
        if sid is None:
            continue
        sid_i = int(sid)
        if sid_i in seen_series:
            continue
        seen_series.add(sid_i)
        out.append(int(pid))

    ep_rows = (
        session.query(Placeholder.id, Season.series_id)
        .join(Episode, Episode.id == Placeholder.episode_id)
        .join(Season, Season.id == Episode.season_id)
        .filter(
            Placeholder.has_placeholder == True,  # noqa: E712
            Placeholder.episode_id.isnot(None),
        )
        .order_by(Placeholder.id.asc())
        .all()
    )
    for pid, sid in ep_rows:
        if sid is None:
            continue
        sid_i = int(sid)
        if sid_i in seen_series:
            continue
        seen_series.add(sid_i)
        out.append(int(pid))

    return sorted(out)


def art_scope_writes_episode_stills(art_scope: str | None, display_status: str | None) -> bool:
    """Whether a narrow art refresh should rewrite episode thumbs.

    ``tv_new_ep`` only updates series/season posters. ``coming_soon_and_tv`` stills
    only for Coming Soon episode rows; other scopes rewrite stills as usual.
    """
    scope = str(art_scope or "all").strip().lower() or "all"
    if scope == "tv_new_ep":
        return False
    if scope == "coming_soon_and_tv":
        from services.source_of_truth.calendar_phase import _is_coming_soon_status

        return _is_coming_soon_status(display_status)
    return True


def collect_placeholder_ids_for_art_scope(session, art_scope: str | None) -> list[int]:
    """Resolve placeholder ids for a narrow or full art refresh scope."""
    scope = str(art_scope or "all").strip().lower() or "all"
    if scope == "coming_soon":
        return collect_coming_soon_placeholder_ids(session)
    if scope == "tv_new_ep":
        return collect_tv_placeholder_ids(session)
    if scope == "coming_soon_and_tv":
        return sorted(
            set(collect_coming_soon_placeholder_ids(session)) | set(collect_tv_placeholder_ids(session))
        )
    return collect_active_placeholder_ids(session)


def collect_active_placeholder_ids_for_movies(session, movie_ids: list[int] | set[int]) -> list[int]:
    """Active placeholder ids linked to the given movie ids."""
    ids = sorted({int(mid) for mid in (movie_ids or []) if mid is not None})
    if not ids:
        return []
    rows = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.has_placeholder == True,  # noqa: E712
            Placeholder.movie_id.in_(ids),
        )
        .order_by(Placeholder.id.asc())
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def collect_active_placeholder_ids_for_episodes(session, episode_ids: list[int] | set[int]) -> list[int]:
    """Active placeholder ids linked to the given episode ids."""
    ids = sorted({int(eid) for eid in (episode_ids or []) if eid is not None})
    if not ids:
        return []
    rows = (
        session.query(Placeholder.id)
        .filter(
            Placeholder.has_placeholder == True,  # noqa: E712
            Placeholder.episode_id.in_(ids),
        )
        .order_by(Placeholder.id.asc())
        .all()
    )
    return [int(r[0]) for r in rows if r and r[0] is not None]


def collect_tv_placeholder_ids_for_series(session, series_ids: list[int] | set[int]) -> list[int]:
    """One active placeholder per series id (for series/season New Ep. posters)."""
    wanted = {int(sid) for sid in (series_ids or []) if sid is not None}
    if not wanted:
        return []
    seen_series: set[int] = set()
    out: list[int] = []

    stub_rows = (
        session.query(Placeholder.id, Placeholder.series_id)
        .filter(
            Placeholder.has_placeholder == True,  # noqa: E712
            Placeholder.series_id.in_(sorted(wanted)),
        )
        .order_by(Placeholder.id.asc())
        .all()
    )
    for pid, sid in stub_rows:
        if sid is None:
            continue
        sid_i = int(sid)
        if sid_i not in wanted or sid_i in seen_series:
            continue
        seen_series.add(sid_i)
        out.append(int(pid))

    remaining = wanted - seen_series
    if remaining:
        ep_rows = (
            session.query(Placeholder.id, Season.series_id)
            .join(Episode, Episode.id == Placeholder.episode_id)
            .join(Season, Season.id == Episode.season_id)
            .filter(
                Placeholder.has_placeholder == True,  # noqa: E712
                Placeholder.episode_id.isnot(None),
                Season.series_id.in_(sorted(remaining)),
            )
            .order_by(Placeholder.id.asc())
            .all()
        )
        for pid, sid in ep_rows:
            if sid is None:
                continue
            sid_i = int(sid)
            if sid_i not in remaining or sid_i in seen_series:
                continue
            seen_series.add(sid_i)
            out.append(int(pid))

    return sorted(out)


def placeholder_art_needs_refresh(session, placeholder: Placeholder, *, art_scope: str | None = "all") -> bool:
    """Desired overlay/source vs on-disk sidecar; True when a rewrite is needed."""
    if not bool(getattr(placeholder, "has_placeholder", False)):
        return False

    scope = str(art_scope or "all").strip().lower() or "all"

    movie = session.query(Movie).get(placeholder.movie_id) if placeholder.movie_id else None
    if movie and bool(getattr(movie, "has_placeholder", False)):
        path = str(
            getattr(placeholder, "path", "") or getattr(movie, "placeholder_filepath", "") or ""
        ).strip()
        if not path:
            return False
        return movie_art_needs_refresh(movie, path)

    episode = session.query(Episode).get(placeholder.episode_id) if placeholder.episode_id else None
    series = None
    season = None
    path = str(getattr(placeholder, "path", "") or "").strip()
    if episode and bool(getattr(episode, "has_placeholder", False)):
        season = session.query(Season).get(episode.season_id) if episode.season_id else None
        series = session.query(Series).get(season.series_id) if season and season.series_id else None
        if not path:
            path = str(getattr(episode, "placeholder_filepath", "") or "").strip()
    elif getattr(placeholder, "series_id", None):
        series = session.query(Series).get(placeholder.series_id)
        if getattr(placeholder, "season_id", None):
            season = session.query(Season).get(placeholder.season_id)

    if not series:
        return False

    series_folder = getattr(series, "placeholder_folder", None) or (
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(path)))) if path else None
    )
    if series_art_needs_refresh(series, series_folder=series_folder):
        return True

    if not episode or not season or not path:
        return False
    if not art_scope_writes_episode_stills(scope, getattr(placeholder, "display_status", None)):
        return False
    return episode_still_art_needs_refresh(episode, season, series, path)


def determine_art_refresh_ids(
    session,
    placeholder_ids: list[int],
    *,
    art_scope: str | None = "all",
) -> list[int]:
    """Return placeholder ids whose local art sidecar does not match desired overlay."""
    from services.source_of_truth.placeholder_job_enqueue import normalize_placeholder_ids

    ids = normalize_placeholder_ids(placeholder_ids)
    if not ids:
        return []
    rows = (
        session.query(Placeholder)
        .filter(Placeholder.id.in_(ids), Placeholder.has_placeholder == True)  # noqa: E712
        .order_by(Placeholder.id.asc())
        .all()
    )
    by_id = {int(ph.id): ph for ph in rows if getattr(ph, "id", None) is not None}
    out: list[int] = []
    for pid in ids:
        ph = by_id.get(int(pid))
        if ph is None:
            continue
        try:
            if placeholder_art_needs_refresh(session, ph, art_scope=art_scope):
                out.append(int(pid))
        except Exception as exc:
            logger.debug(
                f"art determination failed placeholder_id={pid}: {exc}",
                extra={"emoji_type": "debug"},
            )
            # Fail open: enqueue so write-time skip can still decide.
            out.append(int(pid))
    return out


def enqueue_placeholder_art_refresh(
    placeholder_ids: list[int],
    session=None,
    *,
    merge_into_pending: bool = True,
    payload_extras: dict[str, Any] | None = None,
    skip_determination: bool = False,
) -> dict:
    owns_session = session is None
    session = session or get_session()
    try:
        art_scope = "all"
        if isinstance(payload_extras, dict):
            art_scope = str(payload_extras.get("art_scope") or "all").strip().lower() or "all"

        ids = list(placeholder_ids or [])
        skipped = 0
        if not skip_determination and ids:
            needed = determine_art_refresh_ids(session, ids, art_scope=art_scope)
            skipped = max(0, len({int(x) for x in ids if x is not None}) - len(needed))
            ids = needed

        if not ids:
            return {
                "ok": True,
                "placeholder_count": 0,
                "jobs_created": 0,
                "jobs_updated": 0,
                "skipped_determination": skipped,
                "enqueued": False,
                "reason": "no_art_mismatch",
            }

        out = enqueue_batched_placeholder_job(
            PLACEHOLDER_ART_REFRESH_JOB_TYPE,
            ids,
            session,
            merge_into_pending=merge_into_pending,
            payload_extras=payload_extras,
            include_player_metadata_refresh=False,
        )
        if isinstance(out, dict):
            out["skipped_determination"] = skipped
        return out
    except Exception as exc:
        logger.error(f"Failed to enqueue placeholder art refresh: {exc}", exc_info=True)
        session.rollback()
        return {"ok": False, "reason": str(exc)}
    finally:
        if owns_session:
            try:
                session.close()
            except Exception:
                pass


def enqueue_art_refresh_for_calendar_date_changes(
    session,
    *,
    movie_ids: list[int] | set[int] | None = None,
    episode_ids: list[int] | set[int] | None = None,
    series_ids: list[int] | set[int] | None = None,
) -> dict:
    """Enqueue art for placeholders affected by calendar date / New Ep. next-air changes."""
    ph_ids: list[int] = []
    ph_ids.extend(collect_active_placeholder_ids_for_movies(session, list(movie_ids or [])))
    ph_ids.extend(collect_active_placeholder_ids_for_episodes(session, list(episode_ids or [])))
    ph_ids.extend(collect_tv_placeholder_ids_for_series(session, list(series_ids or [])))
    # Dedupe while preserving order
    seen: set[int] = set()
    unique: list[int] = []
    for pid in ph_ids:
        if pid in seen:
            continue
        seen.add(pid)
        unique.append(pid)
    if not unique:
        return {"ok": True, "placeholder_count": 0, "enqueued": False, "reason": "no_placeholders"}
    return enqueue_placeholder_art_refresh(unique, session=session)


def enqueue_placeholder_art_backfill_all(
    *,
    source: str = "full_sync",
    task_run_id: int | None = None,
    placeholder_refresh_task_run_id: int | None = None,
    coming_soon_only: bool = False,
    art_scope: str | None = None,
    skip_determination: bool = False,
) -> dict:
    """Queue art refresh for active placeholders.

    ``art_scope``: ``all`` | ``coming_soon`` | ``tv_new_ep`` | ``coming_soon_and_tv``.
    Legacy ``coming_soon_only=True`` maps to ``coming_soon``.

    Full-library scopes finish with a section metadata refresh. Narrow banner
    toggles use per-batch path refresh only (no whole-library Plex metadata scan).

    ``skip_determination``: enqueue every scoped id without scanning overlay sidecars
    first (used for settings Apply now so batches exist immediately).
    """
    scope = str(art_scope or "").strip().lower()
    if not scope:
        scope = "coming_soon" if coming_soon_only else "all"
    session = get_session()
    try:
        ids = collect_placeholder_ids_for_art_scope(session, scope)
        if not ids:
            return {
                "ok": True,
                "placeholder_count": 0,
                "enqueued": False,
                "source": source,
                "art_scope": scope,
                "coming_soon_only": scope == "coming_soon",
            }
        run_id = uuid.uuid4().hex
        section_completion = art_scope_uses_section_completion_refresh(scope)
        extras: dict[str, Any] = {
            ART_BACKFILL_RUN_ID_KEY: run_id,
            ART_BACKFILL_REFRESH_ON_COMPLETION_KEY: section_completion,
            "art_backfill_source": source,
            "art_scope": scope,
            "coming_soon_only": scope == "coming_soon",
        }
        if task_run_id is not None:
            tid = int(task_run_id)
            extras[FULL_SYNC_TASK_RUN_ID_KEY] = tid
            extras[ART_BACKFILL_TASK_RUN_ID_KEY] = tid
        if placeholder_refresh_task_run_id is not None:
            from services.source_of_truth.placeholder_refresh import PLACEHOLDER_REFRESH_TASK_RUN_ID_KEY

            extras[PLACEHOLDER_REFRESH_TASK_RUN_ID_KEY] = int(placeholder_refresh_task_run_id)
        out = enqueue_placeholder_art_refresh(
            ids,
            session=session,
            merge_into_pending=False,
            payload_extras=extras,
            skip_determination=bool(skip_determination),
        )
        batch_count = int(out.get("jobs_created") or 0)
        phase_task_run_id = task_run_id if task_run_id is not None else placeholder_refresh_task_run_id
        # Only mark the art phase once batches exist. Calling begin with 0 batches
        # (or before enqueue finishes) races Tasks reconcile into a false "done".
        if phase_task_run_id is not None and out.get("ok") and batch_count > 0:
            from services.task_run_phases import begin_art_backfill_phase

            queued_count = out.get("placeholder_count")
            if queued_count is None:
                queued_count = len(ids)
            begin_art_backfill_phase(
                int(phase_task_run_id),
                art_run_id=run_id,
                placeholder_count=int(queued_count),
                batch_count=batch_count,
                source=source,
            )
        out["art_backfill_run_id"] = run_id
        out["batch_count"] = batch_count
        out["enqueued"] = bool(out.get("ok")) and batch_count > 0
        out["art_scope"] = scope
        out["coming_soon_only"] = scope == "coming_soon"
        return out
    finally:
        session.close()


def _art_refresh_pending_same_run(session, job: Job, *, run_id: str) -> bool:
    job_id = int(job.id)
    pending_same_run = (
        session.query(Job.id)
        .filter(
            Job.job_type == PLACEHOLDER_ART_REFRESH_JOB_TYPE,
            Job.status.in_(["PENDING", "CLAIMED", "WORKING"]),
            Job.id != job_id,
            Job.payload[ART_BACKFILL_RUN_ID_KEY].as_string() == run_id,
        )
        .first()
    )
    return bool(pending_same_run)


def _art_refresh_bust_ui_library_cache(*, run_id: str, log_prefix: str) -> None:
    try:
        from services.library_poster_paths import invalidate_library_poster_cache
        from services.postgres.db import get_session
        from services.series_episode_stats_hooks import bump_library_versions_after_bulk

        invalidate_library_poster_cache()
        bump_session = get_session()
        try:
            bump_library_versions_after_bulk(bump_session, movies=True, series=True)
            bump_session.commit()
        finally:
            bump_session.close()
    except Exception as exc:
        logger.warning(
            f"{log_prefix} UI library cache bust failed run_id={run_id}: {exc}",
            extra={"emoji_type": "warning"},
        )


def _art_refresh_ui_bust_if_last_batch(
    session,
    job: Job,
    *,
    run_id: str,
    log_prefix: str = "Placeholder art backfill",
) -> None:
    """For path-scoped narrow runs: bust Placeholdarr UI cache when the last batch finishes."""
    if _art_refresh_pending_same_run(session, job, run_id=run_id):
        return
    _art_refresh_bust_ui_library_cache(run_id=run_id, log_prefix=log_prefix)


def _art_refresh_completion_scan_if_last_batch(
    session,
    job: Job,
    *,
    run_id: str,
    log_prefix: str = "Placeholder art backfill",
) -> None:
    # Capture before commit/close: expire_on_commit would detach Job and make
    # later ``job.id`` access raise DetachedInstanceError (fails the whole batch).
    job_id = int(job.id)
    if _art_refresh_pending_same_run(session, job, run_id=run_id):
        return

    task_run_id = None
    try:
        payload = job.payload if isinstance(job.payload, dict) else {}
        raw_tid = (
            payload.get(FULL_SYNC_TASK_RUN_ID_KEY)
            or payload.get(ART_BACKFILL_TASK_RUN_ID_KEY)
            or payload.get("placeholder_refresh_task_run_id")
        )
        if raw_tid is not None:
            task_run_id = int(raw_tid)
    except Exception:
        task_run_id = None

    # Close the worker DB session before long media-server HTTP (same pattern as nfo_refresh).
    try:
        session.commit()
    except Exception:
        session.rollback()
    try:
        session.close()
    except Exception:
        pass

    _art_refresh_bust_ui_library_cache(run_id=run_id, log_prefix=log_prefix)

    # Mark the full-sync task DONE before Plex/JF/Emby refresh so the Tasks UI does not
    # sit on WORKING for tens of minutes while library scans run.
    if task_run_id:
        from services.task_run_phases import finalize_art_backfill_phase

        finalize_art_backfill_phase(task_run_id, run_id, exclude_job_id=job_id)

    try:
        refresh_stats = refresh_all_sections(
            has_movies=True,
            has_episodes=True,
            plex_force_metadata_refresh=True,
        )
        logger.info(
            f"{log_prefix} run complete; triggered library section refresh "
            f"run_id={run_id} refreshed={int(refresh_stats.get('refreshed', 0) or 0)} "
            f"failed={int(refresh_stats.get('failed', 0) or 0)}",
            extra={"emoji_type": "success"},
        )
    except Exception as exc:
        logger.warning(
            f"{log_prefix} completion refresh failed run_id={run_id}: {exc}",
            extra={"emoji_type": "warning"},
        )


def process_placeholder_art_refresh_job(session, job: Job) -> dict:
    payload = job.payload if isinstance(job.payload, dict) else {}
    placeholder_ids = payload.get("placeholder_ids") or []
    ids = [int(pid) for pid in placeholder_ids if pid is not None]
    if not ids:
        return {"ok": False, "reason": "no_placeholder_ids"}

    try:
        job_id_val = getattr(job, "id", None)
        if isinstance(job_id_val, int):
            idem_key = placeholder_art_refresh_idempotency_key(job_id=int(job_id_val))
            if not try_claim_processed_key(session, idem_key, job_type=PLACEHOLDER_ART_REFRESH_JOB_TYPE):
                logger.info(
                    f"placeholder_art_refresh job_id={job_id_val} skipped — already processed",
                    extra={"emoji_type": "info"},
                )
                return {"ok": True, "skipped": "already_processed"}
    except Exception as exc:
        logger.debug(f"placeholder_art_refresh idempotency skipped: {exc}", extra={"emoji_type": "debug"})

    from services.task_run_phases import accumulate_art_backfill_counts, empty_art_counts

    art_scope = str(payload.get("art_scope") or "all").strip().lower() or "all"
    placeholders = session.query(Placeholder).filter(Placeholder.id.in_(ids)).all()
    n_total = len(placeholders)
    wrote = 0
    batch_counts = empty_art_counts()
    refresh_paths: set[str] = set()
    rewritten_movies: list = []
    rewritten_series: list = []
    processed_movies: set[int] = set()
    processed_series: set[int] = set()
    loop_pos = [0]
    started = time.monotonic()
    stop_hb = threading.Event()

    def _heartbeat():
        while not stop_hb.wait(10.0):
            logger.verbose(
                f"placeholder_art_refresh job_id={job.id}: still running "
                f"progress={loop_pos[0]}/{n_total} wrote={wrote} "
                f"elapsed_s={time.monotonic() - started:.1f}",
                extra={"emoji_type": "processing"},
            )

    hb_thread = None
    if n_total:
        hb_thread = threading.Thread(
            target=_heartbeat,
            name=f"art-refresh-hb-{job.id}",
            daemon=True,
        )
        hb_thread.start()

    try:
        for placeholder in placeholders:
            loop_pos[0] += 1
            if not bool(getattr(placeholder, "has_placeholder", False)):
                continue

            movie = session.query(Movie).get(placeholder.movie_id) if placeholder.movie_id else None
            episode = session.query(Episode).get(placeholder.episode_id) if placeholder.episode_id else None

            if movie and bool(getattr(movie, "has_placeholder", False)):
                mid = int(movie.id)
                path = str(getattr(placeholder, "path", "") or getattr(movie, "placeholder_filepath", "") or "").strip()
                if path and mid not in processed_movies:
                    processed_movies.add(mid)
                    result = ensure_movie_art(movie, path)
                    if result.wrote_any:
                        wrote += 1
                        rewritten_movies.append(movie)
                        for k, v in result.art_counts.items():
                            batch_counts[k] = int(batch_counts.get(k, 0)) + int(v)
                        refresh_paths.add(os.path.dirname(os.path.abspath(path)))
                continue

            series = None
            season = None
            path = str(getattr(placeholder, "path", "") or "").strip()
            if episode and bool(getattr(episode, "has_placeholder", False)):
                season = session.query(Season).get(episode.season_id) if episode.season_id else None
                series = session.query(Series).get(season.series_id) if season and season.series_id else None
                if not path:
                    path = str(getattr(episode, "placeholder_filepath", "") or "").strip()
            elif getattr(placeholder, "series_id", None):
                series = session.query(Series).get(placeholder.series_id)
                if getattr(placeholder, "season_id", None):
                    season = session.query(Season).get(placeholder.season_id)

            if not series:
                continue

            sid = int(series.id)
            series_folder = getattr(series, "placeholder_folder", None) or (
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(path)))) if path else None
            )
            if sid not in processed_series:
                processed_series.add(sid)
                series_result = ensure_series_art(series, series_folder=series_folder)
                if series_result.wrote_any:
                    wrote += 1
                    rewritten_series.append(series)
                    for k, v in series_result.art_counts.items():
                        batch_counts[k] = int(batch_counts.get(k, 0)) + int(v)
                    if series_folder:
                        refresh_paths.add(os.path.abspath(series_folder))

            if not episode or not season or not path:
                continue
            if not art_scope_writes_episode_stills(art_scope, getattr(placeholder, "display_status", None)):
                continue

            still_result = ensure_episode_still_art(episode, season, series, path)
            if still_result.wrote_any:
                wrote += 1
                for k, v in still_result.art_counts.items():
                    batch_counts[k] = int(batch_counts.get(k, 0)) + int(v)
                refresh_paths.add(os.path.dirname(os.path.abspath(path)))
                # Episode thumbs live under the show; refresh series art agents too.
                if series not in rewritten_series:
                    rewritten_series.append(series)
    finally:
        stop_hb.set()

    bulk_completion = bool(payload.get(ART_BACKFILL_REFRESH_ON_COMPLETION_KEY))
    run_id = str(payload.get(ART_BACKFILL_RUN_ID_KEY) or "").strip()
    task_run_id_raw = (
        payload.get(FULL_SYNC_TASK_RUN_ID_KEY)
        or payload.get(ART_BACKFILL_TASK_RUN_ID_KEY)
        or payload.get("placeholder_refresh_task_run_id")
    )
    if task_run_id_raw is not None and run_id:
        try:
            accumulate_art_backfill_counts(
                int(task_run_id_raw),
                run_id,
                batch_counts,
                exclude_job_id=int(job.id),
            )
        except Exception as exc:
            logger.debug(f"art backfill phase stats update skipped: {exc}", extra={"emoji_type": "debug"})

    try:
        session.commit()
    except Exception:
        pass

    if refresh_paths and not bulk_completion:
        try:
            refresh_all_paths(
                refresh_paths,
                update_type="Modified",
                include_plex=True,
                include_jellyfin=True,
                include_emby=True,
            )
        except Exception as exc:
            logger.warning(f"placeholder_art_refresh path refresh failed: {exc}", extra={"emoji_type": "warning"})

    # Path scans do not update Plex posters; refresh rewritten titles at item level.
    if (rewritten_movies or rewritten_series) and not bulk_completion:
        try:
            refresh_players_after_local_art_rewrite(
                movies=rewritten_movies,
                series_rows=rewritten_series,
            )
        except Exception as exc:
            logger.warning(
                f"placeholder_art_refresh player item refresh failed: {exc}",
                extra={"emoji_type": "warning"},
            )

    if bulk_completion and run_id:
        _art_refresh_completion_scan_if_last_batch(session, job, run_id=run_id)
    elif run_id and not bulk_completion:
        # Narrow banner toggles: path refresh above; bust UI cache on the last batch.
        _art_refresh_ui_bust_if_last_batch(session, job, run_id=run_id)

    return {"ok": True, "wrote": wrote, "scanned": len(placeholders), "series_count": len(processed_series)}
