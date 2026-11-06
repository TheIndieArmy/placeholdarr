"""Poll Plex owner + friends watchlists, upsert title + membership rows, optional auto Arr add."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from core.config import settings
from core.logger import logger
from services.media_servers.plex_watchlist import (
    ensure_watchlist_rss_feeds,
    fetch_watchlists_for_account,
    plex_watchlist_token,
    poll_watchlist_rss_feed,
)

# How often the dedicated RSS loop polls CDN feeds (seconds).
WATCHLIST_RSS_POLL_SECONDS = 10
# Without Plex Pass / RSS, fall back to a full API pull on this cadence.
WATCHLIST_API_FALLBACK_SECONDS = 300
_SEEN_GUID_CAP = 500
from services.postgres.db import get_session
from services.postgres.models import (
    PlexWatchlistItem,
    PlexWatchlistMembership,
    PlexWatchlistUser,
)
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
    """Map watchlist TV request scope (or TV density fallback) to Sonarr monitor.

    ``episode`` adds as ``pilot`` (seasons unmonitored), then Accept expands to the
    first ``EPISODES_LOOKAHEAD`` episodes only so indexer RSS cannot grab the rest.
    ``season`` uses ``firstSeason``. ``series`` uses ``all``.
    """
    s = str(scope or "").strip().lower()
    if s == "season":
        return "firstSeason"
    if s == "episode":
        return "pilot"
    return "all"


def _episodes_lookahead_count() -> int:
    try:
        return max(1, int(getattr(settings, "EPISODES_LOOKAHEAD", 3) or 3))
    except (TypeError, ValueError):
        return 3


def default_tv_request_scope() -> str:
    configured = str(getattr(settings, "WATCHLIST_TV_REQUEST_SCOPE", "") or "").strip().lower()
    if configured in {"episode", "season", "series"}:
        return configured
    density = str(getattr(settings, "TV_PLACEHOLDER_DENSITY", "episode") or "episode").strip().lower()
    if density in {"episode", "season", "series"}:
        return density
    return "episode"


_ON_NEW_MODES = frozenset({"manual", "monitor_only", "add_only", "add_and_search"})
_ON_NEW_RANK = {"manual": 0, "add_only": 1, "monitor_only": 2, "add_and_search": 3}


def watchlist_action_mode() -> str:
    mode = str(getattr(settings, "WATCHLIST_ON_NEW", "manual") or "manual").strip().lower()
    if mode in _ON_NEW_MODES:
        return mode
    return "manual"


def normalize_on_new_mode(raw: Any) -> str | None:
    """Return a valid on_new mode, or None when unset / invalid."""
    mode = str(raw or "").strip().lower()
    return mode if mode in _ON_NEW_MODES else None


def on_new_override_for_user(user: PlexWatchlistUser | None) -> str | None:
    if user is None:
        return None
    meta = user.settings_override if isinstance(user.settings_override, dict) else {}
    return normalize_on_new_mode(meta.get("on_new"))


def effective_on_new_for_users(users: list[PlexWatchlistUser], *, default: str | None = None) -> str:
    """Resolve Arr behavior for a title shared by one or more roster users.

    Per-user overrides replace the account default for that person. When several
    people share a title, the strongest non-default-aware mode wins so one
    ``add_and_search`` override still auto-adds.
    """
    base = normalize_on_new_mode(default) or watchlist_action_mode()
    modes: list[str] = []
    for user in users:
        modes.append(on_new_override_for_user(user) or base)
    if not modes:
        return base
    return max(modes, key=lambda m: _ON_NEW_RANK.get(m, 0))


def _resolve_status_for_row(
    session,
    row: PlexWatchlistItem,
    *,
    coverage: str | None = None,
) -> str:
    """Resolve stored decision status (new/added/excluded/failed). No ``in_arr``."""
    _ = session
    if row.excluded:
        return "excluded"
    cov = str(coverage or "").strip().lower()
    # Full watchlist-target coverage ⇒ already allowed into the pipeline.
    if cov == "complete" and not row.excluded:
        return "added"
    if row.status == "added":
        return "added"
    if row.status == "failed":
        return "failed"
    # Migrate legacy in_arr → new (Queue) until coverage says complete.
    if row.status == "in_arr":
        return "new"
    return "new"


def _apply_coverage_statuses(session, rows: list[PlexWatchlistItem]) -> None:
    """Batch-enrich and set decision status from watchlist-target coverage."""
    if not rows:
        return
    from services.watchlist.catalog import enrich_watchlist_items

    enrichments = enrich_watchlist_items(session, rows)
    for row in rows:
        cov = str((enrichments.get(int(row.id)) or {}).get("coverage") or "none")
        prev = row.status
        row.status = _resolve_status_for_row(session, row, coverage=cov)
        if row.status == "added" and prev != "added" and row.added_at is None and cov == "complete":
            row.added_at = _utcnow()
            row.last_error = None


# Cap title lists stored on task runs / logged in detail (counts stay exact).
_WATCHLIST_CHANGE_LIST_CAP = 40


def _title_display(row: PlexWatchlistItem | dict[str, Any] | None) -> str:
    if row is None:
        return "Untitled"
    if isinstance(row, dict):
        title = str(row.get("title") or "").strip() or "Untitled"
        year = row.get("year")
        media = str(row.get("media_type") or "").strip().lower()
    else:
        title = str(getattr(row, "title", None) or "").strip() or "Untitled"
        year = getattr(row, "year", None)
        media = str(getattr(row, "media_type", None) or "").strip().lower()
    bit = f"{title} ({year})" if year not in (None, "") else title
    if media in {"show", "episode", "season"}:
        return f"{bit} [TV]"
    if media == "movie":
        return f"{bit} [Movie]"
    return bit


def _user_display(user: PlexWatchlistUser | None, *, fallback: str = "?") -> str:
    if user is None:
        return fallback
    return (
        str(getattr(user, "title", None) or "").strip()
        or str(getattr(user, "username", None) or "").strip()
        or str(getattr(user, "plex_user_id", None) or "").strip()
        or fallback
    )


def _cap_change_list(rows: list[dict[str, Any]], *, cap: int = _WATCHLIST_CHANGE_LIST_CAP) -> list[dict[str, Any]]:
    if len(rows) <= cap:
        return rows
    return rows[:cap]


def build_watchlist_task_progress(
    upsert: dict[str, Any] | None,
    auto: dict[str, Any] | None = None,
    *,
    trigger: str = "",
    source: str = "",
    targets: list[str] | None = None,
) -> dict[str, Any]:
    """Build Tasks history ``progress.sections`` from an upsert (+ optional auto-add)."""
    u = upsert if isinstance(upsert, dict) else {}
    changes = u.get("changes") if isinstance(u.get("changes"), dict) else {}
    added = changes.get("added") if isinstance(changes.get("added"), list) else []
    removed = changes.get("removed") if isinstance(changes.get("removed"), list) else []
    titles_removed = (
        changes.get("titles_removed") if isinstance(changes.get("titles_removed"), list) else []
    )
    added_total = int(changes.get("added_total") or u.get("memberships_added") or 0)
    removed_total = int(changes.get("removed_total") or u.get("memberships_removed") or 0)
    created_total = int(changes.get("titles_created_total") or u.get("created") or 0)
    orphan_total = int(changes.get("titles_removed_total") or u.get("items_removed") or 0)

    summary_metrics: list[dict[str, Any]] = [
        {"label": "Trigger", "value": str(trigger or "—").replace("_", " ")},
    ]
    if source:
        summary_metrics.append({"label": "Source", "value": str(source)})
    if targets:
        summary_metrics.append(
            {"label": "Targets", "value": ", ".join(str(t) for t in targets[:12]) + ("…" if len(targets) > 12 else "")}
        )
    summary_metrics.extend(
        [
            {"label": "Users fetched", "value": int(u.get("users_fetched") or 0)},
            {"label": "Titles fetched", "value": int(u.get("fetched") or 0)},
            {"label": "Memberships added", "value": added_total},
            {"label": "Memberships removed", "value": removed_total},
            {"label": "New titles", "value": created_total},
            {"label": "Titles cleared", "value": orphan_total},
        ]
    )
    auto_d = auto if isinstance(auto, dict) else {}
    auto_attempted = int(auto_d.get("attempted") or 0)
    if auto_attempted > 0 or str(auto_d.get("mode") or "").lower() not in {"", "manual"}:
        summary_metrics.append(
            {
                "label": "Auto-add",
                "value": (
                    f"{int(auto_d.get('added') or 0)} added / "
                    f"{int(auto_d.get('skipped') or 0)} skipped / "
                    f"{int(auto_d.get('failed') or 0)} failed"
                ),
            }
        )

    sections: list[dict[str, Any]] = [
        {
            "key": "watchlist_summary",
            "name": "Watchlist summary",
            "status": "done",
            "metrics": summary_metrics,
        }
    ]
    if added_total:
        sections.append(
            {
                "key": "watchlist_added",
                "name": "Added" + (f" ({added_total})" if added_total else ""),
                "status": "done",
                "metrics": [
                    {
                        "label": str(ev.get("title") or "Title"),
                        "value": str(ev.get("user") or ev.get("plex_user_id") or "—"),
                    }
                    for ev in added
                ]
                + (
                    [{"label": f"…and {added_total - len(added)} more", "value": "—"}]
                    if added_total > len(added)
                    else []
                ),
            }
        )
    if removed_total:
        sections.append(
            {
                "key": "watchlist_removed",
                "name": "Removed" + (f" ({removed_total})" if removed_total else ""),
                "status": "done",
                "metrics": [
                    {
                        "label": str(ev.get("title") or "Title"),
                        "value": str(ev.get("user") or ev.get("plex_user_id") or "—"),
                    }
                    for ev in removed
                ]
                + (
                    [{"label": f"…and {removed_total - len(removed)} more", "value": "—"}]
                    if removed_total > len(removed)
                    else []
                ),
            }
        )
    if orphan_total:
        sections.append(
            {
                "key": "watchlist_titles_cleared",
                "name": f"Titles cleared ({orphan_total})",
                "status": "done",
                "metrics": [
                    {"label": str(ev.get("title") or "Title"), "value": "no remaining members"}
                    for ev in titles_removed
                ]
                + (
                    [{"label": f"…and {orphan_total - len(titles_removed)} more", "value": "—"}]
                    if orphan_total > len(titles_removed)
                    else []
                ),
            }
        )
    return {"sections": sections, "running": False}


def log_watchlist_upsert_result(
    upsert: dict[str, Any] | None,
    *,
    context: str = "Watchlist sync",
) -> None:
    """Emit readable info/debug lines for membership and title deltas."""
    u = upsert if isinstance(upsert, dict) else {}
    changes = u.get("changes") if isinstance(u.get("changes"), dict) else {}
    added_total = int(changes.get("added_total") or u.get("memberships_added") or 0)
    removed_total = int(changes.get("removed_total") or u.get("memberships_removed") or 0)
    created_total = int(changes.get("titles_created_total") or u.get("created") or 0)
    orphan_total = int(changes.get("titles_removed_total") or u.get("items_removed") or 0)
    users_fetched = int(u.get("users_fetched") or 0)
    fetched = int(u.get("fetched") or 0)
    errors = u.get("user_errors") if isinstance(u.get("user_errors"), dict) else {}

    logger.info(
        f"{context}: fetched {fetched} title(s) across {users_fetched} user(s); "
        f"added {added_total}, removed {removed_total}, "
        f"new titles {created_total}, cleared {orphan_total}"
        + (f", user errors {len(errors)}" if errors else ""),
        extra={"emoji_type": "info"},
    )
    for uid, err in list(errors.items())[:12]:
        logger.warning(
            f"{context}: user {uid} fetch error: {err}",
            extra={"emoji_type": "warning"},
        )

    added = changes.get("added") if isinstance(changes.get("added"), list) else []
    removed = changes.get("removed") if isinstance(changes.get("removed"), list) else []
    for ev in added[:_WATCHLIST_CHANGE_LIST_CAP]:
        logger.info(
            f"{context}: + {ev.get('title')} ← {ev.get('user')}"
            + (" (new title)" if ev.get("new_title") else ""),
            extra={"emoji_type": "success"},
        )
    if added_total > len(added):
        logger.info(
            f"{context}: …and {added_total - len(added)} more addition(s)",
            extra={"emoji_type": "info"},
        )
    for ev in removed[:_WATCHLIST_CHANGE_LIST_CAP]:
        logger.info(
            f"{context}: - {ev.get('title')} ← {ev.get('user')}",
            extra={"emoji_type": "info"},
        )
    if removed_total > len(removed):
        logger.info(
            f"{context}: …and {removed_total - len(removed)} more removal(s)",
            extra={"emoji_type": "info"},
        )
    titles_removed = (
        changes.get("titles_removed") if isinstance(changes.get("titles_removed"), list) else []
    )
    for ev in titles_removed[:_WATCHLIST_CHANGE_LIST_CAP]:
        logger.info(
            f"{context}: cleared title {ev.get('title')} (no remaining members)",
            extra={"emoji_type": "info"},
        )


def metrics_from_watchlist_upsert(upsert: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Count cards for the upsert live phase."""
    u = upsert if isinstance(upsert, dict) else {}
    changes = u.get("changes") if isinstance(u.get("changes"), dict) else {}
    return [
        {"label": "Titles fetched", "value": int(u.get("fetched") or 0)},
        {
            "label": "Memberships added",
            "value": int(changes.get("added_total") or u.get("memberships_added") or 0),
        },
        {
            "label": "Memberships removed",
            "value": int(changes.get("removed_total") or u.get("memberships_removed") or 0),
        },
        {
            "label": "New titles",
            "value": int(changes.get("titles_created_total") or u.get("created") or 0),
        },
        {
            "label": "Titles cleared",
            "value": int(changes.get("titles_removed_total") or u.get("items_removed") or 0),
        },
    ]


