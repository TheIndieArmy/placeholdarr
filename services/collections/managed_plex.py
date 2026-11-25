"""Which Plex libraries this Placeholdarr instance controls for Collections.

Managed = placeholder destinations (primary section IDs, LIBRARY_ROOT / dest map)
plus real-file libraries whose Plex locations overlap configured Arr root folders.
"""

from __future__ import annotations

import json
from typing import Any

from core.config import settings
from core.logger import logger
from services.library_destinations import (
    default_movie_dest_folder,
    default_movie_plex_section_id,
    default_tv_dest_folder,
    default_tv_plex_section_id,
    parse_library_destination_map,
)
from services.media_path_mappings import apply_media_server_path_mappings
from services.media_servers.plex import (
    _normalize_plex_path,
    fetch_plex_section_locations,
    match_plex_sections_for_path,
    prime_plex_section_location_cache,
)


def _norm(path: str | None) -> str:
    return _normalize_plex_path(path).lower()


def _paths_overlap(a: str | None, b: str | None) -> bool:
    aa, bb = _norm(a), _norm(b)
    if not aa or not bb:
        return False
    if aa == bb:
        return True
    return aa.startswith(bb + "/") or bb.startswith(aa + "/")


def _bind_mount_host_aliases(path: str) -> list[str]:
    """If ``path`` is under a Docker bind mount, also return the host-side path.

    Plex locations are usually host paths (e.g. ``/mnt/user/data/placeholdarr/tv``)
    while Placeholdarr in Docker sees ``/placeholdarr/tv``.
    """
    local = _normalize_plex_path(path)
    if not local:
        return []
    aliases: list[str] = []
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    for line in lines:
        # mountinfo: ... mountpoint source ... optional fields ... -
        parts = line.split()
        if len(parts) < 5:
            continue
        mountpoint = _normalize_plex_path(parts[4])
        if not mountpoint or mountpoint == "/":
            continue
        if not (_norm(local) == _norm(mountpoint) or _norm(local).startswith(_norm(mountpoint) + "/")):
            continue
        # Find fs root field after optional fields (separator "-")
        try:
            dash = parts.index("-")
        except ValueError:
            continue
        if dash < 4:
            continue
        # Field 3 is root within the mount source; field after fstype is source device/path for bind
        # For bind mounts, source is often like `/mnt/user/data/placeholdarr`
        source = ""
        if dash + 2 < len(parts):
            source = _normalize_plex_path(parts[dash + 2])
        if not source.startswith("/"):
            continue
        rel = local[len(mountpoint) :] if _norm(local) != _norm(mountpoint) else ""
        aliases.append(source + rel)
    return aliases


def _path_matches_plex_location(local_or_arr_path: str, plex_location: str) -> bool:
    """True when a Placeholdarr/Arr path refers to the same tree as a Plex location."""
    candidates = [local_or_arr_path, *_bind_mount_host_aliases(local_or_arr_path)]
    for candidate in candidates:
        if _paths_overlap(candidate, plex_location):
            return True
        # MEDIA_SERVER_PATH_MAPPINGS_JSON: remote (Plex) → local (container)
        translated = apply_media_server_path_mappings(plex_location)
        if translated and _paths_overlap(candidate, translated):
            return True
    return False


def _watchlist_root_paths() -> list[str]:
    paths: list[str] = []
    for key in ("WATCHLIST_RADARR_TARGETS_JSON", "WATCHLIST_SONARR_TARGETS_JSON"):
        raw = str(getattr(settings, key, "") or "").strip()
        if not raw:
            continue
        try:
            rows = json.loads(raw)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            path = str(row.get("root_folder_path") or row.get("root_folder") or "").strip()
            if path:
                paths.append(path)
    for key in ("WATCHLIST_RADARR_ROOT_FOLDER", "WATCHLIST_SONARR_ROOT_FOLDER"):
        path = str(getattr(settings, key, "") or "").strip()
        if path:
            paths.append(path)
    return paths


