from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Literal
from urllib.parse import quote

import requests

from core.config import settings
from core.logger import logger


def _plex_base_and_token() -> tuple[str | None, str | None]:
    plex_url = getattr(settings, "PLEX_URL", None)
    plex_token = getattr(settings, "PLEX_TOKEN", None)
    if not plex_url or not plex_token:
        return None, None
    return str(plex_url).rstrip('/'), str(plex_token)


_section_locations_lock = threading.Lock()
_section_locations_by_id: dict[int, tuple[str, ...]] | None = None
_section_locations_creds: tuple[str, str] | None = None


def _section_locations_fingerprint(plex_url: str, plex_token: str) -> tuple[str, str]:
    return (str(plex_url).rstrip("/"), str(plex_token or ""))


def clear_plex_section_location_cache() -> None:
    """Drop cached Plex library folder locations (settings change or tests)."""
    global _section_locations_by_id, _section_locations_creds
    with _section_locations_lock:
        _section_locations_by_id = None
        _section_locations_creds = None


def _relpath_under_root(abs_folder: str, root: str) -> str | None:
    """Return the relative path of abs_folder under root, or None if it is not inside."""
    root_abs = os.path.abspath(str(root or "").strip())
    folder_abs = os.path.abspath(abs_folder)
    if not root_abs:
        return None
    try:
        if os.path.commonpath([folder_abs, root_abs]) != root_abs:
            return None
    except ValueError:
        return None
    rel = os.path.relpath(folder_abs, root_abs)
    if rel.startswith(".."):
        return None
    return rel


def _join_plex_location(location: str, relative: str) -> str:
    loc = str(location or "").replace("\\", "/").rstrip("/")
    rel = str(relative or "").replace("\\", "/").lstrip("/")
    if not rel or rel == ".":
        return loc
    return f"{loc}/{rel}"


def rewrite_folder_to_plex_locations(
    abs_folder: str,
    *,
    our_roots: list[str],
    plex_locations: list[str] | tuple[str, ...],
) -> list[str]:
    """Map a Placeholdarr folder onto each Plex library location for that section.

    `/placeholdarr/movies/Title` under our root `/placeholdarr/movies` becomes
    `/mnt/user/data/placeholdarr/movies/Title` when that is Plex's location.
    """
    relative = None
    for root in our_roots:
        if not root:
            continue
        relative = _relpath_under_root(abs_folder, root)
        if relative is not None:
            break
    if relative is None:
        return []
    rewritten: list[str] = []
    seen: set[str] = set()
    for location in plex_locations:
        loc = str(location or "").strip()
        if not loc:
            continue
        path = _join_plex_location(loc, relative)
        if path in seen:
            continue
        seen.add(path)
        rewritten.append(path)
    return rewritten


def _parse_section_locations_xml(payload_text: str) -> dict[int, tuple[str, ...]]:
    root = ET.fromstring(payload_text)
    parsed: dict[int, tuple[str, ...]] = {}
    for directory in root.findall(".//Directory"):
        raw_key = directory.attrib.get("key")
        if raw_key is None:
            continue
        try:
            section_id = int(str(raw_key).strip())
        except (TypeError, ValueError):
            continue
        locations: list[str] = []
        seen: set[str] = set()
        for loc in directory.findall("Location"):
            path = str(loc.attrib.get("path") or "").strip()
            if not path or path in seen:
                continue
            seen.add(path)
            locations.append(path)
        parsed[section_id] = tuple(locations)
    return parsed


def fetch_plex_section_locations(
    *,
    plex_url: str | None = None,
    plex_token: str | None = None,
) -> dict[int, tuple[str, ...]]:
    """GET /library/sections and return section id -> folder locations."""
    url = str(plex_url).rstrip("/") if plex_url else None
    token = str(plex_token or "").strip() if plex_token is not None else None
    if not url or not token:
        base, stored = _plex_base_and_token()
        url = url or base
        token = token or stored
    if not url or not token:
        return {}
    response = requests.get(
        f"{url}/library/sections",
        headers={"X-Plex-Token": token, "Accept": "application/xml"},
        timeout=10,
    )
    response.raise_for_status()
    return _parse_section_locations_xml(response.text)