def _upsert_item_row(
    session,
    existing: dict[str, PlexWatchlistItem],
    item: dict[str, Any],
    *,
    now: datetime,
) -> tuple[PlexWatchlistItem, bool]:
    key = identity_key_for(item)
    row = existing.get(key)
    created = False
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
        session.flush()
        existing[key] = row
        created = True
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
        if item.get("thumb"):
            extra = dict(row.extra or {}) if isinstance(row.extra, dict) else {}
            extra["thumb"] = item.get("thumb")
            row.extra = extra
    # Coverage-based promote runs in a batch after upsert (see _apply_coverage_statuses).
    if not row.excluded and row.status not in {"added", "failed", "excluded"}:
        row.status = "new"
    return row, created


def upsert_watchlist_from_home(payload: dict[str, Any]) -> dict[str, Any]:
    """Upsert users, title rows, and memberships from account/friends (or Home) fetch."""
    now = _utcnow()
    users_raw = payload.get("users") if isinstance(payload.get("users"), list) else []
    by_user = payload.get("by_user") if isinstance(payload.get("by_user"), dict) else {}
    errors = payload.get("errors") if isinstance(payload.get("errors"), dict) else {}
    active_ids = {str(uid).strip() for uid in by_user.keys() if str(uid or "").strip()}
    prune_stale_home = not bool(getattr(settings, "WATCHLIST_SYNC_HOME_USERS", False))

    created = updated = memberships_upserted = memberships_added = memberships_removed = items_removed = 0
    added_events: list[dict[str, Any]] = []
    removed_events: list[dict[str, Any]] = []
    titles_created_events: list[dict[str, Any]] = []
    titles_removed_events: list[dict[str, Any]] = []
    session = get_session()
    try:
        from sqlalchemy import exists

        existing_items = {r.identity_key: r for r in session.query(PlexWatchlistItem).all()}
        existing_users = {u.plex_user_id: u for u in session.query(PlexWatchlistUser).all()}
        items_by_id: dict[int, PlexWatchlistItem] = {
            int(r.id): r for r in existing_items.values() if getattr(r, "id", None) is not None
        }

        for raw in users_raw:
            if not isinstance(raw, dict):
                continue
            uid = str(raw.get("plex_user_id") or "").strip()
            if not uid:
                continue
            user = existing_users.get(uid)
            is_new = user is None
            if is_new:
                user = PlexWatchlistUser(
                    plex_user_id=uid,
                    title=str(raw.get("title") or uid),
                    first_seen_at=now,
                    updated_at=now,
                    sync_enabled=bool(raw.get("sync_enabled", True)),
                )
                session.add(user)
                existing_users[uid] = user
            user.plex_uuid = str(raw.get("plex_uuid") or "") or user.plex_uuid
            user.username = str(raw.get("username") or "") or user.username
            user.title = str(raw.get("title") or user.title or uid)
            user.thumb_url = str(raw.get("thumb_url") or "") or user.thumb_url
            kind = str(raw.get("kind") or "").strip().lower()
            if kind in {"owner", "friend", "home", "unknown"}:
                user.kind = kind
            elif not getattr(user, "kind", None):
                user.kind = "unknown"
            rel = str(raw.get("relationship") or "").strip().lower()
            if rel:
                user.relationship = rel
            fm = str(raw.get("fetch_method") or "").strip().lower()
            if fm:
                user.fetch_method = fm
            br = raw.get("block_reason")
            if br is not None:
                user.block_reason = str(br) if br else None
            user.is_admin = bool(raw.get("is_admin"))
            user.is_restricted = bool(raw.get("is_restricted"))
            user.is_protected = bool(raw.get("is_protected"))
            user.is_guest = bool(raw.get("is_guest"))
            # Preserve admin sync toggles; only seed default on create.
            if is_new and "sync_enabled" in raw:
                user.sync_enabled = bool(raw.get("sync_enabled"))
            user.updated_at = now
            err = errors.get(uid)
            if err:
                user.last_sync_error = str(err)
            elif uid in by_user:
                user.last_sync_error = None
            # Do not clear settings_override here (future per-user policy).

        session.flush()

        for uid, items in by_user.items():
            uid_s = str(uid or "").strip()
            if not uid_s:
                continue
            user = existing_users.get(uid_s)
            if user is not None and not bool(user.sync_enabled):
                continue
            if not isinstance(items, list):
                items = []
            seen_item_ids: set[int] = set()
            user_label = _user_display(user, fallback=uid_s)
            for item in items:
                if not isinstance(item, dict):
                    continue
                row, was_created = _upsert_item_row(session, existing_items, item, now=now)
                items_by_id[int(row.id)] = row
                if was_created:
                    created += 1
                    titles_created_events.append(
                        {
                            "title": _title_display(row),
                            "media_type": str(row.media_type or ""),
                            "user": user_label,
                            "plex_user_id": uid_s,
                        }
                    )
                else:
                    updated += 1
                seen_item_ids.add(int(row.id))
                mem = (
                    session.query(PlexWatchlistMembership)
                    .filter(
                        PlexWatchlistMembership.watchlist_item_id == int(row.id),
                        PlexWatchlistMembership.plex_user_id == uid_s,
                    )
                    .first()
                )
                if mem is None:
                    session.add(
                        PlexWatchlistMembership(
                            watchlist_item_id=int(row.id),
                            plex_user_id=uid_s,
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                    )
                    memberships_upserted += 1
                    memberships_added += 1
                    added_events.append(
                        {
                            "title": _title_display(row),
                            "media_type": str(row.media_type or ""),
                            "user": user_label,
                            "plex_user_id": uid_s,
                            "new_title": bool(was_created),
                        }
                    )
                else:
                    mem.last_seen_at = now
                    memberships_upserted += 1

            # Drop memberships for this user that disappeared from their remote list.
            existing_mems = (
                session.query(PlexWatchlistMembership)
                .filter(PlexWatchlistMembership.plex_user_id == uid_s)
                .all()
            )
            for mem in existing_mems:
                if int(mem.watchlist_item_id) not in seen_item_ids:
                    item_row = items_by_id.get(int(mem.watchlist_item_id))
                    removed_events.append(
                        {
                            "title": _title_display(item_row),
                            "media_type": str(getattr(item_row, "media_type", None) or ""),
                            "user": user_label,
                            "plex_user_id": uid_s,
                        }
                    )
                    session.delete(mem)
                    memberships_removed += 1

            if user is not None and uid_s not in errors:
                user.last_synced_at = now

        # Drop stale Home memberships when Home switch sync is off.
        # Do not prune server_only / friend_only / friend rows when Home is off.
        if prune_stale_home:
            for uid, user in list(existing_users.items()):
                if uid in active_ids:
                    continue
                kind = str(getattr(user, "kind", "") or "").lower()
                rel = str(getattr(user, "relationship", "") or "").lower()
                if rel != "home" and kind != "home":
                    continue
                stale_mems = (
                    session.query(PlexWatchlistMembership)
                    .filter(PlexWatchlistMembership.plex_user_id == uid)
                    .all()
                )
                user_label = _user_display(user, fallback=str(uid))
                for mem in stale_mems:
                    item_row = items_by_id.get(int(mem.watchlist_item_id))
                    removed_events.append(
                        {
                            "title": _title_display(item_row),
                            "media_type": str(getattr(item_row, "media_type", None) or ""),
                            "user": user_label,
                            "plex_user_id": str(uid),
                            "reason": "home_switch_disabled",
                        }
                    )
                    session.delete(mem)
                    memberships_removed += 1
                user.block_reason = "home_switch_disabled"
                user.last_sync_error = "Home switch sync disabled"
                user.updated_at = now

        session.flush()
        # Titles with no remaining memberships leave the watchlist table.
        # Arr catalog / placeholders stay; Arr is SoT after Accept.
        orphans = (
            session.query(PlexWatchlistItem)
            .filter(
                ~exists().where(PlexWatchlistMembership.watchlist_item_id == PlexWatchlistItem.id)
            )
            .all()
        )
        for row in orphans:
            titles_removed_events.append(
                {
                    "title": _title_display(row),
                    "media_type": str(row.media_type or ""),
                }
            )
            existing_items.pop(str(row.identity_key or ""), None)
            items_by_id.pop(int(row.id), None)
            session.delete(row)
            items_removed += 1
        if orphans:
            session.flush()
        _apply_coverage_statuses(session, list(existing_items.values()))
        session.commit()
    finally:
        session.close()

    total_fetched = sum(len(v) for v in by_user.values() if isinstance(v, list))
    users_fetched = len([k for k in by_user.keys() if str(k or "").strip()])
    return {
        "fetched": total_fetched,
        "users": len(users_raw),
        "users_fetched": users_fetched,
        "created": created,
        "updated": updated,
        "memberships_upserted": memberships_upserted,
        "memberships_added": memberships_added,
        "memberships_removed": memberships_removed,
        "items_removed": items_removed,
        "user_errors": {str(k): str(v) for k, v in errors.items()},
        "changes": {
            "added": _cap_change_list(added_events),
            "removed": _cap_change_list(removed_events),
            "titles_created": _cap_change_list(titles_created_events),
            "titles_removed": _cap_change_list(titles_removed_events),
            "added_total": memberships_added,
            "removed_total": memberships_removed,
            "titles_created_total": created,
            "titles_removed_total": items_removed,
            "truncated": bool(
                len(added_events) > _WATCHLIST_CHANGE_LIST_CAP
                or len(removed_events) > _WATCHLIST_CHANGE_LIST_CAP
                or len(titles_created_events) > _WATCHLIST_CHANGE_LIST_CAP
                or len(titles_removed_events) > _WATCHLIST_CHANGE_LIST_CAP
            ),
        },
    }


def upsert_watchlist_from_plex(remote: list[dict[str, Any]]) -> dict[str, Any]:
    """Legacy single-list upsert (owner-only). Prefer ``upsert_watchlist_from_home``."""
    return upsert_watchlist_from_home(
        {
            "users": [
                {
                    "plex_user_id": "owner",
                    "title": "Owner",
                    "username": "Owner",
                    "is_admin": True,
                    "is_restricted": False,
                    "is_protected": False,
                    "is_guest": False,
                }
            ],
            "by_user": {"owner": remote},
            "errors": {},
        }
    )


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
    confirm_remap: bool = False,
) -> dict[str, Any]:
    """Add selected watchlist rows to Arr (fan-out to every enabled target). Returns summary counts.

    When Arr resolves a title to a different primary name (alias remap), auto callers must
    leave ``confirm_remap=False`` so those rows stay in Queue. Manual Accept passes
    ``confirm_remap=True`` after the UI confirms the Arr series/movie.
    """
    from types import SimpleNamespace

    from services.source_of_truth import arr_api
    from services.watchlist.remap import (
        ensure_arr_remap_cached,
        get_cached_arr_remap,
        set_cached_arr_remap,
    )

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
    needs_remap_confirm: list[dict[str, Any]] = []
    session = get_session()
    try:
        rows = session.query(PlexWatchlistItem).filter(PlexWatchlistItem.id.in_(ids)).all()
        ensure_arr_remap_cached(session, rows)

        by_media: dict[str, list[PlexWatchlistItem]] = {"movie": [], "show": []}
        for row in rows:
            if row.excluded:
                continue
            remap = get_cached_arr_remap(row.extra)
            if isinstance(remap, dict) and remap.get("remapped") and not confirm_remap:
                needs_remap_confirm.append(
                    {
                        "id": int(row.id),
                        "watchlist_title": row.title,
                        "arr_title": remap.get("arr_title"),
                        "arr_year": remap.get("arr_year"),
                        "media_type": row.media_type,
                    }
                )
                skipped += 1
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

        from services.watchlist.catalog import enrich_watchlist_items

        enrichable = [r for media_rows in by_media.values() for r in media_rows]
        enrichments = enrich_watchlist_items(session, enrichable) if override_targets is None else {}

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
                # Prefer will_add_keys (missing watchlist targets); fall back to full target list.
                will_keys: set[str] = set()
                for row in media_rows:
                    enr = enrichments.get(int(row.id)) or {}
                    for k in enr.get("will_add_keys") or []:
                        if str(k).strip():
                            will_keys.add(str(k).strip())
                all_targets = arr_targets_for_media(media_type)
                if will_keys:
                    media_targets = [
                        t
                        for t in all_targets
                        if str(t.get("instance_key") or "").strip() in will_keys
                    ]
                else:
                    media_targets = all_targets

            if not media_targets:
                for row in media_rows:
                    row.status = "failed"
                    row.last_error = (
                        f"No {arr_type} watchlist targets configured. "
                        "Enable at least one instance under Settings → Watchlist → Arr targets."
                    )
                    failed += 1
                continue

            # Per-row accumulators across fan-out targets.
            row_ok: dict[int, bool] = {int(r.id): False for r in media_rows}
            row_had_new: dict[int, bool] = {int(r.id): False for r in media_rows}
            row_keys: dict[int, list[str]] = {int(r.id): [] for r in media_rows}
            row_errors: dict[int, list[str]] = {int(r.id): [] for r in media_rows}
            row_remap: dict[int, dict[str, Any] | None] = {int(r.id): get_cached_arr_remap(r.extra) for r in media_rows}

            items = []
            for r in media_rows:
                remap = row_remap.get(int(r.id))
                tmdb_id = r.tmdb_id
                tvdb_id = r.tvdb_id
                imdb_id = r.imdb_id
                if isinstance(remap, dict) and remap.get("remapped"):
                    tmdb_id = remap.get("tmdb_id") or tmdb_id
                    tvdb_id = remap.get("tvdb_id") or tvdb_id
                    imdb_id = remap.get("imdb_id") or imdb_id
                items.append(
                    SimpleNamespace(
                        title=r.title,
                        year=r.year,
                        tmdb_id=tmdb_id,
                        tvdb_id=tvdb_id,
                        imdb_id=imdb_id,
                    )
                )

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

                # Episode scope: add as pilot without Sonarr missing-search, then
                # monitor+EpisodeSearch the first EPISODES_LOOKAHEAD episodes.
                episode_window = media_type == "show" and scope == "episode" and monitored
                add_search = bool(search) and not episode_window
                results = arr_api.add_missing_titles(
                    media_type=media_type,
                    url=url,
                    api_key=api_key,
                    items=items,
                    quality_profile_id=qp,
                    root_folder_path=root,
                    monitored=monitored,
                    search=add_search,
                    tag_ids=tag_ids or None,
                    instance_key=key,
                    sonarr_monitor=sonarr_monitor if media_type == "show" else None,
                )
                # add_missing_titles returns one result per input item in the same order.
                if len(results) != len(media_rows):
                    for row in media_rows:
                        row_errors[int(row.id)].append(
                            f"{key}: Arr add returned {len(results)} results for {len(media_rows)} titles"
                        )
                    continue
                for row, result in zip(media_rows, results):
                    rid = int(row.id)
                    status = str(result.get("status") or "")
                    result_remap = result.get("arr_remap")
                    if isinstance(result_remap, dict):
                        row_remap[rid] = result_remap
                    if status in {"ok", "skipped"}:
                        row_ok[rid] = True
                        if key not in row_keys[rid]:
                            row_keys[rid].append(key)
                        if status == "ok":
                            row_had_new[rid] = True
                        if episode_window:
                            try:
                                arr_id = int(result.get("arr_id") or 0)
                            except (TypeError, ValueError):
                                arr_id = 0
                            if arr_id > 0:
                                try:
                                    arr_api.monitor_and_search_first_episodes(
                                        series_id=arr_id,
                                        count=_episodes_lookahead_count(),
                                        search=bool(search),
                                        url=url,
                                        api_key=api_key,
                                    )
                                except Exception as exc:
                                    logger.warning(
                                        f"Watchlist episode window failed for {row.title!r} on {key}: {exc}",
                                        extra={"emoji_type": "warning"},
                                    )
                    else:
                        row_errors[rid].append(
                            f"{key}: {result.get('error') or 'Arr add failed'}"
                        )

            now = _utcnow()
            for row in media_rows:
                rid = int(row.id)
                meta = row_remap.get(rid)
                if isinstance(meta, dict):
                    row.extra = set_cached_arr_remap(row.extra, meta)
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
    return {
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "needs_remap_confirm": needs_remap_confirm,
    }


