"""Arr instance + root folder → Placeholdarr destination folder + media-server library.

Default with no map rows: ``{LIBRARY_ROOT}/movies`` / ``tv`` and the primary Plex section IDs.
Unmapped Arr roots fall back to those defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

from core.config import settings
from core.logger import logger


@dataclass(frozen=True)
class LibraryDestination:
    dest_folder: str
    plex_section_id: int | None
    arr_type: str
    instance_key: str
    arr_root_path: str
    matched: bool


def _normalize_path(path: str | None) -> str:
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    # Collapse duplicate slashes; keep leading slash for absolute paths.
    while "//" in text:
        text = text.replace("//", "/")
    if len(text) > 1 and text.endswith("/"):
        text = text.rstrip("/")
    return text


def _path_is_under_or_equal(path: str, root: str) -> bool:
    """True when ``path`` equals ``root`` or is a child of ``root`` (normalized)."""
    p = _normalize_path(path).lower()
    r = _normalize_path(root).lower()
    if not p or not r:
        return False
    if p == r:
        return True
    return p.startswith(r + "/")


def parse_library_destination_map(raw: str | None = None) -> list[dict[str, Any]]:
    """Parse ``LIBRARY_DESTINATION_MAP_JSON`` into normalized row dicts."""
    text = str(raw if raw is not None else getattr(settings, "LIBRARY_DESTINATION_MAP_JSON", "") or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception as exc:
        logger.warning(f"Failed to parse LIBRARY_DESTINATION_MAP_JSON: {exc}", extra={"emoji_type": "warning"})
        return []
    if not isinstance(payload, list):
        return []

    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        arr_type = str(item.get("arr_type") or "").strip().lower()
        if arr_type not in {"radarr", "sonarr"}:
            continue
        instance_key = str(item.get("instance_key") or "").strip().lower()
        arr_root = _normalize_path(item.get("arr_root_path") or item.get("arr_root") or "")
        dest = _normalize_path(item.get("dest_folder") or item.get("dest") or "")
        if not instance_key or not arr_root or not dest:
            continue
        plex_raw = item.get("plex_section_id")
        plex_section_id: int | None = None
        if plex_raw is not None and str(plex_raw).strip() != "":
            try:
                plex_section_id = int(plex_raw)
            except (TypeError, ValueError):
                plex_section_id = None
        out.append(
            {
                "instance_key": instance_key,
                "arr_type": arr_type,
                "arr_root_path": arr_root,
                "dest_folder": dest,
                "plex_section_id": plex_section_id,
            }
        )
    return out


def validate_library_destination_map_unique(rows: list[dict[str, Any]]) -> str | None:
    """Return an error message when the same Arr instance+root maps to more than one dest.

    Same instance with different roots across destinations is allowed. Duplicate
    ``(instance_key, arr_root_path)`` rows are not, even when dest folders or Plex
    section IDs differ.
    """
    seen: dict[tuple[str, str], str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        instance_key = str(row.get("instance_key") or "").strip().lower()
        arr_root = _normalize_path(row.get("arr_root_path") or row.get("arr_root") or "").lower()
        dest = _normalize_path(row.get("dest_folder") or row.get("dest") or "")
        if not instance_key or not arr_root:
            continue
        key = (instance_key, arr_root)
        prior_dest = seen.get(key)
        if prior_dest is None:
            seen[key] = dest
            continue
        if prior_dest == dest:
            return (
                f"Arr root '{row.get('arr_root_path') or arr_root}' on instance "
                f"'{instance_key}' is listed more than once."
            )
        return (
            f"Arr root '{row.get('arr_root_path') or arr_root}' on instance "
            f"'{instance_key}' is mapped to more than one Placeholdarr folder "
            f"('{prior_dest}' and '{dest}'). Use different roots per destination."
        )
    return None


def instances_share_dest_folder(arr_type: str, map_rows: list[dict[str, Any]] | None = None) -> bool:
    """True when two or more distinct instances of ``arr_type`` map to the same dest folder."""
    want = str(arr_type or "").strip().lower()
    if want not in {"radarr", "sonarr"}:
        return False
    rows = map_rows if map_rows is not None else parse_library_destination_map()
    by_dest: dict[str, set[str]] = {}
    for row in rows:
        if str(row.get("arr_type") or "").strip().lower() != want:
            continue
        dest = _normalize_path(row.get("dest_folder") or row.get("dest") or "").lower()
        key = str(row.get("instance_key") or "").strip().lower()
        if not dest or not key:
            continue
        by_dest.setdefault(dest, set()).add(key)
    return any(len(keys) >= 2 for keys in by_dest.values())


def default_movie_dest_folder() -> str:
    return str(getattr(settings, "MOVIE_LIBRARY_FOLDER", "") or "").strip()


def default_tv_dest_folder() -> str:
    return str(getattr(settings, "TV_LIBRARY_FOLDER", "") or "").strip()


def default_movie_plex_section_id() -> int | None:
    raw = getattr(settings, "PLEX_MOVIE_SECTION_ID", None)
    try:
        return int(raw) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        return None


def default_tv_plex_section_id() -> int | None:
    raw = getattr(settings, "PLEX_TV_SECTION_ID", None)
    try:
        return int(raw) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        return None


def default_movie_4k_plex_section_id() -> int | None:
    """Removed legacy setting; mapped dests carry their own Plex section IDs."""
    return None


def default_tv_4k_plex_section_id() -> int | None:
    """Removed legacy setting; mapped dests carry their own Plex section IDs."""
    return None


def _map_default_for_instance(
    *,
    arr_type: str,
    instance_key: str,
    map_rows: list[dict[str, Any]],
) -> LibraryDestination | None:
    """When Arr path is missing/unmatched, use the sole (or shortest-root) map row for the key."""
    key = str(instance_key or "").strip().lower()
    if not key:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for row in map_rows:
        if str(row.get("arr_type") or "").lower() != arr_type:
            continue
        if str(row.get("instance_key") or "").lower() != key:
            continue
        root = _normalize_path(row.get("arr_root_path"))
        dest = _normalize_path(row.get("dest_folder"))
        if not dest:
            continue
        candidates.append((len(root), row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    best = candidates[0][1]
    plex_id = best.get("plex_section_id")
    if plex_id is None:
        plex_id = (
            default_tv_plex_section_id()
            if arr_type == "sonarr"
            else default_movie_plex_section_id()
        )
    return LibraryDestination(
        dest_folder=str(best.get("dest_folder") or ""),
        plex_section_id=int(plex_id) if plex_id is not None else None,
        arr_type=arr_type,
        instance_key=key,
        arr_root_path=str(best.get("arr_root_path") or ""),
        matched=True,
    )


def _primary_default_dest(*, arr_type: str, instance_key: str) -> LibraryDestination:
    """Default movie/TV destinations when dest map has no row for this instance."""
    if arr_type == "sonarr":
        return LibraryDestination(
            dest_folder=default_tv_dest_folder(),
            plex_section_id=default_tv_plex_section_id(),
            arr_type=arr_type,
            instance_key=instance_key,
            arr_root_path="",
            matched=False,
        )
    return LibraryDestination(
        dest_folder=default_movie_dest_folder(),
        plex_section_id=default_movie_plex_section_id(),
        arr_type=arr_type,
        instance_key=instance_key,
        arr_root_path="",
        matched=False,
    )


def _default_dest(*, arr_type: str, instance_key: str, map_rows: list[dict[str, Any]] | None = None) -> LibraryDestination:
    """Instance-aware default when Arr root matching fails: map row for key, else primary dest."""
    rows = map_rows if map_rows is not None else parse_library_destination_map()
    key = str(instance_key or "").strip().lower()
    if key:
        mapped = _map_default_for_instance(arr_type=arr_type, instance_key=key, map_rows=rows)
        if mapped is not None:
            return mapped
    return _primary_default_dest(arr_type=arr_type, instance_key=key)


def resolve_destination(
    *,
    arr_type: str,
    instance_key: str | None,
    arr_path: str | None,
    map_rows: list[dict[str, Any]] | None = None,
) -> LibraryDestination:
    """Resolve Placeholdarr dest + Plex section via longest-prefix Arr root match."""
    normalized_type = str(arr_type or "").strip().lower()
    if normalized_type not in {"radarr", "sonarr"}:
        normalized_type = "radarr"
    key = str(instance_key or "").strip().lower()
    path = _normalize_path(arr_path)
    rows = map_rows if map_rows is not None else parse_library_destination_map()

    candidates: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        if str(row.get("arr_type") or "").lower() != normalized_type:
            continue
        if key and str(row.get("instance_key") or "").lower() != key:
            continue
        root = str(row.get("arr_root_path") or "")
        if not root or not path:
            continue
        if _path_is_under_or_equal(path, root):
            candidates.append((len(_normalize_path(root)), row))

    if not candidates:
        return _default_dest(arr_type=normalized_type, instance_key=key, map_rows=rows)

    candidates.sort(key=lambda item: item[0], reverse=True)
    best = candidates[0][1]
    plex_id = best.get("plex_section_id")
    if plex_id is None:
        plex_id = (
            default_tv_plex_section_id()
            if normalized_type == "sonarr"
            else default_movie_plex_section_id()
        )
    return LibraryDestination(
        dest_folder=str(best.get("dest_folder") or ""),
        plex_section_id=int(plex_id) if plex_id is not None else None,
        arr_type=normalized_type,
        instance_key=key,
        arr_root_path=str(best.get("arr_root_path") or ""),
        matched=True,
    )


def resolve_movie_dest(
    *,
    instance_key: str | None,
    arr_path: str | None,
    map_rows: list[dict[str, Any]] | None = None,
) -> LibraryDestination:
    return resolve_destination(
        arr_type="radarr",
        instance_key=instance_key,
        arr_path=arr_path,
        map_rows=map_rows,
    )


def resolve_series_dest(
    *,
    instance_key: str | None,
    arr_path: str | None,
    map_rows: list[dict[str, Any]] | None = None,
) -> LibraryDestination:
    return resolve_destination(
        arr_type="sonarr",
        instance_key=instance_key,
        arr_path=arr_path,
        map_rows=map_rows,
    )


def all_configured_dest_roots(*, map_rows: list[dict[str, Any]] | None = None) -> list[str]:
    """All Placeholdarr destination folders (default movie/TV + mapped dests + Discover), deduped."""
    rows = map_rows if map_rows is not None else parse_library_destination_map()
    roots: list[str] = []
    for folder in (default_movie_dest_folder(), default_tv_dest_folder()):
        if folder:
            roots.append(folder)
    for row in rows:
        dest = str(row.get("dest_folder") or "").strip()
        if dest:
            roots.append(dest)
    # TMDB Discover movies live under a separate root; include it so placeholder
    # dir-mode chmod walks apply there (media servers often run as non-root).
    discover = str(getattr(settings, "DISCOVER_MOVIE_LIBRARY_FOLDER", "") or "").strip()
    if not discover:
        discover_root = str(getattr(settings, "DISCOVER_LIBRARY_ROOT", "") or "").strip()
        if discover_root:
            discover = os.path.join(discover_root, "movies")
    if discover:
        roots.append(discover)
    # Preserve order, drop empties/dupes (case-sensitive path strings as configured).
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        key = _normalize_path(root).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(root)
    return out


def all_movie_dest_roots(*, map_rows: list[dict[str, Any]] | None = None) -> list[str]:
    rows = map_rows if map_rows is not None else parse_library_destination_map()
    roots: list[str] = []
    movie = default_movie_dest_folder()
    if movie:
        roots.append(movie)
    for row in rows:
        if str(row.get("arr_type") or "").lower() != "radarr":
            continue
        dest = str(row.get("dest_folder") or "").strip()
        if dest:
            roots.append(dest)
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        key = _normalize_path(root).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(root)
    return out


def all_tv_dest_roots(*, map_rows: list[dict[str, Any]] | None = None) -> list[str]:
    rows = map_rows if map_rows is not None else parse_library_destination_map()
    roots: list[str] = []
    tv = default_tv_dest_folder()
    if tv:
        roots.append(tv)
    for row in rows:
        if str(row.get("arr_type") or "").lower() != "sonarr":
            continue
        dest = str(row.get("dest_folder") or "").strip()
        if dest:
            roots.append(dest)
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        key = _normalize_path(root).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(root)
    return out


def plex_section_for_folder(
    abs_folder: str,
    *,
    map_rows: list[dict[str, Any]] | None = None,
) -> int | None:
    """Pick the Plex section ID for a Placeholdarr folder under a mapped or default dest."""
    rows = map_rows if map_rows is not None else parse_library_destination_map()
    folder = _normalize_path(abs_folder)
    if not folder:
        return None

    # Prefer longest matching mapped dest folder.
    candidates: list[tuple[int, int | None, str]] = []
    for row in rows:
        dest = _normalize_path(row.get("dest_folder"))
        if not dest:
            continue
        if _path_is_under_or_equal(folder, dest):
            plex_id = row.get("plex_section_id")
            if plex_id is None:
                plex_id = (
                    default_tv_plex_section_id()
                    if str(row.get("arr_type") or "").lower() == "sonarr"
                    else default_movie_plex_section_id()
                )
            candidates.append((len(dest), int(plex_id) if plex_id is not None else None, dest))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    movie_primary = default_movie_dest_folder()
    tv_primary = default_tv_dest_folder()
    if movie_primary and _path_is_under_or_equal(folder, movie_primary):
        return default_movie_plex_section_id()
    if tv_primary and _path_is_under_or_equal(folder, tv_primary):
        return default_tv_plex_section_id()
    return None


def all_plex_section_ids(*, map_rows: list[dict[str, Any]] | None = None) -> list[int]:
    """Unique Plex section IDs from default movie/TV libraries and map rows."""
    rows = map_rows if map_rows is not None else parse_library_destination_map()
    ids: list[int] = []
    for sid in (
        default_movie_plex_section_id(),
        default_tv_plex_section_id(),
    ):
        if sid is not None:
            ids.append(int(sid))
    for row in rows:
        sid = row.get("plex_section_id")
        if sid is None:
            continue
        try:
            ids.append(int(sid))
        except (TypeError, ValueError):
            continue
    out: list[int] = []
    seen: set[int] = set()
    for sid in ids:
        if sid in seen or sid < 1:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def section_ids_for_paths(
    paths: list[str] | None,
    *,
    map_rows: list[dict[str, Any]] | None = None,
) -> list[int]:
    """Plex section IDs covering the given folders (or all configured if paths empty)."""
    rows = map_rows if map_rows is not None else parse_library_destination_map()
    if not paths:
        return all_plex_section_ids(map_rows=rows)
    ids: list[int] = []
    seen: set[int] = set()
    for path in paths:
        sid = plex_section_for_folder(path, map_rows=rows)
        if sid is None or sid in seen or sid < 1:
            continue
        seen.add(sid)
        ids.append(sid)
    return ids


def dest_folder_for_instance(
    *,
    arr_type: str,
    instance_key: str | None,
    arr_path: str | None = None,
    map_rows: list[dict[str, Any]] | None = None,
) -> str:
    """Resolve dest folder by dest map, then default movie/TV destinations."""
    dest = resolve_destination(
        arr_type=arr_type,
        instance_key=instance_key,
        arr_path=arr_path,
        map_rows=map_rows,
    )
    return str(dest.dest_folder or "").strip()


def ensure_dest_folders_exist(dir_mode: int | None = None) -> list[str]:
    """Create all configured destination folders; return paths that were created."""
    created: list[str] = []
    if dir_mode is not None:
        mode = int(dir_mode)
    else:
        raw = str(getattr(settings, "PLACEHOLDER_DIR_MODE", "777") or "777").strip()
        try:
            mode = int(raw, 8)
        except ValueError:
            mode = 0o755
    for root in all_configured_dest_roots():
        if not root:
            continue
        result = ensure_single_dest_folder(root, dir_mode=mode)
        if result.get("created"):
            created.append(str(result.get("path") or root))
    return created


def ensure_single_dest_folder(path: str | None, *, dir_mode: int | None = None) -> dict[str, Any]:
    """Create one destination folder if missing.

    Returns ``{ok, path, created, existed, message}``.
    """
    text = _normalize_path(path)
    if not text:
        return {"ok": False, "path": "", "created": False, "existed": False, "message": "Path is required"}
    if not text.startswith("/"):
        return {
            "ok": False,
            "path": text,
            "created": False,
            "existed": False,
            "message": "Path must be absolute (start with /)",
        }
    if "\x00" in text:
        return {"ok": False, "path": text, "created": False, "existed": False, "message": "Invalid path"}

    if dir_mode is not None:
        mode = int(dir_mode)
    else:
        raw = str(getattr(settings, "PLACEHOLDER_DIR_MODE", "777") or "777").strip()
        try:
            mode = int(raw, 8)
        except ValueError:
            mode = 0o755

    try:
        existed = os.path.isdir(text)
        if existed:
            try:
                os.chmod(text, mode)
            except OSError:
                pass
            return {
                "ok": True,
                "path": text,
                "created": False,
                "existed": True,
                "message": "Folder already exists",
            }
        os.makedirs(text, mode=mode, exist_ok=True)
        try:
            os.chmod(text, mode)
        except OSError:
            pass
        if not os.path.isdir(text):
            return {
                "ok": False,
                "path": text,
                "created": False,
                "existed": False,
                "message": "Path was created but is not a directory",
            }
        return {
            "ok": True,
            "path": text,
            "created": True,
            "existed": False,
            "message": "Folder created",
        }
    except OSError as exc:
        logger.warning(f"Could not ensure library dest folder {text}: {exc}", extra={"emoji_type": "warning"})
        return {
            "ok": False,
            "path": text,
            "created": False,
            "existed": False,
            "message": str(exc) or "Could not create folder",
        }
