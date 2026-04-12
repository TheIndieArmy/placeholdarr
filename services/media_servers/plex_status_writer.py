from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from core.logger import logger
from services.media_servers.plex_lookup import get_plex_server
from services.postgres.models import Episode, Movie, Placeholder, Season, Series
from services.status_projection import project_summary, project_title


def _base_summary(summary: str | None, fallback: str | None = None, secondary_fallback: str | None = None) -> str:
    text = re.sub(r"^\[.*?\]\s*", "", str(summary or ""))
    if not text.strip():
        for candidate in (fallback, secondary_fallback):
            candidate_text = str(candidate or "").strip()
            if candidate_text:
                text = candidate_text
                break
    return text.strip()


def _update_item_projection(
    item: Any,
    *,
    desired_title: str,
    desired_summary: str,
    current_title: str,
    current_summary: str,
    retry_interval: int,
    retry_timeout: int,
) -> tuple[bool, bool, bool]:
    deadline = time.monotonic() + max(0, int(retry_timeout))
    changed_title = desired_title != current_title
    changed_summary = desired_summary != current_summary
    if not changed_title and not changed_summary:
        return True, False, False

    while True:
        try:
            if changed_title:
                item.editTitle(desired_title, locked=True)
            if changed_summary:
                item.editSummary(desired_summary, locked=True)
            try:
                item.reload()
            except Exception:
                pass
            return True, changed_title, changed_summary
        except Exception as ex:
            if time.monotonic() >= deadline:
                logger.debug(f"Plex projection update timed out: {ex}", extra={"emoji_type": "debug"})
                return False, changed_title, changed_summary
            sleep_for = min(max(1, int(retry_interval)), max(1, int(deadline - time.monotonic())))
            time.sleep(sleep_for)


def _movie_target_id(session: Session, entity_id: int, desired_status: str | None) -> str | None:
    movie = session.query(Movie).get(entity_id)
    if not movie:
        return None

    if desired_status:
        if getattr(movie, "plex_dummy_id", None):
            return str(movie.plex_dummy_id)
        placeholder = (
            session.query(Placeholder)
            .filter(
                Placeholder.has_placeholder == True,  # noqa: E712
                Placeholder.movie_id == entity_id,
                Placeholder.plex_placeholder_id.isnot(None),
            )
            .order_by(Placeholder.updated_at.desc())
            .first()
        )
        if placeholder:
            return str(placeholder.plex_placeholder_id)
        return None

    return str(getattr(movie, "plex_id", "") or "") or None


def _episode_placeholder_target_id(session: Session, episode_id: int) -> str | None:
    placeholder = (
        session.query(Placeholder)
        .filter(
            Placeholder.has_placeholder == True,  # noqa: E712
            Placeholder.episode_id == episode_id,
            Placeholder.plex_placeholder_id.isnot(None),
        )
        .order_by(Placeholder.updated_at.desc())
        .first()
    )
    if not placeholder:
        return None
    return str(placeholder.plex_placeholder_id)