def _user_counts_for_on_new(user: PlexWatchlistUser) -> bool:
    """Only effectively syncing people should steer auto-add Arr behavior."""
    from services.watchlist.roster import effective_sync_enabled

    block = getattr(user, "block_reason", None)
    return effective_sync_enabled(
        sync_enabled=bool(user.sync_enabled),
        relationship=str(getattr(user, "relationship", None) or ""),
        is_protected=bool(user.is_protected),
        block_reason=str(block) if block else None,
    )


def auto_add_new_items() -> dict[str, Any]:
    default_mode = watchlist_action_mode()
    session = get_session()
    try:
        rows = (
            session.query(PlexWatchlistItem)
            .filter(
                PlexWatchlistItem.excluded.is_(False),
                PlexWatchlistItem.status == "new",
            )
            .all()
        )
        if not rows:
            return {"mode": default_mode, "attempted": 0, "by_mode": {}}
        users_by_id = {u.plex_user_id: u for u in session.query(PlexWatchlistUser).all()}
        item_ids = [int(r.id) for r in rows]
        mems_by_item: dict[int, list[str]] = {i: [] for i in item_ids}
        if item_ids:
            for mem in (
                session.query(PlexWatchlistMembership)
                .filter(PlexWatchlistMembership.watchlist_item_id.in_(item_ids))
                .all()
            ):
                mems_by_item.setdefault(int(mem.watchlist_item_id), []).append(str(mem.plex_user_id))
        by_mode: dict[str, list[int]] = {}
        for row in rows:
            rid = int(row.id)
            # Stale memberships from disabled / policy-blocked people must not promote
            # titles past the account default (e.g. leftover add_and_search override).
            members = [
                users_by_id[uid]
                for uid in mems_by_item.get(rid, [])
                if uid in users_by_id and _user_counts_for_on_new(users_by_id[uid])
            ]
            mode = effective_on_new_for_users(members, default=default_mode)
            by_mode.setdefault(mode, []).append(rid)
    finally:
        session.close()

    attempted = 0
    combined: dict[str, Any] = {
        "mode": default_mode,
        "by_mode": {k: len(v) for k, v in by_mode.items()},
        "added": 0,
        "skipped": 0,
        "failed": 0,
    }
    for mode, ids in by_mode.items():
        if mode == "manual" or not ids:
            continue
        search = mode == "add_and_search"
        monitored = mode != "add_only"
        summary = add_watchlist_items_to_arr(ids, search=search, monitored=monitored)
        attempted += len(ids)
        combined["added"] = int(combined.get("added") or 0) + int(summary.get("added") or 0)
        combined["skipped"] = int(combined.get("skipped") or 0) + int(summary.get("skipped") or 0)
        combined["failed"] = int(combined.get("failed") or 0) + int(summary.get("failed") or 0)
    combined["attempted"] = attempted
    return combined


