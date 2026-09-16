"""Rewrite catalog/config instance_key values when Arr slots are renamed.

Swap-safe: renames in one settings save (including A↔B) use temporary keys so
rows are not double-moved.

Also detects URL transplants: a surviving settings row keeps its drawer
``instance_id`` but its URL matches a *removed* peer. That is adopt-by-URL
(keep the removed peer's catalog identity), not a rename of the displaced row.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig, ArrState, Episode, Movie, Season, Series


def _normalize_instance_key(value: Any) -> str:
    key_raw = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9_-]+", "_", key_raw).strip("_-")


def _normalize_arr_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _instance_key_of(item: dict[str, Any]) -> str:
    return _normalize_instance_key(item.get("instance_key") or item.get("key") or item.get("name") or "")


def _arr_type_of(item: dict[str, Any]) -> str:
    return str(item.get("arr_type") or item.get("type") or "").strip().lower()


def _parse_instances(raw: str | None) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def detect_url_transplants(previous_json: str, incoming_json: str) -> list[dict[str, str]]:
    """Detect adopt-by-URL when a surviving row takes a removed peer's URL.

    Slot index does not matter. Any combination works (1←2, 2←4, 1←3, several
    in one save) as long as:

    - incoming row still carries previous identity A (same ``instance_id``)
    - incoming URL no longer matches A
    - incoming URL matches previous identity B that is absent from incoming

    Returns rows with displaced (A) and adopted (B) ids/keys plus the incoming key.
    Each removed B is claimed at most once.
    """
    previous = _parse_instances(previous_json)
    incoming = _parse_instances(incoming_json)
    incoming_ids = {
        str(item.get("instance_id") or "").strip().lower()
        for item in incoming
        if str(item.get("instance_id") or "").strip()
    }
    prev_by_id: dict[str, dict[str, Any]] = {}
    removed_by_url: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in previous:
        iid = str(item.get("instance_id") or "").strip().lower()
        if not iid:
            continue
        prev_by_id[iid] = item
        arr_type = _arr_type_of(item)
        url = _normalize_arr_url(item.get("url"))
        if arr_type not in {"radarr", "sonarr"} or not url:
            continue
        if iid in incoming_ids:
            continue
        removed_by_url.setdefault((arr_type, url), []).append(item)

    claimed_adopted: set[str] = set()
    transplants: list[dict[str, str]] = []
    for item in incoming:
        nid = str(item.get("instance_id") or "").strip().lower()
        if not nid or nid not in prev_by_id:
            continue
        arr_type = _arr_type_of(item)
        if arr_type not in {"radarr", "sonarr"}:
            continue
        prev_self = prev_by_id[nid]
        new_url = _normalize_arr_url(item.get("url"))
        old_url = _normalize_arr_url(prev_self.get("url"))
        if not new_url or new_url == old_url:
            continue
        candidates = removed_by_url.get((arr_type, new_url)) or []
        adopted: dict[str, Any] | None = None
        for candidate in candidates:
            cid = str(candidate.get("instance_id") or "").strip().lower()
            if cid and cid not in claimed_adopted:
                adopted = candidate
                break
        if adopted is None:
            continue
        adopted_id = str(adopted.get("instance_id") or "").strip().lower()
        displaced_key = _instance_key_of(prev_self)
        adopted_key = _instance_key_of(adopted)
        new_key = _instance_key_of(item) or adopted_key
        if not adopted_id or not displaced_key or not adopted_key:
            continue
        claimed_adopted.add(adopted_id)
        transplants.append(
            {
                "arr_type": arr_type,
                "displaced_instance_id": nid,
                "displaced_key": displaced_key,
                "adopted_instance_id": adopted_id,
                "adopted_key": adopted_key,
                "new_key": new_key,
                "url": new_url,
            }
        )
    return transplants


def detect_instance_key_renames(previous_json: str, incoming_json: str) -> list[dict[str, str]]:
    """Return ``{arr_type, old_key, new_key, instance_id}`` for same-id key changes."""
    prev_by_id: dict[str, dict[str, Any]] = {}
    for item in _parse_instances(previous_json):
        iid = str(item.get("instance_id") or "").strip().lower()
        if iid:
            prev_by_id[iid] = item

    renames: list[dict[str, str]] = []
    for item in _parse_instances(incoming_json):
        iid = str(item.get("instance_id") or "").strip().lower()
        if not iid or iid not in prev_by_id:
            continue
        arr_type = _arr_type_of(item)
        if arr_type not in {"radarr", "sonarr"}:
            continue
        old_key = _instance_key_of(prev_by_id[iid])
        new_key = _instance_key_of(item)
        if not old_key or not new_key or old_key == new_key:
            continue
        renames.append(
            {
                "arr_type": arr_type,
                "old_key": old_key,
                "new_key": new_key,
                "instance_id": iid,
            }
        )
    return renames


def prepare_url_transplants(
    previous_json: str,
    incoming_json: str,
    *,
    destination_map_json: str | None = None,
) -> dict[str, Any]:
    """Tombstone displaced catalogs before rename rewrite; scrub dest map / prefs / ArrState.

    Call with the pre-merge incoming payload (drawer still has displaced ids) so
    detection matches the UI save. Safe to call when there are no transplants.
    """
    transplants = detect_url_transplants(previous_json, incoming_json)
    empty = {
        "ok": True,
        "transplants": [],
        "movies_tombstoned": 0,
        "series_tombstoned": 0,
        "seasons_tombstoned": 0,
        "episodes_tombstoned": 0,
        "arr_state_deleted": 0,
        "dest_map_removed": 0,
        "search_modes_updated": 0,
        "movie_ids": [],
        "episode_ids": [],
        "destination_map_json": destination_map_json,
    }
    if not transplants:
        return empty

    rad_keys = {t["displaced_key"] for t in transplants if t["arr_type"] == "radarr"}
    son_keys = {t["displaced_key"] for t in transplants if t["arr_type"] == "sonarr"}
    search_remap = {
        t["displaced_key"]: t["new_key"]
        for t in transplants
        if t["displaced_key"] and t["new_key"] and t["displaced_key"] != t["new_key"]
    }

    session = get_session()
    movie_ids: list[int] = []
    episode_ids: list[int] = []
    movies = series = seasons = episodes = arr_state = dest_removed = search_modes = 0
    out_dest = destination_map_json
    try:
        if rad_keys:
            movie_ids = [
                int(r[0])
                for r in session.query(Movie.id)
                .filter(Movie.is_deleted == False, Movie.instance_key.in_(tuple(rad_keys)))  # noqa: E712
                .all()
            ]
            if movie_ids:
                movies = (
                    session.query(Movie)
                    .filter(Movie.id.in_(tuple(movie_ids)))
                    .update({"is_deleted": True}, synchronize_session=False)
                )

        if son_keys:
            series_ids = [
                int(r[0])
                for r in session.query(Series.id)
                .filter(Series.is_deleted == False, Series.instance_key.in_(tuple(son_keys)))  # noqa: E712
                .all()
            ]
            if series_ids:
                episode_ids = [
                    int(r[0])
                    for r in (
                        session.query(Episode.id)
                        .join(Season, Episode.season_id == Season.id)
                        .filter(
                            Season.series_id.in_(tuple(series_ids)),
                            Episode.is_deleted == False,  # noqa: E712
                        )
                        .all()
                    )
                ]
                series = (
                    session.query(Series)
                    .filter(Series.id.in_(tuple(series_ids)))
                    .update({"is_deleted": True}, synchronize_session=False)
                )
                seasons = (
                    session.query(Season)
                    .filter(Season.series_id.in_(tuple(series_ids)))
                    .update({"is_deleted": True}, synchronize_session=False)
                )
                season_ids = [
                    int(r[0]) for r in session.query(Season.id).filter(Season.series_id.in_(tuple(series_ids))).all()
                ]
                if season_ids:
                    episodes = (
                        session.query(Episode)
                        .filter(Episode.season_id.in_(tuple(season_ids)))
                        .update({"is_deleted": True}, synchronize_session=False)
                    )

        displaced_keys = rad_keys | son_keys
        if displaced_keys:
            stale_states = session.query(ArrState).filter(ArrState.instance_key.in_(tuple(displaced_keys))).all()
            for row in stale_states:
                session.delete(row)
                arr_state += 1

        if displaced_keys:
            out_dest, dest_removed = _strip_destination_map_keys(
                destination_map_json
                if destination_map_json is not None
                else _load_destination_map_raw(session),
                displaced_keys,
            )
            if dest_removed:
                dest_row = session.query(AppConfig).filter(AppConfig.key == "LIBRARY_DESTINATION_MAP_JSON").first()
                if dest_row is not None and out_dest is not None:
                    dest_row.value = out_dest

        if search_remap:
            search_modes = _rewrite_search_mode_rows(
                session,
                [{"old_key": old, "new_key": new} for old, new in search_remap.items()],
            )

        if movie_ids or series:
            from services.series_episode_stats_hooks import bump_library_versions_after_bulk

            # Version bump only: a full stats refresh of every series can take minutes and
            # blocks Settings save. Library UI refreshes; scoped stats catch up later.
            bump_library_versions_after_bulk(session, movies=bool(movie_ids), series=bool(series))

        session.commit()
        logger.info(
            "ARR URL transplant prep "
            f"transplants={len(transplants)} movies={movies} series={series} "
            f"arr_state={arr_state} dest_map={dest_removed} search_modes={search_modes}",
            extra={"emoji_type": "update"},
        )
        for row in transplants:
            logger.info(
                "ARR URL transplant: "
                f"{row['arr_type']} displaced {row['displaced_key']} ({row['displaced_instance_id']}) "
                f"adopted {row['adopted_key']} ({row['adopted_instance_id']}) url={row['url']}",
                extra={"emoji_type": "info"},
            )
        return {
            "ok": True,
            "transplants": transplants,
            "movies_tombstoned": movies,
            "series_tombstoned": series,
            "seasons_tombstoned": seasons,
            "episodes_tombstoned": episodes,
            "arr_state_deleted": arr_state,
            "dest_map_removed": dest_removed,
            "search_modes_updated": search_modes,
            "movie_ids": movie_ids,
            "episode_ids": episode_ids,
            "destination_map_json": out_dest,
        }
    except Exception as exc:
        session.rollback()
        logger.error(f"ARR URL transplant prep failed: {exc}", extra={"emoji_type": "error"})
        return {
            **empty,
            "ok": False,
            "error": str(exc),
            "transplants": transplants,
        }
    finally:
        session.close()


def _load_destination_map_raw(session) -> str:
    dest_row = session.query(AppConfig).filter(AppConfig.key == "LIBRARY_DESTINATION_MAP_JSON").first()
    if dest_row is None or _is_blank_value(dest_row.value):
        return ""
    return dest_row.value if isinstance(dest_row.value, str) else json.dumps(dest_row.value)


def _strip_destination_map_keys(raw: str | None, keys: set[str]) -> tuple[str | None, int]:
    text = str(raw or "").strip()
    if not text or not keys:
        return raw, 0
    try:
        payload = json.loads(text)
    except Exception:
        return raw, 0
    if not isinstance(payload, list):
        return raw, 0
    kept: list[Any] = []
    removed = 0
    for item in payload:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        key = _normalize_instance_key(item.get("instance_key") or "")
        if key in keys:
            removed += 1
            continue
        kept.append(item)
    if not removed:
        return text, 0
    return json.dumps(kept), removed


def _swap_safe_steps(renames: list[dict[str, str]]) -> list[tuple[str, str, str]]:
    """Build (arr_type, from_key, to_key) steps that tolerate cycles/swaps."""
    if not renames:
        return []

    by_type: dict[str, list[dict[str, str]]] = {"radarr": [], "sonarr": []}
    for row in renames:
        by_type.setdefault(row["arr_type"], []).append(row)

    steps: list[tuple[str, str, str]] = []
    for arr_type, rows in by_type.items():
        if not rows:
            continue
        # Phase 1: every old key → unique temp
        temps: dict[str, str] = {}
        for row in rows:
            old = row["old_key"]
            if old in temps:
                continue
            temps[old] = f"__ph_rename_{uuid.uuid4().hex[:12]}__"
            steps.append((arr_type, old, temps[old]))
        # Phase 2: temp → final new key
        for row in rows:
            steps.append((arr_type, temps[row["old_key"]], row["new_key"]))
    return steps


def _rewrite_key_column(session, model, *, old_key: str, new_key: str) -> int:
    if old_key == new_key:
        return 0
    # Unique (tvdbid/tmdbid, instance_key) includes soft-deleted rows. Park
    # tombstones that already occupy new_key so a live rename can land.
    if hasattr(model, "is_deleted"):
        parked = f"__ph_tomb_{uuid.uuid4().hex[:12]}_{new_key}"[:80]
        session.query(model).filter(
            model.instance_key == new_key,
            model.is_deleted == True,  # noqa: E712
        ).update({model.instance_key: parked}, synchronize_session=False)
    return (
        session.query(model)
        .filter(model.instance_key == old_key)
        .update({model.instance_key: new_key}, synchronize_session=False)
    )


def _rewrite_arr_state(session, *, old_key: str, new_key: str) -> int:
    existing_new = session.query(ArrState).filter(ArrState.instance_key == new_key).first()
    old_row = session.query(ArrState).filter(ArrState.instance_key == old_key).first()
    if not old_row:
        return 0
    if existing_new and existing_new.id != old_row.id:
        # Prefer the row being renamed (source). Drop a stale destination shell
        # left by a displaced instance after URL transplant.
        session.delete(existing_new)
    old_row.instance_key = new_key
    return 1


def _rewrite_destination_map_value(raw: str, renames: list[dict[str, str]]) -> tuple[str, int]:
    text = str(raw or "").strip()
    if not text:
        return text, 0
    try:
        payload = json.loads(text)
    except Exception:
        return text, 0
    if not isinstance(payload, list):
        return text, 0

    key_map: dict[tuple[str, str], str] = {}
    for row in renames:
        key_map[(row["arr_type"], row["old_key"])] = row["new_key"]

    changed = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        arr_type = str(item.get("arr_type") or "").strip().lower()
        old = _normalize_instance_key(item.get("instance_key") or "")
        new = key_map.get((arr_type, old))
        if not new or new == old:
            continue
        item["instance_key"] = new
        changed += 1
    if not changed:
        return text, 0
    return json.dumps(payload), changed


_SEARCH_MODE_KEYS = (
    "MOVIE_PLACEHOLDER_SEARCH_MODE",
    "TV_PLACEHOLDER_SEARCH_MODE",
    "MOVIE_PLAYBACK_INSTANCE_MODE",
    "TV_PLAYBACK_INSTANCE_MODE",
)


def _rewrite_search_mode_rows(session, renames: list[dict[str, str]]) -> int:
    # Search modes are not typed; map any old_key → new_key (keys are unique across types in practice
    # only within a type, but prefs are movie/tv specific fields).
    flat: dict[str, str] = {}
    for row in renames:
        flat[row["old_key"]] = row["new_key"]
    if not flat:
        return 0
    changed = 0
    rows = session.query(AppConfig).filter(AppConfig.key.in_(_SEARCH_MODE_KEYS)).all()
    for row in rows:
        current = _normalize_instance_key(row.value)
        if current in flat:
            row.value = flat[current]
            changed += 1
    return changed


def apply_instance_key_renames(
    previous_json: str,
    incoming_json: str,
    *,
    destination_map_json: str | None = None,
) -> dict[str, Any]:
    """Apply swap-safe instance_key rewrites for a settings save.

    Returns stats and optionally a rewritten destination map JSON string.
    """
    renames = detect_instance_key_renames(previous_json, incoming_json)
    if not renames:
        return {
            "ok": True,
            "renames": [],
            "movies_updated": 0,
            "series_updated": 0,
            "arr_state_updated": 0,
            "dest_map_updated": 0,
            "search_modes_updated": 0,
            "destination_map_json": destination_map_json,
        }

    steps = _swap_safe_steps(renames)
    session = get_session()
    movies = series = arr_state = dest_map = search_modes = 0
    out_dest = destination_map_json
    try:
        for arr_type, old_key, new_key in steps:
            if arr_type == "radarr":
                movies += _rewrite_key_column(session, Movie, old_key=old_key, new_key=new_key)
            else:
                series += _rewrite_key_column(session, Series, old_key=old_key, new_key=new_key)
            arr_state += _rewrite_arr_state(session, old_key=old_key, new_key=new_key)

        if destination_map_json is not None:
            rewritten, dest_map = _rewrite_destination_map_value(destination_map_json, renames)
            if dest_map:
                out_dest = rewritten
                dest_row = session.query(AppConfig).filter(AppConfig.key == "LIBRARY_DESTINATION_MAP_JSON").first()
                if dest_row is not None:
                    dest_row.value = rewritten

        # Also rewrite dest map already persisted when caller did not pass one.
        if destination_map_json is None:
            dest_row = session.query(AppConfig).filter(AppConfig.key == "LIBRARY_DESTINATION_MAP_JSON").first()
            if dest_row is not None and not _is_blank_value(dest_row.value):
                raw = dest_row.value if isinstance(dest_row.value, str) else json.dumps(dest_row.value)
                rewritten, n = _rewrite_destination_map_value(str(raw or ""), renames)
                if n:
                    dest_row.value = rewritten
                    dest_map += n
                    out_dest = rewritten

        search_modes = _rewrite_search_mode_rows(session, renames)
        session.commit()
        logger.info(
            "ARR instance_key rename rewrite "
            f"renames={len(renames)} movies={movies} series={series} "
            f"arr_state={arr_state} dest_map={dest_map} search_modes={search_modes}",
            extra={"emoji_type": "update"},
        )
        return {
            "ok": True,
            "renames": renames,
            "movies_updated": movies,
            "series_updated": series,
            "arr_state_updated": arr_state,
            "dest_map_updated": dest_map,
            "search_modes_updated": search_modes,
            "destination_map_json": out_dest,
        }
    except Exception as exc:
        session.rollback()
        logger.error(f"ARR instance_key rename rewrite failed: {exc}", extra={"emoji_type": "error"})
        return {
            "ok": False,
            "error": str(exc),
            "renames": renames,
            "movies_updated": 0,
            "series_updated": 0,
            "arr_state_updated": 0,
            "dest_map_updated": 0,
            "search_modes_updated": 0,
            "destination_map_json": destination_map_json,
        }
    finally:
        session.close()


def _is_blank_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False
