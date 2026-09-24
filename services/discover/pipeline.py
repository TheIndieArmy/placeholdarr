"""Orchestrate Discover seed → overlay → determine → materialize → art (movies + series)."""

from __future__ import annotations

from typing import Any

from core.logger import logger
from services.discover.determine import (
    list_undetermined_series_tmdb_ids,
    list_undetermined_tmdb_ids,
    run_discover_determination,
    run_discover_series_determination,
)
from services.discover.materialize import run_discover_art_backfill, run_discover_materialization
from services.discover.materialize_series import (
    run_discover_series_art_backfill,
    run_discover_series_materialization,
)
from services.discover.mode import is_tmdb_discover_mode
from services.discover.overlay import refresh_arr_overlays, refresh_arr_series_overlays
from services.discover.seed import ensure_default_popular_source, run_all_enabled_sources, run_source
from services.task_run_phases import (
    TaskRunPhaseTracker,
    metrics_from_discover_art,
    metrics_from_discover_determination,
    metrics_from_discover_materialization,
    metrics_from_discover_overlay,
    metrics_from_discover_seed,
)


def _collect_determination_scope(
    *,
    seed: dict[str, Any] | None,
    overlay: dict[str, Any] | None,
    created_key: str = "created_tmdb_ids",
    undetermined_fn=list_undetermined_tmdb_ids,
) -> list[int]:
    scope: set[int] = set()
    for tid in (seed or {}).get(created_key) or []:
        try:
            scope.add(int(tid))
        except (TypeError, ValueError):
            continue
    for tid in (overlay or {}).get("changed_tmdb_ids") or []:
        try:
            scope.add(int(tid))
        except (TypeError, ValueError):
            continue
    for tid in undetermined_fn():
        scope.add(int(tid))
    return sorted(scope)


def _compact_pipeline_result(out: dict[str, Any]) -> dict[str, Any]:
    compact = dict(out)
    for key in ("seed", "overlay", "series_overlay"):
        block = compact.get(key)
        if isinstance(block, dict):
            block = dict(block)
            block.pop("created_tmdb_ids", None)
            block.pop("created_series_tmdb_ids", None)
            block.pop("changed_tmdb_ids", None)
            compact[key] = block
    return compact


def _empty_det_skip() -> dict[str, Any]:
    return {
        "updated": 0,
        "needs": 0,
        "exists": 0,
        "obsolete": 0,
        "not_needed": 0,
        "scoped": True,
        "scoped_count": 0,
        "skipped": True,
        "reason": "no_arr_or_seed_changes",
    }