def set_excluded(item_ids: Iterable[int], excluded: bool) -> int:
    ids = [int(i) for i in item_ids]
    session = get_session()
    try:
        rows = session.query(PlexWatchlistItem).filter(PlexWatchlistItem.id.in_(ids)).all()
        now = _utcnow()
        for row in rows:
            row.excluded = bool(excluded)
            row.updated_at = now
            if excluded:
                row.status = "excluded"
                row.last_error = None
                row.excluded_at = now
            else:
                row.excluded_at = None
        if not excluded:
            _apply_coverage_statuses(session, rows)
        session.commit()
        return len(rows)
    finally:
        session.close()


def list_watchlist_users() -> list[dict[str, Any]]:
    session = get_session()
    try:
        rows = (
            session.query(PlexWatchlistUser)
            .order_by(PlexWatchlistUser.is_admin.desc(), PlexWatchlistUser.title.asc())
            .all()
        )
        return [_serialize_user(u) for u in rows]
    finally:
        session.close()


def list_watchlist_items(
    *,
    status: str | None = None,
    limit: int = 500,
    plex_user_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    from sqlalchemy import exists

    session = get_session()
    try:
        users_by_id = {u.plex_user_id: u for u in session.query(PlexWatchlistUser).all()}
        q = session.query(PlexWatchlistItem).order_by(PlexWatchlistItem.last_seen_at.desc())
        filter_ids = [str(x).strip() for x in (plex_user_ids or []) if str(x).strip()]
        if filter_ids:
            # EXISTS avoids SELECT DISTINCT over the JSON `extra` column (Postgres has no
            # equality operator for json, which broke user-filtered list queries).
            q = q.filter(
                exists().where(
                    PlexWatchlistMembership.watchlist_item_id == PlexWatchlistItem.id,
                    PlexWatchlistMembership.plex_user_id.in_(filter_ids),
                )
            )
        if status and status != "all":
            if status == "excluded":
                q = q.filter(PlexWatchlistItem.excluded.is_(True))
            else:
                q = q.filter(PlexWatchlistItem.status == status, PlexWatchlistItem.excluded.is_(False))
        rows = q.limit(max(1, min(int(limit or 500), 2000))).all()
        from services.watchlist.remap import ensure_arr_remap_cached

        # Cache Arr remaps for queue/accepted rows so UI can show Will add as / Library link.
        try:
            ensure_arr_remap_cached(session, rows)
            session.commit()
        except Exception:
            session.rollback()
        item_ids = [int(r.id) for r in rows]
        mems_by_item: dict[int, list[PlexWatchlistMembership]] = {i: [] for i in item_ids}
        if item_ids:
            for mem in (
                session.query(PlexWatchlistMembership)
                .filter(PlexWatchlistMembership.watchlist_item_id.in_(item_ids))
                .all()
            ):
                mems_by_item.setdefault(int(mem.watchlist_item_id), []).append(mem)
        from services.watchlist.catalog import enrich_watchlist_items

        enrichments = enrich_watchlist_items(session, rows)
        return [
            _serialize_item(
                r,
                memberships=mems_by_item.get(int(r.id), []),
                users_by_id=users_by_id,
                library=enrichments.get(int(r.id)),
            )
            for r in rows
        ]
    finally:
        session.close()


def watchlist_summary() -> dict[str, Any]:
    session = get_session()
    try:
        rows = session.query(PlexWatchlistItem).all()
        user_count = session.query(PlexWatchlistUser).count()
        from services.watchlist.catalog import enrich_watchlist_items

        enrichments = enrich_watchlist_items(session, rows) if rows else {}
    finally:
        session.close()
    counts = {"new": 0, "added": 0, "excluded": 0, "failed": 0, "total": len(rows)}
    coverage_counts = {"none": 0, "partial": 0, "complete": 0}
    for r in rows:
        if r.excluded or r.status == "excluded":
            counts["excluded"] += 1
        elif r.status == "in_arr":
            counts["new"] += 1
        elif r.status in counts:
            counts[r.status] += 1
        else:
            counts["new"] += 1
        cov = str((enrichments.get(int(r.id)) or {}).get("coverage") or "none")
        if cov in coverage_counts:
            coverage_counts[cov] += 1
        else:
            coverage_counts["none"] += 1
    enabled = bool(getattr(settings, "ENABLE_PLEX_WATCHLIST_SYNC", False))
    last = getattr(settings, "WATCHLIST_LAST_POLL_AT", None)
    last_error = getattr(settings, "WATCHLIST_LAST_ERROR", None)
    plex_url = str(getattr(settings, "PLEX_URL", "") or "").strip()
    plex_enabled = bool(getattr(settings, "ENABLE_PLEX", False))
    token_present = bool(plex_watchlist_token())
    return {
        "enabled": enabled,
        # Token alone is not enough: disconnect clears URL/enable but blank secrets
        # are retained on save, so leftover tokens can outlive a removed connection.
        "has_token": token_present,
        "plex_ready": bool(plex_enabled and plex_url and token_present),
        "on_new": watchlist_action_mode(),
        "tv_request_scope": default_tv_request_scope(),
        "arr_tags": str(getattr(settings, "WATCHLIST_ARR_TAGS", "") or "placeholdarr-watchlist"),
        "radarr_targets": arr_targets_for_media("movie"),
        "sonarr_targets": arr_targets_for_media("show"),
        "last_poll_at": last,
        "last_error": last_error or None,
        "user_count": int(user_count or 0),
        "counts": counts,
        "coverage_counts": coverage_counts,
    }


def _serialize_user(u: PlexWatchlistUser) -> dict[str, Any]:
    from services.watchlist.roster import effective_sync_enabled, home_profile_detail

    sync_on = bool(u.sync_enabled)
    rel = getattr(u, "relationship", None)
    rel_s = str(rel or "").strip().lower()
    block = getattr(u, "block_reason", None)
    effective = effective_sync_enabled(
        sync_enabled=sync_on,
        relationship=str(rel) if rel else None,
        is_protected=bool(u.is_protected),
        block_reason=str(block) if block else None,
    )
    meta = u.settings_override if isinstance(u.settings_override, dict) else {}
    on_home = bool(meta.get("on_plex_home")) if "on_plex_home" in meta else rel_s == "home"
    home_profile = meta.get("home_profile")
    if rel_s == "home" and not home_profile:
        home_profile = home_profile_detail(
            in_home=True,
            is_restricted=bool(u.is_restricted),
            is_protected=bool(u.is_protected),
        )
    on_new = on_new_override_for_user(u)
    not_found = str(block or "").strip() == "not_found"
    sole_titles = None
    if not_found:
        try:
            from services.watchlist.roster import sole_title_count_for_user

            sole_titles = sole_title_count_for_user(str(u.plex_user_id))
        except Exception:
            sole_titles = None
    return {
        "plex_user_id": u.plex_user_id,
        "plex_uuid": u.plex_uuid,
        "username": u.username,
        "title": u.title,
        "thumb_url": u.thumb_url,
        "kind": str(getattr(u, "kind", None) or "unknown"),
        "relationship": rel,
        "fetch_method": getattr(u, "fetch_method", None),
        "block_reason": block,
        "not_found": not_found,
        "sole_title_count": sole_titles,
        "on_plex_home": bool(on_home),
        "home_profile": str(home_profile) if home_profile else None,
        "is_admin": bool(u.is_admin),
        "is_restricted": bool(u.is_restricted),
        "is_protected": bool(u.is_protected),
        "is_guest": bool(u.is_guest),
        "sync_enabled": sync_on,
        "sync_effective": effective,
        "on_new": on_new,
        "settings_override": u.settings_override,
        "last_synced_at": u.last_synced_at.isoformat() if u.last_synced_at else None,
        "last_sync_error": u.last_sync_error,
    }


def _serialize_item(
    r: PlexWatchlistItem,
    *,
    memberships: list[PlexWatchlistMembership] | None = None,
    users_by_id: dict[str, PlexWatchlistUser] | None = None,
    library: dict[str, Any] | None = None,
) -> dict[str, Any]:
    users_out: list[dict[str, Any]] = []
    for mem in memberships or []:
        uid = str(mem.plex_user_id or "")
        user = (users_by_id or {}).get(uid)
        users_out.append(
            {
                "plex_user_id": uid,
                "title": (user.title if user else uid),
                "username": (user.username if user else None),
                "thumb_url": (user.thumb_url if user else None),
                "is_admin": bool(user.is_admin) if user else False,
            }
        )
    users_out.sort(key=lambda u: (0 if u.get("is_admin") else 1, str(u.get("title") or "").lower()))
    from services.watchlist.remap import get_cached_arr_remap, normalize_title

    thumb = None
    if isinstance(r.extra, dict):
        thumb = r.extra.get("thumb") or None
    status = "excluded" if r.excluded else r.status
    if status == "in_arr":
        status = "new"
    lib = library if isinstance(library, dict) else None
    arr_remap = get_cached_arr_remap(r.extra)
    # Prefer catalog Arr title when present so Accepted shows the library identity.
    if isinstance(lib, dict) and lib.get("arr_title"):
        catalog_title = str(lib.get("arr_title") or "").strip()
        if catalog_title and normalize_title(catalog_title) != normalize_title(r.title):
            if not isinstance(arr_remap, dict) or not arr_remap.get("remapped"):
                arr_remap = {
                    "remapped": True,
                    "watchlist_title": r.title,
                    "arr_title": catalog_title,
                    "arr_year": lib.get("arr_year"),
                    "matched_via_alternate": True,
                }
            elif not arr_remap.get("arr_title"):
                arr_remap = {**arr_remap, "arr_title": catalog_title, "arr_year": lib.get("arr_year")}
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
        "status": status,
        "excluded": bool(r.excluded),
        "last_error": r.last_error,
        "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
        "added_at": r.added_at.isoformat() if r.added_at else None,
        "excluded_at": r.excluded_at.isoformat() if getattr(r, "excluded_at", None) else None,
        "arr_instance_key": r.arr_instance_key,
        "thumb": thumb,
        "users": users_out,
        "library": lib,
        "arr_remap": arr_remap,
    }


