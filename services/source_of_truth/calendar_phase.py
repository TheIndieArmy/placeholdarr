from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from core.config import settings
from core.logger import logger
from services.placeholders import ensure_placeholder_file, resolve_calendar_variant_dummy_path
from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Placeholder
from services.source_of_truth.status_intent import DisplayStatus, StatusIntent, StatusSource
from services.source_of_truth.status_orchestrator import StatusOrchestrator


@dataclass
class CalendarDecision:
    status: str
    reason: str
    days_until: int | None
    release_type: str | None = None  # e.g. "inCinemas", "digitalRelease", "physicalRelease"
    release_type_preferred: bool = False  # True if this is the preferred release type


def _release_type_label(release_type: str | None) -> str:
    key = str(release_type or "").strip()
    mapping = {
        "inCinemas": "Theatrical",
        "digitalRelease": "Digital",
        "physicalRelease": "Physical",
    }
    return mapping.get(key, "Release")


def _preferred_movie_release_date(movie: Movie) -> tuple[date | None, str | None, bool]:
    """Return (date, release_type, is_preferred) for the configured preferred movie release date.
    
    Returns:
      (date, release_type, is_preferred) where:
      - date: the selected release date or None
      - release_type: which type was selected (inCinemas, digitalRelease, etc.)
      - is_preferred: whether this matches the preferred type from config
    """
    preferred = str(getattr(settings, "PREFERRED_MOVIE_DATE_TYPE", "inCinemas") or "inCinemas").strip()
    mapping = {
        "inCinemas": "theater_release_date",
        "digitalRelease": "digital_release_date",
        "physicalRelease": "physical_release_date",
    }

    preferred_field = mapping.get(preferred, "theater_release_date")
    candidate = getattr(movie, preferred_field, None)
    if candidate:
        return (candidate, preferred, True)

    # Strict mode: do not fallback to other release types.
    return (None, preferred, True)


def _compute_calendar_decision(
    *,
    target_date: date | None,
    has_file: bool,
    media_type: str,
    lookahead_days: int,
    countdown_enabled: bool,
    placeholders_enabled: bool,
    now_date: date,
    release_type: str | None = None,
    release_type_preferred: bool = False,
) -> CalendarDecision:
    release_label = _release_type_label(release_type) if media_type == "movie" else ""

    # 0 means calendar-lookahead feature is disabled for future placeholders.
    lookahead_disabled = lookahead_days == 0
    infinite_lookahead = lookahead_days < 0

    if has_file:
        return CalendarDecision(
            status=DisplayStatus.AVAILABLE.value,
            reason="Media file is available",
            days_until=None,
            release_type=release_type,
            release_type_preferred=release_type_preferred,
        )

    if not target_date:
        # TBA is only used in infinite lookahead mode; strict lookahead treats unknown as out-of-window.
        reason = "No release date available"
        if media_type == "movie" and release_type:
            if infinite_lookahead:
                reason = f"{release_label} release date not yet available (TBA)"
            else:
                reason = f"{release_label} release date not available"
        return CalendarDecision(
            status=DisplayStatus.REQUEST.value,
            reason=reason,
            days_until=None,
            release_type=release_type,
            release_type_preferred=release_type_preferred,
        )

    days_until = (target_date - now_date).days

    if not placeholders_enabled:
        return CalendarDecision(
            status=DisplayStatus.REQUEST.value,
            reason="Calendar placeholders disabled",
            days_until=days_until,
            release_type=release_type,
            release_type_preferred=release_type_preferred,
        )

    if lookahead_disabled:
        return CalendarDecision(
            status=DisplayStatus.REQUEST.value,
            reason="Calendar lookahead disabled",
            days_until=days_until,
            release_type=release_type,
            release_type_preferred=release_type_preferred,
        )

    if days_until < 0:
        if media_type == "movie" and release_type:
            reason = f"{release_label} release was {abs(days_until)} days ago"
        else:
            reason = "Release date passed; waiting for import"
        return CalendarDecision(
            status=DisplayStatus.REQUEST.value,
            reason=reason,
            days_until=days_until,
            release_type=release_type,
            release_type_preferred=release_type_preferred,
        )

    if not infinite_lookahead and lookahead_days > 0 and days_until > lookahead_days:
        return CalendarDecision(
            status=DisplayStatus.REQUEST.value,
            reason=f"Outside lookahead window ({lookahead_days} days)",
            days_until=days_until,
            release_type=release_type,
            release_type_preferred=release_type_preferred,
        )

    if not countdown_enabled:
        if media_type == "movie" and release_type:
            label = f"{release_label} release coming soon"
        else:
            label = "Coming Soon" if media_type == "movie" else "Airing soon"
        return CalendarDecision(
            status=DisplayStatus.COMING_SOON.value,
            reason=label,
            days_until=days_until,
            release_type=release_type,
            release_type_preferred=release_type_preferred,
        )

    if days_until == 0:
        if media_type == "movie" and release_type:
            label = f"{release_label} release today"
        else:
            label = "Coming Soon (Today)" if media_type == "movie" else "Airing today"
        return CalendarDecision(
            status=DisplayStatus.COMING_SOON_TODAY.value,
            reason=label,
            days_until=days_until,
            release_type=release_type,
            release_type_preferred=release_type_preferred,
        )

    if media_type == "movie":
        if release_type:
            label = (
                f"{release_label} release in 1 day"
                if days_until == 1
                else f"{release_label} release in {days_until} days"
            )
        else:
            label = "Coming Soon (1 day)" if days_until == 1 else f"Coming Soon ({days_until} days)"
    else:
        label = "Airing in 1 day" if days_until == 1 else f"Airing in {days_until} days"

    if days_until <= 6:
        status = DisplayStatus.COMING_SOON_1.value
    elif days_until <= 13:
        status = DisplayStatus.COMING_SOON_7.value
    elif days_until <= 29:
        status = DisplayStatus.COMING_SOON_14.value
    else:
        status = DisplayStatus.COMING_SOON_30.value

    return CalendarDecision(
        status=status,
        reason=label,
        days_until=days_until,
        release_type=release_type,
        release_type_preferred=release_type_preferred,
    )


