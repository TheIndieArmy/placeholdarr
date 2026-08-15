"""Version-gated dashboard notices (What's new / breaking upgrade prompts)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.version import APP_VERSION
from services.auth import _get_config_value, _set_config_value

LAST_SEEN_APP_VERSION_KEY = "LAST_SEEN_APP_VERSION"
DISMISSED_WHATS_NEW_IDS_KEY = "DISMISSED_WHATS_NEW_IDS"


@dataclass(frozen=True)
class WhatsNewNotice:
    id: str
    since_version: str
    title: str
    body: str
    cta_label: str | None = None
    cta_path: str | None = None


# Show when last_seen < since_version (skip-version upgrades still match).
# New installs stamp last_seen to APP_VERSION before setup completes, so they
# never see upgrade-only notices.
NOTICES: tuple[WhatsNewNotice, ...] = (
    WhatsNewNotice(
        id="webhook-apikey",
        since_version="0.9.16",
        title="Action required: update webhook URLs",
        body=(
            "Action is required. Placeholdarr now requires an API key on every webhook URL "
            "so Radarr, Sonarr, Tautulli, Jellyfin, and Emby can still call /webhook after "
            "dashboard login. Existing notification URLs without ?apikey= will be rejected "
            "until you copy the updated URL from Settings → Security → Webhook URLs and paste "
            "it into each of those services."
        ),
        cta_label="Open Security",
        cta_path="/settings/security",
    ),
    WhatsNewNotice(
        id="collections-beta",
        since_version="0.9.16",
        title="Collections (Beta)",
        body=(
            "You can now build Plex collections from saved recipes. Pull titles from TMDB, "
            "MDBList, Trakt, or your Placeholdarr catalog, filter and sort them, then sync "
            "membership into a Plex collection on a schedule. Open the Collections tab to "
            "create a recipe, preview matches, and run or schedule updates. The feature is "
            "Beta while we keep tightening edge cases."
        ),
        cta_label="Open Collections",
        cta_path="/collections",
    ),
)


def parse_semver(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("vV").split("+", 1)[0].split("-", 1)[0]
    parts = text.split(".")
    nums: list[int] = []
    for part in parts[:3]:
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def version_less(left: str, right: str) -> bool:
    return parse_semver(left) < parse_semver(right)


def _dismissed_ids(session=None) -> list[str]:
    raw = _get_config_value(DISMISSED_WHATS_NEW_IDS_KEY, session=session)
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return []


def _store_dismissed_ids(ids: list[str], session=None) -> None:
    _set_config_value(
        DISMISSED_WHATS_NEW_IDS_KEY,
        ids,
        value_type="json",
        description="Dismissed What's new notice ids",
        session=session,
    )


def get_last_seen_app_version(session=None) -> str | None:
    value = _get_config_value(LAST_SEEN_APP_VERSION_KEY, session=session)
    text = str(value or "").strip()
    return text or None


def set_last_seen_app_version(version: str, session=None) -> None:
    _set_config_value(
        LAST_SEEN_APP_VERSION_KEY,
        version,
        description="Last Placeholdarr version whose What's new notices were acknowledged",
        session=session,
    )


def _notice_payload(notice: WhatsNewNotice) -> dict[str, Any]:
    return {
        "id": notice.id,
        "since_version": notice.since_version,
        "title": notice.title,
        "body": notice.body,
        "cta_label": notice.cta_label,
        "cta_path": notice.cta_path,
    }


def pending_notices(*, setup_complete: bool, session=None) -> dict[str, Any]:
    """Return notices for this install and stamp last_seen for first-run setup."""
    last_seen = get_last_seen_app_version(session=session)
    dismissed = set(_dismissed_ids(session=session))

    if not setup_complete:
        if last_seen != APP_VERSION:
            set_last_seen_app_version(APP_VERSION, session=session)
        return {
            "app_version": APP_VERSION,
            "last_seen_app_version": APP_VERSION,
            "notices": [],
        }

    effective_last = last_seen if last_seen else "0.0.0"
    notices = [
        _notice_payload(notice)
        for notice in NOTICES
        if version_less(effective_last, notice.since_version) and notice.id not in dismissed
    ]
    return {
        "app_version": APP_VERSION,
        "last_seen_app_version": last_seen,
        "notices": notices,
    }


def catalog_notices(session=None) -> dict[str, Any]:
    """Full What's new list for the sidebar version chip (not version-gated)."""
    return {
        "app_version": APP_VERSION,
        "last_seen_app_version": get_last_seen_app_version(session=session),
        "notices": [_notice_payload(notice) for notice in NOTICES],
    }


def dismiss_notices(ids: list[str], *, setup_complete: bool, session=None) -> dict[str, Any]:
    wanted = {str(item).strip() for item in ids if str(item).strip()}
    known = {notice.id for notice in NOTICES}
    merged = list(dict.fromkeys([*(_dismissed_ids(session=session)), *sorted(wanted & known)]))
    _store_dismissed_ids(merged, session=session)
    remaining = pending_notices(setup_complete=setup_complete, session=session)
    if not remaining["notices"]:
        set_last_seen_app_version(APP_VERSION, session=session)
        remaining["last_seen_app_version"] = APP_VERSION
    return remaining