def _persist_config_values(values: dict[str, str]) -> None:
    """Best-effort write of string AppConfig keys + runtime settings attrs."""
    if not values:
        return
    try:
        from services.postgres.models import AppConfig

        session = get_session()
        try:
            for key, value in values.items():
                row = session.query(AppConfig).filter(AppConfig.key == key).first()
                if row is None:
                    session.add(AppConfig(key=key, value=value))
                else:
                    row.value = value
                try:
                    setattr(settings, key, value)
                except Exception:
                    pass
            session.commit()
        finally:
            session.close()
    except Exception as exc:
        logger.debug(f"watchlist config persist skipped: {exc}", extra={"emoji_type": "debug"})


def _persist_poll_meta(*, at: str | None, error: str | None) -> None:
    """Best-effort write of last poll metadata into AppConfig + runtime settings."""
    payload: dict[str, str] = {"WATCHLIST_LAST_ERROR": error or ""}
    if at is not None:
        payload["WATCHLIST_LAST_POLL_AT"] = at
    _persist_config_values(payload)


def _hours_since_iso(raw: str | None) -> float | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (_utcnow() - ts.astimezone(timezone.utc)).total_seconds() / 3600.0)
    except Exception:
        return None


def watchlist_reconcile_hours() -> float:
    try:
        reconcile_h = float(getattr(settings, "WATCHLIST_FULL_RECONCILE_HOURS", 2.0) or 2.0)
    except (TypeError, ValueError):
        reconcile_h = 2.0
    return max(0.25, reconcile_h)


