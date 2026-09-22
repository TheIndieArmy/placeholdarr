"""Orchestrate Discover seed → overlay → determine → materialize → art."""

from __future__ import annotations

from typing import Any

from core.logger import logger
from services.discover.determine import list_undetermined_tmdb_ids, run_discover_determination
from services.discover.materialize import run_discover_art_backfill, run_discover_materialization
from services.discover.mode import is_tmdb_discover_mode
from services.discover.overlay import refresh_arr_overlays
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
) -> list[int]:
    """Ids that need re-scoring: Arr overlay flips, newly seeded rows, never-scored rows."""
    scope: set[int] = set()
    for tid in (seed or {}).get("created_tmdb_ids") or []:
        try:
            scope.add(int(tid))
        except (TypeError, ValueError):
            continue
    for tid in (overlay or {}).get("changed_tmdb_ids") or []:
        try:
            scope.add(int(tid))
        except (TypeError, ValueError):
            continue
    for tid in list_undetermined_tmdb_ids():
        scope.add(int(tid))
    return sorted(scope)


def _compact_pipeline_result(out: dict[str, Any]) -> dict[str, Any]:
    """Drop large id lists before persisting task summaries."""
    compact = dict(out)
    seed = compact.get("seed")
    if isinstance(seed, dict):
        seed = dict(seed)
        seed.pop("created_tmdb_ids", None)
        compact["seed"] = seed
    overlay = compact.get("overlay")
    if isinstance(overlay, dict):
        overlay = dict(overlay)
        overlay.pop("changed_tmdb_ids", None)
        compact["overlay"] = overlay
    return compact


def run_discover_pipeline(
    *,
    source_id: int | None = None,
    ensure_default_source: bool = False,
    phase_tracker: TaskRunPhaseTracker | None = None,
    full_determination: bool = False,
) -> dict[str, Any]:
    """Discover pipeline for movies.

    Steady runs score only Arr-changed / newly seeded / undetermined titles, then
    materialize only actionable determinations (needs / obsolete / missing exists).
    Pass ``full_determination=True`` to re-score the entire catalog (repair).
    """
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

    logger.info("Discover pipeline: running determination...", extra={"emoji_type": "gear"})
    if phase_tracker is not None:
        phase_tracker.begin("determination", "Determination")
    try:
        if full_determination:
            out["determination"] = run_discover_determination()
        else:
            scope_ids = _collect_determination_scope(
                seed=out.get("seed") if isinstance(out.get("seed"), dict) else None,
                overlay=out.get("overlay") if isinstance(out.get("overlay"), dict) else None,
            )
            if scope_ids:
                out["determination"] = run_discover_determination(tmdb_ids=scope_ids)
            else:
                out["determination"] = {
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
                logger.info(
                    "Discover determination: skipped (no Arr overlay changes, new seed rows, or undetermined titles)",
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
    logger.info(f"Discover pipeline: determination done {out.get('determination')}", extra={"emoji_type": "info"})

    logger.info("Discover pipeline: materializing placeholders+NFO (no art)...", extra={"emoji_type": "gear"})
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
    logger.info(f"Discover pipeline: materialize done {out.get('materialization')}", extra={"emoji_type": "info"})

    logger.info("Discover pipeline: backfilling poster art...", extra={"emoji_type": "gear"})
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
    logger.info(
        "Discover pipeline done "
        f"seed_created={seed.get('created')} overlay_changed={(out.get('overlay') or {}).get('changed')} "
        f"det={out.get('determination')} mat={out.get('materialization')} art={out.get('art')}",
        extra={"emoji_type": "success"},
    )
    return _compact_pipeline_result(out)


def run_discover_startup() -> dict[str, Any]:
    """Post-onboarding / boot path for discover mode."""
    return run_discover_pipeline(ensure_default_source=True)
