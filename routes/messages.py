"""HTTP routes for the customizable status message templates feature."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from core.config import settings
from core.logger import logger
from services.messages import (
    DEFAULT_SEPARATOR,
    DEFAULT_WRAPPER_PRESET,
    InvalidTemplateError,
    UnknownTemplateKeyError,
    WRAPPER_PRESETS,
    apply_wrapper,
    get_overrides,
    get_template_config,
    get_token_specs,
    render,
    render_template,
    sample_render,
    save_template_config,
    validate_template_text,
)
from services.messages.context import sample_projection_context
from services.messages.registry import CASE_OPTIONS, SEPARATOR_PRESETS, MessageKey, get_message_key, get_registry_for_settings_ui
from services.messages.template_engine import MAX_TEMPLATE_LENGTH
from services.status_projection import projection_surfaces


_VALID_APPLY_SCOPES = {"now", "next_full_sync", "future"}
_DEFAULT_APPLY_SCOPE = "next_full_sync"


def _preview_placeholder_status_updates_scope(body: dict[str, Any]) -> str:
    raw = body.get("placeholder_status_updates")
    if isinstance(raw, str):
        u = raw.strip().upper()
        if u in ("OFF", "REQUEST", "ALL"):
            return u
    return str(getattr(settings, "PLACEHOLDER_STATUS_UPDATES", "ALL") or "ALL").strip().upper()


def _preview_projection_surfaces(body: dict[str, Any]) -> tuple[bool, bool]:
    mode = body.get("projection_mode")
    if isinstance(mode, str):
        value = mode.strip().lower()
        if value == "both":
            return (True, True)
        if value == "title":
            return (True, False)
        if value == "summary":
            return (False, True)
    return projection_surfaces()


def _empty_tokens_for_template(template_src: str, ctx: dict[str, Any]) -> list[str]:
    used = re.findall(r"\{([A-Za-z][A-Za-z0-9_]*)\}", str(template_src or ""))
    return [t for t in used if t != "Sep" and not str(ctx.get(t, "")).strip()]


router = APIRouter(prefix="/api/messages", tags=["messages"])


def _serialize_token(token) -> dict[str, Any]:
    return {
        "name": token.name,
        "label": token.label,
        "group": token.group,
        "description": token.description,
        "sample": token.sample,
        "placeholder": token.placeholder,
    }


def _muted_under_request_scope(msg: MessageKey) -> bool:
    """Dim in UI when Placeholder status updates = Request only (non-REQUEST templates inactive)."""
    g = str(msg.group or "")
    if msg.key == "line.request" or g in ("Request", "Title Suffix"):
        return False
    return bool(
        g.startswith("Calendar")
        or "Coming Soon" in g
        or g.startswith("Queue")
        or g.startswith("Import Grace")
    )


def _serialize_message(msg) -> dict[str, Any]:
    """Build the API row for a single registered message key, with current value and sample render."""
    config = get_template_config()
    overrides = config["overrides"]
    override = overrides.get(msg.key)
    has_override = isinstance(override, str) and override.strip() != ""

    try:
        sample = sample_render(msg.key)
    except Exception as exc:
        logger.warning(
            f"Sample render failed key={msg.key}: {exc}",
            extra={"emoji_type": "warning"},
        )
        sample = ""

    return {
        "key": msg.key,
        "label": msg.label,
        "group": msg.group,
        "subgroup": msg.subgroup,
        "tooltip": msg.tooltip,
        "default": msg.default,
        "value": override if has_override else msg.default,
        "has_override": has_override,
        "allowed_tokens": list(msg.allowed_tokens),
        "sample_render": sample,
        "muted_under_request_scope": _muted_under_request_scope(msg),
    }


@router.get("/templates")
def get_templates() -> JSONResponse:
    config = get_template_config()
    registry = [_serialize_message(m) for m in get_registry_for_settings_ui()]
    tokens = [_serialize_token(t) for t in get_token_specs()]
    raw_ov = config.get("overrides") or {}
    overrides_ui: dict[str, str] = {}
    if isinstance(raw_ov, dict):
        for k, v in raw_ov.items():
            if not isinstance(k, str):
                continue
            spec = get_message_key(k)
            if spec is not None and spec.settings_ui:
                overrides_ui[k] = v

    pending_backfill = bool(_get_pending_backfill_flag())

    return JSONResponse(
        {
            "registry": registry,
            "tokens": tokens,
            "separator": config.get("separator", DEFAULT_SEPARATOR),
            "case": config.get("case", "default"),
            "wrapper_preset": config.get("wrapper_preset", DEFAULT_WRAPPER_PRESET),
            "wrapper_open": config.get("wrapper_open", ""),
            "wrapper_close": config.get("wrapper_close", ""),
            "overrides": overrides_ui,
            "separator_presets": list(SEPARATOR_PRESETS),
            "case_options": list(CASE_OPTIONS),
            "wrapper_presets": list(WRAPPER_PRESETS),
            "max_template_length": MAX_TEMPLATE_LENGTH,
            "pending_full_sync_backfill": pending_backfill,
        }
    )


@router.post("/preview")
def post_message_preview(body: dict[str, Any]) -> JSONResponse:
    """Live preview for REQUEST lines; honors draft projection toggles and status-update scope."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    media_raw = str(body.get("media") or "movie").strip().lower()
    is_episode = media_raw in ("episode", "tv", "show")
    media_key = "episode" if is_episode else "movie"
    ctx = sample_projection_context(media_key)
    overrides = dict(get_overrides() or {})

    spec_syn = get_message_key("line.request")
    if spec_syn is None:
        raise HTTPException(status_code=500, detail="missing line.request registry")

    template_syn = body.get("template_synopsis")
    if template_syn is None:
        template_syn = body.get("template")
    tmpl_src_syn: str
    if isinstance(template_syn, str) and template_syn.strip():
        syn_inner = render_template("line.request", template_syn.strip(), ctx)
        tmpl_src_syn = template_syn.strip()
    else:
        syn_inner = render("line.request", ctx)
        o = overrides.get("line.request")
        tmpl_src_syn = o if isinstance(o, str) and o.strip() else spec_syn.default

    syn_bracket = apply_wrapper(syn_inner)

    scope = _preview_placeholder_status_updates_scope(body)
    title_on, summary_on = _preview_projection_surfaces(body)
    demo_status = str(body.get("demo_status") or "REQUEST").strip().upper()
    request_demo = demo_status == "REQUEST"
    projection_allowed = scope != "OFF" and (scope == "ALL" or (scope == "REQUEST" and request_demo))

    sample_title = "Cat's in the Bag..." if is_episode else "Inception"
    sample_plot = "Sample library overview text for preview."

    title_line_final = sample_title
    summary_line_final = sample_plot

    if projection_allowed and title_on:
        suffix_key = "title.suffix.episode" if is_episode else "title.suffix.movie"
        title_suffix = render(suffix_key, ctx)
        if title_suffix.strip():
            normalized_suffix = title_suffix if title_suffix[:1].isspace() else f" {title_suffix}"
            title_line_final = f"{sample_title}{normalized_suffix}".strip()
        else:
            title_line_final = sample_title
    if projection_allowed and summary_on:
        summary_line_final = f"{syn_bracket} {sample_plot}".strip()

    empty_syn = _empty_tokens_for_template(tmpl_src_syn, ctx)
    return JSONResponse(
        {
            "media": media_key,
            "inner_synopsis": syn_inner,
            "bracket_synopsis": syn_bracket,
            "title_line": title_line_final,
            "summary_line": summary_line_final,
            "projection_title_enabled": bool(title_on),
            "projection_summary_enabled": bool(summary_on),
            "placeholder_status_updates": scope,
            "projection_blocked": not projection_allowed,
            "empty_tokens": empty_syn,
        }
    )