def run_discover_pipeline(
    *,
    source_id: int | None = None,
    ensure_default_source: bool = False,
    phase_tracker: TaskRunPhaseTracker | None = None,
    full_determination: bool = False,
) -> dict[str, Any]:
    """Discover pipeline for movies and show-level TV."""
    if not is_tmdb_discover_mode():
        return {"skipped": True, "reason": "not_tmdb_discover_mode"}

    out: dict[str, Any] = {}
    if ensure_default_source:
        out["default_source_id"] = ensure_default_popular_source()

    logger.info("Discover pipeline: seeding catalog sources...", extra={"emoji_type": "gear"})
    if phase_tracker is not None:
        phase_tracker.begin("seed", "Catalog seed")
    try:
        if source_id is not None:
            out["seed"] = run_source(int(source_id))
        else:
            out["seed"] = run_all_enabled_sources()
        if phase_tracker is not None:
            phase_tracker.end("seed", metrics=metrics_from_discover_seed(out.get("seed")))
    except Exception:
        if phase_tracker is not None:
            phase_tracker.end("seed", failed=True)
        raise
    seed = out.get("seed") if isinstance(out.get("seed"), dict) else {}
    logger.info(
        "Discover pipeline: seed done "
        f"sources={seed.get('sources', 1 if source_id is not None else 0)} "
        f"fetched={seed.get('fetched')} created={seed.get('created')} "
        f"upserted={seed.get('upserted')}",
        extra={"emoji_type": "info"},
    )

    logger.info("Discover pipeline: refreshing Radarr overlays...", extra={"emoji_type": "gear"})
    if phase_tracker is not None:
        phase_tracker.begin("overlay", "Radarr overlay")
    try:
        out["overlay"] = refresh_arr_overlays()
        overlay = out["overlay"] if isinstance(out["overlay"], dict) else {}
        logger.info(
            "Discover pipeline: overlay done "
            f"instances={overlay.get('instances')} matched={overlay.get('matched')} "
            f"changed={overlay.get('changed')}",
            extra={"emoji_type": "info"},
        )
        if phase_tracker is not None:
            failed = bool(overlay.get("error"))
            phase_tracker.end(
                "overlay",
                status="failed" if failed else "done",
                failed=failed,
                metrics=metrics_from_discover_overlay(overlay),
            )
    except Exception as exc:
        logger.warning(f"Discover Arr overlay failed: {exc}", extra={"emoji_type": "warning"})
        out["overlay"] = {"error": str(exc)}
        if phase_tracker is not None:
            phase_tracker.end(
                "overlay",
                failed=True,
                metrics=metrics_from_discover_overlay(out.get("overlay")),
            )

    logger.info("Discover pipeline: refreshing Sonarr overlays...", extra={"emoji_type": "gear"})
    if phase_tracker is not None:
        phase_tracker.begin("series_overlay", "Sonarr overlay")
    try:
        out["series_overlay"] = refresh_arr_series_overlays()
        series_overlay = out["series_overlay"] if isinstance(out["series_overlay"], dict) else {}
        logger.info(
            "Discover pipeline: series overlay done "
            f"instances={series_overlay.get('instances')} matched={series_overlay.get('matched')} "
            f"changed={series_overlay.get('changed')}",
            extra={"emoji_type": "info"},
        )
        if phase_tracker is not None:
            failed = bool(series_overlay.get("error"))
            phase_tracker.end(
                "series_overlay",
                status="failed" if failed else "done",
                failed=failed,
                metrics=metrics_from_discover_overlay(series_overlay),
            )
    except Exception as exc:
        logger.warning(f"Discover Sonarr overlay failed: {exc}", extra={"emoji_type": "warning"})
        out["series_overlay"] = {"error": str(exc)}
        if phase_tracker is not None:
            phase_tracker.end(
                "series_overlay",
                failed=True,
                metrics=metrics_from_discover_overlay(out.get("series_overlay")),
            )

    logger.info("Discover pipeline: running movie determination...", extra={"emoji_type": "gear"})
    if phase_tracker is not None:
        phase_tracker.begin("determination", "Determination")
    try:
        if full_determination:
            out["determination"] = run_discover_determination()
        else:
            # Single-source runs may create movies or series; use media_type from seed.
            seed_media = str(seed.get("media_type") or "").lower()
            movie_created_key = "created_tmdb_ids"
            if source_id is not None and seed_media == "tv":
                scope_ids = []
            else:
                scope_ids = _collect_determination_scope(
                    seed=seed if source_id is None or seed_media != "tv" else None,
                    overlay=out.get("overlay") if isinstance(out.get("overlay"), dict) else None,
                    created_key=movie_created_key,
                    undetermined_fn=list_undetermined_tmdb_ids,
                )
            if scope_ids:
                out["determination"] = run_discover_determination(tmdb_ids=scope_ids)
            else:
                out["determination"] = _empty_det_skip()
                logger.info(
                    "Discover determination: skipped movies (no Arr overlay changes, new seed rows, or undetermined titles)",
                    extra={"emoji_type": "info"},
                )
        if phase_tracker is not None:
            phase_tracker.end(
                "determination",
                metrics=metrics_from_discover_determination(out.get("determination")),
            )
    except Exception:
        if phase_tracker is not None:
            phase_tracker.end("determination", failed=True)
        raise

    logger.info("Discover pipeline: running series determination...", extra={"emoji_type": "gear"})
    if phase_tracker is not None:
        phase_tracker.begin("series_determination", "Series determination")
    try:
        if full_determination:
            out["series_determination"] = run_discover_series_determination()
        else:
            seed_media = str(seed.get("media_type") or "").lower()
            if source_id is not None and seed_media == "movie":
                series_scope: list[int] = []
            else:
                series_seed = seed
                if source_id is not None and seed_media == "tv":
                    # Single TV source: created ids are under created_tmdb_ids
                    series_seed = {
                        **seed,
                        "created_series_tmdb_ids": seed.get("created_tmdb_ids") or [],
                    }
                series_scope = _collect_determination_scope(
                    seed=series_seed if source_id is None or seed_media != "movie" else None,
                    overlay=out.get("series_overlay") if isinstance(out.get("series_overlay"), dict) else None,
                    created_key="created_series_tmdb_ids" if source_id is None else "created_tmdb_ids",
                    undetermined_fn=list_undetermined_series_tmdb_ids,
                )
            if series_scope:
                out["series_determination"] = run_discover_series_determination(tmdb_ids=series_scope)
            else:
                out["series_determination"] = _empty_det_skip()
                logger.info(
                    "Discover determination: skipped series (no Arr overlay changes, new seed rows, or undetermined titles)",
                    extra={"emoji_type": "info"},
                )
        if phase_tracker is not None:
            phase_tracker.end(
                "series_determination",
                metrics=metrics_from_discover_determination(out.get("series_determination")),
            )
    except Exception:
        if phase_tracker is not None:
            phase_tracker.end("series_determination", failed=True)
        raise

    logger.info("Discover pipeline: materializing movie placeholders+NFO...", extra={"emoji_type": "gear"})
    if phase_tracker is not None:
        phase_tracker.begin("materialization", "Placeholders + NFO")
    try:
        out["materialization"] = run_discover_materialization()
        if phase_tracker is not None:
            phase_tracker.end(
                "materialization",
                metrics=metrics_from_discover_materialization(out.get("materialization")),
            )
    except Exception:
        if phase_tracker is not None:
            phase_tracker.end("materialization", failed=True)
        raise

    logger.info("Discover pipeline: materializing series stubs...", extra={"emoji_type": "gear"})
    if phase_tracker is not None:
        phase_tracker.begin("series_materialization", "Series stubs")
    try:
        out["series_materialization"] = run_discover_series_materialization()
        if phase_tracker is not None:
            phase_tracker.end(
                "series_materialization",
                metrics=metrics_from_discover_materialization(out.get("series_materialization")),
            )
    except Exception:
        if phase_tracker is not None:
            phase_tracker.end("series_materialization", failed=True)
        raise

    logger.info("Discover pipeline: backfilling movie poster art...", extra={"emoji_type": "gear"})
    if phase_tracker is not None:
        phase_tracker.begin("art", "Poster art")
    try:
        out["art"] = run_discover_art_backfill(phase_tracker=phase_tracker)
        if phase_tracker is not None:
            phase_tracker.end("art", metrics=metrics_from_discover_art(out.get("art")))
    except Exception:
        if phase_tracker is not None:
            phase_tracker.end("art", failed=True)
        raise

    logger.info("Discover pipeline: backfilling series poster art...", extra={"emoji_type": "gear"})
    if phase_tracker is not None:
        phase_tracker.begin("series_art", "Series poster art")
    try:
        out["series_art"] = run_discover_series_art_backfill()
        if phase_tracker is not None:
            phase_tracker.end("series_art", metrics=metrics_from_discover_art(out.get("series_art")))
    except Exception:
        if phase_tracker is not None:
            phase_tracker.end("series_art", failed=True)
        raise

    logger.info(
        "Discover pipeline done "
        f"seed_created={seed.get('created')} "
        f"overlay_changed={(out.get('overlay') or {}).get('changed')} "
        f"series_overlay_changed={(out.get('series_overlay') or {}).get('changed')} "
        f"det={out.get('determination')} series_det={out.get('series_determination')} "
        f"mat={out.get('materialization')} series_mat={out.get('series_materialization')}",
        extra={"emoji_type": "success"},
    )
    return _compact_pipeline_result(out)


def run_discover_startup() -> dict[str, Any]:
    """Post-onboarding / boot path for discover mode."""
    return run_discover_pipeline(ensure_default_source=True)