def _is_coming_soon_status(status: str | None) -> bool:
    return str(status or "") in {
        DisplayStatus.COMING_SOON.value,
        DisplayStatus.COMING_SOON_30.value,
        DisplayStatus.COMING_SOON_14.value,
        DisplayStatus.COMING_SOON_7.value,
        DisplayStatus.COMING_SOON_1.value,
        DisplayStatus.COMING_SOON_TODAY.value,
    }


def _dummy_variant_for_status(status: str | None) -> str:
    return "coming_soon" if _is_coming_soon_status(status) else "request"


def _normalize_reason(value: str | None) -> str:
    return str(value or "").strip()


def _calendar_variant_settings_fingerprint() -> str:
    """Hash settings that influence placeholder variant selection/replacement."""
    parts = [
        str(bool(settings.coming_soon_placeholders_enabled)),
        str(int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)),
        str(bool(getattr(settings, "ENABLE_COMING_SOON_COUNTDOWN", True))),
        str(getattr(settings, "PREFERRED_MOVIE_DATE_TYPE", "inCinemas") or "inCinemas"),
        str(getattr(settings, "DUMMY_FILE_PATH", "") or ""),
        str(getattr(settings, "COMING_SOON_DUMMY_FILE_PATH", "") or ""),
    ]
    payload = "|".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _variant_dummy_path(variant: str) -> str:
    return resolve_calendar_variant_dummy_path(variant)


def _switch_placeholder_dummy_variant(
    placeholder: Placeholder,
    variant: str,
    settings_fingerprint: str,
) -> bool:
    path = str(getattr(placeholder, "path", "") or "").strip()
    if not path:
        return False

    extra = dict(getattr(placeholder, "extra", {}) or {})
    current_variant = str(extra.get("calendar_dummy_variant") or "").strip()
    prior_fingerprint = str(extra.get("calendar_variant_settings_fingerprint") or "").strip()
    target_dummy = _variant_dummy_path(variant)
    settings_changed = bool(prior_fingerprint and prior_fingerprint != settings_fingerprint)
    variant_changed = current_variant != variant

    replaced = ensure_placeholder_file(
        path,
        dummy_file_path=target_dummy,
        replace_existing=(variant_changed or settings_changed),
    )

    if variant_changed:
        extra["calendar_dummy_variant"] = variant
    # Always stamp current decision fingerprint so future runs can detect setting flips.
    extra["calendar_variant_settings_fingerprint"] = settings_fingerprint
    if variant_changed or prior_fingerprint != settings_fingerprint:
        placeholder.extra = extra
    return bool(replaced or variant_changed or settings_changed)


