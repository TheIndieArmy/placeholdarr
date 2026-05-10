"""Template engine for customizable status messages.

Renders ``{Token}``-style placeholders against a context dictionary, applying
the configured separator and case. Templates are looked up in the registry by
key with override-or-default resolution.

Usage:
    rendered = render("calendar.tv.countdown.plural", {"DaysUntil": 5})
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from core.logger import logger
from services.messages.registry import (
    DEFAULT_SEPARATOR,
    MessageKey,
    get_message_key,
    get_token_spec,
    get_token_specs,
    get_registry,
    get_wrapper_preset_pair,
)
from services.messages import store


_TOKEN_PATTERN = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")
_LITERAL_BRACE = re.compile(r"\{\{|\}\}")  # Not currently used but reserved for escaping.

# Internal sentinel used in place of the configured separator during substitution.
# After all tokens render, ``_collapse_separators`` splits on the sentinel, drops
# empty segments, and rejoins with `" {separator} "`. This means a template like
# ``{Runtime} {Sep} REQUEST`` collapses cleanly to ``REQUEST`` when Runtime is empty.
_SEP_SENTINEL = "\x00\x01SEP\x01\x00"

MAX_TEMPLATE_LENGTH = 400


class InvalidTemplateError(ValueError):
    """Raised when a template cannot be parsed (unbalanced / oversized / bad token)."""


class UnknownTemplateKeyError(KeyError):
    """Raised when a key is not in the registry."""


# ----- Static validation ----------------------------------------------------


def _scan_tokens(template: str) -> list[str]:
    return [m.group(1) for m in _TOKEN_PATTERN.finditer(template)]


def _check_brace_balance(template: str) -> None:
    """Reject templates with stray ``{`` or ``}`` that weren't matched as tokens."""
    stripped = _TOKEN_PATTERN.sub("", template)
    if "{" in stripped or "}" in stripped:
        raise InvalidTemplateError("template has unbalanced or stray braces")


def validate_template_text(
    key: str,
    template: str,
    *,
    max_length: int = MAX_TEMPLATE_LENGTH,
) -> dict[str, Any]:
    """Validate a free-form template string for a registered key.

    Returns a dict with ``ok``, ``unknown_tokens``, ``disallowed_tokens``, ``warnings``,
    and ``error`` fields. Raises ``UnknownTemplateKeyError`` for unknown registry keys
    and ``InvalidTemplateError`` for fatal problems (length / brace balance).
    """
    spec = get_message_key(key)
    if spec is None:
        raise UnknownTemplateKeyError(key)

    text = "" if template is None else str(template)
    if len(text) > max_length:
        raise InvalidTemplateError(f"template exceeds maximum length of {max_length} characters")

    _check_brace_balance(text)

    used = _scan_tokens(text)
    allowed = set(spec.allowed_tokens)
    known = {t.name for t in get_token_specs()}

    unknown = sorted({t for t in used if t not in known})
    disallowed = sorted({t for t in used if t in known and t not in allowed})

    warnings: list[str] = []
    # Friendly nudges for common mistakes.
    if spec.key.endswith(".plural") and "{DaysUntil}" not in text:
        warnings.append("Consider including {DaysUntil} in plural countdown templates so users see the day count.")
    if spec.key == "queue.downloading" and "{Progress}" not in text:
        warnings.append("Consider including {Progress} so users see the download progress.")
    if spec.key.startswith("import_grace.countdown") and spec.key.endswith("countdown") and "{MinutesRemaining}" not in text:
        warnings.append("Consider including {MinutesRemaining} so the countdown shows the remaining minutes.")

    return {
        "ok": not unknown and not disallowed,
        "unknown_tokens": unknown,
        "disallowed_tokens": disallowed,
        "warnings": warnings,
    }


# ----- Rendering ------------------------------------------------------------


def _apply_case(text: str, case_mode: str) -> str:
    mode = (case_mode or "default").strip().lower()
    if mode == "upper":
        return text.upper()
    if mode == "lower":
        return text.lower()
    if mode == "title":
        return text.title()
    return text


