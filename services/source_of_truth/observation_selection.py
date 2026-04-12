from __future__ import annotations

from datetime import datetime, timezone

from core.config import settings
from services.media_servers.plex import get_plex_section_scan_state
from services.postgres.models import Placeholder


def _dedupe_ids(ids: list[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for value in ids:
        try:
            pid = int(value)
        except Exception:
            continue
        if pid in seen:
            continue
        seen.add(pid)
        ordered.append(pid)
    return ordered


def _created_sort_value(created_at) -> float:
    if not created_at:
        return float("inf")
    try:
        if getattr(created_at, "tzinfo", None) is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return float(created_at.timestamp())
    except Exception:
        return float("inf")


def _scan_media_priority(has_movies: bool, has_episodes: bool) -> tuple[int, int]:
    """Return priority rank tuple (movie_rank, episode_rank). Lower is better."""
    movie_rank = 0
    episode_rank = 0

    if not bool(getattr(settings, "ENABLE_PLEX", False)):
        return movie_rank, episode_rank

    section_ids: set[int] = set()
    movie_section_id = getattr(settings, "PLEX_MOVIE_SECTION_ID", None)
    tv_section_id = getattr(settings, "PLEX_TV_SECTION_ID", None)
    if has_movies and movie_section_id is not None:
        section_ids.add(int(movie_section_id))
    if has_episodes and tv_section_id is not None:
        section_ids.add(int(tv_section_id))
    if not section_ids:
        return movie_rank, episode_rank

    state = get_plex_section_scan_state(section_ids)
    if not bool(state.get("any_target_scanning", False)):
        return movie_rank, episode_rank

    scanning_ids = set(int(x) for x in (state.get("scanning_section_ids") or []) if x is not None)
    movie_scanning = movie_section_id is not None and int(movie_section_id) in scanning_ids
    tv_scanning = tv_section_id is not None and int(tv_section_id) in scanning_ids

    # If one section is actively scanning, prefer placeholders from that media type.
    if movie_scanning and not tv_scanning:
        movie_rank = 0
        episode_rank = 1
    elif tv_scanning and not movie_scanning:
        movie_rank = 1
        episode_rank = 0

    return movie_rank, episode_rank


def _scan_media_priority_state(has_movies: bool, has_episodes: bool) -> tuple[int, int, bool]:
    movie_rank = 0
    episode_rank = 0
    any_target_scanning = False

    if not bool(getattr(settings, "ENABLE_PLEX", False)):
        return movie_rank, episode_rank, any_target_scanning

    section_ids: set[int] = set()
    movie_section_id = getattr(settings, "PLEX_MOVIE_SECTION_ID", None)
    tv_section_id = getattr(settings, "PLEX_TV_SECTION_ID", None)
    if has_movies and movie_section_id is not None:
        section_ids.add(int(movie_section_id))
    if has_episodes and tv_section_id is not None:
        section_ids.add(int(tv_section_id))
    if not section_ids:
        return movie_rank, episode_rank, any_target_scanning

    state = get_plex_section_scan_state(section_ids)
    any_target_scanning = bool(state.get("any_target_scanning", False))
    if not any_target_scanning:
        return movie_rank, episode_rank, any_target_scanning

    scanning_ids = set(int(x) for x in (state.get("scanning_section_ids") or []) if x is not None)
    movie_scanning = movie_section_id is not None and int(movie_section_id) in scanning_ids
    tv_scanning = tv_section_id is not None and int(tv_section_id) in scanning_ids

    if movie_scanning and not tv_scanning:
        movie_rank = 0
        episode_rank = 1
    elif tv_scanning and not movie_scanning:
        movie_rank = 1
        episode_rank = 0

    return movie_rank, episode_rank, any_target_scanning


def _refill_newest_ratio(any_target_scanning: bool) -> float:
    attr = "HYBRID_OBSERVATION_REFILL_NEWEST_RATIO_SCANNING" if any_target_scanning else "HYBRID_OBSERVATION_REFILL_NEWEST_RATIO_IDLE"
    default = 0.7 if any_target_scanning else 0.25
    try:
        value = float(getattr(settings, attr, default) or default)
    except Exception:
        value = default
    return min(1.0, max(0.0, value))


def _row_media_rank(row: Placeholder, movie_rank: int, episode_rank: int) -> int:
    if getattr(row, "movie_id", None) is not None:
        return movie_rank
    if getattr(row, "episode_id", None) is not None:
        return episode_rank
    return max(movie_rank, episode_rank) + 1


def _order_rows_for_age_strategy(rows: list[Placeholder], *, newest_first: bool, movie_rank: int, episode_rank: int) -> list[Placeholder]:
    def _sort_key(row: Placeholder) -> tuple[int, float, str, int]:
        created_value = _created_sort_value(getattr(row, "created_at", None))
        if newest_first:
            created_value = -created_value if created_value != float("inf") else float("inf")
        return (
            _row_media_rank(row, movie_rank, episode_rank),
            created_value,
            str(getattr(row, "path", "") or "").lower(),
            int(getattr(row, "id", 0) or 0),
        )

    return sorted(rows, key=_sort_key)


def select_placeholder_ids_for_hybrid_refill(
    session,
    *,
    exclude_ids: set[int],
    limit: int,
) -> list[int]:
    ids_to_exclude = set(int(x) for x in exclude_ids if x is not None)
    if limit <= 0:
        return []

    try:
        base_query = (
            session.query(Placeholder)
            .filter(
                Placeholder.has_placeholder == True,  # noqa: E712
                Placeholder.plex_placeholder_id.is_(None),
            )
        )
        if ids_to_exclude:
            base_query = base_query.filter(~Placeholder.id.in_(sorted(ids_to_exclude)))

        pool_size = max(int(limit), int(limit) * 2)
        oldest_rows = (
            base_query.order_by(Placeholder.created_at.asc(), Placeholder.path.asc(), Placeholder.id.asc())
            .limit(pool_size)
            .all()
        )
        newest_rows = (
            base_query.order_by(Placeholder.created_at.desc(), Placeholder.path.asc(), Placeholder.id.asc())
            .limit(pool_size)
            .all()
        )
    except Exception:
        return []

    candidate_rows = oldest_rows + newest_rows
    if not candidate_rows:
        return []

    deduped_rows: dict[int, Placeholder] = {}
    for row in candidate_rows:
        row_id = getattr(row, "id", None)
        if row_id is None:
            continue
        deduped_rows[int(row_id)] = row
    all_rows = list(deduped_rows.values())

    has_movies = any(getattr(row, "movie_id", None) is not None for row in all_rows)
    has_episodes = any(getattr(row, "episode_id", None) is not None for row in all_rows)
    movie_rank, episode_rank, any_target_scanning = _scan_media_priority_state(has_movies, has_episodes)
    newest_ratio = _refill_newest_ratio(any_target_scanning)

    newest_target = int(round(limit * newest_ratio))
    newest_target = min(limit, max(0, newest_target))
    oldest_target = max(0, limit - newest_target)

    if limit > 1:
        if any_target_scanning and newest_target == 0:
            newest_target = 1
            oldest_target = max(0, limit - newest_target)
        if not any_target_scanning and oldest_target == 0:
            oldest_target = 1
            newest_target = max(0, limit - oldest_target)

    oldest_pool = _order_rows_for_age_strategy(oldest_rows, newest_first=False, movie_rank=movie_rank, episode_rank=episode_rank)
    newest_pool = _order_rows_for_age_strategy(newest_rows, newest_first=True, movie_rank=movie_rank, episode_rank=episode_rank)

    ordered_ids: list[int] = []
    seen: set[int] = set()

    def _take_from_pool(pool: list[Placeholder], target: int) -> int:
        taken = 0
        for row in pool:
            row_id = int(getattr(row, "id", 0) or 0)
            if row_id <= 0 or row_id in seen:
                continue
            seen.add(row_id)
            ordered_ids.append(row_id)
            taken += 1
            if taken >= target or len(ordered_ids) >= limit:
                break
        return taken

    if any_target_scanning:
        taken_newest = _take_from_pool(newest_pool, newest_target)
        _take_from_pool(oldest_pool, max(0, limit - taken_newest))
    else:
        taken_oldest = _take_from_pool(oldest_pool, oldest_target)
        _take_from_pool(newest_pool, max(0, limit - taken_oldest))

    if len(ordered_ids) < limit:
        for pool in (newest_pool, oldest_pool) if any_target_scanning else (oldest_pool, newest_pool):
            _take_from_pool(pool, limit - len(ordered_ids))
            if len(ordered_ids) >= limit:
                break

    return ordered_ids[:limit]


def rank_placeholder_ids_for_observation(session, placeholder_ids: list[int]) -> list[int]:
    """Return deterministic, scan-aware ordering for observation candidates.

    Ordering policy:
    1) Prefer media type for currently scanning target Plex section (if known).
    2) Older placeholders first.
    3) Stable A-Z path tie-break.
    4) ID tie-break for determinism.
    """
    ids = _dedupe_ids(placeholder_ids)
    if not ids:
        return []

    rows = (
        session.query(Placeholder)
        .filter(Placeholder.id.in_(ids))
        .all()
    )
    if not rows:
        return ids

    ids_present = {int(getattr(row, "id")) for row in rows if getattr(row, "id", None)}
    has_movies = any(getattr(row, "movie_id", None) is not None for row in rows)
    has_episodes = any(getattr(row, "episode_id", None) is not None for row in rows)
    movie_rank, episode_rank = _scan_media_priority(has_movies, has_episodes)

    def _row_sort_key(row: Placeholder) -> tuple[int, float, str, int]:
        if getattr(row, "movie_id", None) is not None:
            media_rank = movie_rank
        elif getattr(row, "episode_id", None) is not None:
            media_rank = episode_rank
        else:
            media_rank = max(movie_rank, episode_rank) + 1

        return (
            media_rank,
            _created_sort_value(getattr(row, "created_at", None)),
            str(getattr(row, "path", "") or "").lower(),
            int(getattr(row, "id", 0) or 0),
        )

    ordered_rows = sorted(rows, key=_row_sort_key)
    ordered_ids = [int(getattr(row, "id")) for row in ordered_rows if getattr(row, "id", None)]

    # Preserve original input order for ids that were not found in this query.
    missing = [pid for pid in ids if pid not in ids_present]
    ordered_ids.extend(missing)
    return _dedupe_ids(ordered_ids)