def prime_plex_section_location_cache(
    *,
    plex_url: str | None = None,
    plex_token: str | None = None,
) -> dict[int, tuple[str, ...]]:
    """Fetch Plex library locations into the process cache. Empty dict on failure."""
    global _section_locations_by_id, _section_locations_creds
    url = str(plex_url).rstrip("/") if plex_url else None
    token = str(plex_token or "").strip() if plex_token is not None else None
    if not url or not token:
        base, stored = _plex_base_and_token()
        url = url or base
        token = token or stored
    if not url or not token:
        return {}
    try:
        parsed = fetch_plex_section_locations(plex_url=url, plex_token=token)
    except Exception as exc:
        logger.warning(
            f"Failed to load Plex library locations: {exc}",
            extra={"emoji_type": "warning"},
        )
        return {}
    fingerprint = _section_locations_fingerprint(url, token)
    with _section_locations_lock:
        _section_locations_by_id = dict(parsed)
        _section_locations_creds = fingerprint
    logger.info(
        f"Cached Plex library locations for {len(parsed)} section(s)",
        extra={"emoji_type": "info"},
    )
    return parsed


def _cached_section_locations(section_id: int) -> tuple[str, ...]:
    """Return cached locations for a section, fetching once per process/credentials."""
    global _section_locations_by_id, _section_locations_creds
    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return ()
    fingerprint = _section_locations_fingerprint(plex_url, plex_token)
    with _section_locations_lock:
        if _section_locations_by_id is not None and _section_locations_creds == fingerprint:
            return _section_locations_by_id.get(int(section_id), ())
    parsed = prime_plex_section_location_cache(plex_url=plex_url, plex_token=plex_token)
    return parsed.get(int(section_id), ())


def _all_cached_section_locations() -> dict[int, tuple[str, ...]]:
    """Return the full section-id -> locations map, priming the process cache if needed."""
    global _section_locations_by_id, _section_locations_creds
    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return {}
    fingerprint = _section_locations_fingerprint(plex_url, plex_token)
    with _section_locations_lock:
        if _section_locations_by_id is not None and _section_locations_creds == fingerprint:
            return dict(_section_locations_by_id)
    return dict(prime_plex_section_location_cache(plex_url=plex_url, plex_token=plex_token))


def _normalize_plex_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    # Preserve leading slash; collapse duplicate separators.
    while "//" in text:
        text = text.replace("//", "/")
    if len(text) > 1 and text.endswith("/"):
        text = text.rstrip("/")
    return text


def _path_component_count(path: str) -> int:
    normalized = _normalize_plex_path(path)
    if not normalized or normalized == "/":
        return 0
    return len([part for part in normalized.split("/") if part])


def _event_path_under_location(event_path: str, location: str) -> bool:
    """True when event_path is the location or a child path (boundary-safe prefix)."""
    event = _normalize_plex_path(event_path)
    loc = _normalize_plex_path(location)
    if not event or not loc:
        return False
    if event == loc:
        return True
    prefix = loc if loc.endswith("/") else f"{loc}/"
    return event.startswith(prefix)


def match_plex_sections_for_path(path: str) -> list[int]:
    """Return Plex section ids whose library locations contain ``path``.

    Autopulse-style: keep location prefixes of the event path, prefer the deepest
    location depth, and return section ids that have a match at that max depth.
    Uses the in-process section-location cache (not a per-call /library/sections fetch).
    """
    event_path = _normalize_plex_path(path)
    if not event_path:
        return []

    locations_by_id = _all_cached_section_locations()
    if not locations_by_id:
        return []

    matches: list[tuple[int, int]] = []  # (component_count, section_id)
    for section_id, locations in locations_by_id.items():
        best_for_section = 0
        for location in locations:
            if not _event_path_under_location(event_path, location):
                continue
            depth = _path_component_count(location)
            if depth > best_for_section:
                best_for_section = depth
        if best_for_section > 0:
            matches.append((best_for_section, int(section_id)))

    if not matches:
        return []

    max_depth = max(depth for depth, _ in matches)
    section_ids = sorted({sid for depth, sid in matches if depth == max_depth})
    logger.debug(
        f"Plex real-library path match path={event_path!r} depth={max_depth} "
        f"sections={section_ids}",
        extra={"emoji_type": "debug"},
    )
    return section_ids


def _scan_directory_for_refresh(path: str) -> str:
    """Parent directory for files; path itself for directories (Plex refresh?path=)."""
    candidate = str(path or "").strip()
    if not candidate:
        return ""
    abs_path = os.path.abspath(candidate)
    if os.path.isfile(abs_path) or (
        not os.path.isdir(abs_path) and os.path.splitext(abs_path)[1]
    ):
        return os.path.dirname(abs_path) or abs_path
    return abs_path


