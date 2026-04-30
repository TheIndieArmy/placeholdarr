from __future__ import annotations

import os
import re
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
    except Exception as e:
        logger.warning(
            f"Plex item metadata refresh failed rating_key={key}: {e}",
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


def refresh_plex_paths(paths: set[str], *, update_type: str = "Created") -> dict[str, int]:
    """Request path-scoped Plex refresh for changed folders.

    Path-scoped refresh avoids broad library sweeps by targeting only the
    specific folders where placeholder files were created or deleted.
    """
    if not paths:
        return {"refreshed": 0, "failed": 0}

    if not getattr(settings, "plex_enabled", False):
        return {"refreshed": 0, "failed": 0}

    plex_url = getattr(settings, "PLEX_URL", None)
    plex_token = getattr(settings, "PLEX_TOKEN", None)
    movie_section_id = getattr(settings, "PLEX_MOVIE_SECTION_ID", None)
    tv_section_id = getattr(settings, "PLEX_TV_SECTION_ID", None)
    if not plex_url or not plex_token:
        return {"refreshed": 0, "failed": 0}

    refreshed = 0
    failed = 0

    normalized_folders = []
    for path in sorted(paths):
        abs_path = os.path.abspath(path)
        normalized_folders.append(os.path.dirname(abs_path) if os.path.isfile(abs_path) else abs_path)

    for folder in dict.fromkeys(normalized_folders):
        try:
            abs_folder = os.path.abspath(folder)

            section_id = None
            movie_roots = [
                getattr(settings, "MOVIE_LIBRARY_FOLDER", None),
                getattr(settings, "MOVIE_LIBRARY_4K_FOLDER", None),
            ]
            tv_roots = [
                getattr(settings, "TV_LIBRARY_FOLDER", None),
                getattr(settings, "TV_LIBRARY_4K_FOLDER", None),
            ]

            for root in [r for r in movie_roots if r]:
                try:
                    if os.path.commonpath([abs_folder, os.path.abspath(root)]) == os.path.abspath(root):
                        section_id = movie_section_id
                        break
                except Exception:
                    continue

            if section_id is None:
                for root in [r for r in tv_roots if r]:
                    try:
                        if os.path.commonpath([abs_folder, os.path.abspath(root)]) == os.path.abspath(root):
                            section_id = tv_section_id
                            break
                    except Exception:
                        continue

            if section_id is None:
                logger.debug(
                    f"Skipping Plex refresh for out-of-scope folder: {abs_folder}",
                    extra={"emoji_type": "debug"},
                )
                continue

            url = f"{str(plex_url).rstrip('/')}/library/sections/{section_id}/refresh?path={quote(abs_folder)}"
            response = requests.get(url, headers={"X-Plex-Token": plex_token}, timeout=15)
            response.raise_for_status()
            refreshed += 1
        except Exception as e:
            failed += 1
            logger.warning(
                f"Path-scoped Plex refresh failed for folder={folder}: {e}",
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