def watchlist_reconcile_due() -> bool:
    since = _hours_since_iso(str(getattr(settings, "WATCHLIST_LAST_FULL_FETCH_AT", "") or "") or None)
    return since is None or since >= watchlist_reconcile_hours()


def next_watchlist_reconcile_at() -> datetime | None:
    """UTC datetime when the next full reconcile is due (None if never synced)."""
    raw = str(getattr(settings, "WATCHLIST_LAST_FULL_FETCH_AT", "") or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        from datetime import timedelta

        return ts.astimezone(timezone.utc) + timedelta(hours=watchlist_reconcile_hours())
    except Exception:
        return None


def _load_seen_guids(key: str) -> set[str]:
    import json

    raw = str(getattr(settings, key, "") or "").strip()
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except Exception:
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x).strip() for x in data if str(x or "").strip()}


def _store_seen_guids(key: str, guids: set[str]) -> None:
    import json

    ordered = sorted(guids)
    if len(ordered) > _SEEN_GUID_CAP:
        ordered = ordered[-_SEEN_GUID_CAP:]
    _persist_config_values({key: json.dumps(ordered)})


def _roster_dicts_from_db() -> list[dict[str, Any]]:
    """Build fetch-ready roster rows from the DB (no live discover)."""
    session = get_session()
    try:
        rows = session.query(PlexWatchlistUser).all()
        out: list[dict[str, Any]] = []
        for u in rows:
            meta = u.settings_override if isinstance(u.settings_override, dict) else {}
            numeric = str(meta.get("plex_numeric_id") or "").strip()
            out.append(
                {
                    "plex_user_id": str(u.plex_user_id or "").strip(),
                    "plex_uuid": str(u.plex_uuid or "").strip() or None,
                    "plex_numeric_id": numeric if numeric.isdigit() else None,
                    "username": u.username,
                    "title": u.title,
                    "thumb_url": u.thumb_url,
                    "kind": str(getattr(u, "kind", None) or "unknown"),
                    "relationship": getattr(u, "relationship", None),
                    "fetch_method": getattr(u, "fetch_method", None),
                    "is_admin": bool(u.is_admin),
                    "is_restricted": bool(u.is_restricted),
                    "is_protected": bool(u.is_protected),
                    "is_guest": bool(u.is_guest),
                    "sync_enabled": bool(u.sync_enabled),
                }
            )
        return out
    finally:
        session.close()


def resolve_rss_authors_to_user_ids(authors: Iterable[str]) -> set[str]:
    """Map RSS ``author`` UUIDs to roster ``plex_user_id`` values (case-insensitive)."""
    wanted = {str(a or "").strip().lower() for a in authors if str(a or "").strip()}
    if not wanted:
        return set()
    matched: set[str] = set()
    session = get_session()
    try:
        for u in session.query(PlexWatchlistUser).all():
            uid = str(u.plex_user_id or "").strip()
            uuid = str(u.plex_uuid or "").strip()
            if uid.lower() in wanted or (uuid and uuid.lower() in wanted):
                matched.add(uid)
    finally:
        session.close()
    return matched


def owner_plex_user_id() -> str | None:
    session = get_session()
    try:
        row = (
            session.query(PlexWatchlistUser)
            .filter(
                (PlexWatchlistUser.kind == "owner")
                | (PlexWatchlistUser.relationship == "owner")
                | (PlexWatchlistUser.is_admin.is_(True))
            )
            .order_by(PlexWatchlistUser.is_admin.desc())
            .first()
        )
        if row is None:
            return None
        return str(row.plex_user_id or "").strip() or None
    finally:
        session.close()


def poll_rss_for_changed_authors() -> dict[str, Any]:
    """Poll owner + friends RSS; return new authors and feed availability.

    Updates etag/hash/seen-guid state when feeds change. Does not fetch watchlists.
    """
    result: dict[str, Any] = {
        "rss_available": False,
        "self_rss_available": False,
        "friends_rss_available": False,
        "self_changed": False,
        "friends_changed": False,
        "author_ids": set(),
        "owner_changed": False,
    }
    if not bool(getattr(settings, "WATCHLIST_USE_RSS_HINT", True)):
        return result

    feeds = ensure_watchlist_rss_feeds()
    self_url = str(feeds.get("watchlist") or getattr(settings, "WATCHLIST_RSS_SELF_URL", "") or "").strip()
    friends_url = str(
        feeds.get("friendsWatchlist") or getattr(settings, "WATCHLIST_RSS_FRIENDS_URL", "") or ""
    ).strip()
    persist_urls: dict[str, str] = {}
    if self_url:
        persist_urls["WATCHLIST_RSS_SELF_URL"] = self_url
    if friends_url:
        persist_urls["WATCHLIST_RSS_FRIENDS_URL"] = friends_url
    # Clear a stale friends URL when Plex no longer returns that feed.
    if not friends_url and str(getattr(settings, "WATCHLIST_RSS_FRIENDS_URL", "") or "").strip():
        persist_urls["WATCHLIST_RSS_FRIENDS_URL"] = ""
    if persist_urls:
        _persist_config_values(persist_urls)

    result["self_rss_available"] = bool(self_url)
    result["friends_rss_available"] = bool(friends_url)
    if not self_url and not friends_url:
        return result
    result["rss_available"] = True

    authors: set[str] = set()
    rss_meta: dict[str, str] = {}

    if self_url:
        self_poll = poll_watchlist_rss_feed(
            self_url,
            etag=str(getattr(settings, "WATCHLIST_RSS_SELF_ETAG", "") or "") or None,
            content_hash=str(getattr(settings, "WATCHLIST_RSS_SELF_HASH", "") or "") or None,
        )
        result["self_changed"] = bool(self_poll.get("changed"))
        if self_poll.get("etag"):
            rss_meta["WATCHLIST_RSS_SELF_ETAG"] = str(self_poll.get("etag"))
        if self_poll.get("content_hash"):
            rss_meta["WATCHLIST_RSS_SELF_HASH"] = str(self_poll.get("content_hash"))
        if self_poll.get("changed"):
            # Feed body can change without new titles (CDN/metadata noise). Only sync
            # the owner when the feed has item guids we have not seen before.
            prev = _load_seen_guids("WATCHLIST_RSS_SELF_GUIDS")
            items = self_poll.get("items") if isinstance(self_poll.get("items"), list) else []
            new_guids: set[str] = set()
            had_new_item = False
            for item in items:
                if not isinstance(item, dict):
                    continue
                guid = str(item.get("guid") or "").strip()
                if guid:
                    new_guids.add(guid)
                    if guid not in prev:
                        had_new_item = True
                        author = str(item.get("author") or "").strip()
                        if author:
                            authors.add(author.lower())
            if new_guids:
                _store_seen_guids("WATCHLIST_RSS_SELF_GUIDS", prev | new_guids)
            if had_new_item:
                result["owner_changed"] = True
                result["self_changed"] = True
                oid = owner_plex_user_id()
                if oid:
                    authors.add(oid.lower())
            else:
                # Hash flipped, same titles: do not treat as a watchlist change.
                result["self_changed"] = False

    if friends_url:
        friends_poll = poll_watchlist_rss_feed(
            friends_url,
            etag=str(getattr(settings, "WATCHLIST_RSS_FRIENDS_ETAG", "") or "") or None,
            content_hash=str(getattr(settings, "WATCHLIST_RSS_FRIENDS_HASH", "") or "") or None,
        )
        result["friends_changed"] = bool(friends_poll.get("changed"))
        if friends_poll.get("etag"):
            rss_meta["WATCHLIST_RSS_FRIENDS_ETAG"] = str(friends_poll.get("etag"))
        if friends_poll.get("content_hash"):
            rss_meta["WATCHLIST_RSS_FRIENDS_HASH"] = str(friends_poll.get("content_hash"))
        if friends_poll.get("changed"):
            prev = _load_seen_guids("WATCHLIST_RSS_FRIENDS_GUIDS")
            items = friends_poll.get("items") if isinstance(friends_poll.get("items"), list) else []
            new_guids: set[str] = set()
            for item in items:
                if not isinstance(item, dict):
                    continue
                guid = str(item.get("guid") or "").strip()
                author = str(item.get("author") or "").strip().lower()
                if guid:
                    new_guids.add(guid)
                    if guid not in prev and author:
                        authors.add(author)
            if new_guids:
                _store_seen_guids("WATCHLIST_RSS_FRIENDS_GUIDS", prev | new_guids)

    if rss_meta:
        _persist_config_values(rss_meta)

    # Resolve authors to roster ids (owner id already a roster key when added above).
    resolved = resolve_rss_authors_to_user_ids(authors)
    # Keep unresolved author strings that already match a roster id casing.
    for a in authors:
        if a and a not in {x.lower() for x in resolved}:
            # resolve_rss_authors only returns matched roster ids; drop orphans.
            pass
    result["author_ids"] = resolved
    return result


