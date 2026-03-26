"""Plex scan-state helpers used by the observer flow."""

from typing import Any

import requests

from core.config import settings
from core.logger import logger


def get_plex_server() -> Any | None:
    """Get the Plex server instance from services.services_old.plex_client."""
    try:
        from services.services_old import plex_client
        plex = getattr(plex_client, 'plex', None)
        return plex
    except Exception as e:
        logger.debug(f"Failed to get Plex server: {e}")
        return None


def _coerce_section_id(value: Any) -> int | None:
    if value in (None, "", "None"):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _extract_section_id_from_context(context: Any) -> int | None:
    if context is None:
        return None

    if isinstance(context, dict):
        return (
            _coerce_section_id(context.get('librarySectionID'))
            or _coerce_section_id(context.get('sectionID'))
        )

    return (
        _coerce_section_id(getattr(context, 'librarySectionID', None))
        or _coerce_section_id(getattr(context, 'sectionID', None))
    )


def _extract_section_id_from_activity_obj(activity: Any) -> int | None:
    direct_section_id = (
        _coerce_section_id(getattr(activity, 'librarySectionID', None))
        or _coerce_section_id(getattr(activity, 'sectionID', None))
    )
    if direct_section_id is not None:
        return direct_section_id

    for attr_name in ('context', 'Context'):
        section_id = _extract_section_id_from_context(getattr(activity, attr_name, None))
        if section_id is not None:
            return section_id

    data = getattr(activity, '_data', None)
    if data is not None:
        section_id = _coerce_section_id(getattr(data, 'attrib', {}).get('librarySectionID'))
        if section_id is not None:
            return section_id
        for child in list(data):
            if getattr(child, 'tag', None) != 'Context':
                continue
            section_id = _coerce_section_id(getattr(child, 'attrib', {}).get('librarySectionID'))
            if section_id is not None:
                return section_id

    return None


def _extract_section_id_from_activity_row(row: dict[str, Any]) -> int | None:
    return (
        _coerce_section_id(row.get('librarySectionID'))
        or _coerce_section_id(row.get('sectionID'))
        or _extract_section_id_from_context(row.get('Context'))
        or _extract_section_id_from_context(row.get('context'))
    )


def _is_library_update_section(activity_type: Any) -> bool:
    return bool(activity_type and 'library.update.section' in str(activity_type).lower())


def _fetch_raw_plex_activity_rows() -> list[dict[str, Any]]:
    plex_url = getattr(settings, 'PLEX_URL', None)
    plex_token = getattr(settings, 'PLEX_TOKEN', None)
    if not plex_url or not plex_token:
        return []

    try:
        response = requests.get(
            f"{str(plex_url).rstrip('/')}/activities",
            headers={
                'X-Plex-Token': plex_token,
                'Accept': 'application/json',
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json() or {}
        container = payload.get('MediaContainer') or {}
        rows = container.get('Activity') or []
        return [row for row in rows if isinstance(row, dict)]
    except Exception as e:
        logger.debug(f"Failed to fetch raw Plex activities JSON: {e}")
        return []


def _collect_active_scan_state(plex: Any) -> tuple[bool, set[int]]:
    raw_rows = _fetch_raw_plex_activity_rows()
    if raw_rows:
        active_sections: set[int] = set()
        has_any_scan = False
        for row in raw_rows:
            if not _is_library_update_section(row.get('type')):
                continue
            has_any_scan = True
            section_id = _extract_section_id_from_activity_row(row)
            if section_id is not None:
                active_sections.add(section_id)
        return has_any_scan, active_sections

    activities: list[Any] = []
    activities_attr = getattr(plex, 'activities', None)
    if callable(activities_attr):
        activities = activities_attr() or []
    elif isinstance(activities_attr, list):
        activities = activities_attr
    elif hasattr(plex, 'getActivities'):
        get_activities = getattr(plex, 'getActivities')
        if callable(get_activities):
            activities = get_activities() or []

    active_sections: set[int] = set()
    has_any_scan = False
    for activity in activities:
        if not _is_library_update_section(getattr(activity, 'type', None)):
            continue
        has_any_scan = True
        section_id = _extract_section_id_from_activity_obj(activity)
        if section_id is not None:
            active_sections.add(section_id)
    return has_any_scan, active_sections


def is_plex_busy_checking_enabled() -> bool:
    """Busy checking is always enabled for the source-of-truth observer."""
    return True


def check_plex_section_refreshing(plex: Any, section_key: int | str) -> bool:
    """Check whether a specific Plex section reports itself as refreshing."""
    if not plex:
        return False
    
    try:
        # Query /library/sections endpoint to get section metadata
        # The section_refreshing attribute is directly accessible
        section = plex.library.sectionByID(int(section_key))
        if section:
            is_refreshing = section.refreshing if hasattr(section, 'refreshing') else False
            logger.debug(
                f"Section {section_key} refreshing status: {is_refreshing}",
                extra={'emoji_type': 'debug'}
            )
            return bool(is_refreshing)
    except Exception as e:
        logger.debug(f"Failed to check section {section_key} refreshing status: {e}")
        return False
    
    return False


def check_plex_active_activities(plex: Any, section_key: int | str | None = None) -> bool:
    """Check whether Plex reports active section-scan activity."""
    if not plex:
        return False
    
    try:
        has_any_scan, active_sections = _collect_active_scan_state(plex)
        if not has_any_scan:
            return False

        section_key_int = int(section_key) if section_key is not None else None

        if section_key_int is not None:
            if section_key_int in active_sections:
                logger.debug(
                    f"Plex actively scanning section {section_key}",
                    extra={'emoji_type': 'debug'}
                )
                return True
            return False

        debug_section = sorted(active_sections) if active_sections else None
        logger.debug(
            f"Plex actively scanning (section {debug_section})",
            extra={'emoji_type': 'debug'}
        )
        return True
    except Exception as e:
        logger.debug(f"Failed to check Plex activities: {e}")
        return False


def is_plex_busy_for_section(section_key: int | str | None = None) -> bool:
	"""Check whether Plex is busy scanning, optionally for a specific section.
	
	Uses only the /activities endpoint (authoritative source of truth).
	Does not fall back to the refreshing flag to avoid stale PlexAPI cache issues.
	"""
	if not is_plex_busy_checking_enabled():
		return False
	
	plex = get_plex_server()
	if not plex:
		return False
	
	return check_plex_active_activities(plex, section_key)


def has_any_active_plex_scan() -> bool:
    """Return True when Plex reports any active library scan, even without a section id."""
    if not is_plex_busy_checking_enabled():
        return False

    plex = get_plex_server()
    if not plex:
        return False

    return check_plex_active_activities(plex, None)

def get_active_plex_scan_section_ids() -> set[int]:
    """Return the set of Plex section IDs currently observed as actively scanning.

    Uses only the /activities endpoint (authoritative source of truth).
    Does not fall back to the refreshing flag to avoid stale PlexAPI cache issues.
    """
    if not is_plex_busy_checking_enabled():
        return set()

    plex = get_plex_server()
    if not plex:
        return set()

    try:
        _, active_sections = _collect_active_scan_state(plex)
        return active_sections
    except Exception as e:
        logger.debug(f"Failed to collect active Plex scan section IDs: {e}")
        return set()


def get_active_expected_scan_sections(expected_section_ids: set[int]) -> set[int]:
    """Return the subset of expected Plex section IDs that are actively scanning."""
    if not expected_section_ids:
        return set()
    return get_active_plex_scan_section_ids().intersection({int(section_id) for section_id in expected_section_ids})