def refresh_plex_library_paths(
    paths: set[str] | list[str],
    *,
    update_type: str = "Created",
) -> dict[str, Any]:
    """Path-scoped refresh for real Arr media paths (not placeholder dest roots).

    Matches each path against Plex library Location folders and calls
    ``GET /library/sections/{id}/refresh?path=``. Does not use placeholder
    ``_library_roots_and_section`` gating.
    """
    del update_type  # Plex path refresh has no Created/Deleted distinction.
    if not paths:
        return {"refreshed": 0, "failed": 0, "skipped": 0, "section_ids": []}

    if not getattr(settings, "plex_enabled", False):
        return {"refreshed": 0, "failed": 0, "skipped": 0, "section_ids": []}

    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return {"refreshed": 0, "failed": 0, "skipped": 0, "section_ids": []}

    refreshed = 0
    failed = 0
    skipped = 0
    touched_sections: set[int] = set()

    for raw_path in sorted({str(p).strip() for p in paths if str(p).strip()}):
        scan_path = _scan_directory_for_refresh(raw_path)
        section_ids = match_plex_sections_for_path(scan_path) or match_plex_sections_for_path(raw_path)
        if not section_ids:
            skipped += 1
            logger.info(
                f"No matching Plex library for path {raw_path}",
                extra={"emoji_type": "info"},
            )
            continue

        refresh_path = _normalize_plex_path(scan_path) or scan_path
        for section_id in section_ids:
            try:
                url = f"{plex_url}/library/sections/{section_id}/refresh?path={quote(refresh_path)}"
                logger.debug(
                    f"Plex real-library refresh url={url}",
                    extra={"emoji_type": "debug"},
                )
                response = requests.get(url, headers={"X-Plex-Token": plex_token}, timeout=15)
                response.raise_for_status()
                refreshed += 1
                touched_sections.add(int(section_id))
            except Exception as exc:
                failed += 1
                logger.warning(
                    f"Plex real-library path refresh failed section={section_id} "
                    f"path={refresh_path}: {exc}",
                    extra={"emoji_type": "warning"},
                )

    return {
        "refreshed": refreshed,
        "failed": failed,
        "skipped": skipped,
        "section_ids": sorted(touched_sections),
    }


def _parse_section_scan_meta_xml(payload_text: str) -> dict[int, dict[str, Any]]:
    root = ET.fromstring(payload_text)
    parsed: dict[int, dict[str, Any]] = {}
    for directory in root.findall(".//Directory"):
        raw_key = directory.attrib.get("key")
        if raw_key is None:
            continue
        try:
            section_id = int(str(raw_key).strip())
        except (TypeError, ValueError):
            continue
        refreshing_raw = directory.attrib.get("refreshing")
        refreshing = str(refreshing_raw).strip().lower() in {"1", "true", "yes"} if refreshing_raw is not None else None
        scanned_raw = directory.attrib.get("scannedAt")
        scanned_at: int | None = None
        if scanned_raw is not None and str(scanned_raw).strip():
            try:
                scanned_at = int(str(scanned_raw).strip())
            except (TypeError, ValueError):
                scanned_at = None
        parsed[section_id] = {"refreshing": refreshing, "scanned_at": scanned_at}
    return parsed


def fetch_plex_section_scan_meta() -> dict[int, dict[str, Any]]:
    """Fresh GET /library/sections metadata (refreshing / scannedAt) for empty-trash waits."""
    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return {}
    response = requests.get(
        f"{plex_url}/library/sections",
        headers={"X-Plex-Token": plex_token, "Accept": "application/xml"},
        timeout=10,
    )
    response.raise_for_status()
    return _parse_section_scan_meta_xml(response.text)


