"""Apply Never/Pinned placeholder policy from Arr tags on movies and series."""

from __future__ import annotations

import json
from typing import Any, Iterable

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import Movie, Series
from services.source_of_truth.placeholder_policy import (
    PlaceholderPolicy,
    apply_placeholder_policy,
    apply_movie_placeholder_policy_fast,
    apply_series_gate_fast,
    policy_from_entity,
)

_DEFAULT_NEVER = ["placeholdarr-never"]
_DEFAULT_PINNED = ["placeholdarr-pinned"]


def _normalize_label_list(raw: Any, *, defaults: list[str]) -> list[str]:
    if raw is None:
        items = list(defaults)
    elif isinstance(raw, list):
        items = [str(x) for x in raw]
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            items = list(defaults)
        else:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    items = [str(x) for x in parsed]
                else:
                    items = [p.strip() for p in text.split(",") if p.strip()]
            except Exception:
                items = [p.strip() for p in text.split(",") if p.strip()]
    else:
        items = list(defaults)

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        label = str(item or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


def never_tag_labels() -> list[str]:
    return _normalize_label_list(
        getattr(settings, "PLACEHOLDER_POLICY_NEVER_TAGS", None),
        defaults=_DEFAULT_NEVER,
    )


def pinned_tag_labels() -> list[str]:
    return _normalize_label_list(
        getattr(settings, "PLACEHOLDER_POLICY_PINNED_TAGS", None),
        defaults=_DEFAULT_PINNED,
    )


def _tag_ids_from_payload(payload: Any) -> set[int]:
    if not isinstance(payload, dict):
        return set()
    raw = payload.get("tags")
    out: set[int] = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        try:
            out.add(int(item))
        except Exception:
            continue
    return out


def _label_to_ids_for_instance(instance_key: str, arr_type: str) -> dict[str, set[int]]:
    from services.list_sources import ListSourceError, fetch_arr_tags

    try:
        rows = fetch_arr_tags(instance_key, arr_type)
    except ListSourceError as exc:
        logger.debug(
            "Arr tag fetch skipped instance=%s type=%s: %s",
            instance_key,
            arr_type,
            exc,
            extra={"emoji_type": "debug"},
        )
        return {}
    except Exception as exc:
        logger.warning(
            "Arr tag fetch failed instance=%s type=%s: %s",
            instance_key,
            arr_type,
            exc,
            extra={"emoji_type": "warning"},
        )
        return {}

    out: dict[str, set[int]] = {}
    for row in rows:
        try:
            tag_id = int(row.get("id"))
        except Exception:
            continue
        label = str(row.get("label") or "").strip().lower()
        if not label:
            continue
        out.setdefault(label, set()).add(tag_id)
    return out


def _desired_policy_from_tags(
    tag_ids: set[int],
    *,
    label_map: dict[str, set[int]],
    never_labels: list[str],
    pinned_labels: list[str],
) -> PlaceholderPolicy:
    never_ids: set[int] = set()
    for label in never_labels:
        never_ids |= label_map.get(label.lower(), set())
    pinned_ids: set[int] = set()
    for label in pinned_labels:
        pinned_ids |= label_map.get(label.lower(), set())

    if tag_ids & never_ids:
        return "never"
    if tag_ids & pinned_ids:
        return "pinned"
    return "auto"


def _source_is_manual(entity) -> bool:
    return str(getattr(entity, "placeholder_policy_source", None) or "").strip().lower() == "manual"


def resolve_policy_tag_control(
    *,
    instance_key: str | None,
    arr_type: str,
    payload: Any,
    policy_source: str | None = None,
) -> dict[str, Any]:
    """Describe which configured Never/Pinned tags match a title payload."""
    never_labels = never_tag_labels()
    pinned_labels = pinned_tag_labels()
    tag_ids = _tag_ids_from_payload(payload)
    label_map = _label_to_ids_for_instance(str(instance_key or "").strip().lower(), arr_type) if instance_key else {}

    matching_never: list[str] = []
    matching_pinned: list[str] = []
    never_ids: set[int] = set()
    pinned_ids: set[int] = set()
    for label in never_labels:
        ids = label_map.get(label.lower(), set())
        if tag_ids & ids:
            matching_never.append(label)
            never_ids |= ids & tag_ids
    for label in pinned_labels:
        ids = label_map.get(label.lower(), set())
        if tag_ids & ids:
            matching_pinned.append(label)
            pinned_ids |= ids & tag_ids

    matching_never.sort(key=lambda s: s.lower())
    matching_pinned.sort(key=lambda s: s.lower())
    conflict = bool(matching_never and matching_pinned)
    desired = _desired_policy_from_tags(
        tag_ids,
        label_map=label_map,
        never_labels=never_labels,
        pinned_labels=pinned_labels,
    )
    src = str(policy_source or "").strip().lower() or None
    return {
        "source": src,
        "matching_never_tags": matching_never,
        "matching_pinned_tags": matching_pinned,
        "matching_never_tag_ids": sorted(never_ids),
        "matching_pinned_tag_ids": sorted(pinned_ids),
        "conflict": conflict,
        "desired_from_tags": desired,
        # Matching Never/Pinned tags always control policy (including over library chips).
        "controlled_by_tag": desired in ("never", "pinned"),
    }


def clear_arr_policy_tags_for_movie(
    movie_id: int,
    *,
    remove_never: bool = False,
    remove_pinned: bool = False,
) -> dict[str, Any]:
    """Remove matching Never/Pinned tags from Radarr and re-apply local policy."""
    from services.source_of_truth.arr_api import set_radarr_movie_tags

    if not remove_never and not remove_pinned:
        return {"ok": False, "message": "Select at least one tag group to clear."}

    session = get_session()
    try:
        movie = session.query(Movie).filter(Movie.id == int(movie_id), Movie.is_deleted == False).first()  # noqa: E712
        if not movie:
            return {"ok": False, "message": "Movie not found"}
        instance_key = str(getattr(movie, "instance_key", "") or "").strip().lower()
        radarr_id = getattr(movie, "radarrid", None)
        if not instance_key or radarr_id is None:
            return {"ok": False, "message": "Movie is missing Radarr identity"}

        control = resolve_policy_tag_control(
            instance_key=instance_key,
            arr_type="radarr",
            payload=getattr(movie, "radarr_payload_raw", None),
            policy_source=getattr(movie, "placeholder_policy_source", None),
        )
        remove_ids: set[int] = set()
        if remove_never:
            remove_ids |= set(control.get("matching_never_tag_ids") or [])
        if remove_pinned:
            remove_ids |= set(control.get("matching_pinned_tag_ids") or [])
        if not remove_ids:
            return {"ok": False, "message": "No matching policy tags to clear on this title."}

        current_ids = _tag_ids_from_payload(getattr(movie, "radarr_payload_raw", None))
        next_ids = sorted(current_ids - remove_ids)

        resolved = settings.resolve_arr_instance("radarr", instance_key=instance_key) or {}
        url = str(resolved.get("url") or "").strip()
        api_key = str(resolved.get("api_key") or "").strip()
        if not url or not api_key:
            return {"ok": False, "message": f"Radarr instance {instance_key!r} is not configured"}

        ok = set_radarr_movie_tags(int(radarr_id), next_ids, url=url, api_key=api_key)
        if not ok:
            return {"ok": False, "message": "Radarr rejected the tag update"}

        payload = getattr(movie, "radarr_payload_raw", None)
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["tags"] = next_ids
            movie.radarr_payload_raw = payload
        session.add(movie)
        session.commit()
        row_id = int(movie.id)
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        return {"ok": False, "message": str(exc)}
    finally:
        session.close()

    apply_out = apply_arr_tag_placeholder_policies(movie_row_ids=[row_id], series_row_ids=[])
    return {
        "ok": True,
        "removed_tag_ids": sorted(remove_ids),
        "remaining_tag_ids": next_ids,
        "tag_policies": apply_out,
    }


def clear_arr_policy_tags_for_series(
    series_id: int,
    *,
    remove_never: bool = False,
    remove_pinned: bool = False,
) -> dict[str, Any]:
    """Remove matching Never/Pinned tags from Sonarr and re-apply local policy."""
    from services.source_of_truth.arr_api import set_sonarr_series_tags

    if not remove_never and not remove_pinned:
        return {"ok": False, "message": "Select at least one tag group to clear."}

    session = get_session()
    try:
        series = session.query(Series).filter(Series.id == int(series_id), Series.is_deleted == False).first()  # noqa: E712
        if not series:
            return {"ok": False, "message": "Series not found"}
        instance_key = str(getattr(series, "instance_key", "") or "").strip().lower()
        sonarr_id = getattr(series, "sonarrid", None)
        if not instance_key or sonarr_id is None:
            return {"ok": False, "message": "Series is missing Sonarr identity"}

        control = resolve_policy_tag_control(
            instance_key=instance_key,
            arr_type="sonarr",
            payload=getattr(series, "sonarr_payload_raw", None),
            policy_source=getattr(series, "placeholder_policy_source", None),
        )
        remove_ids: set[int] = set()
        if remove_never:
            remove_ids |= set(control.get("matching_never_tag_ids") or [])
        if remove_pinned:
            remove_ids |= set(control.get("matching_pinned_tag_ids") or [])
        if not remove_ids:
            return {"ok": False, "message": "No matching policy tags to clear on this title."}

        current_ids = _tag_ids_from_payload(getattr(series, "sonarr_payload_raw", None))
        next_ids = sorted(current_ids - remove_ids)

        resolved = settings.resolve_arr_instance("sonarr", instance_key=instance_key) or {}
        url = str(resolved.get("url") or "").strip()
        api_key = str(resolved.get("api_key") or "").strip()
        if not url or not api_key:
            return {"ok": False, "message": f"Sonarr instance {instance_key!r} is not configured"}

        ok = set_sonarr_series_tags(int(sonarr_id), next_ids, url=url, api_key=api_key)
        if not ok:
            return {"ok": False, "message": "Sonarr rejected the tag update"}

        payload = getattr(series, "sonarr_payload_raw", None)
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["tags"] = next_ids
            series.sonarr_payload_raw = payload
        session.add(series)
        session.commit()
        row_id = int(series.id)
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        return {"ok": False, "message": str(exc)}
    finally:
        session.close()

    apply_out = apply_arr_tag_placeholder_policies(movie_row_ids=[], series_row_ids=[row_id])
    return {
        "ok": True,
        "removed_tag_ids": sorted(remove_ids),
        "remaining_tag_ids": next_ids,
        "tag_policies": apply_out,
    }


def _entity_needs_tag_policy_apply(entity, desired: PlaceholderPolicy) -> bool:
    """True when tag applicator would change flags/source.

    Matching Never/Pinned tags always win (including over ``source=manual``).
    When no policy tags match, leave manual chip settings alone; only unwind prior
    ``source=tag`` control back toward Auto.
    """
    current = policy_from_entity(entity)
    src = str(getattr(entity, "placeholder_policy_source", None) or "").strip().lower()
    if desired in ("never", "pinned"):
        return not (desired == current and src == "tag")
    # desired == auto: do not clobber a library chip; only unwind prior tag Never/Pinned.
    if _source_is_manual(entity):
        return False
    if src == "tag":
        return current != "auto"
    if desired == current == "auto" and not src:
        return False
    return current != "auto"


def collect_tag_policy_drift_row_ids(
    *,
    instance_keys: Iterable[str] | None = None,
) -> tuple[list[int], list[int]]:
    """Find movie/series rows whose Arr tags disagree with current policy.

    Covers races where catalog tags land via entity_reconcile (or another sync) without
    going through the tag applicator, so lite/full ``touched`` sets miss them.
    """
    never_labels = never_tag_labels()
    pinned_labels = pinned_tag_labels()
    if not never_labels and not pinned_labels:
        return [], []

    keys = {
        str(k or "").strip().lower()
        for k in (instance_keys or [])
        if str(k or "").strip()
    }

    label_maps: dict[tuple[str, str], dict[str, set[int]]] = {}
    movie_out: list[int] = []
    series_out: list[int] = []

    session = get_session()
    try:
        movie_q = session.query(Movie).filter(Movie.is_deleted == False)  # noqa: E712
        series_q = session.query(Series).filter(Series.is_deleted == False)  # noqa: E712
        if keys:
            movie_q = movie_q.filter(Movie.instance_key.in_(keys))
            series_q = series_q.filter(Series.instance_key.in_(keys))

        for movie in movie_q.all():
            instance_key = str(getattr(movie, "instance_key", "") or "").strip().lower()
            if not instance_key:
                continue
            map_key = (instance_key, "radarr")
            if map_key not in label_maps:
                label_maps[map_key] = _label_to_ids_for_instance(instance_key, "radarr")
            desired = _desired_policy_from_tags(
                _tag_ids_from_payload(getattr(movie, "radarr_payload_raw", None)),
                label_map=label_maps[map_key],
                never_labels=never_labels,
                pinned_labels=pinned_labels,
            )
            if _entity_needs_tag_policy_apply(movie, desired):
                movie_out.append(int(movie.id))

        for series in series_q.all():
            instance_key = str(getattr(series, "instance_key", "") or "").strip().lower()
            if not instance_key:
                continue
            map_key = (instance_key, "sonarr")
            if map_key not in label_maps:
                label_maps[map_key] = _label_to_ids_for_instance(instance_key, "sonarr")
            desired = _desired_policy_from_tags(
                _tag_ids_from_payload(getattr(series, "sonarr_payload_raw", None)),
                label_map=label_maps[map_key],
                never_labels=never_labels,
                pinned_labels=pinned_labels,
            )
            if _entity_needs_tag_policy_apply(series, desired):
                series_out.append(int(series.id))
    finally:
        session.close()

    return sorted(set(movie_out)), sorted(set(series_out))


def _counts_from_materialize_action(action: Any) -> tuple[int, int]:
    """Map materializer / policy-fast action strings to (created, deleted)."""
    a = str(action or "").strip().lower()
    if a in {"created_or_exists", "created", "create", "materialized"}:
        return 1, 0
    if a in {"deleted_or_absent", "deleted", "removed", "delete", "obsolete"}:
        return 0, 1
    return 0, 0


def _add_materialization_counts(into: dict[str, int], src: Any) -> None:
    if not isinstance(src, dict):
        return
    into["created"] += int(src.get("created") or 0)
    into["deleted"] += int(src.get("deleted") or src.get("removed") or 0)
    into["files_created"] += int(src.get("files_created") or 0)
    into["files_deleted"] += int(src.get("files_deleted") or 0)
    into["errors"] += int(src.get("errors") or src.get("error_count") or 0)
    into["noop"] += int(src.get("noop") or 0)


def merge_tag_policy_materialization_into(
    materialization: dict[str, Any] | None,
    tag_policies: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fold Arr tag-policy create/delete counts into lite/full materialization task stats."""
    mat = dict(materialization or {})
    for key in ("created", "deleted", "files_created", "files_deleted", "errors", "noop"):
        mat[key] = int(mat.get(key) or 0)
    if isinstance(tag_policies, dict):
        _add_materialization_counts(mat, tag_policies)
        nested = tag_policies.get("materialization")
        if isinstance(nested, dict):
            _add_materialization_counts(mat, nested)
    return mat


def sum_tag_policy_counts_from_arr_instance_stats(sync_stats: Any) -> dict[str, int]:
    """Sum tag_policies create/delete across per-instance full-sync results."""
    totals = {
        "created": 0,
        "deleted": 0,
        "files_created": 0,
        "files_deleted": 0,
        "errors": 0,
        "noop": 0,
    }
    if not isinstance(sync_stats, dict):
        return totals
    for value in sync_stats.values():
        if not isinstance(value, dict):
            continue
        tp = value.get("tag_policies")
        if isinstance(tp, dict):
            _add_materialization_counts(totals, tp)
    return totals


def apply_arr_tag_placeholder_policies(
    *,
    movie_row_ids: Iterable[int] | None = None,
    series_row_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Apply tag-driven Never/Pinned to movie/series rows.

    Matching Never/Pinned tags always win (including over ``source=manual``).
    When no policy tags match, manual chip settings are left alone; prior
    ``source=tag`` Never/Pinned is unwound to Auto.
    """
    never_labels = never_tag_labels()
    pinned_labels = pinned_tag_labels()
    if not never_labels and not pinned_labels:
        return {
            "ok": True,
            "skipped": True,
            "reason": "no_tag_lists",
            "movies_changed": 0,
            "series_changed": 0,
            "created": 0,
            "deleted": 0,
        }

    movie_ids = sorted({int(x) for x in (movie_row_ids or []) if x is not None})
    series_ids = sorted({int(x) for x in (series_row_ids or []) if x is not None})
    if not movie_ids and not series_ids:
        return {
            "ok": True,
            "movies_changed": 0,
            "series_changed": 0,
            "skipped": True,
            "reason": "no_rows",
            "created": 0,
            "deleted": 0,
        }

    label_maps: dict[tuple[str, str], dict[str, set[int]]] = {}
    movies_changed = 0
    series_changed = 0
    movies_materialize: list[tuple[int, PlaceholderPolicy]] = []
    series_materialize: list[tuple[int, PlaceholderPolicy]] = []

    session = get_session()
    try:
        if movie_ids:
            movies = (
                session.query(Movie)
                .filter(Movie.id.in_(movie_ids), Movie.is_deleted == False)  # noqa: E712
                .all()
            )
            for movie in movies:
                instance_key = str(getattr(movie, "instance_key", "") or "").strip().lower()
                if not instance_key:
                    continue
                map_key = (instance_key, "radarr")
                if map_key not in label_maps:
                    label_maps[map_key] = _label_to_ids_for_instance(instance_key, "radarr")
                desired = _desired_policy_from_tags(
                    _tag_ids_from_payload(getattr(movie, "radarr_payload_raw", None)),
                    label_map=label_maps[map_key],
                    never_labels=never_labels,
                    pinned_labels=pinned_labels,
                )
                if not _entity_needs_tag_policy_apply(movie, desired):
                    continue
                apply_placeholder_policy(movie, policy=desired, source="tag")
                session.add(movie)
                movies_changed += 1
                movies_materialize.append((int(movie.id), desired))

        if series_ids:
            series_rows = (
                session.query(Series)
                .filter(Series.id.in_(series_ids), Series.is_deleted == False)  # noqa: E712
                .all()
            )
            for series in series_rows:
                instance_key = str(getattr(series, "instance_key", "") or "").strip().lower()
                if not instance_key:
                    continue
                map_key = (instance_key, "sonarr")
                if map_key not in label_maps:
                    label_maps[map_key] = _label_to_ids_for_instance(instance_key, "sonarr")
                desired = _desired_policy_from_tags(
                    _tag_ids_from_payload(getattr(series, "sonarr_payload_raw", None)),
                    label_map=label_maps[map_key],
                    never_labels=never_labels,
                    pinned_labels=pinned_labels,
                )
                if not _entity_needs_tag_policy_apply(series, desired):
                    continue
                apply_placeholder_policy(series, policy=desired, source="tag")
                session.add(series)
                series_changed += 1
                series_materialize.append((int(series.id), desired))

        if movies_changed or series_changed:
            session.commit()
        else:
            session.commit()
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning(
            f"Arr tag placeholder policy apply failed: {exc}",
            extra={"emoji_type": "warning"},
            exc_info=True,
        )
        return {
            "ok": False,
            "error": str(exc),
            "movies_changed": movies_changed,
            "series_changed": series_changed,
        }
    finally:
        session.close()

    followup_counts = {
        "created": 0,
        "deleted": 0,
        "files_created": 0,
        "files_deleted": 0,
        "errors": 0,
        "noop": 0,
    }

    for movie_id, policy in movies_materialize:
        try:
            if policy == "auto":
                from services.source_of_truth.determiner import run_determination_for_entities
                from services.source_of_truth.materializer import run_materialization_for_entities

                # Sync determine + materialize so clearing Never/Pinned does not wait on workers.
                run_determination_for_entities(movie_ids=[int(movie_id)], episode_ids=[])
                mat_out = run_materialization_for_entities(
                    movie_ids=[int(movie_id)],
                    episode_ids=[],
                    observation_source=f"tag_policy_movie:{movie_id}",
                )
                _add_materialization_counts(followup_counts, mat_out)
            else:
                fast_out = apply_movie_placeholder_policy_fast(int(movie_id))
                created_n, deleted_n = _counts_from_materialize_action(fast_out.get("action"))
                followup_counts["created"] += created_n
                followup_counts["deleted"] += deleted_n
                if not fast_out.get("ok", True):
                    followup_counts["errors"] += 1
        except Exception as exc:
            followup_counts["errors"] += 1
            logger.warning(
                f"Tag policy movie follow-up failed id={movie_id}: {exc}",
                extra={"emoji_type": "warning"},
            )

    for series_id, policy in series_materialize:
        try:
            if policy == "auto":
                from services.postgres.models import Episode, Season
                from services.source_of_truth.determiner import run_determination_for_entities
                from services.source_of_truth.materializer import run_materialization_for_entities

                ep_session = get_session()
                try:
                    episode_ids = [
                        int(r[0])
                        for r in ep_session.query(Episode.id)
                        .join(Season, Episode.season_id == Season.id)
                        .filter(Season.series_id == int(series_id), Episode.is_deleted == False)  # noqa: E712
                        .all()
                        if r[0] is not None
                    ]
                finally:
                    ep_session.close()
                run_determination_for_entities(movie_ids=[], episode_ids=episode_ids)
                mat_out = run_materialization_for_entities(
                    movie_ids=[],
                    episode_ids=episode_ids,
                    observation_source=f"tag_policy_series:{series_id}",
                )
                _add_materialization_counts(followup_counts, mat_out)
            else:
                gate_out = apply_series_gate_fast(int(series_id), policy=policy)
                _add_materialization_counts(followup_counts, gate_out)
                if not gate_out.get("ok", True):
                    followup_counts["errors"] += 1
        except Exception as exc:
            followup_counts["errors"] += 1
            logger.warning(
                f"Tag policy series follow-up failed id={series_id}: {exc}",
                extra={"emoji_type": "warning"},
            )

    if movies_changed or series_changed:
        logger.info(
            "Arr tag placeholder policies applied movies_changed=%s series_changed=%s "
            "placeholders_created=%s placeholders_deleted=%s",
            movies_changed,
            series_changed,
            followup_counts["created"],
            followup_counts["deleted"],
            extra={"emoji_type": "info"},
        )

    return {
        "ok": True,
        "movies_changed": movies_changed,
        "series_changed": series_changed,
        "movies_followups": len(movies_materialize),
        "series_followups": len(series_materialize),
        "created": followup_counts["created"],
        "deleted": followup_counts["deleted"],
        "files_created": followup_counts["files_created"],
        "files_deleted": followup_counts["files_deleted"],
        "errors": followup_counts["errors"],
        "noop": followup_counts["noop"],
    }