def run_watchlist_targeted_sync(
    user_ids: Iterable[str],
    *,
    trigger: str = "rss",
) -> dict[str, Any]:
    """Fetch + upsert only the given roster users (RSS author targeting).

    Does not advance ``WATCHLIST_LAST_FULL_FETCH_AT`` (reconcile still required for
    removals and Home-only people). Records a task run only when at least one id
    is requested.
    """
    targets = {str(u or "").strip() for u in user_ids if str(u or "").strip()}
    summary: dict[str, Any] = {
        "ok": False,
        "trigger": trigger,
        "targeted": True,
        "targets": sorted(targets),
    }
    if not targets:
        summary.update({"ok": True, "skipped": "no_targets"})
        return summary
    if not bool(getattr(settings, "ENABLE_PLEX_WATCHLIST_SYNC", False)):
        summary["skipped"] = "disabled"
        return summary
    if not plex_watchlist_token():
        raise RuntimeError("No Plex token for watchlist (configure PLEX_TOKEN or sign in with Plex)")

    from services.task_run_history import get_working_run

    if get_working_run("watchlist_sync"):
        summary["skipped"] = "already_running"
        return summary

    task_run_id = None
    try:
        task_run_id = begin_task_run(task_key="watchlist_sync", trigger=trigger)
    except Exception as exc:
        logger.debug(f"watchlist targeted begin_task_run skipped: {exc}", extra={"emoji_type": "debug"})

    try:
        roster_users = _roster_dicts_from_db()
        # Intersect with sync_enabled.
        enabled_targets: set[str] = set()
        for u in roster_users:
            uid = str(u.get("plex_user_id") or "").strip()
            if uid in targets and bool(u.get("sync_enabled", True)):
                enabled_targets.add(uid)
        if not enabled_targets:
            summary.update({"ok": True, "skipped": "targets_disabled", "upsert": {"fetched": 0}})
            _persist_poll_meta(at=_utcnow().isoformat(), error=None)
            if task_run_id:
                finish_task_run(task_run_id, status="done", summary=summary)
            return summary

        target_labels = [
            str(u.get("title") or u.get("username") or u.get("plex_user_id") or "").strip()
            for u in roster_users
            if str(u.get("plex_user_id") or "").strip() in enabled_targets
        ]
        target_labels = [t for t in target_labels if t]
        logger.info(
            f"Watchlist targeted sync for {len(enabled_targets)} user(s) "
            f"(trigger={trigger}): {', '.join(target_labels[:12])}"
            + ("…" if len(target_labels) > 12 else ""),
            extra={"emoji_type": "info"},
        )
        account_payload = fetch_watchlists_for_account(
            limit_per_user=1000,
            enabled_user_ids=enabled_targets,
            roster_users=roster_users,
        )
        by_user = account_payload.get("by_user") if isinstance(account_payload, dict) else {}
        for uid, items in (by_user or {}).items():
            label = next(
                (
                    str(u.get("title") or u.get("username") or uid)
                    for u in roster_users
                    if str(u.get("plex_user_id") or "").strip() == str(uid)
                ),
                str(uid),
            )
            count = len(items) if isinstance(items, list) else 0
            logger.info(
                f"Watchlist targeted fetch: {label} → {count} title(s)",
                extra={"emoji_type": "info"},
            )
        upsert = upsert_watchlist_from_home(account_payload)
        log_watchlist_upsert_result(upsert, context="Watchlist targeted sync")
        auto = auto_add_new_items()
        if isinstance(auto, dict) and int(auto.get("attempted") or 0) > 0:
            logger.info(
                f"Watchlist auto-add: added={auto.get('added')} skipped={auto.get('skipped')} "
                f"failed={auto.get('failed')} mode={auto.get('mode')}",
                extra={"emoji_type": "info"},
            )
        progress = build_watchlist_task_progress(
            upsert,
            auto,
            trigger=trigger,
            source="rss_targeted",
            targets=target_labels,
        )
        summary.update(
            {
                "ok": True,
                "upsert": upsert,
                "auto_add": auto,
                "fetched_users": len(account_payload.get("by_user") or {}),
                "source": "rss_targeted",
                "progress": progress,
            }
        )
        _persist_poll_meta(at=_utcnow().isoformat(), error=None)
        if task_run_id:
            finish_task_run(task_run_id, status="done", summary=summary)
        return summary
    except Exception as exc:
        logger.error(f"Watchlist targeted sync failed: {exc}", extra={"emoji_type": "error"})
        summary["error"] = str(exc)
        _persist_poll_meta(at=_utcnow().isoformat(), error=str(exc))
        if task_run_id:
            finish_task_run(task_run_id, status="failed", summary=summary, error_message=str(exc))
        return summary


def _watchlist_rss_gate(*, force_full: bool) -> dict[str, Any]:
    """Ensure RSS feeds and decide whether a full fetch is needed."""
    result: dict[str, Any] = {
        "use_rss": bool(getattr(settings, "WATCHLIST_USE_RSS_HINT", True)),
        "force_full": force_full,
        "need_full": True,
        "self_changed": True,
        "friends_changed": True,
        "rss_available": False,
        "reconcile_due": True,
    }
    if not result["use_rss"]:
        return result

    feeds = ensure_watchlist_rss_feeds()
    self_url = str(feeds.get("watchlist") or getattr(settings, "WATCHLIST_RSS_SELF_URL", "") or "").strip()
    friends_url = str(
        feeds.get("friendsWatchlist") or getattr(settings, "WATCHLIST_RSS_FRIENDS_URL", "") or ""
    ).strip()
    persist: dict[str, str] = {}
    if self_url:
        persist["WATCHLIST_RSS_SELF_URL"] = self_url
    if friends_url:
        persist["WATCHLIST_RSS_FRIENDS_URL"] = friends_url
    if persist:
        _persist_config_values(persist)

    if not self_url and not friends_url:
        result["rss_available"] = False
        return result

    result["rss_available"] = True
    if force_full:
        # Still ensure feeds above; skip change detection and always pull.
        return result

    self_poll = (
        poll_watchlist_rss_feed(
            self_url,
            etag=str(getattr(settings, "WATCHLIST_RSS_SELF_ETAG", "") or "") or None,
            content_hash=str(getattr(settings, "WATCHLIST_RSS_SELF_HASH", "") or "") or None,
        )
        if self_url
        else {"changed": False, "available": False}
    )
    friends_poll = (
        poll_watchlist_rss_feed(
            friends_url,
            etag=str(getattr(settings, "WATCHLIST_RSS_FRIENDS_ETAG", "") or "") or None,
            content_hash=str(getattr(settings, "WATCHLIST_RSS_FRIENDS_HASH", "") or "") or None,
        )
        if friends_url
        else {"changed": False, "available": False}
    )
    result["self_changed"] = bool(self_poll.get("changed"))
    result["friends_changed"] = bool(friends_poll.get("changed"))

    rss_meta: dict[str, str] = {}
    if self_poll.get("etag"):
        rss_meta["WATCHLIST_RSS_SELF_ETAG"] = str(self_poll.get("etag"))
    if self_poll.get("content_hash"):
        rss_meta["WATCHLIST_RSS_SELF_HASH"] = str(self_poll.get("content_hash"))
    if friends_poll.get("etag"):
        rss_meta["WATCHLIST_RSS_FRIENDS_ETAG"] = str(friends_poll.get("etag"))
    if friends_poll.get("content_hash"):
        rss_meta["WATCHLIST_RSS_FRIENDS_HASH"] = str(friends_poll.get("content_hash"))
    if rss_meta:
        _persist_config_values(rss_meta)

    reconcile_due = watchlist_reconcile_due()
    result["reconcile_due"] = reconcile_due
    # RSS change alone no longer forces a full roster pull (targeted loop handles that).
    # Full pull when reconcile is due, RSS unavailable, or force_full.
    result["need_full"] = bool(force_full or reconcile_due or not result["rss_available"])
    return result


