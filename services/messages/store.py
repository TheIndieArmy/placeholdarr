"""Persistence for customizable status message templates.

Stores a single JSON document in the ``app_config`` table under the key
``PLACEHOLDER_MESSAGE_TEMPLATES``:

    {
        "separator": "\u00b7",
        "case": "default",
        "overrides": { "<message-key>": "<template string>", ... }
    }

Reads are cached in process for fast hot-path access from the projection
engine. Writes go straight through to the database.
"""

from __future__ import annotations

import threading
from typing import Any

from services.messages.registry import (
    CASE_OPTIONS,
    DEFAULT_SEPARATOR,
    DEFAULT_WRAPPER_PRESET,
    SEPARATOR_PRESETS,
    WRAPPER_PRESETS,
    get_message_key,
)
from services.postgres.db import get_session
from services.postgres.models import AppConfig


CONFIG_KEY = "PLACEHOLDER_MESSAGE_TEMPLATES"

_CACHE_LOCK = threading.Lock()
_CACHE_LOADED = False
_CACHE: dict[str, Any] = {
    "separator": DEFAULT_SEPARATOR,
    "case": "default",
    "overrides": {},
    "wrapper_preset": DEFAULT_WRAPPER_PRESET,
    "wrapper_open": "",
    "wrapper_close": "",
}


def _empty_config() -> dict[str, Any]:
    return {
        "separator": DEFAULT_SEPARATOR,
        "case": "default",
        "overrides": {},
        "wrapper_preset": DEFAULT_WRAPPER_PRESET,
        "wrapper_open": "",
        "wrapper_close": "",
    }


_WRAPPER_PRESET_VALUES = {entry["value"] for entry in WRAPPER_PRESETS}

_TITLE_SUFFIX_KEYS = frozenset(
    {
        "title.suffix.movie",
        "title.suffix.series",
        "title.suffix.season",
        "title.suffix.episode",
    }
)


def _coerce_title_suffix_value(key: str, raw: str) -> str:
    """Title suffix templates are literal-only (no {Tokens}). Invalid stored values fall back to default."""
    spec = get_message_key(key)
    if spec is None:
        return raw
    text = str(raw) if raw is not None else ""
    if not text.strip():
        return str(spec.default)
    # Import here: template_engine imports store at module load.
    from services.messages.template_engine import validate_template_text

    try:
        vr = validate_template_text(key, text)
        if vr.get("ok"):
            return text
    except Exception:
        pass
    return str(spec.default)


def _normalize(payload: Any) -> dict[str, Any]:
    """Coerce raw stored payload into the canonical shape used in memory."""
    base = _empty_config()
    if not isinstance(payload, dict):
        return base

    sep = payload.get("separator")
    if isinstance(sep, str) and sep:
        base["separator"] = sep

    case = payload.get("case")
    if isinstance(case, str) and case in {opt["value"] for opt in CASE_OPTIONS}:
        base["case"] = case

    wp = payload.get("wrapper_preset")
    if isinstance(wp, str) and wp.strip().lower() in _WRAPPER_PRESET_VALUES:
        base["wrapper_preset"] = wp.strip().lower()

    wo = payload.get("wrapper_open")
    if isinstance(wo, str):
        base["wrapper_open"] = wo[:8]
    wc = payload.get("wrapper_close")
    if isinstance(wc, str):
        base["wrapper_close"] = wc[:8]

    overrides_raw = payload.get("overrides") or {}
    if isinstance(overrides_raw, dict):
        migrated = dict(overrides_raw)
        legacy = migrated.get("title.suffix.format")
        if isinstance(legacy, str) and legacy.strip():
            for nk in _TITLE_SUFFIX_KEYS:
                if nk not in migrated:
                    migrated[nk] = legacy
        clean: dict[str, str] = {}
        for k, v in migrated.items():
            if not isinstance(k, str):
                continue
            if get_message_key(k) is None:
                continue
            text = "" if v is None else str(v)
            if k in _TITLE_SUFFIX_KEYS:
                text = _coerce_title_suffix_value(k, text)
            clean[k] = text
        base["overrides"] = clean

    return base


def _load_locked() -> dict[str, Any]:
    """Caller must hold _CACHE_LOCK."""
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == CONFIG_KEY).first()
        return _normalize(row.value if row else None)
    finally:
        session.close()


def _ensure_loaded() -> None:
    global _CACHE_LOADED
    if _CACHE_LOADED:
        return
    with _CACHE_LOCK:
        if _CACHE_LOADED:
            return
        try:
            _CACHE.update(_load_locked())
        except Exception:
            _CACHE.update(_empty_config())
        _CACHE_LOADED = True


def reload_cache() -> None:
    """Force a re-read from the database; useful after external migrations."""
    global _CACHE_LOADED
    with _CACHE_LOCK:
        try:
            _CACHE.update(_load_locked())
        except Exception:
            _CACHE.update(_empty_config())
        _CACHE_LOADED = True


def get_template_config() -> dict[str, Any]:
    """Return a defensive copy of the current template config."""
    _ensure_loaded()
    with _CACHE_LOCK:
        return {
            "separator": _CACHE.get("separator", DEFAULT_SEPARATOR),
            "case": _CACHE.get("case", "default"),
            "overrides": dict(_CACHE.get("overrides", {})),
            "wrapper_preset": _CACHE.get("wrapper_preset", DEFAULT_WRAPPER_PRESET),
            "wrapper_open": _CACHE.get("wrapper_open", ""),
            "wrapper_close": _CACHE.get("wrapper_close", ""),
        }


def get_overrides() -> dict[str, str]:
    return get_template_config()["overrides"]


def get_separator() -> str:
    return get_template_config()["separator"]


def get_case() -> str:
    return get_template_config()["case"]


def get_wrapper() -> dict[str, str]:
    """Return the current wrapper config as a dict with ``preset``/``open``/``close``."""
    cfg = get_template_config()
    return {
        "preset": str(cfg.get("wrapper_preset") or DEFAULT_WRAPPER_PRESET),
        "open": str(cfg.get("wrapper_open") or ""),
        "close": str(cfg.get("wrapper_close") or ""),
    }


def save_template_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a new config payload. Caller is expected to have validated entries."""
    normalized = _normalize(payload)
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == CONFIG_KEY).first()
        if row is None:
            row = AppConfig(
                key=CONFIG_KEY,
                value=normalized,
                value_type="json",
                description="Customizable player projection message templates.",
            )
            session.add(row)
        else:
            row.value = normalized
            row.value_type = "json"
        session.commit()
    finally:
        session.close()

    with _CACHE_LOCK:
        _CACHE["separator"] = normalized["separator"]
        _CACHE["case"] = normalized["case"]
        _CACHE["overrides"] = dict(normalized["overrides"])
        _CACHE["wrapper_preset"] = normalized["wrapper_preset"]
        _CACHE["wrapper_open"] = normalized["wrapper_open"]
        _CACHE["wrapper_close"] = normalized["wrapper_close"]
        global _CACHE_LOADED
        _CACHE_LOADED = True

    return normalized


def get_separator_presets() -> tuple[dict[str, str], ...]:
    return SEPARATOR_PRESETS


def get_case_options() -> tuple[dict[str, str], ...]:
    return CASE_OPTIONS


def get_wrapper_preset_options() -> tuple[dict[str, str], ...]:
    return WRAPPER_PRESETS
