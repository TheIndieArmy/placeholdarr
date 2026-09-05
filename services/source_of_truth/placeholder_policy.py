"""Placeholder policy (auto / pinned / never) preview and apply for library detail UI."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import func

from services.postgres.db import get_session
from services.postgres.models import Episode, Movie
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
    _sibling_would_suppress_creation,
)
from services.source_of_truth.force_placeholder import (
    _episode_blocking_reasons,
    _movie_blocking_reasons,
)
from services.source_of_truth.materializer import (
    apply_episode_materialization,
    apply_movie_materialization,
)

PlaceholderPolicy = Literal["auto", "pinned", "never"]

_ACTIVITY_REASON = "Placeholder policy"


def policy_from_entity(entity: Movie | Episode) -> PlaceholderPolicy:
    if bool(getattr(entity, "force_placeholder", False)):
        return "pinned"
    if bool(getattr(entity, "block_placeholder", False)):
        return "never"
    return "auto"


def apply_placeholder_policy(
    entity: Movie | Episode,
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


def _policy_target_determination(
    entity: Movie | Episode,
    *,
    arr_type: str,
    sibling_has_file: bool,
) -> str:
    """Map pinned/never intent to a determination without full calendar rules."""
    has_placeholder = bool(getattr(entity, "has_placeholder", False))
    has_file = bool(getattr(entity, "has_file", False))
    is_deleted = bool(getattr(entity, "is_deleted", False))
    policy = policy_from_entity(entity)

    if policy == "never":
        if has_file or is_deleted:
            return DETERMINATION_NOT_NEEDED
        if has_placeholder:
            return DETERMINATION_OBSOLETE
        return DETERMINATION_NOT_NEEDED

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


def _entity_state_snapshot(entity: Movie | Episode) -> dict[str, Any]:
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
    """Create/remove episode placeholder from current pinned/never flags (no Arr reconcile)."""
    session = get_session()
    try:
        episode = session.query(Episode).filter(Episode.id == int(episode_id)).first()
        if not episode:
            return {"ok": False, "message": "Episode not found", "job_id": None}
        sibling_has_file = sibling_episode_has_file(session, episode)
        target = _policy_target_determination(
            episode,
            arr_type="sonarr",
            sibling_has_file=sibling_has_file,
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


def _base_preview(
    *,
    media_type: str,
    title: str,
    entity: Movie | Episode,
    session,
    arr_type: str,
    sibling_has_file: bool,
    blocking_reasons: list[str],
) -> dict[str, Any]:
    has_file = bool(getattr(entity, "has_file", False))
    is_deleted = bool(getattr(entity, "is_deleted", False))
    has_placeholder = bool(getattr(entity, "has_placeholder", False))
    shared_on = shared_placeholder_suppresses_creation(arr_type)
    policy = policy_from_entity(entity)
    block_message = None
    if is_deleted:
        block_message = "This title was removed from the library. Pin cannot be applied."
    return {
        "ok": True,
        "media_type": media_type,
        "title": title,
        "placeholder_policy": policy,
        "force_placeholder": bool(getattr(entity, "force_placeholder", False)),
        "block_placeholder": bool(getattr(entity, "block_placeholder", False)),
        "force_placeholder_despite_sibling": bool(
            getattr(entity, "force_placeholder_despite_sibling", False)
        ),
        "can_force": not is_deleted,
        "block_message": block_message,
        "has_file": has_file,
        "has_placeholder": has_placeholder,
        "is_deleted": is_deleted,
        "blocking_reasons": blocking_reasons,
        "sibling_has_file": sibling_has_file,
        "shared_suppression_enabled": shared_on,
        "sibling_option_available": bool(not is_deleted and shared_on and sibling_has_file),
        "sibling_would_suppress": _sibling_would_suppress_creation(
            arr_type=arr_type,
            has_file=has_file,
            is_deleted=is_deleted,
            sibling_has_file=sibling_has_file,
        ),
    }


def preview_movie_placeholder_policy(session, movie: Movie) -> dict[str, Any]:
    now_date = datetime.now(timezone.utc).date()
    sibling_has_file = sibling_movie_has_file(session, movie)
    return _base_preview(
        media_type="movie",
        title=str(getattr(movie, "title", "") or "Movie"),
        entity=movie,
        session=session,
        arr_type="radarr",
        sibling_has_file=sibling_has_file,
        blocking_reasons=_movie_blocking_reasons(session, movie, now_date=now_date),
    )


def preview_episode_placeholder_policy(session, episode: Episode) -> dict[str, Any]:
    now_date = datetime.now(timezone.utc).date()
    sibling_has_file = sibling_episode_has_file(session, episode)
    title = str(getattr(episode, "title", "") or f"Episode {getattr(episode, 'episode_number', '')}")
    return _base_preview(
        media_type="episode",
        title=title,
        entity=episode,
        session=session,
        arr_type="sonarr",
        sibling_has_file=sibling_has_file,
        blocking_reasons=_episode_blocking_reasons(session, episode, now_date=now_date),
    )