@router.post("/templates")
def post_templates(body: dict[str, Any]) -> JSONResponse:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    overrides_in = body.get("overrides")
    if overrides_in is None:
        overrides_in = {}
    if not isinstance(overrides_in, dict):
        raise HTTPException(status_code=400, detail="overrides must be an object")

    sep = body.get("separator", DEFAULT_SEPARATOR)
    if sep is None or not isinstance(sep, str) or sep == "":
        sep = DEFAULT_SEPARATOR

    case = body.get("case", "default")
    if not isinstance(case, str):
        case = "default"
    if case not in {opt["value"] for opt in CASE_OPTIONS}:
        case = "default"

    wrapper_preset = body.get("wrapper_preset", DEFAULT_WRAPPER_PRESET)
    wrapper_preset_values = {entry["value"] for entry in WRAPPER_PRESETS}
    if not isinstance(wrapper_preset, str) or wrapper_preset.strip().lower() not in wrapper_preset_values:
        wrapper_preset = DEFAULT_WRAPPER_PRESET
    wrapper_preset = wrapper_preset.strip().lower()

    wrapper_open = body.get("wrapper_open", "")
    if not isinstance(wrapper_open, str):
        wrapper_open = ""
    wrapper_close = body.get("wrapper_close", "")
    if not isinstance(wrapper_close, str):
        wrapper_close = ""

    apply_scope_raw = body.get("apply_scope", _DEFAULT_APPLY_SCOPE)
    apply_scope = (
        str(apply_scope_raw).strip().lower()
        if isinstance(apply_scope_raw, str)
        else _DEFAULT_APPLY_SCOPE
    )
    if apply_scope not in _VALID_APPLY_SCOPES:
        apply_scope = _DEFAULT_APPLY_SCOPE

    cleaned_overrides: dict[str, str] = {}
    field_errors: dict[str, str] = {}

    for key_raw, value_raw in overrides_in.items():
        if not isinstance(key_raw, str):
            continue

        spec = get_message_key(key_raw)
        if spec is None:
            field_errors[key_raw] = "unknown message key"
            continue

        text = "" if value_raw is None else str(value_raw)
        if text.strip() == "" or text == spec.default:
            continue

        try:
            result = validate_template_text(spec.key, text)
        except InvalidTemplateError as exc:
            field_errors[spec.key] = str(exc)
            continue

        if not result["ok"]:
            issues: list[str] = []
            if result["unknown_tokens"]:
                issues.append("unknown tokens: " + ", ".join(result["unknown_tokens"]))
            if result["disallowed_tokens"]:
                issues.append("not allowed in this scenario: " + ", ".join(result["disallowed_tokens"]))
            field_errors[spec.key] = "; ".join(issues) if issues else "invalid template"
            continue

        cleaned_overrides[spec.key] = text

    if field_errors:
        raise HTTPException(
            status_code=400,
            detail={"errors": field_errors},
        )

    # Settings UI only submits user-visible keys. Preserve hidden keys (e.g. legacy status.label.*).
    prior = dict(get_template_config().get("overrides") or {})
    merged_overrides: dict[str, str] = {}
    for pk, pv in prior.items():
        sk = get_message_key(pk)
        if sk is not None and not sk.settings_ui:
            merged_overrides[pk] = pv
    merged_overrides.update(cleaned_overrides)

    saved = save_template_config({
        "separator": sep,
        "case": case,
        "overrides": merged_overrides,
        "wrapper_preset": wrapper_preset,
        "wrapper_open": wrapper_open,
        "wrapper_close": wrapper_close,
    })

    backfill_summary = _execute_apply_scope(apply_scope)

    return JSONResponse(
        {
            "ok": True,
            "saved": saved,
            "registry": [_serialize_message(m) for m in get_registry_for_settings_ui()],
            "apply_scope": apply_scope,
            "backfill": backfill_summary,
        }
    )