def _placeholder_target(session, placeholder: Placeholder) -> tuple[str | None, date | None, bool, str | None, bool]:
    """Return (media_type, target_date, has_file, release_type, release_type_preferred)"""
    if getattr(placeholder, "movie_id", None):
        movie = session.query(Movie).get(int(placeholder.movie_id))
        if not movie:
            return None, None, False, None, False
        target_date, release_type, is_preferred = _preferred_movie_release_date(movie)
        return "movie", target_date, bool(getattr(movie, "has_file", False)), release_type, is_preferred

    if getattr(placeholder, "episode_id", None):
        episode = session.query(Episode).get(int(placeholder.episode_id))
        if not episode:
            return None, None, False, None, False
        # Episodes don't have release type preferences; air_date is singular
        return "episode", getattr(episode, "air_date", None), bool(getattr(episode, "has_file", False)), None, False

    return None, None, False, None, False


def run_calendar_phase() -> dict[str, Any]:
    stats: dict[str, Any] = {
        "scanned": 0,
        "status_intents": 0,
        "status_applied": 0,
        "status_skipped": 0,
        "variant_switched": 0,
        "errors": 0,
    }

    if not bool(getattr(settings, "ENABLE_STATUS_ORCHESTRATOR_CALENDAR", True)):
        stats["skipped"] = 1
        stats["reason"] = "calendar_phase_disabled"
        return stats

    lookahead_days = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    placeholders_enabled = bool(settings.coming_soon_placeholders_enabled)
    countdown_enabled = bool(getattr(settings, "ENABLE_COMING_SOON_COUNTDOWN", True))
    now_date = datetime.now(timezone.utc).date()
    settings_fingerprint = _calendar_variant_settings_fingerprint()

    session = get_session()
    try:
        placeholders = session.query(Placeholder).filter(Placeholder.has_placeholder == True).all()  # noqa: E712
        stats["scanned"] = len(placeholders)

        intents: list[StatusIntent] = []
        for placeholder in placeholders:
            media_type, target_date, has_file, release_type, release_type_preferred = _placeholder_target(session, placeholder)
            if not media_type:
                continue

            decision = _compute_calendar_decision(
                target_date=target_date,
                has_file=has_file,
                media_type=media_type,
                lookahead_days=lookahead_days,
                countdown_enabled=countdown_enabled,
                placeholders_enabled=placeholders_enabled,
                now_date=now_date,
                release_type=release_type,
                release_type_preferred=release_type_preferred,
            )

            desired_variant = _dummy_variant_for_status(decision.status)
            try:
                if _switch_placeholder_dummy_variant(placeholder, desired_variant, settings_fingerprint):
                    stats["variant_switched"] += 1
            except Exception as exc:
                stats["errors"] += 1
                logger.warning(
                    f"Calendar variant switch failed placeholder_id={placeholder.id}: {exc}",
                    extra={"emoji_type": "warning"},
                )

            current_status = str(getattr(placeholder, "display_status", "") or "")
            current_reason = _normalize_reason(getattr(placeholder, "display_reason", None))
            desired_reason = _normalize_reason(decision.reason)

            # Important: bucketed status values (COMING_SOON_7, etc.) still need
            # daily updates to display_reason so users see daily countdown changes.
            if current_status == decision.status and current_reason == desired_reason:
                stats["status_skipped"] += 1
                continue

            intents.append(
                StatusIntent(
                    placeholder_id=int(placeholder.id),
                    new_status=decision.status,
                    reason=decision.reason,
                    source=StatusSource.CALENDAR_RELEASE_WINDOW,
                    trigger_nfo_refresh=True,
                    metadata={
                        "days_until_release": decision.days_until,
                        "media_type": media_type,
                        "target_date": str(target_date) if target_date else None,
                        "release_type": decision.release_type,
                        "release_type_preferred": decision.release_type_preferred,
                    },
                )
            )

        stats["status_intents"] = len(intents)
        if intents:
            orchestrator = StatusOrchestrator(session=session)
            stats["status_applied"] = int(orchestrator.apply_and_project_statuses(intents) or 0)

        session.commit()
    except Exception as exc:
        session.rollback()
        stats["errors"] += 1
        logger.error(f"Calendar phase failed: {exc}", extra={"emoji_type": "error"})
    finally:
        session.close()

    return stats
