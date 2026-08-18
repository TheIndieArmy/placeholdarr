from __future__ import annotations

import os
import re
import threading
import xml.etree.ElementTree as ET
from typing import Literal
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


def update_plex_item_text(rating_key: str | int, *, title: str, summary: str) -> PlexMetadataRefreshResult:
    """Directly update Plex item title/summary via PlexAPI edit methods."""
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
        # Avoid locking title/summary fields so later NFO/library refreshes can still
        # overwrite metadata if direct projection fails or drifts.
        try:
            item.editTitle(str(title or ""), locked=False)
        except TypeError:
            item.editTitle(str(title or ""))
        try:
            item.editSummary(str(summary or ""), locked=False)
        except TypeError:
            item.editSummary(str(summary or ""))
        item.reload()
        title_ok = str(getattr(item, "title", "") or "") == str(title or "")
        summary_ok = str(getattr(item, "summary", "") or "") == str(summary or "")
        return "ok" if (title_ok and summary_ok) else "failed"
    except Exception as ex:
        text = str(ex).lower()
        if "404" in text or "not found" in text:
            logger.warning(
                f"Plex direct update not found (stale rating_key?) rating_key={key}: {ex}",
                extra={"emoji_type": "warning"},
            )
            return "not_found"
        logger.warning(
            f"Plex direct update failed rating_key={key}: {ex}",
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
            failed += 1

    return {"refreshed": refreshed, "failed": failed}


def _library_roots_and_section(abs_folder: str) -> tuple[list[str], int | None]:
    movie_roots = [
        str(r).strip()
        for r in (
            getattr(settings, "MOVIE_LIBRARY_FOLDER", None),
            getattr(settings, "MOVIE_LIBRARY_4K_FOLDER", None),
        )
        if r
    ]
    tv_roots = [
        str(r).strip()
        for r in (
            getattr(settings, "TV_LIBRARY_FOLDER", None),
            getattr(settings, "TV_LIBRARY_4K_FOLDER", None),
        )
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
    """Send a single full-section refresh per affected Plex library."""
    if not getattr(settings, "plex_enabled", False):
        return {"refreshed": 0, "failed": 0}

    plex_url, plex_token = _plex_base_and_token()
    if not plex_url or not plex_token:
        return {"refreshed": 0, "failed": 0}

    section_ids: list[int] = []
    if has_movies:
        sid = getattr(settings, "PLEX_MOVIE_SECTION_ID", None)
        if sid:
            section_ids.append(int(sid))
    if has_episodes:
        sid = getattr(settings, "PLEX_TV_SECTION_ID", None)
        if sid:
            section_ids.append(int(sid))

    return refresh_plex_section_ids(section_ids, force_refresh_metadata=force_refresh_metadata)