def _get_pending_backfill_flag() -> bool:
    """Read the ``PLACEHOLDER_TEMPLATE_BACKFILL_PENDING`` flag from app_config."""
    try:
        from services.postgres.db import get_session
        from services.postgres.models import AppConfig
        from services.source_of_truth.template_backfill import PENDING_FLAG_KEY
    except Exception:
        return False

    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == PENDING_FLAG_KEY).first()
        return bool(row and row.value)
    finally:
        try:
            session.close()
        except Exception:
            pass


def _execute_apply_scope(apply_scope: str) -> dict[str, Any]:
    """Materialize the user's chosen apply policy after a successful template save.

    - ``now``: enqueue an immediate template-backfill job covering all active placeholders.
    - ``next_full_sync``: set the pending flag so the next scheduled or manual full sync runs the job.
    - ``future``: clear any pending flag and do nothing retroactive.
    """
    try:
        from services.source_of_truth.template_backfill import (
            clear_pending_template_backfill,
            enqueue_template_backfill,
            mark_template_backfill_pending,
        )
    except Exception as exc:
        logger.warning(
            f"Template backfill module unavailable: {exc}",
            extra={"emoji_type": "warning"},
        )
        return {"scope": apply_scope, "ok": False, "reason": "backfill_module_unavailable"}

    if apply_scope == "now":
        out = enqueue_template_backfill()
        out.setdefault("scope", "now")
        try:
            clear_pending_template_backfill()
        except Exception:
            pass
        return out

    if apply_scope == "next_full_sync":
        out = mark_template_backfill_pending()
        out.setdefault("scope", "next_full_sync")
        return out

    out = clear_pending_template_backfill()
    out.setdefault("scope", "future")
    return out


