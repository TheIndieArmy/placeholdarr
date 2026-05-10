from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from core.config import settings
from core.logger import logger
from sqlalchemy.orm.attributes import flag_modified

from services.messages import render as render_message
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Placeholder
from services.activity_snapshot import clear_queue_download_snapshot, set_queue_download_snapshot
from services.source_of_truth.arr_api import (
    trigger_radarr_refresh_monitored_downloads,
    trigger_sonarr_refresh_monitored_downloads,
)
from services.source_of_truth.status_intent import DisplayStatus, StatusIntent, StatusSource
from services.source_of_truth.status_orchestrator import StatusOrchestrator


ACTIVE_QUEUE_STATUSES = {
    DisplayStatus.SEARCHING.value,
    DisplayStatus.DOWNLOADING.value,
    DisplayStatus.IMPORT_IN_PROGRESS.value,
    "RETRYING",
    # NOT_FOUND is terminal for queue-monitor purposes; do not keep polling/nudging ARR.
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _expire_stale_queue_monitor_flags(session) -> None:
    """FM-15 mitigation: clear playback queue-monitor flags older than 24 hours."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        session.query(Placeholder).filter(
            Placeholder.queue_monitor_active == True,  # noqa: E712
            Placeholder.queue_monitor_active_set_at.isnot(None),
            Placeholder.queue_monitor_active_set_at < cutoff,
        ).update(
            {
                Placeholder.queue_monitor_active: False,
                Placeholder.queue_monitor_active_set_at: None,
            },
            synchronize_session=False,
        )
    except Exception:
        pass


def _should_emit_wait_log(qm: dict[str, Any], key: str, now: datetime, *, period_seconds: int = 60) -> bool:
    """Rate-limit wait-state logs by storing last-emitted timestamp in placeholder.extra."""
    last = _from_iso(qm.get(key))
    if last is None or (now - last).total_seconds() >= float(max(1, period_seconds)):
        qm[key] = _to_iso(now)
        return True
    return False


def _collect_queue_monitor_poll_context(session):
    """Shared scan: active queue-like placeholders that map to Radarr/Sonarr queue polling.

    Returns None when there is nothing to poll or nudge. Otherwise a dict with
    movie_targets, episode_targets, and per-instance needs_* flags.

    Only placeholders with ``queue_monitor_active`` set (playback-initiated ARR
    search) are considered; the producer sleeps idle until NOTIFY otherwise.
    """
    q = session.query(Placeholder).filter(
        Placeholder.has_placeholder == True,  # noqa: E712
        Placeholder.display_status.in_(ACTIVE_QUEUE_STATUSES),
        Placeholder.queue_monitor_active == True,  # noqa: E712
    )

    placeholders = q.all()
    if not placeholders:
        return None

    needs_radarr_std = False
    needs_radarr_4k = False
    needs_sonarr_std = False
    needs_sonarr_4k = False

    movie_ids = [int(ph.movie_id) for ph in placeholders if getattr(ph, "movie_id", None)]
    episode_ids = [int(ph.episode_id) for ph in placeholders if getattr(ph, "episode_id", None)]

    movie_map = {
        int(row.id): row
        for row in session.query(Movie).filter(Movie.id.in_(movie_ids)).all()
    } if movie_ids else {}
    episode_map = {
        int(row.id): row
        for row in session.query(Episode).filter(Episode.id.in_(episode_ids)).all()
    } if episode_ids else {}

    movie_targets: list[tuple[Placeholder, Movie, bool]] = []
    episode_targets: list[tuple[Placeholder, Episode, bool]] = []

    for ph in placeholders:
        movie = movie_map.get(int(ph.movie_id)) if getattr(ph, "movie_id", None) else None
        episode = episode_map.get(int(ph.episode_id)) if getattr(ph, "episode_id", None) else None

        if movie is not None:
            if bool(getattr(movie, "has_file", False)):
                continue
            radarrid = getattr(movie, "radarrid", None)
            if not radarrid:
                continue
            is_4k = bool(getattr(movie, "is_4k", False))
            movie_targets.append((ph, movie, is_4k))
            if is_4k:
                needs_radarr_4k = True
            else:
                needs_radarr_std = True
            continue

        if episode is not None:
            if bool(getattr(episode, "has_file", False)):
                continue
            sonarrid = getattr(episode, "sonarrid", None)
            if not sonarrid:
                continue
            season = getattr(episode, "season", None)
            series = getattr(season, "series", None) if season else None
            is_4k = bool(getattr(series, "is_4k", False)) if series is not None else False
            episode_targets.append((ph, episode, is_4k))
            if is_4k:
                needs_sonarr_4k = True
            else:
                needs_sonarr_std = True

    if not movie_targets and not episode_targets:
        return None

    return {
        "placeholders": placeholders,
        "movie_targets": movie_targets,
        "episode_targets": episode_targets,
        "needs_radarr_std": needs_radarr_std,
        "needs_radarr_4k": needs_radarr_4k,
        "needs_sonarr_std": needs_sonarr_std,
        "needs_sonarr_4k": needs_sonarr_4k,
    }


def _queue_item_percent(queue_item: dict[str, Any] | None) -> int:
    if not isinstance(queue_item, dict):
        return 0
    try:
        size_left = float(queue_item.get("sizeleft", queue_item.get("sizeLeft", 0)) or 0)
        size_total = float(queue_item.get("size", queue_item.get("totalSize", 0)) or 0)
        if size_total <= 0:
            return 0
        return int(max(0.0, min(100.0, 100.0 - ((size_left / size_total) * 100.0))))
    except Exception:
        return 0


def _publish_queue_activity_snapshot(
    session,
    movie_targets: list[tuple[Placeholder, Movie, bool]],
    episode_targets: list[tuple[Placeholder, Episode, bool]],
    radarr_std_map: dict[str, dict[str, Any]],
    radarr_4k_map: dict[str, dict[str, Any]],
    sonarr_std_map: dict[str, dict[str, Any]],
    sonarr_4k_map: dict[str, dict[str, Any]],
) -> None:
    """Publish a batched view of titles the queue monitor is tracking (for the activity page)."""
    ph_ids = [int(ph.id) for ph, _, _ in movie_targets] + [int(ph.id) for ph, _, _ in episode_targets]
    fresh: dict[int, Placeholder] = {}
    if ph_ids:
        fresh = {int(r.id): r for r in session.query(Placeholder).filter(Placeholder.id.in_(ph_ids)).all()}

    items: list[dict[str, Any]] = []
    for ph, movie, is_4k in movie_targets:
        ph2 = fresh.get(int(ph.id), ph)
        qm = radarr_4k_map if is_4k else radarr_std_map
        qi = qm.get(str(getattr(movie, "radarrid", "") or "")) or {}
        pct = _queue_item_percent(qi if isinstance(qi, dict) else None)
        status = str(getattr(ph2, "display_status", "") or "")
        reason = str(getattr(ph2, "display_reason", "") or "").strip()
        line = status
        if reason:
            line = f"{status} — {reason}" if status else reason
        if pct and status.upper() == "DOWNLOADING":
            line = f"{line} ({pct}%)" if line else f"{pct}%"
        items.append(
            {
                "kind": "movie",
                "title": str(getattr(movie, "title", "") or "Movie").strip() or "Movie",
                "subtitle": str(getattr(movie, "year", "") or ""),
                "instance": "4K" if is_4k else "HD",
                "line": line or "—",
                "arr_percent": pct or None,
            }
        )

    for ph, episode, is_4k in episode_targets:
        ph2 = fresh.get(int(ph.id), ph)
        season = getattr(episode, "season", None)
        series = getattr(season, "series", None) if season else None
        st = str(getattr(series, "title", "") or "").strip()
        sn = int(getattr(season, "season_number", 0) or 0) if season else 0
        en = int(getattr(episode, "episode_number", 0) or 0)
        et = str(getattr(episode, "title", "") or "").strip() or "Episode"
        subtitle = f"S{sn:02d}E{en:02d} — {et}"
        title = st or "TV"
        qm = sonarr_4k_map if is_4k else sonarr_std_map
        qi = qm.get(str(getattr(episode, "sonarrid", "") or "")) or {}
        pct = _queue_item_percent(qi if isinstance(qi, dict) else None)
        status = str(getattr(ph2, "display_status", "") or "")
        reason = str(getattr(ph2, "display_reason", "") or "").strip()
        line = status
        if reason:
            line = f"{status} — {reason}" if status else reason
        if pct and status.upper() == "DOWNLOADING":
            line = f"{line} ({pct}%)" if line else f"{pct}%"
        items.append(
            {
                "kind": "episode",
                "title": title,
                "subtitle": subtitle,
                "instance": "4K" if is_4k else "HD",
                "line": line or "—",
                "arr_percent": pct or None,
            }
        )

    set_queue_download_snapshot(items)


def _queue_endpoint(base_url: str) -> str:
    root = str(base_url or "").rstrip("/")
    if not root:
        return ""
    if "/api/" in root or root.endswith("/api"):
        return f"{root}/queue"
    return f"{root}/api/v3/queue"


def _safe_extra_dict(placeholder: Placeholder) -> dict[str, Any]:
    raw = getattr(placeholder, "extra", None)
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _queue_status_priority(status_value: Any, tracked_state_value: Any = None) -> int:
    """Rank queue records so active download states win over stale completed rows."""
    status = str(status_value or "").strip().lower()
    tracked_state = str(tracked_state_value or "").strip().lower()

    if status == "downloading":
        return 100
    if status in {"queued", "paused", "delay", "downloadclientunavailable", "fallback"}:
        return 90
    if status == "completed":
        if tracked_state in {"importpending", "importing", "importblocked", "failedpending"}:
            return 80
        return 70
    if status in {"warning", "error", "failed"}:
        return 60
    return 50


class QueueMonitorProducer:
    """DB-native queue monitor that infers retry/error after queue exit.

    Available transitions remain import-event-driven and are intentionally not
    implemented in this producer.

    Poll interval: ``QUEUE_MONITOR_POLL_INTERVAL_SECONDS`` when >0, else ``CHECK_INTERVAL``
    (see ``core.config.Settings`` comments — two names kept for backward compatibility).
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._qm_listen_registered = False
        self._thread: threading.Thread | None = None
        poll_override = int(getattr(settings, "QUEUE_MONITOR_POLL_INTERVAL_SECONDS", 0) or 0)
        self._poll_interval = max(
            1,
            poll_override if poll_override > 0 else int(getattr(settings, "CHECK_INTERVAL", 10) or 10),
        )
        self._retry_grace_seconds = max(30, int(getattr(settings, "QUEUE_MONITOR_RETRY_GRACE_SECONDS", 300) or 300))
        self._search_timeout_seconds = max(
            60,
            int(getattr(settings, "QUEUE_MONITOR_SEARCH_TIMEOUT_SECONDS", 120) or 120),
        )
        self._arr_refresh_interval = max(
            0,
            int(getattr(settings, "QUEUE_MONITOR_REFRESH_MONITORED_DOWNLOADS_INTERVAL_SECONDS", 0) or 0),
        )
        self._arr_refresh_stagger = max(
            0,
            int(getattr(settings, "QUEUE_MONITOR_REFRESH_STAGGER_SECONDS", 0) or 0),
        )
        self._notify_safety_seconds = float(
            max(30, int(getattr(settings, "QUEUE_MONITOR_NOTIFY_SAFETY_POLL_SECONDS", 300) or 300))
        )

    def _on_queue_monitor_notify(self, _payload: Any) -> None:
        self._wake_event.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        try:
            from services.postgres.notifier import QUEUE_MONITOR_CHANNEL, start_shared_notifier

            notifier = start_shared_notifier()
            notifier.listen(QUEUE_MONITOR_CHANNEL, self._on_queue_monitor_notify)
            self._qm_listen_registered = True
            logger.info(
                f"Queue monitor listening on NOTIFY channel '{QUEUE_MONITOR_CHANNEL}'",
                extra={"emoji_type": "gear"},
            )
        except Exception as exc:
            logger.warning(
                f"Queue monitor could not register NOTIFY listener: {exc}",
                extra={"emoji_type": "warning"},
            )
        self._thread = threading.Thread(target=self._run_loop, name="queue-monitor-producer", daemon=True)
        self._thread.start()
        refresh_note = (
            f" arr_refresh_monitored_downloads={self._arr_refresh_interval}s stagger={self._arr_refresh_stagger}s"
            if self._arr_refresh_interval > 0
            else " arr_refresh_monitored_downloads=off"
        )
        logger.info(
            f"Queue monitor producer started poll_interval={self._poll_interval}s "
            f"(CHECK_INTERVAL={getattr(settings, 'CHECK_INTERVAL', 10)}) "
            f"retry_grace={self._retry_grace_seconds}s "
            f"search_timeout={self._search_timeout_seconds}s{refresh_note}",
            extra={"emoji_type": "gear"},
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._qm_listen_registered:
            try:
                from services.postgres.notifier import QUEUE_MONITOR_CHANNEL, get_shared_notifier

                get_shared_notifier().unlisten(QUEUE_MONITOR_CHANNEL)
            except Exception:
                pass
            self._qm_listen_registered = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        logger.info("Queue monitor producer stopped", extra={"emoji_type": "info"})

    def _run_loop(self) -> None:
        """Idle until NOTIFY / safety timeout while no flagged placeholders; tight poll while active."""
        poll_iv = float(self._poll_interval)
        refresh_iv = float(max(0, int(self._arr_refresh_interval)))
        safety = float(self._notify_safety_seconds)

        while not self._stop_event.is_set():
            probe = get_session()
            try:
                ctx = _collect_queue_monitor_poll_context(probe)
            finally:
                probe.close()

            if ctx is None:
                clear_queue_download_snapshot()
                self._wake_event.clear()
                self._wake_event.wait(timeout=safety)
                continue

            logger.debug(
                "Queue monitor NOTIFY mode: active flagged placeholders — polling",
                extra={"emoji_type": "debug"},
            )

            next_poll = time.monotonic()
            next_refresh = time.monotonic() + self._arr_refresh_stagger if refresh_iv > 0 else float("inf")

            while not self._stop_event.is_set():
                now = time.monotonic()
                if now >= next_poll:
                    t0 = time.monotonic()
                    try:
                        self._poll_once()
                    except Exception as e:
                        logger.error(f"Queue monitor poll cycle failed: {e}", extra={"emoji_type": "error"})
                    next_poll = t0 + poll_iv

                now = time.monotonic()
                if refresh_iv > 0 and now >= next_refresh:
                    t1 = time.monotonic()
                    try:
                        self._nudge_arr_monitored_downloads()
                    except Exception as e:
                        logger.error(f"Queue monitor ARR refresh command failed: {e}", extra={"emoji_type": "error"})
                    next_refresh = t1 + refresh_iv

                probe = get_session()
                try:
                    still_active = _collect_queue_monitor_poll_context(probe) is not None
                finally:
                    probe.close()

                if not still_active:
                    break

                now = time.monotonic()
                delay = min(next_poll, next_refresh) - now
                self._stop_event.wait(max(0.25, delay))

    def _nudge_arr_monitored_downloads(self) -> None:
        """POST RefreshMonitoredDownloads so Radarr/Sonarr pick up queue changes before our next /queue read."""
        session = get_session()
        try:
            ctx = _collect_queue_monitor_poll_context(session)
            if ctx is None:
                return
            ok: list[str] = []
            if ctx["needs_radarr_std"] and trigger_radarr_refresh_monitored_downloads(is_4k=False):
                ok.append("radarr_std")
            if ctx["needs_radarr_4k"] and trigger_radarr_refresh_monitored_downloads(is_4k=True):
                ok.append("radarr_4k")
            if ctx["needs_sonarr_std"] and trigger_sonarr_refresh_monitored_downloads(is_4k=False):
                ok.append("sonarr_std")
            if ctx["needs_sonarr_4k"] and trigger_sonarr_refresh_monitored_downloads(is_4k=True):
                ok.append("sonarr_4k")
            if ok:
                logger.debug(
                    f"Queue monitor triggered RefreshMonitoredDownloads: {','.join(ok)}",
                    extra={"emoji_type": "refresh"},
                )
        finally:
            session.close()

    def _poll_once(self) -> None:
        """One full poll cycle: scan -> ARR HTTP (no DB held) -> apply intents.

        Phase 1 connection-lifecycle: this method previously held one DB
        session across all four ARR HTTP calls (each up to 30s timeout).
        Now we run in three short phases:

        1. Read-only scan session: figure out which ARR endpoints we need
           to poll. Closed before HTTP.
        2. HTTP phase: poll Radarr/Sonarr without any DB connection held.
        3. Apply session: re-collect targets, build intents, publish
           snapshot, commit. Closed at the end.

        Re-collecting targets in phase 3 keeps the apply step on fresh data
        (a placeholder may have completed during the HTTP window).
        """
        # Phase 1: scan to determine which ARR endpoints need polling.
        scan_session = get_session()
        try:
            _expire_stale_queue_monitor_flags(scan_session)
            scan_ctx = _collect_queue_monitor_poll_context(scan_session)
            if scan_ctx is None:
                clear_queue_download_snapshot()
                try:
                    scan_session.commit()
                except Exception:
                    scan_session.rollback()
                return
            needs_radarr_std = bool(scan_ctx["needs_radarr_std"])
            needs_radarr_4k = bool(scan_ctx["needs_radarr_4k"])
            needs_sonarr_std = bool(scan_ctx["needs_sonarr_std"])
            needs_sonarr_4k = bool(scan_ctx["needs_sonarr_4k"])
            try:
                scan_session.commit()
            except Exception:
                scan_session.rollback()
        finally:
            try:
                scan_session.close()
            except Exception:
                pass

        # Phase 2: ARR HTTP without holding any DB connection.
        radarr_std_map = self._poll_radarr_queue(is_4k=False) if needs_radarr_std else {}
        radarr_4k_map = self._poll_radarr_queue(is_4k=True) if needs_radarr_4k else {}
        sonarr_std_map = self._poll_sonarr_queue(is_4k=False) if needs_sonarr_std else {}
        sonarr_4k_map = self._poll_sonarr_queue(is_4k=True) if needs_sonarr_4k else {}

        # Phase 3: re-collect on a fresh session and apply intents.
        apply_session = get_session()
        try:
            ctx = _collect_queue_monitor_poll_context(apply_session)
            if ctx is None:
                # Nothing left to apply (placeholders cleared during HTTP window).
                try:
                    apply_session.commit()
                except Exception:
                    apply_session.rollback()
                return

            movie_targets = ctx["movie_targets"]
            episode_targets = ctx["episode_targets"]
            placeholders = ctx["placeholders"]

            orchestrator = StatusOrchestrator(session=apply_session)
            intents: list[StatusIntent] = []

            for ph, movie, is_4k in movie_targets:
                queue_map = radarr_4k_map if is_4k else radarr_std_map
                queue_item = queue_map.get(str(getattr(movie, "radarrid", "")))
                intent = self._build_intent_for_placeholder(ph, queue_item)
                if intent:
                    intents.append(intent)

            for ph, episode, is_4k in episode_targets:
                queue_map = sonarr_4k_map if is_4k else sonarr_std_map
                queue_item = queue_map.get(str(getattr(episode, "sonarrid", "")))
                intent = self._build_intent_for_placeholder(ph, queue_item)
                if intent:
                    intents.append(intent)

            if intents:
                applied = orchestrator.apply_and_project_statuses(intents)
                logger.info(
                    f"Queue monitor applied={applied} intents={len(intents)} active_placeholders={len(placeholders)}",
                    extra={"emoji_type": "process"},
                )

            _publish_queue_activity_snapshot(
                apply_session,
                movie_targets,
                episode_targets,
                radarr_std_map,
                radarr_4k_map,
                sonarr_std_map,
                sonarr_4k_map,
            )
            apply_session.commit()
        finally:
            try:
                apply_session.close()
            except Exception:
                pass

    def _poll_radarr_queue(self, is_4k: bool) -> dict[str, dict[str, Any]]:
        base_url, api_key = settings.resolve_arr_endpoint('radarr', is_4k=is_4k)
        return self._poll_queue_map(base_url, api_key, id_field="movieId")

    def _poll_sonarr_queue(self, is_4k: bool) -> dict[str, dict[str, Any]]:
        base_url, api_key = settings.resolve_arr_endpoint('sonarr', is_4k=is_4k)
        return self._poll_queue_map(base_url, api_key, id_field="episodeId")

    def _poll_queue_map(self, base_url: str, api_key: str, *, id_field: str) -> dict[str, dict[str, Any]]:
        if not base_url or not api_key:
            return {}

        endpoint = _queue_endpoint(base_url)
        if not endpoint:
            return {}

        try:
            response = requests.get(endpoint, headers={"X-Api-Key": api_key}, timeout=30)
            response.raise_for_status()
            payload = response.json() if response.text else {}
            records = payload.get("records", []) if isinstance(payload, dict) else []
            result: dict[str, dict[str, Any]] = {}
            for rec in records:
                rec_id = rec.get(id_field)
                if rec_id is None:
                    continue
                key = str(rec_id)
                existing = result.get(key)
                if existing is None:
                    result[key] = rec
                    continue

                existing_priority = _queue_status_priority(
                    existing.get("status"),
                    existing.get("trackedDownloadState"),
                )
                candidate_priority = _queue_status_priority(
                    rec.get("status"),
                    rec.get("trackedDownloadState"),
                )
                if candidate_priority >= existing_priority:
                    result[key] = rec
            return result
        except Exception as e:
            logger.error(f"Queue monitor failed polling endpoint={endpoint}: {e}", extra={"emoji_type": "error"})
            return {}

    def _build_intent_for_placeholder(self, placeholder: Placeholder, queue_item: dict[str, Any] | None) -> StatusIntent | None:
        now = _utc_now()
        extra = _safe_extra_dict(placeholder)
        qm = dict(extra.get("queue_monitor") or {})

        seen_queue_once = bool(qm.get("seen_queue_once", False))
        left_queue_at = _from_iso(qm.get("left_queue_at"))

        current_status = str(getattr(placeholder, "display_status", "") or "")
        current_progress = getattr(placeholder, "display_progress", None)

        target_status = current_status
        target_reason = str(getattr(placeholder, "display_reason", "") or "")
        target_progress: int | None = current_progress if isinstance(current_progress, int) else None

        if queue_item:
            queue_status = str(queue_item.get("status", "") or "").strip().lower()
            tracked_state = str(queue_item.get("trackedDownloadState", "") or "").strip().lower()
            qm["seen_queue_once"] = True
            qm["left_queue_at"] = None
            qm["empty_queue_search_started_at"] = None
            qm["search_wait_last_log_at"] = None
            qm["left_queue_wait_last_log_at"] = None
            qm["last_seen_in_queue_at"] = _to_iso(now)
            qm["last_queue_status"] = queue_status
            qm["last_tracked_state"] = tracked_state

            if queue_status == "downloading":
                progress = self._extract_progress(queue_item)
                if progress > 0:
                    target_status = DisplayStatus.DOWNLOADING.value
                    target_reason = render_message("queue.downloading", {"Progress": str(progress)})
                    target_progress = progress
                else:
                    target_status = DisplayStatus.SEARCHING.value
                    target_reason = render_message("queue.queued", {})
                    target_progress = None
            elif queue_status in {"queued", "delay", "paused"}:
                target_status = DisplayStatus.SEARCHING.value
                target_reason = render_message("queue.queued", {})
                target_progress = None
            elif queue_status == "completed":
                # Completed means download is done; tracked import states add clarity.
                target_status = DisplayStatus.IMPORT_IN_PROGRESS.value
                if tracked_state == "importpending":
                    target_reason = render_message("queue.import.pending", {})
                elif tracked_state == "importing":
                    target_reason = render_message("queue.import.importing", {})
                elif tracked_state == "importblocked":
                    target_reason = render_message("queue.import.blocked", {})
                elif tracked_state == "failedpending":
                    target_reason = render_message("queue.import.failed_pending", {})
                else:
                    target_reason = render_message("queue.import.processing", {})
                target_progress = None
            elif queue_status in {"warning", "error", "failed"}:
                target_status = "RETRYING"
                target_reason = render_message("queue.retry.queue_failure", {})
                target_progress = None
            else:
                target_status = DisplayStatus.SEARCHING.value
                # Operator-fallback line stays in English for diagnostics.
                target_reason = f"Queue status: {queue_status or 'unknown'}"
                target_progress = None
        else:
            if seen_queue_once:
                if not left_queue_at:
                    left_queue_at = now
                    qm["left_queue_at"] = _to_iso(now)

                elapsed = (now - left_queue_at).total_seconds() if left_queue_at else 0
                if elapsed < self._retry_grace_seconds:
                    target_status = "RETRYING"
                    target_reason = render_message("queue.retry.left_queue", {})
                    target_progress = None
                    if _should_emit_wait_log(qm, "left_queue_wait_last_log_at", now, period_seconds=60):
                        remaining = max(0, int(self._retry_grace_seconds - elapsed))
                        logger.info(
                            "Queue monitor waiting after queue exit: "
                            f"placeholder_id={int(getattr(placeholder, 'id', 0) or 0)} "
                            f"elapsed={int(elapsed)}s remaining={remaining}s status={target_status}",
                            extra={"emoji_type": "info"},
                        )
                else:
                    target_status = DisplayStatus.NOT_FOUND.value
                    target_reason = render_message("queue.not_found", {})
                    target_progress = None
                    qm["left_queue_wait_last_log_at"] = None
                    logger.info(
                        "Queue monitor terminal status reached after queue exit grace: "
                        f"placeholder_id={int(getattr(placeholder, 'id', 0) or 0)} "
                        f"elapsed={int(elapsed)}s grace={self._retry_grace_seconds}s "
                        f"status={target_status} reason={target_reason}",
                        extra={"emoji_type": "warning"},
                    )
            else:
                search_start = _from_iso(qm.get("empty_queue_search_started_at"))
                if not search_start:
                    search_start = now
                    qm["empty_queue_search_started_at"] = _to_iso(search_start)
                elapsed_search = (now - search_start).total_seconds() if search_start else 0.0
                if elapsed_search >= float(self._search_timeout_seconds):
                    target_status = DisplayStatus.NOT_FOUND.value
                    target_reason = render_message("queue.not_found", {})
                    qm["empty_queue_search_started_at"] = None
                    qm["search_wait_last_log_at"] = None
                    logger.info(
                        "Queue monitor search timeout reached before queue entry: "
                        f"placeholder_id={int(getattr(placeholder, 'id', 0) or 0)} "
                        f"elapsed={int(elapsed_search)}s timeout={self._search_timeout_seconds}s "
                        f"status={target_status} reason={target_reason}",
                        extra={"emoji_type": "warning"},
                    )
                else:
                    target_status = DisplayStatus.SEARCHING.value
                    target_reason = render_message("queue.searching", {})
                    target_progress = None
                    if _should_emit_wait_log(qm, "search_wait_last_log_at", now, period_seconds=60):
                        remaining = max(0, int(float(self._search_timeout_seconds) - elapsed_search))
                        logger.info(
                            "Queue monitor searching with no queue entry yet: "
                            f"placeholder_id={int(getattr(placeholder, 'id', 0) or 0)} "
                            f"elapsed={int(elapsed_search)}s remaining={remaining}s status={target_status}",
                            extra={"emoji_type": "info"},
                        )

        extra["queue_monitor"] = qm
        placeholder.extra = extra
        flag_modified(placeholder, "extra")

        status_changed = target_status != current_status
        reason_changed = target_reason != str(getattr(placeholder, "display_reason", "") or "")
        progress_changed = target_progress != current_progress

        if not (status_changed or reason_changed or progress_changed):
            return None

        return StatusIntent(
            placeholder_id=int(placeholder.id),
            new_status=target_status,
            reason=target_reason,
            source=StatusSource.QUEUE_MONITOR,
            progress=target_progress,
            trigger_nfo_refresh=True,
            metadata={"queue_monitor": True},
        )

    @staticmethod
    def _extract_progress(queue_item: dict[str, Any]) -> int:
        try:
            size_left = float(queue_item.get("sizeleft", queue_item.get("sizeLeft", 0)) or 0)
            size_total = float(queue_item.get("size", queue_item.get("totalSize", 0)) or 0)
            if size_total <= 0:
                return 0
            percent = int(max(0.0, min(100.0, 100.0 - ((size_left / size_total) * 100.0))))
            return percent
        except Exception:
            return 0


_producer_lock = threading.Lock()
_producer: QueueMonitorProducer | None = None


def start_queue_monitor_producer() -> None:
    if not bool(getattr(settings, "ENABLE_QUEUE_MONITOR", True)):
        logger.info("Queue monitor producer disabled by ENABLE_QUEUE_MONITOR=false", extra={"emoji_type": "info"})
        return

    global _producer
    with _producer_lock:
        if _producer is None:
            _producer = QueueMonitorProducer()
        _producer.start()


def stop_queue_monitor_producer() -> None:
    global _producer
    with _producer_lock:
        if _producer is not None:
            _producer.stop()