def _resolve_token(
    name: str,
    ctx: dict[str, Any],
    *,
    separator: str,
    case_mode: str,
    nesting: tuple[str, ...],
) -> str:
    if name == "Sep":
        # Emit a sentinel; it is resolved (or collapsed away) by ``_collapse_separators``
        # after all tokens have been substituted.
        return _SEP_SENTINEL

    if name in ctx and ctx[name] is not None:
        text = str(ctx[name])
        return text  # already a string; engine treats empty/missing identically

    if name == "Runtime":
        runtime = ctx.get("__runtime_text__")
        if isinstance(runtime, str):
            return runtime
        hours = ctx.get("Hours")
        minutes = ctx.get("Minutes")
        try:
            h = int(hours or 0)
            m = int(minutes or 0)
        except (TypeError, ValueError):
            return ""
        if h > 0 and m > 0:
            return f"{h}h {m}m"
        if h > 0:
            return f"{h}h"
        if m > 0:
            return f"{m}m"
        return ""

    return ""


def _resolve_template_text(spec: MessageKey, ctx: dict[str, Any], overrides: dict[str, str]) -> str:
    """Pick an override or default, with movie-no-release-type alt-default support."""
    override = overrides.get(spec.key)
    if isinstance(override, str) and override.strip():
        return override

    if spec.alt_defaults:
        if not str(ctx.get("ReleaseLabel") or "").strip() and "no_release_type" in spec.alt_defaults:
            return spec.alt_defaults["no_release_type"]

    return spec.default


def render(
    key: str,
    ctx: dict[str, Any] | None = None,
    *,
    overrides: dict[str, str] | None = None,
    separator: str | None = None,
    case_mode: str | None = None,
    _nesting: tuple[str, ...] = (),
) -> str:
    """Render a registered key against ``ctx``.

    ``ctx`` may carry standard tokens (``Title``, ``DaysUntil``, ...) plus
    ``__runtime_text__`` used by REQUEST runtime formatting.
    """
    spec = get_message_key(key)
    if spec is None:
        raise UnknownTemplateKeyError(key)

    if len(_nesting) > 6:
        logger.warning(
            f"Template render aborted due to deep nesting key={key} chain={_nesting}",
            extra={"emoji_type": "warning"},
        )
        return ""

    if ctx is None:
        ctx = {}

    config = store.get_template_config()
    overrides = overrides if overrides is not None else config["overrides"]
    sep = separator if separator is not None else (config.get("separator") or DEFAULT_SEPARATOR)
    case = case_mode if case_mode is not None else (config.get("case") or "default")

    template = _resolve_template_text(spec, ctx, overrides)

    def _replace(match: re.Match[str]) -> str:
        token_name = match.group(1)
        if get_token_spec(token_name) is None:
            return ""
        return _resolve_token(
            token_name,
            ctx,
            separator=sep,
            case_mode=case,
            nesting=_nesting + (key,),
        )

    rendered = _TOKEN_PATTERN.sub(_replace, template)
    rendered = _collapse_separators(rendered, sep)

    return _normalize_whitespace(rendered)


def render_template(
    key: str,
    template: str,
    ctx: dict[str, Any] | None = None,
    *,
    separator: str | None = None,
    case_mode: str | None = None,
) -> str:
    """Render an arbitrary template string under ``key``'s rules (used for previews)."""
    spec = get_message_key(key)
    if spec is None:
        raise UnknownTemplateKeyError(key)

    overrides = {**store.get_overrides(), key: template}
    return render(key, ctx or {}, overrides=overrides, separator=separator, case_mode=case_mode)


def sample_render(key: str, *, override_text: str | None = None) -> str:
    """Render a registered key against its registered ``sample_context``."""
    spec = get_message_key(key)
    if spec is None:
        raise UnknownTemplateKeyError(key)

    ctx = dict(spec.sample_context)
    overrides_in_use = store.get_overrides()
    if override_text is not None:
        overrides_in_use = {**overrides_in_use, key: override_text}

    return render(key, ctx, overrides=overrides_in_use)