@router.get("/templates/apply_estimate")
def get_apply_estimate() -> JSONResponse:
    """How many placeholders an immediate ``Apply now`` save would refresh."""
    try:
        from services.source_of_truth.template_backfill import (
            is_template_backfill_pending,
            placeholder_count_for_apply_now,
        )
        count = placeholder_count_for_apply_now()
        pending = is_template_backfill_pending()
    except Exception as exc:
        logger.warning(f"Apply estimate unavailable: {exc}", extra={"emoji_type": "warning"})
        return JSONResponse({"placeholder_count": 0, "pending_full_sync_backfill": False})
    return JSONResponse({"placeholder_count": int(count), "pending_full_sync_backfill": bool(pending)})


@router.post("/templates/preview")
def post_preview(body: dict[str, Any]) -> JSONResponse:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    key = body.get("key")
    template = body.get("template")
    sep_override = body.get("separator")
    case_override = body.get("case")

    if not isinstance(key, str) or not key:
        raise HTTPException(status_code=400, detail="key is required")

    spec = get_message_key(key)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"unknown message key: {key}")

    if template is None:
        template = spec.default
    template_text = str(template)

    try:
        validation = validate_template_text(spec.key, template_text)
    except InvalidTemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except UnknownTemplateKeyError:
        raise HTTPException(status_code=400, detail=f"unknown message key: {key}")

    ctx = dict(spec.sample_context)

    try:
        rendered = render_template(
            spec.key,
            template_text,
            ctx,
            separator=sep_override if isinstance(sep_override, str) and sep_override else None,
            case_mode=case_override if isinstance(case_override, str) and case_override else None,
        )
    except Exception as exc:
        logger.warning(
            f"Template preview render failed key={spec.key}: {exc}",
            extra={"emoji_type": "warning"},
        )
        rendered = ""

    return JSONResponse(
        {
            "key": spec.key,
            "rendered": rendered,
            "validation": validation,
            "sample_context": ctx,
        }
    )


@router.post("/templates/reset")
def post_reset(body: dict[str, Any]) -> JSONResponse:
    """Reset a single override (or all overrides) to their defaults.

    Body: ``{"key": "..."}`` to reset one, ``{"all": true}`` to reset everything.
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    config = get_template_config()
    overrides = dict(config["overrides"])

    if body.get("all"):
        overrides = {}
    else:
        key = body.get("key")
        if not isinstance(key, str) or not key:
            raise HTTPException(status_code=400, detail="key is required")
        spec = get_message_key(key)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"unknown message key: {key}")
        overrides.pop(spec.key, None)

    saved = save_template_config(
        {
            "separator": config.get("separator"),
            "case": config.get("case"),
            "overrides": overrides,
            "wrapper_preset": config.get("wrapper_preset"),
            "wrapper_open": config.get("wrapper_open"),
            "wrapper_close": config.get("wrapper_close"),
        }
    )

    return JSONResponse(
        {
            "ok": True,
            "saved": saved,
            "registry": [_serialize_message(m) for m in get_registry_for_settings_ui()],
        }
    )