def batch_update_plex_statuses(
    dbsession: Session,
    intents: list[dict[str, Any]],
    retry_interval: int = 30,
    retry_timeout: int = 600,
) -> dict[str, Any]:
    """Batch-update Plex summaries based on explicit status intents."""
    plex = get_plex_server()
    if not plex:
        logger.error("Plex server not available", extra={"emoji_type": "error"})
        return {"status_updates": 0, "unchanged": 0, "skipped": 0, "errors": 0, "details": []}

    if not intents:
        return {"status_updates": 0, "unchanged": 0, "skipped": 0, "errors": 0, "details": []}

    stats = {"status_updates": 0, "unchanged": 0, "skipped": 0, "errors": 0, "details": []}
    movie_intents = [intent for intent in intents if intent.get("entity_type") is Movie]
    episode_intents = [intent for intent in intents if intent.get("entity_type") is Episode]

    for intent in movie_intents:
        entity_id = int(intent.get("entity_id"))
        desired_status = intent.get("status")
        movie = dbsession.query(Movie).get(entity_id)
        if not movie:
            stats["errors"] += 1
            stats["details"].append({"entity_id": entity_id, "result": "movie_not_found"})
            continue

        target_id = _movie_target_id(dbsession, entity_id, desired_status)
        if not target_id:
            stats["skipped"] += 1
            stats["details"].append({"entity_id": entity_id, "title": movie.title, "result": "skipped_no_plex_id"})
            continue

        try:
            plex_item = plex.fetchItem(f"/library/metadata/{target_id}")
        except Exception:
            plex_item = None

        if not plex_item:
            stats["errors"] += 1
            stats["details"].append(
                {"entity_id": entity_id, "title": movie.title, "target_id": target_id, "result": "plex_not_found"}
            )
            continue

        current_title = str(getattr(plex_item, "title", "") or "")
        current_summary = str(getattr(plex_item, "summary", "") or "")
        desired_title = project_title(current_title, desired_status)
        desired_summary = project_summary(
            _base_summary(
                current_summary,
                fallback=getattr(movie, "plex_overview", None),
                secondary_fallback=getattr(movie, "radarr_overview", None),
            ),
            desired_status,
        )
        ok, changed_title, changed_summary = _update_item_projection(
            plex_item,
            desired_title=desired_title,
            desired_summary=desired_summary,
            current_title=current_title,
            current_summary=current_summary,
            retry_interval=retry_interval,
            retry_timeout=retry_timeout,
        )
        if ok and not changed_title and not changed_summary:
            stats["unchanged"] += 1
            stats["details"].append({"entity_id": entity_id, "title": movie.title, "result": "unchanged"})
            continue

        if ok:
            stats["status_updates"] += 1
            stats["details"].append(
                {
                    "entity_id": entity_id,
                    "title": movie.title,
                    "result": "updated",
                    "title_updated": bool(changed_title),
                    "summary_updated": bool(changed_summary),
                }
            )
        else:
            stats["errors"] += 1
            stats["details"].append({"entity_id": entity_id, "title": movie.title, "result": "write_failed"})

    episode_groups: dict[str, list[tuple[dict[str, Any], Episode, Series]]] = defaultdict(list)
    direct_episode_entries: list[tuple[dict[str, Any], Episode, Series, str]] = []
    for intent in episode_intents:
        entity_id = int(intent.get("entity_id"))
        desired_status = intent.get("status")
        episode = dbsession.query(Episode).get(entity_id)
        if not episode:
            stats["errors"] += 1
            stats["details"].append({"entity_id": entity_id, "result": "episode_not_found"})
            continue

        season = dbsession.query(Season).get(episode.season_id) if episode.season_id else None
        series = dbsession.query(Series).get(season.series_id) if season else None
        if not series:
            stats["errors"] += 1
            stats["details"].append({"entity_id": entity_id, "result": "series_not_found"})
            continue

        target_series_id = getattr(series, "plex_dummy_id", None)
        if not target_series_id:
            episode_target_id = _episode_placeholder_target_id(dbsession, entity_id)
            if episode_target_id:
                direct_episode_entries.append((intent, episode, series, episode_target_id))
                continue
            stats["skipped"] += 1
            stats["details"].append({"entity_id": entity_id, "result": "skipped_series_no_plex_id"})
            continue

        episode_groups[str(target_series_id)].append((intent, episode, series))

    for target_series_id, entries in episode_groups.items():
        try:
            plex_show = plex.fetchItem(f"/library/metadata/{target_series_id}")
        except Exception:
            plex_show = None

        if not plex_show:
            for intent, episode, series in entries:
                entity_id = int(intent.get("entity_id"))
                episode_target_id = _episode_placeholder_target_id(dbsession, entity_id)
                if not episode_target_id:
                    stats["errors"] += 1
                    stats["details"].append({"entity_id": entity_id, "result": "plex_show_not_found"})
                    continue
                try:
                    plex_episode = plex.fetchItem(f"/library/metadata/{episode_target_id}")
                except Exception:
                    plex_episode = None
                if not plex_episode:
                    stats["errors"] += 1
                    stats["details"].append({"entity_id": entity_id, "series": series.title, "result": "plex_episode_not_found"})
                    continue

                current_title = str(getattr(plex_episode, "title", "") or "")
                current_summary = str(getattr(plex_episode, "summary", "") or "")
                desired_title = project_title(current_title, intent.get("status"))
                desired_summary = project_summary(
                    _base_summary(
                        current_summary,
                        fallback=getattr(episode, "plex_overview", None),
                        secondary_fallback=getattr(episode, "sonarr_episode_overview", None),
                    ),
                    intent.get("status"),
                )
                ok, changed_title, changed_summary = _update_item_projection(
                    plex_episode,
                    desired_title=desired_title,
                    desired_summary=desired_summary,
                    current_title=current_title,
                    current_summary=current_summary,
                    retry_interval=retry_interval,
                    retry_timeout=retry_timeout,
                )
                if ok and not changed_title and not changed_summary:
                    stats["unchanged"] += 1
                    stats["details"].append({"entity_id": entity_id, "series": series.title, "result": "unchanged"})
                    continue

                if ok:
                    stats["status_updates"] += 1
                    stats["details"].append(
                        {
                            "entity_id": entity_id,
                            "series": series.title,
                            "result": "updated",
                            "title_updated": bool(changed_title),
                            "summary_updated": bool(changed_summary),
                        }
                    )
                else:
                    stats["errors"] += 1
                    stats["details"].append({"entity_id": entity_id, "series": series.title, "result": "write_failed"})
            continue

        episodes_by_number = {}
        try:
            for plex_episode in plex_show.episodes():
                season_number = int(getattr(plex_episode, "seasonNumber", -1) or -1)
                episode_number = int(getattr(plex_episode, "episodeNumber", -1) or -1)
                if season_number >= 0 and episode_number >= 0:
                    episodes_by_number[(season_number, episode_number)] = plex_episode
        except Exception:
            for intent, _, _ in entries:
                stats["errors"] += 1
                stats["details"].append(
                    {"entity_id": int(intent.get("entity_id")), "result": "plex_episodes_fetch_error"}
                )
            continue

        for intent, episode, series in entries:
            entity_id = int(intent.get("entity_id"))
            desired_status = intent.get("status")
            key = (int(getattr(episode, "season_number", -1) or -1), int(getattr(episode, "episode_number", -1) or -1))
            plex_episode = episodes_by_number.get(key)

            if not plex_episode:
                stats["errors"] += 1
                stats["details"].append({"entity_id": entity_id, "series": series.title, "result": "plex_episode_not_found"})
                continue

            current_title = str(getattr(plex_episode, "title", "") or "")
            current_summary = str(getattr(plex_episode, "summary", "") or "")
            desired_title = project_title(current_title, desired_status)
            desired_summary = project_summary(
                _base_summary(
                    current_summary,
                    fallback=getattr(episode, "plex_overview", None),
                    secondary_fallback=getattr(episode, "sonarr_episode_overview", None),
                ),
                desired_status,
            )
            ok, changed_title, changed_summary = _update_item_projection(
                plex_episode,
                desired_title=desired_title,
                desired_summary=desired_summary,
                current_title=current_title,
                current_summary=current_summary,
                retry_interval=retry_interval,
                retry_timeout=retry_timeout,
            )
            if ok and not changed_title and not changed_summary:
                stats["unchanged"] += 1
                stats["details"].append({"entity_id": entity_id, "series": series.title, "result": "unchanged"})
                continue

            if ok:
                stats["status_updates"] += 1
                stats["details"].append(
                    {
                        "entity_id": entity_id,
                        "series": series.title,
                        "result": "updated",
                        "title_updated": bool(changed_title),
                        "summary_updated": bool(changed_summary),
                    }
                )
            else:
                stats["errors"] += 1
                stats["details"].append({"entity_id": entity_id, "series": series.title, "result": "write_failed"})

    for intent, episode, series, episode_target_id in direct_episode_entries:
        entity_id = int(intent.get("entity_id"))
        desired_status = intent.get("status")
        try:
            plex_episode = plex.fetchItem(f"/library/metadata/{episode_target_id}")
        except Exception:
            plex_episode = None

        if not plex_episode:
            stats["errors"] += 1
            stats["details"].append({"entity_id": entity_id, "series": series.title, "result": "plex_episode_not_found"})
            continue

        current_title = str(getattr(plex_episode, "title", "") or "")
        current_summary = str(getattr(plex_episode, "summary", "") or "")
        desired_title = project_title(current_title, desired_status)
        desired_summary = project_summary(
            _base_summary(
                current_summary,
                fallback=getattr(episode, "plex_overview", None),
                secondary_fallback=getattr(episode, "sonarr_episode_overview", None),
            ),
            desired_status,
        )
        ok, changed_title, changed_summary = _update_item_projection(
            plex_episode,
            desired_title=desired_title,
            desired_summary=desired_summary,
            current_title=current_title,
            current_summary=current_summary,
            retry_interval=retry_interval,
            retry_timeout=retry_timeout,
        )
        if ok and not changed_title and not changed_summary:
            stats["unchanged"] += 1
            stats["details"].append({"entity_id": entity_id, "series": series.title, "result": "unchanged"})
            continue

        if ok:
            stats["status_updates"] += 1
            stats["details"].append(
                {
                    "entity_id": entity_id,
                    "series": series.title,
                    "result": "updated",
                    "title_updated": bool(changed_title),
                    "summary_updated": bool(changed_summary),
                }
            )
        else:
            stats["errors"] += 1
            stats["details"].append({"entity_id": entity_id, "series": series.title, "result": "write_failed"})

    return stats