def empty_plex_section_trash(
    section_ids: set[int] | list[int],
    *,
    before_scanned_at: dict[int, int | None] | None = None,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """Wait for scanned sections to go idle, then empty Plex trash per section.

    Pass ``before_scanned_at`` captured *before* the path refresh so short scans
    are detected via scannedAt advancing. Skips a section when no scan is observed.
    """
    target_ids = sorted({int(x) for x in (section_ids or []) if int(x) > 0})
    if not target_ids:
        return {"emptied": 0, "skipped": 0, "failed": 0}

    if not getattr(settings, "plex_enabled", False):
        return {"emptied": 0, "skipped": 0, "failed": 0}

    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return {"emptied": 0, "skipped": 0, "failed": 0}

    before_map: dict[int, int | None] = dict(before_scanned_at or {})
    if not before_map:
        try:
            before_map = {
                sid: (meta.get("scanned_at") if isinstance(meta, dict) else None)
                for sid, meta in fetch_plex_section_scan_meta().items()
            }
        except Exception as exc:
            logger.warning(
                f"Plex empty trash: failed to load section scan state: {exc}",
                extra={"emoji_type": "warning"},
            )
            return {"emptied": 0, "skipped": 0, "failed": len(target_ids)}

    emptied = 0
    skipped = 0
    failed = 0
    max_wait = max(5.0, float(timeout_seconds))

    for section_id in target_ids:
        before_scanned = before_map.get(section_id)
        observed_scan = False
        scan_finished = False
        started = time.monotonic()

        while time.monotonic() - started < max_wait:
            try:
                meta = fetch_plex_section_scan_meta().get(section_id) or {}
            except Exception as exc:
                logger.debug(
                    f"Plex empty trash poll failed section={section_id}: {exc}",
                    extra={"emoji_type": "debug"},
                )
                time.sleep(1.0)
                continue

            refreshing = meta.get("refreshing")
            scanned_at = meta.get("scanned_at")
            scanned_advanced = (
                before_scanned is not None
                and scanned_at is not None
                and int(scanned_at) > int(before_scanned)
            )
            if refreshing is True:
                observed_scan = True
            elif refreshing is False:
                # Idle alone can mean queued. A newer timestamp also catches
                # short scans that start and finish between polls.
                if observed_scan or scanned_advanced:
                    scan_finished = True
                    break
                if not observed_scan and (time.monotonic() - started) >= 5.0:
                    break
            time.sleep(1.0)

        if not scan_finished:
            skipped += 1
            logger.info(
                f"Skipped Plex trash for section {section_id}: no scan observed",
                extra={"emoji_type": "info"},
            )
            continue

        try:
            url = f"{plex_url}/library/sections/{section_id}/emptyTrash"
            logger.debug(
                f"Plex empty trash url={url}",
                extra={"emoji_type": "debug"},
            )
            response = requests.put(url, headers={"X-Plex-Token": plex_token}, timeout=30)
            response.raise_for_status()
            emptied += 1
            logger.info(
                f"Plex trash emptied for section {section_id}",
                extra={"emoji_type": "success"},
            )
        except Exception as exc:
            failed += 1
            logger.warning(
                f"Plex empty trash failed for section {section_id}: {exc}",
                extra={"emoji_type": "warning"},
            )

    return {"emptied": emptied, "skipped": skipped, "failed": failed}


def get_plex_section_scan_state(section_ids: set[int] | list[int]) -> dict[str, object]:
    """Return best-effort scanning state for target Plex library sections.

    Primary signal comes from Plex activities/task feed. Section-level
    refreshing state is used as a fallback when activities are unavailable.
    """
    target_ids = sorted({int(x) for x in (section_ids or [])})
    if not target_ids:
        return {
            "target_section_ids": [],
            "any_target_scanning": False,
            "all_target_idle": True,
            "unknown_state": False,
            "reason": "no_expected_sections",
            "source": "none",
        }

    if not getattr(settings, "plex_enabled", False):
        return {
            "target_section_ids": target_ids,
            "any_target_scanning": False,
            "all_target_idle": True,
            "unknown_state": False,
            "reason": "plex_disabled",
            "source": "none",
        }

    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return {
            "target_section_ids": target_ids,
            "any_target_scanning": False,
            "all_target_idle": False,
            "unknown_state": True,
            "reason": "plex_unavailable",
            "source": "none",
        }

    scanning_ids: set[int] = set()
    unknown_ids: set[int] = set()

    # 1) Task/activity feed (preferred)
    try:
        response = requests.get(
            f"{plex_url}/activities",
            headers={"X-Plex-Token": plex_token},
            timeout=10,
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
        for activity in root.findall('.//Activity'):
            attrs = dict(activity.attrib or {})
            text_blob = " ".join(str(v) for v in attrs.values()).lower()
            # Only consider active scan-like activities.
            if not any(token in text_blob for token in ("scan", "scanning", "library", "refresh")):
                continue

            section_id = None
            for key in ("librarySectionID", "sectionID", "sectionId", "librarySectionId"):
                raw = attrs.get(key)
                if raw is None:
                    continue
                try:
                    section_id = int(str(raw).strip())
                    break
                except Exception:
                    continue

            if section_id is None:
                # Best-effort parse from context-like fields if present.
                for key in ("Context", "context", "title", "subtitle"):
                    raw = str(attrs.get(key, "") or "")
                    match = re.search(r"/library/sections/(\d+)", raw)
                    if match:
                        section_id = int(match.group(1))
                        break

            if section_id is not None and section_id in target_ids:
                scanning_ids.add(section_id)
    except Exception:
        # Continue to section.refreshing fallback.
        pass

    # 2) Section refreshing fallback when tasks were not decisive.
    unresolved = [sid for sid in target_ids if sid not in scanning_ids]
    if unresolved:
        try:
            from services.media_servers.plex_lookup import get_plex_server

            plex = get_plex_server()
            if plex is None:
                unknown_ids.update(unresolved)
            else:
                for section_id in unresolved:
                    try:
                        section = plex.library.sectionByID(section_id)
                        refreshing = getattr(section, "refreshing", None)
                        if refreshing is None:
                            raw = getattr(getattr(section, "_data", None), "attrib", {}).get("refreshing")
                            refreshing = raw
                        if refreshing is None:
                            unknown_ids.add(section_id)
                            continue
                        is_scanning = str(refreshing).strip().lower() in {"1", "true", "yes"}
                        if is_scanning:
                            scanning_ids.add(section_id)
                    except Exception:
                        unknown_ids.add(section_id)
        except Exception:
            unknown_ids.update(unresolved)

    unknown_state = bool(unknown_ids)
    any_target_scanning = bool(scanning_ids)
    all_target_idle = (not any_target_scanning) and (not unknown_state)
    if unknown_state:
        reason = "unknown_target_scan_state"
    elif any_target_scanning:
        reason = "target_sections_scanning"
    else:
        reason = "target_sections_idle"

    return {
        "target_section_ids": target_ids,
        "any_target_scanning": any_target_scanning,
        "all_target_idle": all_target_idle,
        "unknown_state": unknown_state,
        "reason": reason,
        "source": "activities_plus_section_refreshing",
        "scanning_section_ids": sorted(scanning_ids),
        "unknown_section_ids": sorted(unknown_ids),
    }


PlexMetadataRefreshResult = Literal["ok", "not_found", "failed", "skipped"]


def _parse_plex_guid_ids(guid_entries: list[Any] | None, *, top_guid: str | None = None) -> dict[str, Any]:
    """Extract tvdb / tmdb / imdb ids from Plex Guid list and optional top-level guid."""
    out: dict[str, Any] = {}
    texts: list[str] = []
    if isinstance(top_guid, str) and top_guid.strip():
        texts.append(top_guid.strip())
    for entry in guid_entries or []:
        if isinstance(entry, dict):
            raw = entry.get("id") or entry.get("tag")
        else:
            raw = entry
        if isinstance(raw, str) and raw.strip():
            texts.append(raw.strip())
    for text in texts:
        lower = text.lower()
        m = re.search(r"(?:tvdb|thetvdb)(?:://|%3a%2f%2f)(\d+)", lower)
        if m and "tvdb" not in out:
            out["tvdb_id"] = int(m.group(1))
        m = re.search(r"(?:tmdb|themoviedb)(?:://|%3a%2f%2f)(\d+)", lower)
        if m and "tmdb_id" not in out:
            out["tmdb_id"] = int(m.group(1))
        m = re.search(r"(?:imdb)(?:://|%3a%2f%2f)?(tt\d+)", lower)
        if m and "imdb_id" not in out:
            out["imdb_id"] = m.group(1)
    return out


def fetch_plex_item_playback_hints(rating_key: str | int) -> dict[str, Any] | None:
    """Fetch Guids, file path, and S/E for a Plex ``ratingKey``.

    Used when Tracearr (or similar) sends a thin playback webhook with a ratingKey
    but no path / external ids yet.
    """
    if not getattr(settings, "plex_enabled", False):
        return None
    key = str(rating_key or "").strip()
    if not key or key == "0":
        return None
    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return None
    try:
        response = requests.get(
            f"{plex_url}/library/metadata/{key}",
            headers={"Accept": "application/json", "X-Plex-Token": plex_token},
            timeout=15,
        )
        response.raise_for_status()
        meta_list = (response.json().get("MediaContainer") or {}).get("Metadata") or []
        if not meta_list or not isinstance(meta_list[0], dict):
            return None
        meta = meta_list[0]
        hints = _parse_plex_guid_ids(meta.get("Guid"), top_guid=meta.get("guid") if isinstance(meta.get("guid"), str) else None)
        media_type = str(meta.get("type") or "").strip().lower()
        if media_type in {"episode", "show", "season"}:
            hints["media_type"] = "episode"
        elif media_type in {"movie"}:
            hints["media_type"] = "movie"
        parent_index = meta.get("parentIndex")
        index = meta.get("index")
        try:
            if parent_index is not None:
                hints["season_number"] = int(parent_index)
        except (TypeError, ValueError):
            pass
        try:
            if index is not None and media_type == "episode":
                hints["episode_number"] = int(index)
        except (TypeError, ValueError):
            pass
        file_path = None
        for media in meta.get("Media") or []:
            if not isinstance(media, dict):
                continue
            for part in media.get("Part") or []:
                if not isinstance(part, dict):
                    continue
                candidate = part.get("file") or part.get("key")
                if isinstance(candidate, str) and candidate.strip() and not candidate.startswith("/library/"):
                    file_path = candidate.strip()
                    break
            if file_path:
                break
        if file_path:
            hints["file_path"] = file_path
        title = meta.get("title")
        if isinstance(title, str) and title.strip():
            hints["title"] = title.strip()
        gp = meta.get("grandparentTitle")
        if isinstance(gp, str) and gp.strip():
            hints["grandparent_title"] = gp.strip()
        return hints or None
    except Exception as exc:
        logger.debug(
            f"Plex playback hints failed rating_key={key}: {type(exc).__name__}: {exc}",
            extra={"emoji_type": "debug"},
        )
        return None


def refresh_plex_item_metadata(rating_key: str | int) -> PlexMetadataRefreshResult:
    """Trigger a metadata refresh for a single Plex library item by rating key.

    Returns ``not_found`` when Plex responds 404 (typically a stale ``ratingKey``
    after a library item was removed and re-added — Plex assigns a new key).
    """
    if not getattr(settings, "plex_enabled", False):
        return "skipped"
    key = str(rating_key or "").strip()
    if not key:
        return "skipped"
    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return "skipped"
    try:
        url = f"{plex_url}/library/metadata/{key}/refresh"
        response = requests.get(url, headers={"X-Plex-Token": plex_token}, timeout=20)
        response.raise_for_status()
        logger.debug(
            f"Plex item metadata refresh ok rating_key={key}",
            extra={"emoji_type": "refresh"},
        )
        return "ok"
    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        code = getattr(resp, "status_code", None) if resp is not None else None
        snippet = ""
        try:
            if resp is not None and resp.text:
                snippet = str(resp.text).replace("\n", " ")[:400]
        except Exception:
            snippet = ""
        if code == 404:
            logger.warning(
                f"Plex item metadata refresh not found (stale rating_key?) rating_key={key} "
                f"http_status={code} body={snippet!r}",
                extra={"emoji_type": "warning"},
            )
            return "not_found"
        logger.warning(
            f"Plex item metadata refresh failed rating_key={key} http_status={code} "
            f"body={snippet!r}: {e}",
            extra={"emoji_type": "warning"},
        )
        return "failed"


def _plex_text_preview(value: str, *, limit: int = 80) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def unlock_and_restore_plex_item_text(
    rating_key: str | int,
    *,
    title: str,
    summary: str,
) -> PlexMetadataRefreshResult:
    """Write clean title/summary and unlock fields so Plex agents can refresh.

    Used to repair real-library items that received locked placeholder status text.
    """
    if not getattr(settings, "plex_enabled", False):
        return "skipped"
    key = str(rating_key or "").strip()
    if not key:
        return "skipped"
    try:
        from services.media_servers.plex_lookup import get_plex_server

        plex = get_plex_server()
        if plex is None:
            return "failed"
        item = plex.fetchItem(f"/library/metadata/{key}")
        wanted_title = str(title or "")
        wanted_summary = str(summary or "")
        try:
            item.editTitle(wanted_title, locked=False)
        except TypeError:
            item.editTitle(wanted_title)
        try:
            item.editSummary(wanted_summary, locked=False)
        except TypeError:
            item.editSummary(wanted_summary)
        try:
            # Best-effort unlock via edits API when locked= kw unsupported above.
            item.edit(**{"title.locked": 0, "summary.locked": 0})
        except Exception:
            pass
        item.reload()
        logger.info(
            f"Plex: unlocked/restored title/summary rating_key={key} "
            f"title={_plex_text_preview(wanted_title)!r}",
            extra={"emoji_type": "success"},
        )
        return "ok"
    except Exception as ex:
        text = str(ex).lower()
        if "404" in text or "not found" in text:
            return "not_found"
        logger.warning(
            f"Plex unlock/restore failed rating_key={key}: {ex}",
            extra={"emoji_type": "warning"},
        )
        return "failed"


def update_plex_item_text(
    rating_key: str | int,
    *,
    title: str,
    summary: str,
    item_label: str | None = None,
) -> PlexMetadataRefreshResult:
    """Directly update Plex item title/summary via PlexAPI edit methods.

    Title and summary are locked after the edit so Plex agents (TMDB / local NFO)
    cannot immediately revert the projected status. Callers must only pass
    placeholder-library rating keys; real-library keys must never be edited here.

    ``item_label`` is optional log context (e.g. ``Bleach S17E50`` or a movie title)
    so per-item DEBUG/WARNING lines stay identifiable when batch INFO summaries truncate.
    """
    if not getattr(settings, "plex_enabled", False):
        return "skipped"
    key = str(rating_key or "").strip()
    if not key:
        return "skipped"
    label = str(item_label or "").strip()
    label_part = f" item={label!r}" if label else ""
    try:
        from services.media_servers.plex_lookup import get_plex_server

        plex = get_plex_server()
        if plex is None:
            return "failed"
        item = plex.fetchItem(f"/library/metadata/{key}")
        wanted_title = str(title or "")
        wanted_summary = str(summary or "")
        before_title = str(getattr(item, "title", "") or "")
        before_summary = str(getattr(item, "summary", "") or "")
        try:
            item.editTitle(wanted_title, locked=True)
        except TypeError:
            item.editTitle(wanted_title)
        try:
            item.editSummary(wanted_summary, locked=True)
        except TypeError:
            item.editSummary(wanted_summary)
        item.reload()
        after_title = str(getattr(item, "title", "") or "")
        after_summary = str(getattr(item, "summary", "") or "")
        title_ok = after_title == wanted_title
        summary_ok = after_summary == wanted_summary
        if title_ok and summary_ok:
            logger.debug(
                "Plex: title/summary edit confirmed "
                f"(rating_key={key}{label_part} title {_plex_text_preview(before_title)!r} -> "
                f"{_plex_text_preview(after_title)!r} summary_changed="
                f"{before_summary != after_summary})",
                extra={"emoji_type": "debug"},
            )
            return "ok"
        logger.warning(
            "Plex: title/summary edit did not stick "
            f"(rating_key={key}{label_part} title_ok={title_ok} summary_ok={summary_ok} "
            f"before_title={_plex_text_preview(before_title)!r} "
            f"after_title={_plex_text_preview(after_title)!r} "
            f"expected_title={_plex_text_preview(wanted_title)!r} "
            f"before_summary={_plex_text_preview(before_summary)!r} "
            f"after_summary={_plex_text_preview(after_summary)!r} "
            f"expected_summary={_plex_text_preview(wanted_summary)!r})",
            extra={"emoji_type": "warning"},
        )
        return "failed"
    except Exception as ex:
        text = str(ex).lower()
        if "404" in text or "not found" in text:
            logger.warning(
                f"Plex direct update not found (stale rating_key?) rating_key={key}{label_part}: {ex}",
                extra={"emoji_type": "warning"},
            )
            return "not_found"
        logger.warning(
            f"Plex direct update failed rating_key={key}{label_part}: {ex}",
            extra={"emoji_type": "warning"},
        )
        return "failed"


def refresh_plex_section_ids(
    section_ids: list[int] | set[int],
    *,
    force_refresh_metadata: bool = False,
) -> dict[str, int]:
    """Trigger full refresh for explicit Plex section ids."""
    if not getattr(settings, "plex_enabled", False):
        return {"refreshed": 0, "failed": 0}

    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return {"refreshed": 0, "failed": 0}

    refreshed = 0
    failed = 0
    for section_id in sorted({int(x) for x in (section_ids or [])}):
        try:
            url = f"{plex_url}/library/sections/{section_id}/refresh"
            if force_refresh_metadata:
                url = f"{url}?force=1"
            response = requests.get(url, headers={"X-Plex-Token": plex_token}, timeout=15)
            response.raise_for_status()
            logger.info(
                f"Triggered full Plex section refresh: section_id={section_id} force={int(force_refresh_metadata)}",
                extra={"emoji_type": "info"},
            )
            refreshed += 1
        except Exception as e:
            logger.warning(
                f"Plex section refresh failed for section_id={section_id}: {e}",
                extra={"emoji_type": "warning"},
            )
            try:
                from services.integration_status import is_connectivity_exception, mark_media_connectivity_failure

                if is_connectivity_exception(e):
                    mark_media_connectivity_failure("plex", message=str(e) or type(e).__name__)
            except Exception:
                pass
            failed += 1

    return {"refreshed": refreshed, "failed": failed}


def _library_roots_and_section(abs_folder: str) -> tuple[list[str], int | None]:
    try:
        from services.library_destinations import (
            all_movie_dest_roots,
            all_tv_dest_roots,
            plex_section_for_folder,
        )

        movie_roots = [str(r).strip() for r in all_movie_dest_roots() if r]
        tv_roots = [str(r).strip() for r in all_tv_dest_roots() if r]
        section_id = plex_section_for_folder(abs_folder)
        for root in movie_roots:
            if _relpath_under_root(abs_folder, root) is not None:
                return movie_roots, section_id
        for root in tv_roots:
            if _relpath_under_root(abs_folder, root) is not None:
                return tv_roots, section_id
        return [], None
    except Exception:
        movie_roots = [
            str(r).strip()
            for r in (getattr(settings, "MOVIE_LIBRARY_FOLDER", None),)
            if r
        ]
        tv_roots = [
            str(r).strip()
            for r in (getattr(settings, "TV_LIBRARY_FOLDER", None),)
            if r
        ]
        movie_section = getattr(settings, "PLEX_MOVIE_SECTION_ID", None)
        tv_section = getattr(settings, "PLEX_TV_SECTION_ID", None)
        for root in movie_roots:
            if _relpath_under_root(abs_folder, root) is not None:
                try:
                    return movie_roots, int(movie_section) if movie_section is not None else None
                except (TypeError, ValueError):
                    return movie_roots, None
        for root in tv_roots:
            if _relpath_under_root(abs_folder, root) is not None:
                try:
                    return tv_roots, int(tv_section) if tv_section is not None else None
                except (TypeError, ValueError):
                    return tv_roots, None
        return [], None


def refresh_plex_paths(paths: set[str], *, update_type: str = "Created") -> dict[str, int]:
    """Request path-scoped Plex refresh for changed folders.

    Paths are rewritten onto Plex's library locations so Docker/container
    mounts (e.g. /placeholdarr/movies) match what Plex actually scans.
    """
    if not paths:
        return {"refreshed": 0, "failed": 0}

    if not getattr(settings, "plex_enabled", False):
        return {"refreshed": 0, "failed": 0}

    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return {"refreshed": 0, "failed": 0}

    refreshed = 0
    failed = 0

    normalized_folders = []
    for path in sorted(paths):
        abs_path = os.path.abspath(path)
        normalized_folders.append(os.path.dirname(abs_path) if os.path.isfile(abs_path) else abs_path)

    for folder in dict.fromkeys(normalized_folders):
        abs_folder = os.path.abspath(folder)
        our_roots, section_id = _library_roots_and_section(abs_folder)
        if section_id is None:
            logger.debug(
                f"Skipping Plex refresh for out-of-scope folder: {abs_folder}",
                extra={"emoji_type": "debug"},
            )
            continue

        plex_locations = _cached_section_locations(section_id)
        refresh_paths = rewrite_folder_to_plex_locations(
            abs_folder, our_roots=our_roots, plex_locations=plex_locations
        )
        if not refresh_paths:
            logger.warning(
                f"Plex library locations unavailable for section {section_id}; "
                f"sending Placeholdarr path as-is folder={abs_folder}",
                extra={"emoji_type": "warning"},
            )
            refresh_paths = [abs_folder]
        elif any(path != abs_folder for path in refresh_paths):
            logger.info(
                f"Plex path refresh rewritten local={abs_folder} plex={refresh_paths} section={section_id}",
                extra={"emoji_type": "refresh"},
            )

        for refresh_path in refresh_paths:
            try:
                url = f"{plex_url}/library/sections/{section_id}/refresh?path={quote(refresh_path)}"
                response = requests.get(url, headers={"X-Plex-Token": plex_token}, timeout=15)
                response.raise_for_status()
                refreshed += 1
            except Exception as e:
                failed += 1
                logger.warning(
                    f"Path-scoped Plex refresh failed for folder={refresh_path}: {e}",
                    extra={"emoji_type": "warning"},
                )

    return {"refreshed": refreshed, "failed": failed}


def refresh_plex_sections(
    has_movies: bool,
    has_episodes: bool,
    *,
    force_refresh_metadata: bool = False,
) -> dict[str, int]:
    """Send a single full-section refresh per affected Plex library.

    Includes primary defaults, legacy 4K section IDs, and dest-map section IDs.
    """
    if not getattr(settings, "plex_enabled", False):
        return {"refreshed": 0, "failed": 0}

    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return {"refreshed": 0, "failed": 0}

    from services.library_destinations import (
        all_plex_section_ids,
        parse_library_destination_map,
        plex_section_ids_for_arr_type,
    )

    section_ids: list[int] = []
    if has_movies and has_episodes:
        section_ids = list(all_plex_section_ids())
    else:
        map_rows = parse_library_destination_map()
        if has_movies:
            section_ids.extend(plex_section_ids_for_arr_type("radarr", map_rows=map_rows))
        if has_episodes:
            section_ids.extend(plex_section_ids_for_arr_type("sonarr", map_rows=map_rows))

    deduped: list[int] = []
    seen: set[int] = set()
    for sid in section_ids:
        if sid in seen or sid < 1:
            continue
        seen.add(sid)
        deduped.append(sid)

    return refresh_plex_section_ids(deduped, force_refresh_metadata=force_refresh_metadata)