def _collapse_separators(text: str, separator: str) -> str:
    """Replace separator sentinels with the configured separator, dropping empty segments.

    Splitting on the sentinel and discarding empty/whitespace-only segments ensures
    that templates like ``{Runtime} {Sep} REQUEST`` become just ``REQUEST`` when
    runtime is missing — no orphaned " · " on the front. Adjacent separators with
    nothing between them are likewise collapsed into a single one.
    """
    if not text:
        return ""
    if _SEP_SENTINEL not in text:
        return text
    parts = text.split(_SEP_SENTINEL)
    cleaned: list[str] = []
    for part in parts:
        stripped = part.strip()
        if stripped:
            cleaned.append(stripped)
    if not cleaned:
        return ""
    sep = separator if separator else DEFAULT_SEPARATOR
    return f" {sep} ".join(cleaned)


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of spaces and tidy up spacing around brackets/parens produced by
    optional/empty token resolution."""
    if not text:
        return ""
    out = re.sub(r"[ \t]+", " ", text)
    out = re.sub(r"\(\s+", "(", out)
    out = re.sub(r"\s+\)", ")", out)
    # Drop empty bracket/paren islands left behind when every token inside collapsed.
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"\[\s*\]", "", out)
    out = re.sub(r"\{\s*\}", "", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"[ \t]+", " ", out)
    return out.strip()


def apply_wrapper(inner: str, *, preset: str | None = None, open_text: str | None = None, close_text: str | None = None) -> str:
    """Wrap ``inner`` in the configured global presentation chrome.

    Resolution order:
      1. Explicit ``open_text`` / ``close_text`` arguments (used by previews).
      2. Resolved preset pair (e.g. ``brackets``).
      3. Custom open/close from the saved store config.
      4. Default brackets ``[ ]``.

    An empty inner string returns empty (no naked wrapper).
    """
    inner_text = (inner or "").strip()
    if not inner_text:
        return ""

    if open_text is not None or close_text is not None:
        o = "" if open_text is None else str(open_text)
        c = "" if close_text is None else str(close_text)
        return f"{o}{inner_text}{c}"

    config = store.get_template_config()
    preset_name = preset if preset is not None else str(config.get("wrapper_preset") or "")
    preset_name = preset_name.strip().lower() or "brackets"

    if preset_name == "custom":
        o = str(config.get("wrapper_open") or "")
        c = str(config.get("wrapper_close") or "")
        return f"{o}{inner_text}{c}"

    pair = get_wrapper_preset_pair(preset_name)
    if pair is None:
        # Unknown preset → fall back to default brackets to keep output readable.
        pair = ("[", "]")
    o, c = pair
    return f"{o}{inner_text}{c}"


# ----- Convenience: render lists of media tokens ---------------------------


def media_tokens_for_movie(movie: Any) -> dict[str, Any]:
    """Map a Movie ORM row to the standard media token namespace."""
    if movie is None:
        return {}
    title = str(getattr(movie, "title", "") or "").strip()
    year = getattr(movie, "year", None)
    runtime_min = getattr(movie, "radarr_runtime", None)
    cert = str(getattr(movie, "radarr_certification", "") or "").strip()
    studio = str(getattr(movie, "radarr_studio", "") or "").strip()
    genres = getattr(movie, "radarr_genres", None)
    if isinstance(genres, list):
        genres_text = ", ".join(str(g) for g in genres if g)
    elif isinstance(genres, str):
        genres_text = genres
    else:
        genres_text = ""

    hours, minutes = _split_minutes(runtime_min)
    return {
        "Title": title,
        "Year": str(year) if year else "",
        "ReleaseYear": str(year) if year else "",
        "Genres": genres_text,
        "Certification": cert,
        "Studio": studio,
        "RuntimeMinutes": str(int(runtime_min)) if isinstance(runtime_min, (int, float)) and int(runtime_min) > 0 else "",
        "Hours": str(hours) if hours else "",
        "Minutes": str(minutes) if minutes else "",
    }


def media_tokens_for_series(series: Any) -> dict[str, Any]:
    """Map a Series ORM row to the media token namespace (series-level surfaces)."""
    if series is None:
        return {}
    title = str(getattr(series, "title", "") or "").strip()
    year_val = getattr(series, "year", None)
    runtime_min = getattr(series, "sonarr_runtime", None)
    cert = str(getattr(series, "sonarr_certification", "") or "").strip()
    studio = str(getattr(series, "sonarr_network", "") or "").strip()
    genres = getattr(series, "sonarr_genres", None)
    if isinstance(genres, list):
        genres_text = ", ".join(str(g) for g in genres if g)
    elif isinstance(genres, str):
        genres_text = genres
    else:
        genres_text = ""

    hours, minutes = _split_minutes(runtime_min)
    return {
        "Title": title,
        "SeriesTitle": title,
        "EpisodeTitle": "",
        "SeasonNumber": "",
        "EpisodeNumber": "",
        "SXXEYY": "",
        "Year": str(year_val) if year_val else "",
        "ReleaseYear": str(year_val) if year_val else "",
        "Genres": genres_text,
        "Certification": cert,
        "Studio": studio,
        "RuntimeMinutes": str(int(runtime_min)) if isinstance(runtime_min, (int, float)) and int(runtime_min) > 0 else "",
        "Hours": str(hours) if hours else "",
        "Minutes": str(minutes) if minutes else "",
    }


def media_tokens_for_season(season: Any, series: Any = None) -> dict[str, Any]:
    """Map Season (+ optional Series) for season-level title suffix templates."""
    if season is None:
        return {}
    if series is None:
        series = getattr(season, "series", None)
    st = str(getattr(season, "title", "") or "").strip()
    series_title = str(getattr(series, "title", "") or "").strip() if series else ""
    sn = int(getattr(season, "season_number", 0) or 0)
    year_val = getattr(season, "year", None)
    if year_val is None and series is not None:
        year_val = getattr(series, "year", None)
    runtime_min = getattr(series, "sonarr_runtime", None) if series is not None else None
    hours, minutes = _split_minutes(runtime_min)
    genres_text = ""
    cert = ""
    studio = ""
    if series is not None:
        genres = getattr(series, "sonarr_genres", None)
        if isinstance(genres, list):
            genres_text = ", ".join(str(g) for g in genres if g)
        elif isinstance(genres, str):
            genres_text = genres
        cert = str(getattr(series, "sonarr_certification", "") or "").strip()
        studio = str(getattr(series, "sonarr_network", "") or "").strip()
    return {
        "Title": st,
        "SeriesTitle": series_title,
        "EpisodeTitle": "",
        "SeasonNumber": f"{sn:02d}",
        "EpisodeNumber": "",
        "SXXEYY": "",
        "Year": str(year_val) if year_val else "",
        "ReleaseYear": str(year_val) if year_val else "",
        "Genres": genres_text,
        "Certification": cert,
        "Studio": studio,
        "RuntimeMinutes": str(int(runtime_min)) if isinstance(runtime_min, (int, float)) and int(runtime_min) > 0 else "",
        "Hours": str(hours) if hours else "",
        "Minutes": str(minutes) if minutes else "",
    }


def media_tokens_for_episode(episode: Any, season: Any = None, series: Any = None) -> dict[str, Any]:
    if episode is None:
        return {}
    if season is None:
        season = getattr(episode, "season", None)
    if series is None and season is not None:
        series = getattr(season, "series", None)

    series_title = str(getattr(series, "title", "") or "").strip() if series else ""
    sn = int(getattr(season, "season_number", 0) or 0) if season else 0
    en = int(getattr(episode, "episode_number", 0) or 0)
    ep_title = str(getattr(episode, "title", "") or "").strip()

    runtime_min = getattr(episode, "sonarr_runtime", None)
    if not runtime_min and series is not None:
        runtime_min = getattr(series, "sonarr_runtime", None)

    hours, minutes = _split_minutes(runtime_min)

    year_val = getattr(episode, "year", None)
    if year_val is None and series is not None:
        year_val = getattr(series, "year", None)
    genres_text = ""
    cert = ""
    studio = ""
    if series is not None:
        genres = getattr(series, "sonarr_genres", None)
        if isinstance(genres, list):
            genres_text = ", ".join(str(g) for g in genres if g)
        elif isinstance(genres, str):
            genres_text = genres
        cert = str(getattr(series, "sonarr_certification", "") or "").strip()
        studio = str(getattr(series, "sonarr_network", "") or "").strip()

    return {
        "Title": series_title or ep_title,
        "SeriesTitle": series_title,
        "EpisodeTitle": ep_title,
        "SeasonNumber": f"{sn:02d}",
        "EpisodeNumber": f"{en:02d}",
        "SXXEYY": f"S{sn:02d}E{en:02d}",
        "Year": str(year_val) if year_val else "",
        "ReleaseYear": str(year_val) if year_val else "",
        "Genres": genres_text,
        "Certification": cert,
        "Studio": studio,
        "RuntimeMinutes": str(int(runtime_min)) if isinstance(runtime_min, (int, float)) and int(runtime_min) > 0 else "",
        "Hours": str(hours) if hours else "",
        "Minutes": str(minutes) if minutes else "",
    }


def _split_minutes(runtime_min: Any) -> tuple[int, int]:
    try:
        total = int(runtime_min) if runtime_min is not None else 0
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return 0, 0
    return total // 60, total % 60
