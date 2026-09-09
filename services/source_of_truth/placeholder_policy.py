"""Placeholder policy (auto / pinned / never) apply for library detail UI.

Series = gate (locks children without rewriting their flags).
Season = bulk stamp (writes episode flags when series is Auto).
Episode = exceptions after a season stamp, unless the series gate is active.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Literal

from sqlalchemy import func

from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Season, Series
from services.source_of_truth.arr_share_guard import (
    sibling_episode_has_file,
    sibling_movie_has_file,
)
from services.source_of_truth.determiner import (
    DETERMINATION_EXISTS,
    DETERMINATION_NEEDS,
    DETERMINATION_NOT_NEEDED,
    DETERMINATION_OBSOLETE,
    _sibling_would_suppress_creation,
)
from services.source_of_truth.materializer import (
    apply_episode_materialization,
    apply_movie_materialization,
)

PlaceholderPolicy = Literal["auto", "pinned", "never"]
PolicySource = Literal["series", "season", "episode"]

_ACTIVITY_REASON = "Placeholder policy"


def policy_from_entity(entity) -> PlaceholderPolicy:
    if bool(getattr(entity, "force_placeholder", False)):
        return "pinned"
    if bool(getattr(entity, "block_placeholder", False)):
        return "never"
    return "auto"


def apply_placeholder_policy(
    entity,
    *,
    policy: PlaceholderPolicy,
    despite_sibling: bool = False,
) -> None:
    """Mutate entity policy flags. Caller owns session commit.

    despite_sibling is ignored; multi-instance behavior follows Shared Placeholder Cleanup settings.
    """
    del despite_sibling
    pol = str(policy or "auto").strip().lower()
    if pol == "pinned":
        entity.force_placeholder = True
        entity.force_placeholder_despite_sibling = False
        entity.block_placeholder = False
    elif pol == "never":
        entity.force_placeholder = False
        entity.force_placeholder_despite_sibling = False
        entity.block_placeholder = True
    else:
        entity.force_placeholder = False
        entity.force_placeholder_despite_sibling = False
        entity.block_placeholder = False


def resolve_episode_effective_policy(
    *,
    series_policy: PlaceholderPolicy,
    episode_policy: PlaceholderPolicy,
) -> PlaceholderPolicy:
    """Sonarr-style gate: series Never vetoes all; series Pinned honors episode Never."""
    if series_policy == "never":
        return "never"
    if series_policy == "pinned":
        return "never" if episode_policy == "never" else "pinned"
    return episode_policy


def series_gate_active(series) -> bool:
    return policy_from_entity(series) != "auto"


def episode_effective_policy(
    *,
    series,
    episode,
) -> tuple[PlaceholderPolicy, PolicySource]:
    series_pol = policy_from_entity(series) if series is not None else "auto"
    episode_pol = policy_from_entity(episode)
    effective = resolve_episode_effective_policy(
        series_policy=series_pol,
        episode_policy=episode_pol,
    )
    if series_pol != "auto":
        return effective, "series"
    return effective, "episode"


def policy_flag_view(
    policy: PlaceholderPolicy,
    *,
    despite_sibling: bool = False,
) -> SimpleNamespace:
    """Synthetic entity flags for determiner/explain using an effective policy."""
    return SimpleNamespace(
        force_placeholder=(policy == "pinned"),
        block_placeholder=(policy == "never"),
        force_placeholder_despite_sibling=bool(despite_sibling) if policy == "pinned" else False,
    )


def load_series_for_episode(session, episode: Episode) -> tuple[Season | None, Series | None]:
    season = session.get(Season, int(episode.season_id)) if getattr(episode, "season_id", None) else None
    series = session.get(Series, int(season.series_id)) if season and getattr(season, "series_id", None) else None
    return season, series


def episode_effectively_pinned(session, episode: Episode) -> bool:
    _, series = load_series_for_episode(session, episode)
    effective, _ = episode_effective_policy(series=series, episode=episode)
    return effective == "pinned"


def stamp_season_policy_onto_new_episode(season: Season | None, series: Series | None, episode: Episode) -> None:
    """When a season is Never/Pinned and the series gate is open, new episodes inherit the stamp."""
    if season is None or series is None:
        return
    if series_gate_active(series):
        return
    season_pol = policy_from_entity(season)
    if season_pol == "auto":
        return
    apply_placeholder_policy(episode, policy=season_pol)


def _policy_target_determination(
    entity: Movie | Episode,
    *,
    arr_type: str,
    sibling_has_file: bool,
    policy: PlaceholderPolicy | None = None,
) -> str:
    """Map pinned/never intent to a determination without full calendar rules."""
    has_placeholder = bool(getattr(entity, "has_placeholder", False))
    has_file = bool(getattr(entity, "has_file", False))
    is_deleted = bool(getattr(entity, "is_deleted", False))
    resolved = policy if policy is not None else policy_from_entity(entity)

    if resolved == "never":
        if has_file or is_deleted:
            return DETERMINATION_NOT_NEEDED
        if has_placeholder:
            return DETERMINATION_OBSOLETE
        return DETERMINATION_NOT_NEEDED

    if resolved != "pinned":
        return DETERMINATION_NOT_NEEDED if (has_file or is_deleted or not has_placeholder) else DETERMINATION_EXISTS

    # pinned
    if has_file or is_deleted:
        return DETERMINATION_NOT_NEEDED
    sibling_block = _sibling_would_suppress_creation(
        arr_type=arr_type,
        has_file=has_file,
        is_deleted=is_deleted,
        sibling_has_file=sibling_has_file,
    )
    if sibling_block and not bool(getattr(entity, "force_placeholder_despite_sibling", False)):
        return DETERMINATION_OBSOLETE if has_placeholder else DETERMINATION_NOT_NEEDED
    if has_placeholder:
        return DETERMINATION_EXISTS
    return DETERMINATION_NEEDS


def _entity_state_snapshot(entity) -> dict[str, Any]:
    return {
        "placeholder_policy": policy_from_entity(entity),
        "force_placeholder": bool(getattr(entity, "force_placeholder", False)),
        "block_placeholder": bool(getattr(entity, "block_placeholder", False)),
        "has_file": bool(getattr(entity, "has_file", False)),
        "has_placeholder": bool(getattr(entity, "has_placeholder", False)),
    }


def apply_movie_placeholder_policy_fast(movie_id: int) -> dict[str, Any]:
    """Create/remove movie placeholder from current pinned/never flags (no Arr reconcile)."""
    session = get_session()
    try:
        movie = session.query(Movie).filter(Movie.id == int(movie_id)).first()
        if not movie:
            return {"ok": False, "message": "Movie not found", "job_id": None}
        sibling_has_file = sibling_movie_has_file(session, movie)
        target = _policy_target_determination(
            movie,
            arr_type="radarr",
            sibling_has_file=sibling_has_file,
        )
        movie.determination = target
        movie.determination_updated_at = func.now()
        session.add(movie)
        if target not in (DETERMINATION_NEEDS, DETERMINATION_OBSOLETE):
            session.commit()
            return {
                "ok": True,
                "action": "noop",
                "job_id": None,
                **_entity_state_snapshot(movie),
            }
        out = apply_movie_materialization(
            int(movie_id),
            session=session,
            activity_reason=_ACTIVITY_REASON,
        )
        if not out.get("ok", True):
            session.rollback()
            return {
                "ok": False,
                "message": str(out.get("reason") or "Placeholder update failed"),
                "job_id": None,
            }
        session.commit()
        session.refresh(movie)
        from services.source_of_truth.calendar_phase import refresh_pinned_entity_calendar_status

        refresh_pinned_entity_calendar_status(session, movie)
        session.commit()
        return {
            "ok": True,
            "action": out.get("action") or "noop",
            "path": out.get("path"),
            "job_id": None,
            **_entity_state_snapshot(movie),
        }
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        return {"ok": False, "message": str(exc), "job_id": None}
    finally:
        session.close()


def apply_episode_placeholder_policy_fast(episode_id: int) -> dict[str, Any]:
    """Create/remove episode placeholder from effective policy (series gate + episode flags)."""
    session = get_session()
    try:
        episode = session.query(Episode).filter(Episode.id == int(episode_id)).first()
        if not episode:
            return {"ok": False, "message": "Episode not found", "job_id": None}
        _, series = load_series_for_episode(session, episode)
        effective, _ = episode_effective_policy(series=series, episode=episode)
        if effective == "auto":
            session.commit()
            return {
                "ok": True,
                "action": "noop",
                "job_id": None,
                **_entity_state_snapshot(episode),
            }
        sibling_has_file = sibling_episode_has_file(session, episode)
        target = _policy_target_determination(
            episode,
            arr_type="sonarr",
            sibling_has_file=sibling_has_file,
            policy=effective,
        )
        episode.determination = target
        episode.determination_updated_at = func.now()
        session.add(episode)
        if target not in (DETERMINATION_NEEDS, DETERMINATION_OBSOLETE):
            session.commit()
            return {
                "ok": True,
                "action": "noop",
                "job_id": None,
                **_entity_state_snapshot(episode),
            }
        out = apply_episode_materialization(
            int(episode_id),
            session=session,
            activity_reason=_ACTIVITY_REASON,
        )
        if not out.get("ok", True):
            session.rollback()
            return {
                "ok": False,
                "message": str(out.get("reason") or "Placeholder update failed"),
                "job_id": None,
            }
        session.commit()
        session.refresh(episode)
        from services.source_of_truth.calendar_phase import refresh_pinned_entity_calendar_status

        refresh_pinned_entity_calendar_status(session, episode)
        session.commit()
        return {
            "ok": True,
            "action": out.get("action") or "noop",
            "path": out.get("path"),
            "job_id": None,
            **_entity_state_snapshot(episode),
        }
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        return {"ok": False, "message": str(exc), "job_id": None}
    finally:
        session.close()


def _episode_ids_for_series(session, series_id: int) -> list[int]:
    season_ids = [
        int(r[0])
        for r in session.query(Season.id)
        .filter(Season.series_id == int(series_id), Season.is_deleted == False)  # noqa: E712
        .all()
    ]
    if not season_ids:
        return []
    return [
        int(r[0])
        for r in session.query(Episode.id)
        .filter(Episode.season_id.in_(season_ids), Episode.is_deleted == False)  # noqa: E712
        .order_by(Episode.id.asc())
        .all()
    ]


def _episode_ids_for_season(session, season_id: int) -> list[int]:
    return [
        int(r[0])
        for r in session.query(Episode.id)
        .filter(Episode.season_id == int(season_id), Episode.is_deleted == False)  # noqa: E712
        .order_by(Episode.id.asc())
        .all()
    ]


def apply_series_gate_fast(series_id: int, *, policy: PlaceholderPolicy) -> dict[str, Any]:
    """Apply series Never/Pinned across episodes via effective policy (no flag rewrite on children)."""
    episode_ids: list[int] = []
    session = get_session()
    try:
        series = session.query(Series).filter(Series.id == int(series_id)).first()
        if not series:
            return {"ok": False, "message": "Series not found", "job_id": None}
        episode_ids = _episode_ids_for_series(session, int(series_id))
    finally:
        session.close()

    created = 0
    removed = 0
    errors: list[str] = []
    for eid in episode_ids:
        out = apply_episode_placeholder_policy_fast(int(eid))
        if not out.get("ok", False):
            errors.append(str(out.get("message") or f"Episode {eid} failed"))
            continue
        action = str(out.get("action") or "")
        if action in ("created", "create", "materialized"):
            created += 1
        elif action in ("removed", "delete", "deleted", "obsolete"):
            removed += 1
    if errors and created == 0 and removed == 0:
        return {
            "ok": False,
            "message": errors[0],
            "job_id": None,
            "placeholder_policy": policy,
            "episodes_touched": len(episode_ids),
        }
    return {
        "ok": True,
        "action": "series_gate",
        "job_id": None,
        "placeholder_policy": policy,
        "episodes_touched": len(episode_ids),
        "created": created,
        "removed": removed,
        "error_count": len(errors),
    }


def apply_season_stamp_fast(season_id: int, *, policy: PlaceholderPolicy) -> dict[str, Any]:
    """Stamp season Auto/Never/Pinned onto all episodes, then apply (Auto → per-episode reconcile)."""
    episode_ids: list[int] = []
    session = get_session()
    try:
        season = session.query(Season).filter(Season.id == int(season_id)).first()
        if not season:
            return {"ok": False, "message": "Season not found", "job_id": None}
        episode_ids = _episode_ids_for_season(session, int(season_id))
        for eid in episode_ids:
            ep = session.get(Episode, int(eid))
            if not ep:
                continue
            apply_placeholder_policy(ep, policy=policy)
            session.add(ep)
        session.commit()
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        return {"ok": False, "message": str(exc), "job_id": None}
    finally:
        session.close()

    if policy == "auto":
        # Same bulk wipe as Never/Pinned: episodes are now Auto and need full determination.
        return {
            "ok": True,
            "action": "season_stamp",
            "job_id": None,
            "placeholder_policy": policy,
            "episodes_touched": len(episode_ids),
            "needs_reconcile": True,
            "episode_ids": episode_ids,
        }

    created = 0
    removed = 0
    errors: list[str] = []
    for eid in episode_ids:
        out = apply_episode_placeholder_policy_fast(int(eid))
        if not out.get("ok", False):
            errors.append(str(out.get("message") or f"Episode {eid} failed"))
            continue
        action = str(out.get("action") or "")
        if action in ("created", "create", "materialized"):
            created += 1
        elif action in ("removed", "delete", "deleted", "obsolete"):
            removed += 1
    if errors and created == 0 and removed == 0:
        return {
            "ok": False,
            "message": errors[0],
            "job_id": None,
            "placeholder_policy": policy,
            "episodes_touched": len(episode_ids),
        }
    return {
        "ok": True,
        "action": "season_stamp",
        "job_id": None,
        "placeholder_policy": policy,
        "episodes_touched": len(episode_ids),
        "created": created,
        "removed": removed,
        "error_count": len(errors),
    }