def run_watchlist_sync(*, trigger: str = "manual") -> dict[str, Any]:
    """Full owner + friends poll (+ optional auto-add). Records a scheduled task run when possible.

    Used for manual / startup / reconcile / no-RSS fallback. Light RSS author targeting
    lives in ``run_watchlist_targeted_sync`` via the dedicated RSS loop.
    """
    task_run_id = None
    try:
        task_run_id = begin_task_run(task_key="watchlist_sync", trigger=trigger)
    except Exception as exc:
        logger.debug(f"watchlist begin_task_run skipped: {exc}", extra={"emoji_type": "debug"})

    summary: dict[str, Any] = {"ok": False, "trigger": trigger}
    phases = None
    try:
        trig = str(trigger or "").lower()
        if not bool(getattr(settings, "ENABLE_PLEX_WATCHLIST_SYNC", False)) and trig in {
            "scheduled",
            "reconcile",
            "fallback",
            "rss",
        }:
            summary["skipped"] = "disabled"
            if task_run_id:
                finish_task_run(task_run_id, status="skipped", summary=summary, skip_reason="disabled")
            return summary
        if not plex_watchlist_token():
            raise RuntimeError("No Plex token for watchlist (configure PLEX_TOKEN or sign in with Plex)")

        if task_run_id:
            try:
                from services.task_live_progress import WATCHLIST_SYNC_PHASES, seed_sync_live_phases
                from services.task_run_phases import TaskRunPhaseTracker

                seed_sync_live_phases(
                    int(task_run_id),
                    kind="watchlist_sync",
                    label="Watchlist sync",
                    phase_defs=WATCHLIST_SYNC_PHASES,
                )
                phases = TaskRunPhaseTracker(int(task_run_id), started_at=_utcnow())
            except Exception as live_exc:
                logger.debug(
                    f"watchlist_sync live seed skipped: {live_exc}",
                    extra={"emoji_type": "debug"},
                )

        force_full = trig in {"manual", "startup", "reconcile", "fallback"}

        # Light roster refresh even when RSS skips a full title pull.
        roster_meta: dict[str, Any] = {}
        try:
            from services.watchlist.roster import refresh_roster

            roster_meta = refresh_roster(preserve_sync_toggles=True)
            summary["roster"] = {
                "users": roster_meta.get("users"),
                "created": roster_meta.get("created"),
                "updated": roster_meta.get("updated"),
            }
        except Exception as roster_exc:
            logger.warning(f"Watchlist roster refresh failed: {roster_exc}", extra={"emoji_type": "warning"})
            summary["roster_error"] = str(roster_exc)

        if phases is not None:
            phases.begin("rss", "RSS check")
        rss_gate = _watchlist_rss_gate(force_full=force_full)
        summary["rss"] = {
            "available": bool(rss_gate.get("rss_available")),
            "need_full": bool(rss_gate.get("need_full")),
            "self_changed": bool(rss_gate.get("self_changed")),
            "friends_changed": bool(rss_gate.get("friends_changed")),
            "reconcile_due": bool(rss_gate.get("reconcile_due")),
        }
        if phases is not None:
            if not rss_gate.get("use_rss"):
                phases.end("rss", status="skipped", done=0, total=0, unit_label="feeds")
            else:
                phases.end(
                    "rss",
                    done=1 if rss_gate.get("rss_available") else 0,
                    total=1,
                    unit_label="feeds",
                )

        if not rss_gate.get("need_full"):
            logger.info(
                f"Watchlist sync skipped full fetch (trigger={trig}; "
                f"rss available, unchanged, reconcile not due)",
                extra={"emoji_type": "info"},
            )
            summary.update(
                {
                    "ok": True,
                    "skipped_full_fetch": True,
                    "upsert": {"fetched": 0},
                    "auto_add": {},
                    "progress": {
                        "sections": [
                            {
                                "key": "watchlist_summary",
                                "name": "Watchlist summary",
                                "status": "done",
                                "metrics": [
                                    {"label": "Trigger", "value": trig or "—"},
                                    {"label": "Result", "value": "Skipped (RSS unchanged)"},
                                    {
                                        "label": "Self RSS",
                                        "value": "changed" if rss_gate.get("self_changed") else "unchanged",
                                    },
                                    {
                                        "label": "Friends RSS",
                                        "value": "changed" if rss_gate.get("friends_changed") else "unchanged",
                                    },
                                ],
                            }
                        ],
                        "running": False,
                    },
                }
            )
            if phases is not None:
                phases.begin("fetch", "Watchlist fetch")
                phases.end("fetch", status="skipped", done=0, total=0, unit_label="users")
                phases.begin("upsert", "Catalog upsert")
                phases.end("upsert", status="skipped", done=0, total=0, unit_label="titles")
                phases.begin("auto_add", "Auto-add to Arr")
                phases.end("auto_add", status="skipped", done=0, total=0, unit_label="titles")
            _persist_poll_meta(at=_utcnow().isoformat(), error=None)
            if task_run_id:
                finish_task_run(task_run_id, status="done", summary=summary)
            return summary

        enabled_ids: set[str] = set()
        roster_count = 0
        session = get_session()
        try:
            all_users = session.query(PlexWatchlistUser).all()
            roster_count = len(all_users)
            for u in all_users:
                if not bool(u.sync_enabled):
                    continue
                uid = str(u.plex_user_id or "").strip()
                if uid:
                    enabled_ids.add(uid)
        finally:
            session.close()

        if phases is not None:
            phases.begin("fetch", "Watchlist fetch")
        # Empty enabled set means admin disabled everyone (skip pulls). None = no roster yet.
        enabled_arg: set[str] | None = enabled_ids if roster_count > 0 else None
        logger.info(
            f"Watchlist full sync starting (trigger={trig}, enabled_users="
            f"{len(enabled_ids) if roster_count > 0 else 'all'})",
            extra={"emoji_type": "info"},
        )
        account_payload = fetch_watchlists_for_account(
            limit_per_user=1000,
            enabled_user_ids=enabled_arg,
        )
        users_raw = account_payload.get("users") if isinstance(account_payload, dict) else None
        users_count = len(users_raw) if isinstance(users_raw, list) else 0
        fetched_users = (
            len(account_payload.get("by_user") or {}) if isinstance(account_payload, dict) else 0
        )
        by_user = account_payload.get("by_user") if isinstance(account_payload, dict) else {}
        title_total = sum(len(v) for v in (by_user or {}).values() if isinstance(v, list))
        logger.info(
            f"Watchlist fetch complete: {fetched_users}/{users_count} user(s), "
            f"{title_total} title row(s)",
            extra={"emoji_type": "info"},
        )
        if phases is not None:
            phases.end(
                "fetch",
                done=fetched_users,
                total=users_count,
                unit_label="users",
                metrics=[
                    {"label": "Users fetched", "value": fetched_users},
                    {"label": "Users in roster", "value": users_count},
                    {"label": "Title rows", "value": title_total},
                ],
            )

        if phases is not None:
            phases.begin("upsert", "Catalog upsert")
        upsert = upsert_watchlist_from_home(account_payload)
        log_watchlist_upsert_result(upsert, context=f"Watchlist sync ({trig})")
        fetched = int(upsert.get("fetched") or 0) if isinstance(upsert, dict) else 0
        if phases is not None:
            phases.end(
                "upsert",
                done=fetched,
                total=fetched,
                unit_label="titles",
                metrics=metrics_from_watchlist_upsert(upsert),
            )

        if phases is not None:
            phases.begin("auto_add", "Auto-add to Arr")
        auto = auto_add_new_items()
        if isinstance(auto, dict) and int(auto.get("attempted") or 0) > 0:
            logger.info(
                f"Watchlist auto-add: added={auto.get('added')} skipped={auto.get('skipped')} "
                f"failed={auto.get('failed')} mode={auto.get('mode')}",
                extra={"emoji_type": "info"},
            )
        if phases is not None:
            mode = str((auto or {}).get("mode") or "")
            attempted = int((auto or {}).get("attempted") or 0)
            if mode == "manual" or attempted <= 0:
                phases.end("auto_add", status="skipped", done=0, total=0, unit_label="titles")
            else:
                added = int((auto or {}).get("added") or 0)
                phases.end(
                    "auto_add",
                    done=added,
                    total=attempted,
                    unit_label="titles",
                    metrics=[
                        {"label": "Added", "value": added},
                        {"label": "Skipped", "value": int((auto or {}).get("skipped") or 0)},
                        {"label": "Failed", "value": int((auto or {}).get("failed") or 0)},
                        {"label": "Mode", "value": mode or "—"},
                    ],
                )

        progress = build_watchlist_task_progress(
            upsert,
            auto if isinstance(auto, dict) else {},
            trigger=trig,
            source="roster",
        )
        summary.update(
            {
                "ok": True,
                "upsert": upsert,
                "auto_add": auto,
                "source": "roster",
                "progress": progress,
            }
        )
        _persist_poll_meta(at=_utcnow().isoformat(), error=None)
        _persist_config_values({"WATCHLIST_LAST_FULL_FETCH_AT": _utcnow().isoformat()})
        if task_run_id:
            finish_task_run(task_run_id, status="done", summary=summary)
        return summary
    except Exception as exc:
        logger.error(f"Watchlist sync failed: {exc}", extra={"emoji_type": "error"})
        summary["error"] = str(exc)
        _persist_poll_meta(at=_utcnow().isoformat(), error=str(exc))
        if phases is not None:
            try:
                for phase in phases.phases():
                    if str(phase.get("status") or "").lower() == "working":
                        phases.end(str(phase.get("key") or "fetch"), failed=True, status="failed")
                        break
            except Exception:
                pass
        if task_run_id:
            finish_task_run(task_run_id, status="failed", summary=summary, error_message=str(exc))
        return summary
