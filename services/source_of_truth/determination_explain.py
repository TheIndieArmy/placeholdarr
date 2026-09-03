"""On-demand explanation of placeholder determination for library detail UI."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from core.config import settings
from services.placeholders import episode_placeholder_path, movie_placeholder_path
from services.postgres.models import Episode, Movie, Season, Series
from services.source_of_truth.arr_share_guard import (
    shared_placeholder_suppresses_creation,
    sibling_episode_has_file,
    sibling_movie_has_file,
)
from services.source_of_truth.determiner import (
    DETERMINATION_EXISTS,
    DETERMINATION_NEEDS,
    DETERMINATION_NOT_NEEDED,
    DETERMINATION_OBSOLETE,
    _apply_force_placeholder,
    _apply_block_placeholder,
    _apply_monitored_placeholder_suppression,
    _apply_sibling_placeholder_suppression,
    _episode_placeholder_path_drifts,
    _movie_placeholder_path_drifts,
    _preferred_movie_release_date,
    _series_monitored_for_episode,
    _sibling_would_suppress_creation,
    _skip_placeholders_when_monitored_enabled,
    _skip_placeholders_when_series_monitored_enabled,
)


ExplainStatus = str  # pass | fail | skip | applied


def _format_determination_label(value: str) -> str:
    return str(value or "").replace("_", " ")


def _preferred_movie_date_label() -> str:
    preferred = str(getattr(settings, "PREFERRED_MOVIE_DATE_TYPE", "inCinemas") or "inCinemas").strip()
    mapping = {
        "inCinemas": "theatrical release",
        "digitalRelease": "digital release",
        "physicalRelease": "physical release",
    }
    return mapping.get(preferred, "theatrical release")


def _step(
    key: str,
    label: str,
    status: ExplainStatus,
    *,
    detail: str | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"key": key, "label": label, "status": status}
    if detail:
        row["detail"] = detail
    if outcome:
        row["outcome"] = outcome
    return row


def _deciding_step_key(steps: list[dict[str, Any]], final: str) -> str:
    for step in steps:
        if step.get("outcome") == final and step.get("status") in {"fail", "applied"}:
            return str(step.get("key") or "final")
    for step in steps:
        if step.get("status") == "fail":
            return str(step.get("key") or "final")
    return str(steps[-1].get("key") if steps else "final")


def _build_summary(final: str, deciding_key: str, steps: list[dict[str, Any]]) -> str:
    deciding = next((s for s in steps if s.get("key") == deciding_key), None)
    detail = str(deciding.get("detail") or "").strip() if deciding else ""
    label = _format_determination_label(final)
    if detail:
        return f"Determination is {label}. {detail}"
    return f"Determination is {label}."


def _pin_blocked_by_file_detail(is_deleted: bool) -> str:
    if is_deleted:
        return (
            "Pinned on this title, but the title was removed from the library so no placeholder "
            "is created."
        )
    return (
        "Pinned on this title. Placeholdarr would override calendar, monitored, and sibling "
        "rules, but a real file is on disk so no placeholder is needed."
    )


def _explain_deciding_step_key(
    steps: list[dict[str, Any]],
    final: str,
    entity,
    *,
    has_file: bool,
    is_deleted: bool,
) -> str:
    pinned = bool(getattr(entity, "force_placeholder", False))
    blocked = bool(getattr(entity, "block_placeholder", False))
    if pinned and (has_file or is_deleted) and final == DETERMINATION_NOT_NEEDED:
        return "placeholder_policy"
    if blocked and (has_file or is_deleted) and final == DETERMINATION_NOT_NEEDED:
        return "placeholder_policy"
    if blocked and final in (DETERMINATION_NOT_NEEDED, DETERMINATION_OBSOLETE):
        return "placeholder_policy"
    return _deciding_step_key(steps, final)


def _explain_summary(
    final: str,
    deciding_key: str,
    steps: list[dict[str, Any]],
    entity,
    *,
    has_file: bool,
    is_deleted: bool,
) -> str:
    pinned = bool(getattr(entity, "force_placeholder", False))
    blocked = bool(getattr(entity, "block_placeholder", False))
    label = _format_determination_label(final)
    if pinned and has_file and final == DETERMINATION_NOT_NEEDED:
        return (
            f"Determination is {label}. This title is pinned, so calendar, monitored, and sibling "
            "rules would be overridden, but a real file is on disk so no placeholder is needed."
        )
    if pinned and is_deleted and final == DETERMINATION_NOT_NEEDED:
        return (
            f"Determination is {label}. This title is pinned, but it was removed from the library "
            "so no placeholder is created."
        )
    if blocked and has_file and final == DETERMINATION_NOT_NEEDED:
        return (
            f"Determination is {label}. Never is set on this title, but a real file is on disk so "
            "no placeholder is needed."
        )
    return _build_summary(final, deciding_key, steps)


def _calendar_guard_result(
    *,
    has_placeholder: bool,
    has_file: bool,
    is_deleted: bool,
    target_date: date | None,
    release_status: str | None,
    lookahead_days: int,
    placeholders_enabled: bool,
    now_date: date,
    date_label: str,
) -> tuple[str | None, dict[str, Any]]:
    """Return (outcome_if_calendar_decided, step) or (None, step) when calendar passes."""
    if not placeholders_enabled:
        return None, _step(
            "calendar_window",
            "Within calendar window",
            "skip",
            detail="Coming-soon placeholders are disabled.",
        )
    if has_file or is_deleted:
        return None, _step(
            "calendar_window",
            "Within calendar window",
            "skip",
            detail="Skipped because the title already has a file or is removed from the library.",
        )

    lookahead = int(lookahead_days)
    if lookahead < 0:
        return None, _step(
            "calendar_window",
            "Within calendar window",
            "pass",
            detail="Calendar lookahead is unlimited.",
        )

    if target_date is None:
        released = str(release_status or "").strip().lower() == "released"
        if released:
            return None, _step(
                "calendar_window",
                "Within calendar window",
                "pass",
                detail=f"No {date_label} date on file, but Radarr reports released.",
            )
        outcome = DETERMINATION_OBSOLETE if has_placeholder else DETERMINATION_NOT_NEEDED
        return outcome, _step(
            "calendar_window",
            "Within calendar window",
            "fail",
            detail=f"No {date_label} date and Radarr does not report released.",
            outcome=outcome,
        )

    days_until = (target_date - now_date).days
    date_str = target_date.isoformat()
    if lookahead == 0:
        if days_until > 0:
            outcome = DETERMINATION_OBSOLETE if has_placeholder else DETERMINATION_NOT_NEEDED
            return outcome, _step(
                "calendar_window",
                "Within calendar window",
                "fail",
                detail=f"{date_label} is {date_str} ({days_until} day(s) away). Future placeholders are off.",
                outcome=outcome,
            )
        return None, _step(
            "calendar_window",
            "Within calendar window",
            "pass",
            detail=f"{date_label} is {date_str} (today or in the past).",
        )

    if days_until > lookahead:
        outcome = DETERMINATION_OBSOLETE if has_placeholder else DETERMINATION_NOT_NEEDED
        return outcome, _step(
            "calendar_window",
            "Within calendar window",
            "fail",
            detail=(
                f"{date_label} is {date_str} ({days_until} days away). "
                f"Calendar lookahead is {lookahead} days."
            ),
            outcome=outcome,
        )
    return None, _step(
        "calendar_window",
        "Within calendar window",
        "pass",
        detail=f"{date_label} is {date_str} ({days_until} days away). Within {lookahead}-day lookahead.",
    )


def _file_state_result(
    *,
    has_placeholder: bool,
    has_file: bool,
    is_deleted: bool,
    prior_outcome: str | None,
) -> tuple[str, dict[str, Any]]:
    if prior_outcome is not None:
        return prior_outcome, _step(
            "file_state",
            "File and placeholder state",
            "skip",
            detail="Calendar window already decided the outcome.",
            outcome=prior_outcome,
        )

    if has_placeholder and (has_file or is_deleted):
        return DETERMINATION_OBSOLETE, _step(
            "file_state",
            "File and placeholder state",
            "fail",
            detail="Placeholder exists but a real file is on disk or the title was removed.",
            outcome=DETERMINATION_OBSOLETE,
        )
    if has_file or is_deleted:
        return DETERMINATION_NOT_NEEDED, _step(
            "file_state",
            "File and placeholder state",
            "fail",
            detail="Real file on disk or title removed from the library.",
            outcome=DETERMINATION_NOT_NEEDED,
        )
    if has_placeholder:
        return DETERMINATION_EXISTS, _step(
            "file_state",
            "File and placeholder state",
            "pass",
            detail="Placeholder on disk and no real file yet.",
            outcome=DETERMINATION_EXISTS,
        )
    return DETERMINATION_NEEDS, _step(
        "file_state",
        "File and placeholder state",
        "pass",
        detail="No real file and no placeholder yet.",
        outcome=DETERMINATION_NEEDS,
    )


def _apply_modifier_step(
    *,
    key: str,
    label: str,
    before: str,
    after: str,
    skip_detail: str | None = None,
) -> dict[str, Any]:
    if before == after and skip_detail:
        return _step(key, label, "skip", detail=skip_detail)
    if before != after:
        return _step(
            key,
            label,
            "applied",
            detail=f"Changed from {_format_determination_label(before)} to {_format_determination_label(after)}.",
            outcome=after,
        )
    return _step(key, label, "pass", detail="Did not change the outcome.")


def _placeholder_policy_step(
    *,
    entity,
    before: str,
    after: str,
    has_file: bool,
    is_deleted: bool,
    sibling_would_suppress: bool,
) -> dict[str, Any]:
    """One Why? step for the Auto / Never / Pinned chip (not two flag rows)."""
    from services.source_of_truth.placeholder_policy import policy_from_entity

    policy = policy_from_entity(entity)
    if policy == "auto":
        return _step(
            "placeholder_policy",
            "Placeholder policy",
            "skip",
            detail="Policy is Auto: follow Placeholdarr settings.",
        )
    if policy == "never":
        if has_file or is_deleted:
            return _step(
                "placeholder_policy",
                "Placeholder policy",
                "applied",
                detail=(
                    "Policy is Never. A real file is on disk or the title was removed, "
                    "so placeholders are not created or removed."
                ),
                outcome=before,
            )
        if before != after:
            detail = (
                f"Policy is Never. Set determination to {_format_determination_label(after)}."
                if after != DETERMINATION_OBSOLETE
                else "Policy is Never. Marks the existing placeholder obsolete so it can be removed."
            )
            return _step(
                "placeholder_policy",
                "Placeholder policy",
                "applied",
                detail=detail,
                outcome=after,
            )
        return _step(
            "placeholder_policy",
            "Placeholder policy",
            "pass",
            detail="Policy is Never. Placeholders are already blocked for this title.",
            outcome=after,
        )
    if has_file or is_deleted:
        return _step(
            "placeholder_policy",
            "Placeholder policy",
            "applied",
            detail=_pin_blocked_by_file_detail(is_deleted),
            outcome=before,
        )
    if sibling_would_suppress and not bool(getattr(entity, "force_placeholder_despite_sibling", False)):
        return _step(
            "placeholder_policy",
            "Placeholder policy",
            "skip",
            detail=(
                "Policy is Pinned, but a shared-instance sibling has a file and "
                "Shared Placeholder Cleanup is on."
            ),
            outcome=before,
        )
    if before != after:
        return _step(
            "placeholder_policy",
            "Placeholder policy",
            "applied",
            detail=f"Policy is Pinned. Set determination to {_format_determination_label(after)}.",
            outcome=after,
        )
    return _step(
        "placeholder_policy",
        "Placeholder policy",
        "pass",
        detail="Policy is Pinned. Determination already needed a placeholder.",
        outcome=after,
    )


def _build_series_max_known_order_within_horizon(
    session,
    series_ids: list[int],
    *,
    lookahead_days: int,
    now_date: date,
) -> dict[int, tuple[int, int]]:
    if not series_ids:
        return {}
    horizon = now_date + timedelta(days=int(lookahead_days)) if int(lookahead_days) >= 0 else None
    known_rows = (
        session.query(Season.series_id, Season.season_number, Episode.episode_number)
        .join(Season, Episode.season_id == Season.id)
        .filter(
            Season.series_id.in_(list(series_ids)),
            Episode.air_date.isnot(None),
            Episode.is_deleted == False,  # noqa: E712
        )
        .filter(Episode.air_date <= horizon if horizon is not None else True)
        .all()
    )
    out: dict[int, tuple[int, int]] = {}
    for series_id, season_number, episode_number in known_rows:
        if series_id is None:
            continue
        order = (int(season_number or 0), int(episode_number or 0))
        sid = int(series_id)
        prev = out.get(sid)
        if prev is None or order > prev:
            out[sid] = order
    return out


def explain_movie_determination(session, movie: Movie) -> dict[str, Any]:
    now_date = datetime.now(timezone.utc).date()
    placeholders_enabled = bool(settings.coming_soon_placeholders_enabled)
    lookahead_days = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    has_placeholder = bool(getattr(movie, "has_placeholder", False))
    has_file = bool(getattr(movie, "has_file", False))
    is_deleted = bool(getattr(movie, "is_deleted", False))
    target_date = _preferred_movie_release_date(movie)
    date_label = _preferred_movie_date_label()

    steps: list[dict[str, Any]] = []

    calendar_outcome, calendar_step = _calendar_guard_result(
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
        target_date=target_date,
        release_status=getattr(movie, "radarr_release_status", None),
        lookahead_days=lookahead_days,
        placeholders_enabled=placeholders_enabled,
        now_date=now_date,
        date_label=date_label,
    )
    steps.append(calendar_step)

    base, file_step = _file_state_result(
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
        prior_outcome=calendar_outcome,
    )
    steps.append(file_step)

    if has_placeholder and _movie_placeholder_path_drifts(movie):
        base = DETERMINATION_OBSOLETE
        expected = movie_placeholder_path(movie)
        stored = getattr(movie, "placeholder_filepath", None)
        steps.append(
            _step(
                "path_drift",
                "Placeholder path",
                "fail",
                detail=f"Stored path does not match expected location ({stored!r} vs {expected!r}).",
                outcome=base,
            )
        )
    else:
        steps.append(
            _step(
                "path_drift",
                "Placeholder path",
                "skip",
                detail="No placeholder path drift detected.",
            )
        )

    before = base
    base = _apply_monitored_placeholder_suppression(
        session,
        base=base,
        entity=movie,
        media_type="movie",
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
        movie_id=int(movie.id) if getattr(movie, "id", None) is not None else None,
    )
    monitored_skip = None
    if not _skip_placeholders_when_monitored_enabled():
        monitored_skip = "Skip-when-monitored is off."
    elif has_file or is_deleted:
        monitored_skip = "Skipped because a real file exists or the title was removed."
    elif not bool(getattr(movie, "radarr_monitored", False)):
        monitored_skip = "Title is not monitored in Radarr."
    steps.append(
        _apply_modifier_step(
            key="monitored_suppression",
            label="Monitored suppression",
            before=before,
            after=base,
            skip_detail=monitored_skip,
        )
    )

    before = base
    sibling_has_file = sibling_movie_has_file(session, movie)
    base = _apply_sibling_placeholder_suppression(
        arr_type="radarr",
        base=base,
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
        sibling_has_file=sibling_has_file,
    )
    sibling_skip = None
    if not shared_placeholder_suppresses_creation("radarr"):
        sibling_skip = "Shared-instance suppression is off."
    elif has_file or is_deleted:
        sibling_skip = "Skipped because a real file exists or the title was removed."
    elif not sibling_has_file:
        sibling_skip = "No sibling instance has this title on disk."
    steps.append(
        _apply_modifier_step(
            key="sibling_suppression",
            label="Shared-instance suppression",
            before=before,
            after=base,
            skip_detail=sibling_skip,
        )
    )

    sibling_block = _sibling_would_suppress_creation(
        arr_type="radarr",
        has_file=has_file,
        is_deleted=is_deleted,
        sibling_has_file=sibling_has_file,
    )
    before = base
    base = _apply_block_placeholder(
        base=base,
        entity=movie,
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
    )
    base = _apply_force_placeholder(
        base=base,
        entity=movie,
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
        sibling_would_suppress=sibling_block,
    )
    steps.append(
        _placeholder_policy_step(
            entity=movie,
            before=before,
            after=base,
            has_file=has_file,
            is_deleted=is_deleted,
            sibling_would_suppress=sibling_block,
        )
    )

    final = base
    deciding = _explain_deciding_step_key(
        steps,
        final,
        movie,
        has_file=has_file,
        is_deleted=is_deleted,
    )
    title = str(getattr(movie, "title", "") or "Movie")
    return {
        "ok": True,
        "media_type": "movie",
        "title": title,
        "determination": final,
        "deciding_step_key": deciding,
        "summary": _explain_summary(
            final,
            deciding,
            steps,
            movie,
            has_file=has_file,
            is_deleted=is_deleted,
        ),
        "steps": steps,
    }


def explain_episode_determination(session, episode: Episode) -> dict[str, Any]:
    now_date = datetime.now(timezone.utc).date()
    placeholders_enabled = bool(settings.coming_soon_placeholders_enabled)
    lookahead_days = int(getattr(settings, "CALENDAR_LOOKAHEAD_DAYS", 30) or 30)
    has_placeholder = bool(getattr(episode, "has_placeholder", False))
    has_file = bool(getattr(episode, "has_file", False))
    is_deleted = bool(getattr(episode, "is_deleted", False))

    season = session.query(Season).filter(Season.id == episode.season_id).first()
    series = (
        session.query(Series).filter(Series.id == season.series_id).first()
        if season and getattr(season, "series_id", None) is not None
        else None
    )
    season_number = int(getattr(season, "season_number", 0) or 0) if season else 0
    episode_number = int(getattr(episode, "episode_number", 0) or 0)
    series_id = int(season.series_id) if season and getattr(season, "series_id", None) is not None else None
    episode_meta = (
        (series_id, season_number, episode_number) if series_id is not None else None
    )

    steps: list[dict[str, Any]] = []

    include_specials = bool(getattr(settings, "INCLUDE_SPECIALS", False))
    forced = bool(getattr(episode, "force_placeholder", False))
    if not include_specials and season_number == 0 and not forced:
        final = DETERMINATION_NOT_NEEDED
        steps.append(
            _step(
                "episode_specials",
                "Specials policy",
                "fail",
                detail="Season 0 specials are excluded by settings.",
                outcome=final,
            )
        )
        deciding = _deciding_step_key(steps, final)
        title = str(getattr(episode, "title", "") or f"Episode {episode_number}")
        return {
            "ok": True,
            "media_type": "episode",
            "title": title,
            "determination": final,
            "deciding_step_key": deciding,
            "summary": _build_summary(final, deciding, steps),
            "steps": steps,
        }
    if not include_specials and season_number == 0 and forced:
        steps.append(
            _step(
                "episode_specials",
                "Specials policy",
                "applied",
                detail="Season 0 specials are excluded by settings, but policy is Pinned.",
            )
        )
    else:
        steps.append(
            _step(
                "episode_specials",
                "Specials policy",
                "skip",
                detail="Not an excluded special, or specials are included.",
            )
        )

    target_date = getattr(episode, "air_date", None)
    inferred_air_date = False
    if (
        target_date is None
        and placeholders_enabled
        and lookahead_days >= 0
        and episode_meta is not None
    ):
        series_max = _build_series_max_known_order_within_horizon(
            session,
            [int(series_id)],
            lookahead_days=lookahead_days,
            now_date=now_date,
        )
        max_known = series_max.get(int(series_id))
        if max_known is not None and max_known > (season_number, episode_number):
            target_date = now_date
            inferred_air_date = True

    if inferred_air_date:
        steps.append(
            _step(
                "episode_unknown_air_date",
                "Unknown air date inference",
                "applied",
                detail="Air date unknown, but a later episode in the run has a known date inside lookahead.",
                outcome=None,
            )
        )
    else:
        air_detail = (
            f"Air date is {target_date.isoformat()}."
            if target_date is not None
            else "Air date is unknown."
        )
        steps.append(
            _step(
                "episode_unknown_air_date",
                "Unknown air date inference",
                "skip",
                detail=air_detail,
            )
        )

    calendar_outcome, calendar_step = _calendar_guard_result(
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
        target_date=target_date,
        release_status=None,
        lookahead_days=lookahead_days,
        placeholders_enabled=placeholders_enabled,
        now_date=now_date,
        date_label="Air date",
    )
    steps.append(calendar_step)

    base, file_step = _file_state_result(
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
        prior_outcome=calendar_outcome,
    )
    steps.append(file_step)

    if has_placeholder and _episode_placeholder_path_drifts(session, episode):
        base = DETERMINATION_OBSOLETE
        expected = (
            episode_placeholder_path(episode, season, series)
            if season and series
            else None
        )
        stored = getattr(episode, "placeholder_filepath", None)
        steps.append(
            _step(
                "path_drift",
                "Placeholder path",
                "fail",
                detail=f"Stored path does not match expected location ({stored!r} vs {expected!r}).",
                outcome=base,
            )
        )
    else:
        steps.append(
            _step(
                "path_drift",
                "Placeholder path",
                "skip",
                detail="No placeholder path drift detected.",
            )
        )

    series_monitored = (
        _series_monitored_for_episode(session, episode)
        if _skip_placeholders_when_series_monitored_enabled()
        else False
    )
    before = base
    base = _apply_monitored_placeholder_suppression(
        session,
        base=base,
        entity=episode,
        media_type="episode",
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
        episode_id=int(episode.id) if getattr(episode, "id", None) is not None else None,
        series_monitored=series_monitored,
    )
    monitored_skip = None
    if not _skip_placeholders_when_monitored_enabled():
        monitored_skip = "Skip-when-monitored is off."
    elif has_file or is_deleted:
        monitored_skip = "Skipped because a real file exists or the episode was removed."
    elif not bool(getattr(episode, "sonarr_monitored", False)) and not series_monitored:
        monitored_skip = "Episode and series are not monitored in Sonarr."
    steps.append(
        _apply_modifier_step(
            key="monitored_suppression",
            label="Monitored suppression",
            before=before,
            after=base,
            skip_detail=monitored_skip,
        )
    )

    before = base
    sibling_has_file = sibling_episode_has_file(session, episode)
    base = _apply_sibling_placeholder_suppression(
        arr_type="sonarr",
        base=base,
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
        sibling_has_file=sibling_has_file,
    )
    sibling_skip = None
    if not shared_placeholder_suppresses_creation("sonarr"):
        sibling_skip = "Shared-instance suppression is off."
    elif has_file or is_deleted:
        sibling_skip = "Skipped because a real file exists or the episode was removed."
    elif not sibling_has_file:
        sibling_skip = "No sibling instance has this episode on disk."
    steps.append(
        _apply_modifier_step(
            key="sibling_suppression",
            label="Shared-instance suppression",
            before=before,
            after=base,
            skip_detail=sibling_skip,
        )
    )

    sibling_block = _sibling_would_suppress_creation(
        arr_type="sonarr",
        has_file=has_file,
        is_deleted=is_deleted,
        sibling_has_file=sibling_has_file,
    )
    before = base
    base = _apply_block_placeholder(
        base=base,
        entity=episode,
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
    )
    base = _apply_force_placeholder(
        base=base,
        entity=episode,
        has_placeholder=has_placeholder,
        has_file=has_file,
        is_deleted=is_deleted,
        sibling_would_suppress=sibling_block,
    )
    steps.append(
        _placeholder_policy_step(
            entity=episode,
            before=before,
            after=base,
            has_file=has_file,
            is_deleted=is_deleted,
            sibling_would_suppress=sibling_block,
        )
    )

    final = base
    deciding = _explain_deciding_step_key(
        steps,
        final,
        episode,
        has_file=has_file,
        is_deleted=is_deleted,
    )
    ep_label = f"E{episode_number:02d}"
    series_title = str(getattr(series, "title", "") or "Series") if series else "Series"
    title = str(getattr(episode, "title", "") or f"Episode {episode_number}")
    return {
        "ok": True,
        "media_type": "episode",
        "title": f"{series_title} {ep_label} {title}".strip(),
        "determination": final,
        "deciding_step_key": deciding,
        "summary": _explain_summary(
            final,
            deciding,
            steps,
            episode,
            has_file=has_file,
            is_deleted=is_deleted,
        ),
        "steps": steps,
    }
