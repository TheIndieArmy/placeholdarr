"""HTTP routes for the poster / thumb grid layout designer."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from services.poster_layouts import (
    PROFILE_KEYS,
    changed_profile_keys,
    ensure_layouts_persisted,
    get_layout_doc,
    layout_fingerprint,
    normalize_doc,
    reload_layout_cache,
    reset_profile_to_seed,
    save_layout_doc,
    serialize_layout_catalog,
)

router = APIRouter(prefix="/api/settings/poster-layouts", tags=["poster-layouts"])

_VALID_APPLY_SCOPES = {"now", "next_full_sync", "future"}
_DEFAULT_APPLY_SCOPE = "next_full_sync"


@router.get("")
async def get_poster_layouts():
    """Return layout catalog (profiles, tokens, grids) for the designer."""
    ensure_layouts_persisted()
    return JSONResponse(content={"ok": True, **serialize_layout_catalog()})


@router.put("")
async def put_poster_layouts(request: Request):
    """Persist layout profiles and optionally enqueue a full-library art refresh."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    profiles_in = body.get("profiles")
    if profiles_in is None and isinstance(body.get("doc"), dict):
        profiles_in = (body.get("doc") or {}).get("profiles")
    if not isinstance(profiles_in, dict):
        raise HTTPException(status_code=400, detail="profiles is required")

    apply_scope_raw = body.get("apply_scope", _DEFAULT_APPLY_SCOPE)
    apply_scope = (
        str(apply_scope_raw).strip().lower()
        if isinstance(apply_scope_raw, str)
        else _DEFAULT_APPLY_SCOPE
    )
    if apply_scope not in _VALID_APPLY_SCOPES:
        apply_scope = _DEFAULT_APPLY_SCOPE

    def _persist_and_apply() -> dict[str, Any]:
        prior = get_layout_doc()
        prior_fp = layout_fingerprint(prior)
        normalized = normalize_doc(
            {"version": body.get("version"), "profiles": profiles_in},
            seed_if_empty=False,
        )
        saved = save_layout_doc(normalized)
        changed_keys = changed_profile_keys(prior, saved)
        changed = bool(changed_keys) or layout_fingerprint(saved) != prior_fp

        backfill: dict[str, Any] = {"ok": True, "enqueued": False, "scope": apply_scope}
        if changed:
            from services.source_of_truth.placeholder_refresh import (
                execute_placeholder_refresh_apply_scope,
            )

            # Use settings_save* so Apply now skips the multi-minute sidecar scan
            # (same as overlay/language settings). Per-profile layout_hash still
            # skips unchanged titles when workers bake.
            out = execute_placeholder_refresh_apply_scope(
                apply_scope=apply_scope,
                metadata=False,
                art=True,
                templates=False,
                art_scope="all",
                source="settings_save:poster_layouts",
                task_run_trigger="settings_change" if apply_scope == "now" else None,
            )
            art = out.get("art_backfill") if isinstance(out.get("art_backfill"), dict) else {}
            backfill = {
                "ok": bool(out.get("ok", True)),
                "enqueued": bool(out.get("enqueued")),
                "scope": out.get("scope") or apply_scope,
                "pending": bool(out.get("pending")),
                "changed_profiles": changed_keys,
                **({} if not art else {"art": art}),
            }

        return {
            "ok": True,
            "changed": changed,
            "changed_profiles": changed_keys,
            "apply_scope": apply_scope,
            "backfill": backfill,
            **serialize_layout_catalog(),
        }

    # Sync DB + enqueue must not block the uvicorn event loop (one worker).
    payload = await asyncio.to_thread(_persist_and_apply)
    return JSONResponse(content=payload)


@router.get("/preview-art")
async def poster_layout_preview_art(
    kind: str = "movie",
    tmdb_id: int | None = None,
    random: bool = False,
):
    """Resolve designer board art from a TMDB id or a random library title.

    ``kind`` is ``movie``, ``series``/``tv``, or ``episode`` (episode uses stills).
    """
    from services.poster_layout_preview_art import resolve_from_tmdb, resolve_random_from_library

    want = str(kind or "movie").strip().lower()
    if want in {"tv", "show"}:
        want = "series"
    if random:
        result = resolve_random_from_library(kind=want)
    elif tmdb_id is not None:
        result = resolve_from_tmdb(kind=want, tmdb_id=int(tmdb_id))
    else:
        return JSONResponse(
            {"ok": False, "message": "Provide tmdb_id or set random=true"},
            status_code=400,
        )
    status = 200 if result.get("ok") else 404
    if result.get("message") and "not configured" in str(result.get("message")).lower():
        status = 400
    return JSONResponse(content=result, status_code=status)


@router.post("/reset/{profile_key}")
async def reset_poster_layout_profile(profile_key: str):
    """Reset one profile to the legacy-seeded default layout."""
    from services.poster_layouts import _LEGACY_PROFILE_KEY_MAP

    key = _LEGACY_PROFILE_KEY_MAP.get(str(profile_key or "").strip(), str(profile_key or "").strip())
    if key not in PROFILE_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown profile: {key}")
    try:
        reset_profile_to_seed(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown profile: {key}") from None
    return JSONResponse(content={"ok": True, **serialize_layout_catalog()})


@router.post("/reload")
async def reload_poster_layouts():
    """Force cache reload (tests / ops)."""
    reload_layout_cache()
    return JSONResponse(content={"ok": True, **serialize_layout_catalog()})