def _arr_instance_root_paths() -> list[str]:
    """Best-effort Arr root folders for every configured instance."""
    import requests

    paths: list[str] = []
    instances = list(getattr(settings, "configured_arr_instances", None) or [])
    for item in instances:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").rstrip("/")
        api_key = str(item.get("api_key") or "")
        if not url or not api_key:
            continue
        try:
            resp = requests.get(
                f"{url}/api/v3/rootfolder",
                headers={"X-Api-Key": api_key, "Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            for raw in resp.json() or []:
                if isinstance(raw, dict) and raw.get("path"):
                    paths.append(str(raw["path"]))
        except Exception as exc:
            logger.debug(
                f"managed_plex: root folders skipped for {item.get('instance_key')}: {exc}",
                extra={"emoji_type": "debug"},
            )
    return paths


def _placeholder_section_ids(map_rows: list[dict[str, Any]]) -> set[int]:
    ids: set[int] = set()
    for raw in (default_movie_plex_section_id(), default_tv_plex_section_id()):
        if raw is not None:
            ids.add(int(raw))
    for row in map_rows:
        sid = row.get("plex_section_id")
        if sid is None or str(sid).strip() == "":
            continue
        try:
            ids.add(int(sid))
        except (TypeError, ValueError):
            continue
    return ids


def _placeholder_dest_paths(map_rows: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for folder in (default_movie_dest_folder(), default_tv_dest_folder()):
        if folder:
            paths.append(folder)
    for row in map_rows:
        dest = str(row.get("dest_folder") or "").strip()
        if dest:
            paths.append(dest)
    return paths


def _real_root_paths(map_rows: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for row in map_rows:
        root = str(row.get("arr_root_path") or "").strip()
        if root:
            paths.append(root)
    paths.extend(_watchlist_root_paths())
    paths.extend(_arr_instance_root_paths())
    # Dedupe preserving order
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = _norm(path)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def annotate_managed_plex_sections(
    sections: list[dict[str, Any]],
    *,
    locations_by_id: dict[int, tuple[str, ...]] | None = None,
) -> list[dict[str, Any]]:
    """Attach ``managed`` and ``managed_role`` to each Plex section dict.

    ``managed_role`` is ``placeholder``, ``real``, or ``both``.
    """
    map_rows = parse_library_destination_map()
    placeholder_ids = _placeholder_section_ids(map_rows)
    placeholder_paths = _placeholder_dest_paths(map_rows)
    real_paths = _real_root_paths(map_rows)

    if locations_by_id is None:
        try:
            locations_by_id = dict(prime_plex_section_location_cache() or {})
            if not locations_by_id:
                locations_by_id = dict(fetch_plex_section_locations() or {})
        except Exception as exc:
            logger.warning(
                f"managed_plex: could not load Plex library locations: {exc}",
                extra={"emoji_type": "warning"},
            )
            locations_by_id = {}

    roles: dict[int, set[str]] = {}

    def _add(sid: int, role: str) -> None:
        roles.setdefault(int(sid), set()).add(role)

    for sid in placeholder_ids:
        _add(sid, "placeholder")

    # Path overlap for placeholder dests (and Arr roots) against each section location.
    for sec in sections:
        try:
            sid = int(sec.get("id"))
        except (TypeError, ValueError):
            continue
        locs = list(locations_by_id.get(sid, ()) or ())
        for loc in locs:
            for dest in placeholder_paths:
                if _path_matches_plex_location(dest, loc):
                    _add(sid, "placeholder")
                    break
            for root in real_paths:
                if _path_matches_plex_location(root, loc):
                    _add(sid, "real")
                    break

    # match_plex_sections_for_path is the same matcher used for real-library refresh.
    for root in real_paths:
        try:
            for sid in match_plex_sections_for_path(root) or []:
                _add(int(sid), "real")
        except Exception:
            continue

    annotated: list[dict[str, Any]] = []
    for sec in sections:
        row = dict(sec)
        try:
            sid = int(row.get("id"))
        except (TypeError, ValueError):
            row["managed"] = False
            row["managed_role"] = None
            annotated.append(row)
            continue
        role_set = roles.get(sid) or set()
        if not role_set:
            row["managed"] = False
            row["managed_role"] = None
        else:
            row["managed"] = True
            if role_set == {"placeholder"}:
                row["managed_role"] = "placeholder"
            elif role_set == {"real"}:
                row["managed_role"] = "real"
            else:
                row["managed_role"] = "both"
        annotated.append(row)
    return annotated
