"""Poster / thumb layout profiles for the grid label designer.

Stores a versioned JSON document in AppConfig under ``PLACEHOLDER_POSTER_LAYOUTS``.
Twelve profiles: movie|tv|episode × placeholder|real × default|coming_soon.
Season posters use TV profiles. Bake uses placeholder today; real is designer-ready.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from typing import Any

from services.postgres.db import get_session
from services.postgres.models import AppConfig

CONFIG_KEY = "PLACEHOLDER_POSTER_LAYOUTS"
# Shipped layout-designer schema: odd snap grids + placeholder|real profile keys.
LAYOUT_DOC_VERSION = 1

# ``{media}.{availability}.{variant}`` — bake uses placeholder today; real is designer-ready.
PROFILE_KEYS: tuple[str, ...] = (
    "movie.placeholder.default",
    "movie.placeholder.coming_soon",
    "movie.real.default",
    "movie.real.coming_soon",
    "tv.placeholder.default",
    "tv.placeholder.coming_soon",
    "tv.real.default",
    "tv.real.coming_soon",
    "episode.placeholder.default",
    "episode.placeholder.coming_soon",
    "episode.real.default",
    "episode.real.coming_soon",
)

# Pre-ship two-segment keys → placeholder availability.
_LEGACY_PROFILE_KEY_MAP: dict[str, str] = {
    "movie.default": "movie.placeholder.default",
    "movie.coming_soon": "movie.placeholder.coming_soon",
    "tv.default": "tv.placeholder.default",
    "tv.coming_soon": "tv.placeholder.coming_soon",
    "episode.default": "episode.placeholder.default",
    "episode.coming_soon": "episode.placeholder.coming_soon",
}

PORTRAIT_COLS = 25
PORTRAIT_ROWS = 37
LANDSCAPE_COLS = 33
LANDSCAPE_ROWS = 21

# Unreleased interim docs only (dev testing before ship). Migrate once into version 1.
# 2/3 used denser even grids; 4/5 already used the current odd grids (5 added real keys).
_PRERELEASE_GRID_BY_VERSION: dict[int, dict[str, tuple[int, int]]] = {
    2: {"portrait": (12, 18), "landscape": (16, 10)},
    3: {"portrait": (24, 36), "landscape": (32, 20)},
    4: {"portrait": (PORTRAIT_COLS, PORTRAIT_ROWS), "landscape": (LANDSCAPE_COLS, LANDSCAPE_ROWS)},
    5: {"portrait": (PORTRAIT_COLS, PORTRAIT_ROWS), "landscape": (LANDSCAPE_COLS, LANDSCAPE_ROWS)},
}

# Tokens available in the designer (filtered per media kind in the UI).
# ``Banner`` is a textless full-edge fill strip; place chips on top for copy.
LABEL_TOKENS: tuple[str, ...] = (
    "Banner",
    "PLACEHOLDER",
    "ComingSoonOrNewEp",
    "Custom",
    "Badge",
    "SeriesStatus",
    "Title",
    "Year",
    "Certification",
    "Genres",
    "Runtime",
    "TheatricalDate",
    "DigitalDate",
    "PhysicalDate",
    "AirDate",
    "SeriesPremiereDate",
    "SXXEYY",
    "Rating",
)

MOVIE_TOKENS = frozenset(
    {
        "Banner",
        "PLACEHOLDER",
        "ComingSoonOrNewEp",
        "Custom",
        "Badge",
        "Title",
        "Year",
        "Certification",
        "Genres",
        "Runtime",
        "TheatricalDate",
        "DigitalDate",
        "PhysicalDate",
        "Rating",
    }
)
TV_TOKENS = frozenset(
    {
        "Banner",
        "PLACEHOLDER",
        "ComingSoonOrNewEp",
        "Custom",
        "Badge",
        "SeriesStatus",
        "Title",
        "Year",
        "Certification",
        "Genres",
        "Runtime",
        "AirDate",
        "SeriesPremiereDate",
        "SXXEYY",
        "Rating",
    }
)
EPISODE_TOKENS = frozenset(
    {
        "Banner",
        "PLACEHOLDER",
        "ComingSoonOrNewEp",
        "Custom",
        "Badge",
        "SeriesStatus",
        "Title",
        "Year",
        "Certification",
        "Genres",
        "Runtime",
        "AirDate",
        "SeriesPremiereDate",
        "SXXEYY",
        "Rating",
    }
)

CUSTOM_TOKEN = "Custom"
_CUSTOM_TEXT_MAX_LEN = 120
_CUSTOM_SEP_DEFAULT = "·"
_CUSTOM_SEP_SENTINEL = "\x1e"
# Tokens Custom chips may interpolate (static poster metadata only; no countdowns/queue).
_CUSTOM_TEMPLATE_TOKENS: frozenset[str] = frozenset(
    {
        "PLACEHOLDER",
        "ComingSoonOrNewEp",
        "Title",
        "Year",
        "Certification",
        "Genres",
        "Runtime",
        "TheatricalDate",
        "DigitalDate",
        "PhysicalDate",
        "AirDate",
        "SeriesPremiereDate",
        "SXXEYY",
        "SeriesStatus",
        "Rating",
        "Sep",
    }
)
_CUSTOM_TOKEN_PATTERN = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")

_DEFAULT_TEXT = "#ffffff"
_DEFAULT_FILL = "#000000"
_DEFAULT_BORDER = "#000000"
_DEFAULT_FILL_OPACITY = 0.65
_DEFAULT_BORDER_OPACITY = 0.0
LABEL_SIZES: tuple[str, ...] = ("small", "medium", "large")
# Relative to small. Placeholdarr badge defaults to medium (matches legacy corner size).
_SIZE_MULT: dict[str, float] = {"small": 1.0, "medium": 1.5, "large": 2.0}
# Chip = content-sized pill. Banner token = textless full-edge fill strip.
LABEL_STYLES: tuple[str, ...] = ("chip", "banner")
BANNER_EDGES: tuple[str, ...] = ("top", "bottom", "left", "right")
BANNER_TOKEN = "Banner"
_ARTED_TOKENS = frozenset({"Rating"})
_RATING_SOURCES: tuple[str, ...] = ("imdb", "tmdb", "trakt", "rottentomatoes", "metacritic")
_RATING_SOURCE_PRIORITY: tuple[str, ...] = (
    "imdb",
    "tmdb",
    "themoviedb",
    "trakt",
    "rottentomatoes",
    "rotten_tomatoes",
    "metacritic",
)

_SERIES_STATUS_LABELS = {
    "continuing": "Airing",
    "airing": "Airing",
    "ended": "Ended",
    "canceled": "Canceled",
    "cancelled": "Canceled",
    "upcoming": "Returning",
    "returning": "Returning",
}

_CACHE_LOCK = threading.Lock()
_CACHE_LOADED = False
_CACHE: dict[str, Any] = {}


def grid_size_for_profile(profile_key: str) -> tuple[int, int]:
    """Return (cols, rows) for a profile key."""
    kind = str(profile_key or "").split(".", 1)[0]
    if kind == "episode":
        return (LANDSCAPE_COLS, LANDSCAPE_ROWS)
    return (PORTRAIT_COLS, PORTRAIT_ROWS)


def tokens_for_profile(profile_key: str) -> frozenset[str]:
    kind = str(profile_key or "").split(".", 1)[0]
    if kind == "movie":
        return MOVIE_TOKENS
    if kind == "episode":
        return EPISODE_TOKENS
    return TV_TOKENS


def _new_label_id() -> str:
    return uuid.uuid4().hex[:12]


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _hex_color(value: Any, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if not text.startswith("#"):
        text = "#" + text
    body = text[1:]
    if len(body) not in (6, 8):
        return default
    try:
        int(body, 16)
    except ValueError:
        return default
    return "#" + body.lower()


def _opacity(value: Any, default: float = _DEFAULT_FILL_OPACITY) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n > 1.0:
        n = n / 100.0
    return max(0.0, min(1.0, n))


def _border_color_and_opacity(raw: Any) -> tuple[str, float]:
    """RGB border + opacity. Migrates legacy ``#RRGGBBAA`` (alpha 00 meant off)."""
    if not isinstance(raw, dict):
        return _DEFAULT_BORDER, _DEFAULT_BORDER_OPACITY
    raw_border = raw.get("border_color")
    parsed = _hex_color(raw_border, _DEFAULT_BORDER)
    body = parsed[1:]
    migrated_opacity: float | None = None
    if len(body) == 8:
        border_color = "#" + body[:6]
        migrated_opacity = int(body[6:8], 16) / 255.0
    else:
        border_color = parsed if len(body) == 6 else _DEFAULT_BORDER
    if raw.get("border_opacity") is not None:
        return border_color, _opacity(raw.get("border_opacity"), _DEFAULT_BORDER_OPACITY)
    if migrated_opacity is not None:
        return border_color, migrated_opacity
    # Explicit solid ``#RRGGBB`` → full stroke; missing color stays off.
    if raw_border is not None and str(raw_border).strip():
        text = str(raw_border).strip().lstrip("#")
        if len(text) == 6:
            try:
                int(text, 16)
                return border_color, 1.0
            except ValueError:
                pass
    return border_color, _DEFAULT_BORDER_OPACITY


def _normalize_size(raw: Any, *, token: str) -> str:
    text = str(raw or "").strip().lower()
    if text in LABEL_SIZES:
        return text
    return "medium" if token in {"Badge", BANNER_TOKEN} else "small"


def _normalize_custom_text(raw: Any, *, token: str) -> str:
    """User-entered chip template for the Custom token; ignored for every other token."""
    if token != CUSTOM_TOKEN:
        return ""
    text = str(raw or "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) > _CUSTOM_TEXT_MAX_LEN:
        text = text[:_CUSTOM_TEXT_MAX_LEN].rstrip()
    return text


def custom_template_tokens_for_profile(profile_key: str) -> list[str]:
    """Ordered `{Token}` names allowed in Custom chips for this profile."""
    allowed = tokens_for_profile(profile_key)
    names = [name for name in LABEL_TOKENS if name in _CUSTOM_TEMPLATE_TOKENS and name in allowed]
    # Sep is always available for Status Updates-style separators.
    if "Sep" not in names:
        names.append("Sep")
    return names


def render_custom_chip_text(
    template: str,
    token_map: dict[str, str] | None = None,
    *,
    separator: str = _CUSTOM_SEP_DEFAULT,
) -> str:
    """Fill a Custom chip template from static layout tokens; empty values collapse.

    Supports ``{Title}``, ``{Year}``, ``{Sep}``, and the other poster metadata tokens.
    Missing / blank tokens drop out (same idea as Status Updates).
    """
    text = str(template or "")
    if not text.strip():
        return ""
    ctx = dict(token_map or {})
    sep = str(separator or _CUSTOM_SEP_DEFAULT).strip() or _CUSTOM_SEP_DEFAULT

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "Sep":
            return _CUSTOM_SEP_SENTINEL
        if name not in _CUSTOM_TEMPLATE_TOKENS:
            return ""
        return str(ctx.get(name) or "").strip()

    rendered = _CUSTOM_TOKEN_PATTERN.sub(_replace, text)
    if _CUSTOM_SEP_SENTINEL in rendered:
        parts = [p.strip() for p in rendered.split(_CUSTOM_SEP_SENTINEL)]
        parts = [p for p in parts if p]
        rendered = f" {sep} ".join(parts) if parts else ""
    rendered = re.sub(r"[ \t]+", " ", rendered)
    rendered = re.sub(r"\(\s+", "(", rendered)
    rendered = re.sub(r"\s+\)", ")", rendered)
    rendered = re.sub(r"\(\s*\)", "", rendered)
    rendered = re.sub(r"\[\s*\]", "", rendered)
    rendered = re.sub(r"\s+([,.;:!?])", r"\1", rendered)
    rendered = re.sub(r"[ \t]+", " ", rendered)
    return rendered.strip()


# Banner strip depth as a fraction of poster height (top/bottom) or width (left/right).
_BANNER_THICKNESS_MIN = 0.04
_BANNER_THICKNESS_MAX = 0.45
_BANNER_SIZE_MULT = {"small": 0.72, "medium": 1.0, "large": 1.35}
_BANNER_SPAN_MIN_GAP = 0.01


def _normalize_banner_span(start: Any, end: Any) -> tuple[float, float]:
    """Along-edge coverage as fractions 0..1 (full edge is 0..1)."""

    def _one(raw: Any, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            return default

    a = _one(start, 0.0)
    b = _one(end, 1.0)
    if b < a:
        a, b = b, a
    if b - a < _BANNER_SPAN_MIN_GAP:
        mid = (a + b) / 2.0
        a = max(0.0, mid - _BANNER_SPAN_MIN_GAP / 2.0)
        b = min(1.0, a + _BANNER_SPAN_MIN_GAP)
        if b - a < _BANNER_SPAN_MIN_GAP:
            a = max(0.0, b - _BANNER_SPAN_MIN_GAP)
    return a, b


def banner_base_frac(edge: str, *, landscape: bool) -> float:
    """Medium-tier strip depth for an edge (matches legacy bake)."""
    side = str(edge or "top").strip().lower() in {"left", "right"}
    if side:
        return 0.14 if landscape else 0.16
    return 0.18 if landscape else 0.11


def _normalize_banner_thickness(
    raw: Any,
    *,
    size: str,
    edge: str,
    landscape: bool,
) -> float:
    """Clamp continuous banner depth; migrate missing values from small/medium/large."""
    if raw is not None and str(raw).strip() != "":
        try:
            n = float(raw)
        except (TypeError, ValueError):
            n = None
        else:
            if n > 1.0:
                n = n / 100.0
            if n is not None:
                return max(_BANNER_THICKNESS_MIN, min(_BANNER_THICKNESS_MAX, n))
    base = banner_base_frac(edge, landscape=landscape)
    mult = float(_BANNER_SIZE_MULT.get(str(size or "medium").strip().lower(), 1.0))
    return max(_BANNER_THICKNESS_MIN, min(_BANNER_THICKNESS_MAX, base * mult))


def _normalize_rating_source(raw: Any) -> str:
    text = str(raw or "").strip().lower().replace(" ", "_")
    if text in ("", "auto"):
        return "auto"
    if text in ("themoviedb",):
        return "tmdb"
    if text in ("rotten_tomatoes", "rottentomatoes"):
        return "rottentomatoes"
    if text in _RATING_SOURCES:
        return text
    return "auto"


def _rating_source_for_label(*, token: str, arted: bool, raw: Any) -> str:
    if token != "Rating":
        return "auto"
    source = _normalize_rating_source(raw)
    # Arted chips always pick a concrete source; legacy ``auto`` becomes IMDb.
    if arted and source == "auto":
        return "imdb"
    return source


def _normalize_style(raw: Any, *, token: str) -> str:
    """Banner token is always a fill strip; every other token is a chip."""
    if token == BANNER_TOKEN:
        return "banner"
    return "chip"


def _normalize_edge(raw: Any, *, default: str = "top") -> str:
    text = str(raw or "").strip().lower().replace("-", "_")
    if text in BANNER_EDGES:
        return text
    return default if default in BANNER_EDGES else "top"


def _edge_anchor_cell(edge: str, *, cols: int, rows: int) -> tuple[int, int]:
    """Representative grid cell for a banner edge (used for occupancy / migration)."""
    mid_c = max(0, cols // 2)
    mid_r = max(0, rows // 2)
    e = _normalize_edge(edge)
    if e == "bottom":
        return rows - 1, mid_c
    if e == "left":
        return mid_r, 0
    if e == "right":
        return mid_r, cols - 1
    return 0, mid_c


def infer_banner_edge(row: int, col: int, *, cols: int, rows: int) -> str:
    """Pick the nearest poster edge for a grid cell (designer / migration helper)."""
    r = max(0, min(rows - 1, int(row)))
    c = max(0, min(cols - 1, int(col)))
    dist = {
        "top": r,
        "bottom": (rows - 1) - r,
        "left": c,
        "right": (cols - 1) - c,
    }
    return min(dist.items(), key=lambda item: item[1])[0]


def size_multiplier(size: str) -> float:
    return float(_SIZE_MULT.get(str(size or "small").strip().lower(), 1.0))


def normalize_label(raw: Any, *, cols: int, rows: int, landscape: bool = False) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    token = str(raw.get("token") or "").strip()
    if token not in LABEL_TOKENS:
        return None
    label_id = str(raw.get("id") or "").strip() or _new_label_id()
    border_color, border_opacity = _border_color_and_opacity(raw)
    arted = bool(raw.get("arted")) and token in _ARTED_TOKENS
    style = _normalize_style(raw.get("style"), token=token)
    size = _normalize_size(raw.get("size"), token=token)
    row = _clamp_int(raw.get("row"), 0, rows - 1, 0)
    col = _clamp_int(raw.get("col"), 0, cols - 1, 0)
    if style == "banner":
        edge = _normalize_edge(raw.get("edge")) if raw.get("edge") is not None else infer_banner_edge(
            row, col, cols=cols, rows=rows
        )
        row, col = _edge_anchor_cell(edge, cols=cols, rows=rows)
        arted = False
        border_opacity = 0.0
        thickness = _normalize_banner_thickness(
            raw.get("thickness"), size=size, edge=edge, landscape=landscape
        )
        span_start, span_end = _normalize_banner_span(raw.get("span_start"), raw.get("span_end"))
    else:
        edge = "top"
        thickness = banner_base_frac("top", landscape=landscape)
        span_start, span_end = 0.0, 1.0
    return {
        "id": label_id,
        "token": token,
        "row": row,
        "col": col,
        "text_color": _hex_color(raw.get("text_color"), _DEFAULT_TEXT),
        "fill_color": _hex_color(raw.get("fill_color"), _DEFAULT_FILL),
        "border_color": border_color,
        "border_opacity": border_opacity,
        "fill_opacity": _opacity(raw.get("fill_opacity"), _DEFAULT_FILL_OPACITY),
        "size": size,
        "thickness": thickness,
        "span_start": span_start,
        "span_end": span_end,
        "style": style,
        "edge": edge,
        "arted": arted,
        "rating_source": _rating_source_for_label(token=token, arted=arted, raw=raw.get("rating_source")),
        "custom_text": _normalize_custom_text(raw.get("custom_text"), token=token),
    }


def _expand_legacy_text_banners(raw_labels: Any, *, cols: int, rows: int) -> list[Any]:
    """Split old text-on-banner labels into a fill strip + a chip on top."""
    if not isinstance(raw_labels, list):
        return []
    out: list[Any] = []
    for item in raw_labels:
        if not isinstance(item, dict):
            continue
        token = str(item.get("token") or "").strip()
        style = str(item.get("style") or "").strip().lower()
        if style == "banner" and token and token not in {BANNER_TOKEN, "Badge"}:
            row = _clamp_int(item.get("row"), 0, rows - 1, 0)
            col = _clamp_int(item.get("col"), 0, cols - 1, 0)
            edge = (
                _normalize_edge(item.get("edge"))
                if item.get("edge") is not None
                else infer_banner_edge(row, col, cols=cols, rows=rows)
            )
            anchor_r, anchor_c = _edge_anchor_cell(edge, cols=cols, rows=rows)
            banner = {
                **item,
                "id": _new_label_id(),
                "token": BANNER_TOKEN,
                "style": "banner",
                "edge": edge,
                "row": anchor_r,
                "col": anchor_c,
                "arted": False,
                "border_opacity": 0.0,
            }
            chip = {
                **item,
                "id": str(item.get("id") or "").strip() or _new_label_id(),
                "token": token,
                "style": "chip",
                "edge": "top",
                "row": anchor_r,
                "col": anchor_c,
                # Text sits on the strip; chip fill stays off by default.
                "fill_opacity": 0.0,
                "border_opacity": 0.0,
                "arted": False,
            }
            out.append(banner)
            out.append(chip)
        else:
            out.append(item)
    return out


def _legacy_global_grayscale() -> bool:
    try:
        from services.poster_overlay import poster_grayscale_enabled

        return bool(poster_grayscale_enabled())
    except Exception:
        return False


def normalize_profile(profile_key: str, raw: Any) -> dict[str, Any]:
    cols, rows = grid_size_for_profile(profile_key)
    landscape = str(profile_key or "").startswith("episode.")
    allowed = tokens_for_profile(profile_key)
    labels_in = raw.get("labels") if isinstance(raw, dict) else None
    labels: list[dict[str, Any]] = []
    seen_banner_edges: set[str] = set()
    if isinstance(labels_in, list):
        for item in _expand_legacy_text_banners(labels_in, cols=cols, rows=rows):
            norm = normalize_label(item, cols=cols, rows=rows, landscape=landscape)
            if norm is None:
                continue
            if norm["token"] not in allowed:
                continue
            if norm["style"] == "banner":
                edge = str(norm.get("edge") or "top")
                if edge in seen_banner_edges:
                    continue
                seen_banner_edges.add(edge)
            labels.append(norm)
    # Per-profile grayscale (Default vs Coming Soon independent). Missing key
    # migrates from the legacy global PLACEHOLDER_POSTER_GRAYSCALE toggle.
    if isinstance(raw, dict) and "grayscale" in raw:
        grayscale = bool(raw.get("grayscale"))
    else:
        grayscale = _legacy_global_grayscale()
    return {"labels": labels, "grayscale": grayscale}


def _empty_doc() -> dict[str, Any]:
    return {
        "version": LAYOUT_DOC_VERSION,
        "profiles": {key: {"labels": [], "grayscale": False} for key in PROFILE_KEYS},
    }


def _banner_cell(profile_key: str, position: str) -> tuple[int, int]:
    cols, rows = grid_size_for_profile(profile_key)
    return _edge_anchor_cell(_normalize_edge(position), cols=cols, rows=rows)


def _badge_cell(profile_key: str, position: str) -> tuple[int, int]:
    cols, rows = grid_size_for_profile(profile_key)
    pos = str(position or "bottom_right").strip().lower().replace("-", "_")
    top = pos.startswith("top_")
    left = pos.endswith("_left")
    row = 0 if top else rows - 1
    col = 0 if left else cols - 1
    return row, col


def _make_label(token: str, row: int, col: int, **style: Any) -> dict[str, Any]:
    border_color = style.get("border_color", _DEFAULT_BORDER)
    if "border_opacity" in style:
        border_opacity = style.get("border_opacity", _DEFAULT_BORDER_OPACITY)
    else:
        # Legacy seed used ``#RRGGBBAA``; treat missing opacity as off when alpha was 00.
        parsed = _hex_color(border_color, _DEFAULT_BORDER)
        if len(parsed) == 9:
            border_opacity = int(parsed[7:9], 16) / 255.0
            border_color = parsed[:7]
        else:
            border_opacity = _DEFAULT_BORDER_OPACITY
    label_style = _normalize_style(style.get("style"), token=token)
    edge = _normalize_edge(style.get("edge"), default="top")
    size = _normalize_size(style.get("size"), token=token)
    landscape = bool(style.get("landscape"))
    if label_style == "banner":
        thickness = _normalize_banner_thickness(
            style.get("thickness"), size=size, edge=edge, landscape=landscape
        )
        span_start, span_end = _normalize_banner_span(style.get("span_start"), style.get("span_end"))
    else:
        thickness = banner_base_frac("top", landscape=landscape)
        span_start, span_end = 0.0, 1.0
    return {
        "id": _new_label_id(),
        "token": token,
        "row": row,
        "col": col,
        "text_color": style.get("text_color", _DEFAULT_TEXT),
        "fill_color": style.get("fill_color", _DEFAULT_FILL),
        "border_color": border_color[:7] if str(border_color).startswith("#") else border_color,
        "border_opacity": float(border_opacity) if label_style != "banner" else 0.0,
        "fill_opacity": style.get("fill_opacity", _DEFAULT_FILL_OPACITY),
        "size": size,
        "thickness": thickness,
        "span_start": span_start,
        "span_end": span_end,
        "style": label_style,
        "edge": edge if label_style == "banner" else "top",
        "arted": bool(style.get("arted")) and token in _ARTED_TOKENS and label_style != "banner",
        "rating_source": _normalize_rating_source(style.get("rating_source")) if token == "Rating" else "auto",
        "custom_text": _normalize_custom_text(style.get("custom_text"), token=token),
    }


def seed_profiles_from_legacy_settings() -> dict[str, Any]:
    """Build starter layouts from current PLACEHOLDER_POSTER_* toggles."""
    from services.poster_overlay import (
        poster_badge_enabled,
        poster_badge_position,
        poster_banner_position,
        poster_grayscale_enabled,
        poster_placeholder_banner_enabled,
        coming_soon_banner_enabled,
    )

    banner_pos = poster_banner_position()
    badge_pos = poster_badge_position()
    ph_on = poster_placeholder_banner_enabled()
    cs_on = coming_soon_banner_enabled()
    badge_on = poster_badge_enabled()
    gs_on = poster_grayscale_enabled()

    profiles: dict[str, Any] = {}
    for key in PROFILE_KEYS:
        labels: list[dict[str, Any]] = []
        _media, _availability, variant = parse_profile_key(key)
        landscape = key.startswith("episode.")
        if variant == "coming_soon" and cs_on:
            edge = _normalize_edge(banner_pos)
            r, c = _banner_cell(key, edge)
            labels.append(
                _make_label(
                    BANNER_TOKEN,
                    r,
                    c,
                    style="banner",
                    edge=edge,
                    size="medium",
                    fill_opacity=0.65,
                    landscape=landscape,
                )
            )
            labels.append(
                _make_label(
                    "ComingSoonOrNewEp",
                    r,
                    c,
                    size="medium",
                    fill_opacity=0.0,
                    border_opacity=0.0,
                )
            )
        elif variant == "default" and ph_on:
            edge = _normalize_edge(banner_pos)
            r, c = _banner_cell(key, edge)
            labels.append(
                _make_label(
                    BANNER_TOKEN,
                    r,
                    c,
                    style="banner",
                    edge=edge,
                    size="medium",
                    fill_opacity=0.65,
                    landscape=landscape,
                )
            )
            labels.append(
                _make_label(
                    "PLACEHOLDER",
                    r,
                    c,
                    size="medium",
                    fill_opacity=0.0,
                    border_opacity=0.0,
                )
            )
        if badge_on:
            r, c = _badge_cell(key, badge_pos)
            labels.append(
                _make_label(
                    "Badge",
                    r,
                    c,
                    fill_color="#0b111b",  # dark logo variant (yellow on navy)
                    border_color="#000000",
                    border_opacity=0.0,
                    fill_opacity=1.0,
                )
            )
        profiles[key] = normalize_profile(key, {"labels": labels, "grayscale": gs_on})
    return {"version": LAYOUT_DOC_VERSION, "profiles": profiles}


def _source_grid_for_prior_version(prior_version: int, *, landscape: bool) -> tuple[int, int] | None:
    """Return (cols, rows) for a stored prior version, or None when no remap is needed."""
    if prior_version == LAYOUT_DOC_VERSION:
        return None
    kind = "landscape" if landscape else "portrait"
    interim = _PRERELEASE_GRID_BY_VERSION.get(prior_version)
    if interim is None:
        return None
    cols, rows = interim[kind]
    return int(cols), int(rows)


def _scale_label_coords_from_version(
    profile_key: str,
    labels: list[dict[str, Any]],
    *,
    prior_version: int,
) -> list[dict[str, Any]]:
    """Map cells from a prior stored grid onto the current grid (preserve cell centers)."""
    if not labels:
        return labels
    landscape = str(profile_key).startswith("episode.")
    source = _source_grid_for_prior_version(prior_version, landscape=landscape)
    if source is None:
        return labels
    old_c, old_r = source
    cols, rows = grid_size_for_profile(profile_key)
    if old_c == cols and old_r == rows:
        return labels
    scale_c = cols // old_c if old_c > 0 else 1
    scale_r = rows // old_r if old_r > 0 else 1
    use_center_map = scale_c * old_c != cols or scale_r * old_r != rows
    out: list[dict[str, Any]] = []
    for label in labels:
        item = dict(label)
        old_row = int(item.get("row") or 0)
        old_col = int(item.get("col") or 0)
        if use_center_map and old_c > 0 and old_r > 0:
            cx = (old_col + 0.5) / old_c
            cy = (old_row + 0.5) / old_r
            item["col"] = _clamp_int(int(round(cx * cols - 0.5)), 0, cols - 1, 0)
            item["row"] = _clamp_int(int(round(cy * rows - 0.5)), 0, rows - 1, 0)
        else:
            item["row"] = _clamp_int(old_row * max(1, scale_r), 0, rows - 1, 0)
            item["col"] = _clamp_int(old_col * max(1, scale_c), 0, cols - 1, 0)
        out.append(item)
    return out


def normalize_doc(payload: Any, *, seed_if_empty: bool = True) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        return seed_profiles_from_legacy_settings() if seed_if_empty else _empty_doc()
    # Missing version means "already on the shipped schema" (designer may omit it).
    raw_version = payload.get("version")
    if raw_version is None or raw_version == "":
        prior_version = LAYOUT_DOC_VERSION
    else:
        try:
            prior_version = int(raw_version)
        except (TypeError, ValueError):
            prior_version = LAYOUT_DOC_VERSION
    profiles_raw = _rewrite_legacy_profile_keys(payload.get("profiles") or {})
    profiles: dict[str, Any] = {}
    any_labels = False
    for key in PROFILE_KEYS:
        prof = normalize_profile(key, profiles_raw.get(key))
        if prior_version != LAYOUT_DOC_VERSION and prof["labels"]:
            prof = {
                **prof,
                "labels": _scale_label_coords_from_version(
                    key, prof["labels"], prior_version=prior_version
                ),
            }
        if prof["labels"]:
            any_labels = True
        profiles[key] = prof
    # First-time / empty doc: seed from legacy toggles so visual parity holds.
    if seed_if_empty and not any_labels and not any(
        isinstance(profiles_raw.get(k), dict) and "labels" in (profiles_raw.get(k) or {})
        for k in PROFILE_KEYS
    ):
        return seed_profiles_from_legacy_settings()
    return {"version": LAYOUT_DOC_VERSION, "profiles": profiles}


def layout_fingerprint(doc: dict[str, Any] | None = None) -> str:
    """Stable hash of the full layout document (settings dirty / catalog)."""
    data = doc if isinstance(doc, dict) else get_layout_doc()
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def profile_layout_fingerprint(profile_key: str, doc: dict[str, Any] | None = None) -> str:
    """Hash one profile for per-title art sidecars (only that profile invalidates).

    Includes ``version`` so schema migrations still rewrite art once.
    """
    data = doc if isinstance(doc, dict) else get_layout_doc()
    key = str(profile_key or "").strip()
    key = _LEGACY_PROFILE_KEY_MAP.get(key, key)
    if key not in PROFILE_KEYS:
        key = "movie.placeholder.default"
    prof = (data.get("profiles") or {}).get(key) or {"labels": [], "grayscale": False}
    payload = {
        "version": int(data.get("version") or LAYOUT_DOC_VERSION),
        "profile_key": key,
        "profile": prof,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def changed_profile_keys(prior: dict[str, Any] | None, current: dict[str, Any] | None) -> list[str]:
    """Return profile keys whose fingerprint differs between two layout docs."""
    prior_doc = prior if isinstance(prior, dict) else {}
    current_doc = current if isinstance(current, dict) else {}
    changed: list[str] = []
    for key in PROFILE_KEYS:
        if profile_layout_fingerprint(key, prior_doc) != profile_layout_fingerprint(key, current_doc):
            changed.append(key)
    return changed


def profile_has_labels(profile_key: str, doc: dict[str, Any] | None = None) -> bool:
    data = doc if isinstance(doc, dict) else get_layout_doc()
    prof = (data.get("profiles") or {}).get(profile_key) or {}
    labels = prof.get("labels") if isinstance(prof, dict) else None
    return bool(labels)


def layouts_have_any_labels(doc: dict[str, Any] | None = None) -> bool:
    data = doc if isinstance(doc, dict) else get_layout_doc()
    for key in PROFILE_KEYS:
        if profile_has_labels(key, data):
            return True
    return False


def parse_profile_key(profile_key: str) -> tuple[str, str, str]:
    """Return ``(media_kind, availability, variant)`` for a profile key."""
    parts = str(profile_key or "").strip().split(".")
    if len(parts) >= 3:
        media, availability, variant = parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        media, variant = parts[0], parts[1]
        availability = "placeholder"
    else:
        media, availability, variant = "movie", "placeholder", "default"
    if media not in {"movie", "tv", "episode"}:
        media = "movie"
    if availability not in {"placeholder", "real"}:
        availability = "placeholder"
    if variant not in {"default", "coming_soon"}:
        variant = "default"
    return media, availability, variant


def pick_profile_key(
    media_kind: str,
    *,
    coming_soon: bool,
    availability: str = "placeholder",
) -> str:
    kind = str(media_kind or "movie").strip().lower()
    if kind in {"series", "season", "tv", "show"}:
        base = "tv"
    elif kind in {"episode", "thumb"}:
        base = "episode"
    else:
        base = "movie"
    avail = str(availability or "placeholder").strip().lower()
    if avail not in {"placeholder", "real"}:
        avail = "placeholder"
    variant = "coming_soon" if coming_soon else "default"
    return f"{base}.{avail}.{variant}"


def _rewrite_legacy_profile_keys(profiles_raw: Any) -> dict[str, Any]:
    """Map pre-ship ``movie.default`` keys to ``movie.placeholder.default`` and fill gaps."""
    raw = profiles_raw if isinstance(profiles_raw, dict) else {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        mapped = _LEGACY_PROFILE_KEY_MAP.get(str(key), str(key))
        if mapped in out and str(key) in _LEGACY_PROFILE_KEY_MAP:
            # Prefer an already-migrated three-segment key over a leftover two-segment copy.
            continue
        out[mapped] = value
    for key in PROFILE_KEYS:
        if key in out:
            continue
        media, availability, variant = parse_profile_key(key)
        if availability == "real":
            sibling = f"{media}.placeholder.{variant}"
            sibling_val = out.get(sibling)
            if isinstance(sibling_val, dict):
                out[key] = json.loads(json.dumps(sibling_val))
                continue
        out[key] = {"labels": [], "grayscale": False}
    return out


def _load_locked() -> dict[str, Any]:
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == CONFIG_KEY).first()
        raw = row.value if row else None
        return normalize_doc(raw, seed_if_empty=True)
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
            _CACHE.clear()
            _CACHE.update(_load_locked())
        except Exception:
            _CACHE.clear()
            _CACHE.update(seed_profiles_from_legacy_settings())
        _CACHE_LOADED = True


def reload_layout_cache() -> None:
    global _CACHE_LOADED
    with _CACHE_LOCK:
        try:
            _CACHE.clear()
            _CACHE.update(_load_locked())
        except Exception:
            _CACHE.clear()
            _CACHE.update(seed_profiles_from_legacy_settings())
        _CACHE_LOADED = True


def get_layout_doc() -> dict[str, Any]:
    _ensure_loaded()
    with _CACHE_LOCK:
        return json.loads(json.dumps(_CACHE))


def get_profile(profile_key: str) -> dict[str, Any]:
    key = str(profile_key or "").strip()
    key = _LEGACY_PROFILE_KEY_MAP.get(key, key)
    if key not in PROFILE_KEYS:
        key = "movie.placeholder.default"
    doc = get_layout_doc()
    return dict((doc.get("profiles") or {}).get(key) or {"labels": [], "grayscale": False})


def save_layout_doc(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_doc(payload, seed_if_empty=False)
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == CONFIG_KEY).first()
        if row is None:
            row = AppConfig(
                key=CONFIG_KEY,
                value=normalized,
                value_type="json",
                description="Poster/thumb grid layout profiles for placeholder art overlays.",
            )
            session.add(row)
        else:
            row.value = normalized
            row.value_type = "json"
        session.commit()
    finally:
        session.close()
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE.update(normalized)
        global _CACHE_LOADED
        _CACHE_LOADED = True
    return normalized


def ensure_layouts_persisted() -> dict[str, Any]:
    """Load (seeding if needed) and persist so bake fingerprints are stable."""
    doc = get_layout_doc()
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == CONFIG_KEY).first()
        if row is None:
            return save_layout_doc(doc)
    finally:
        session.close()
    return doc


def reset_profile_to_seed(profile_key: str) -> dict[str, Any]:
    key = _LEGACY_PROFILE_KEY_MAP.get(str(profile_key or "").strip(), str(profile_key or "").strip())
    if key not in PROFILE_KEYS:
        raise KeyError(key)
    seeded = seed_profiles_from_legacy_settings()
    doc = get_layout_doc()
    doc["profiles"][key] = seeded["profiles"][key]
    return save_layout_doc(doc)


def _format_runtime_minutes(minutes: Any) -> str:
    try:
        total = int(minutes or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    if total < 60:
        return f"{total}m"
    h, m = divmod(total, 60)
    if m == 0:
        return f"{h}h"
    return f"{h}h {m}m"


def _token_date(value: Any) -> str:
    if value is None or value == "":
        return ""
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            text = str(iso())
            return text[:10] if text else ""
        except Exception:
            return ""
    text = str(value).strip()
    return text[:10] if text else ""


def _genres_short(raw: Any) -> str:
    if isinstance(raw, list):
        parts = [str(g).strip() for g in raw if str(g).strip()]
        return ", ".join(parts[:2])
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return ", ".join(parts[:2])
    return ""


def _format_rating_value(value: Any) -> str:
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    # Keep whole numbers short; one decimal otherwise (8.8, 10).
    if abs(num - round(num)) < 1e-6:
        return str(int(round(num)))
    return f"{num:.1f}".rstrip("0").rstrip(".")


def _normalize_rating_key(name: str) -> str:
    key = str(name or "").strip().lower().replace(" ", "_")
    if key in ("themoviedb",):
        return "tmdb"
    if key in ("rotten_tomatoes", "rottentomatoes"):
        return "rottentomatoes"
    return key


def rating_values_by_source(raw: Any) -> dict[str, str]:
    """Map canonical source → display value from Arr ratings JSON."""
    if isinstance(raw, (int, float)):
        text = _format_rating_value(raw)
        return {"imdb": text} if text else {}
    if not isinstance(raw, dict):
        text = str(raw or "").strip()
        return {"imdb": text} if text else {}

    if any(isinstance(v, dict) for v in raw.values()):
        by_source: dict[str, str] = {}
        for name, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            val = payload.get("value")
            if val is None:
                val = payload.get("Value") or payload.get("rating") or payload.get("Rating")
            text = _format_rating_value(val)
            if not text:
                continue
            by_source[_normalize_rating_key(str(name))] = text
        return by_source

    for key in ("value", "Value", "rating", "Rating"):
        if raw.get(key) is not None:
            text = _format_rating_value(raw.get(key))
            return {"imdb": text} if text else {}
    return {}


def pick_rating(raw: Any, *, preferred_source: str | None = None) -> tuple[str, str]:
    """Return ``(source, display_value)`` from Arr ratings JSON (empty if none)."""
    by_source = rating_values_by_source(raw)
    if not by_source:
        return "", ""
    prefer = _normalize_rating_source(preferred_source)
    if prefer != "auto" and prefer in by_source:
        return prefer, by_source[prefer]
    for key in _RATING_SOURCE_PRIORITY:
        canon = _normalize_rating_key(key)
        if canon in by_source:
            return canon, by_source[canon]
    source, text = next(iter(by_source.items()))
    return source, text


def _attach_rating_tokens(out: dict[str, str], raw: Any, *, preferred_source: str | None = None) -> None:
    by_source = rating_values_by_source(raw)
    for source, text in by_source.items():
        out[f"Rating:{source}"] = text
    source, text = pick_rating(raw, preferred_source=preferred_source)
    out["Rating"] = text
    out["RatingSource"] = source


def series_status_label(raw_status: Any) -> str:
    key = str(raw_status or "").strip().lower()
    if not key:
        return ""
    if key in _SERIES_STATUS_LABELS:
        return _SERIES_STATUS_LABELS[key]
    # Title-case unknown Sonarr statuses for readability.
    return key.replace("_", " ").title()


def resolve_label_tokens(
    *,
    media_kind: str,
    movie: Any = None,
    series: Any = None,
    season: Any = None,
    episode: Any = None,
    coming_soon_label: str | None = None,
) -> dict[str, str]:
    """Build token → display string map for layout compose (empty means omit label)."""
    kind = str(media_kind or "movie").strip().lower()
    out: dict[str, str] = {
        "Banner": "",  # textless fill strip; chips supply copy
        "PLACEHOLDER": "PLACEHOLDER",
        "ComingSoonOrNewEp": str(coming_soon_label or "").strip(),
        "Badge": "badge",  # non-empty keeps the badge label; draw uses the logo asset
    }

    if kind == "movie" and movie is not None:
        title = str(getattr(movie, "title", "") or "").strip()
        year = getattr(movie, "year", None)
        out["Title"] = title
        out["Year"] = str(year) if year else ""
        out["Certification"] = str(getattr(movie, "radarr_certification", "") or "").strip()
        out["Genres"] = _genres_short(getattr(movie, "radarr_genres", None))
        out["Runtime"] = _format_runtime_minutes(getattr(movie, "radarr_runtime", None))
        out["TheatricalDate"] = _token_date(getattr(movie, "theater_release_date", None))
        out["DigitalDate"] = _token_date(getattr(movie, "digital_release_date", None))
        out["PhysicalDate"] = _token_date(getattr(movie, "physical_release_date", None))
        _attach_rating_tokens(out, getattr(movie, "radarr_ratings", None))
        out["SeriesStatus"] = ""
        out["AirDate"] = ""
        out["SeriesPremiereDate"] = ""
        out["SXXEYY"] = ""
        return out

    # TV / episode
    series_title = str(getattr(series, "title", "") or "").strip() if series is not None else ""
    ep_title = str(getattr(episode, "title", "") or "").strip() if episode is not None else ""
    out["Title"] = series_title or ep_title
    year_val = None
    if episode is not None:
        year_val = getattr(episode, "year", None)
    if year_val is None and series is not None:
        year_val = getattr(series, "year", None)
    out["Year"] = str(year_val) if year_val else ""
    cert = ""
    genres = None
    runtime = None
    ratings = None
    if series is not None:
        cert = str(getattr(series, "sonarr_certification", "") or "").strip()
        genres = getattr(series, "sonarr_genres", None)
        runtime = getattr(series, "sonarr_runtime", None)
        ratings = getattr(series, "sonarr_ratings", None)
        out["SeriesStatus"] = series_status_label(getattr(series, "sonarr_status", None))
        out["SeriesPremiereDate"] = _token_date(getattr(series, "sonarr_first_aired", None))
    else:
        out["SeriesStatus"] = ""
        out["SeriesPremiereDate"] = ""
    if episode is not None:
        runtime = getattr(episode, "sonarr_runtime", None) or runtime
        out["AirDate"] = _token_date(getattr(episode, "air_date", None))
        sn = int(getattr(season, "season_number", 0) or 0) if season is not None else 0
        en = int(getattr(episode, "episode_number", 0) or 0)
        out["SXXEYY"] = f"S{sn:02d}E{en:02d}" if sn or en else ""
    else:
        out["AirDate"] = ""
        out["SXXEYY"] = ""
    out["Certification"] = cert
    out["Genres"] = _genres_short(genres)
    out["Runtime"] = _format_runtime_minutes(runtime)
    _attach_rating_tokens(out, ratings)
    out["TheatricalDate"] = ""
    out["DigitalDate"] = ""
    out["PhysicalDate"] = ""
    return out


def sample_tokens_for_profile(profile_key: str) -> dict[str, str]:
    """Demo tokens for Settings preview / designer sizing (prefer longest realistic values).

    Coming Soon / New Ep. samples are always filled (even on ``*.default`` profiles) so
    placing that label in the designer still shows the longest example chip. Real bake
    still omits empty tokens per title.
    """
    kind = str(profile_key or "movie.placeholder.default").split(".", 1)[0]
    # Prefer longest common forms so layout chips are sized for worst-case copy.
    base: dict[str, str] = {
        "Banner": "",
        "PLACEHOLDER": "PLACEHOLDER",
        "ComingSoonOrNewEp": "Theatrical - Sep 30",
        "Custom": "{Title} {Sep} {Year}",
        "Badge": "badge",
        "Title": "The Super Mario Galaxy Movie",
        "Sep": "·",
        "Year": "2026",
        "Certification": "PG-13",
        "Genres": "Science Fiction, Adventure",
        "Runtime": "2h 28m",
        "TheatricalDate": "2026-04-12",
        "DigitalDate": "2026-12-07",
        "PhysicalDate": "2026-12-07",
        "AirDate": "2008-01-20",
        "SeriesPremiereDate": "2008-01-20",
        "SXXEYY": "S10E24",
        # 8.8 is as wide as typical Arr ratings; whole numbers render as "10" not "10.0".
        "Rating": "8.8",
        "RatingSource": "imdb",
        "Rating:imdb": "8.8",
        "Rating:tmdb": "7.4",
        "Rating:trakt": "8.1",
        "Rating:rottentomatoes": "91%",
        "Rating:metacritic": "78",
        "SeriesStatus": "Returning",
    }
    if kind == "movie":
        base["ComingSoonOrNewEp"] = "Theatrical - Sep 30"
    elif kind == "episode":
        base["Title"] = "Ozymandias"
        base["Year"] = "2013"
        base["Certification"] = "TV-MA"
        base["Genres"] = "Science Fiction, Adventure"
        base["Runtime"] = "47m"
        base["ComingSoonOrNewEp"] = "Airing - Sep 30"
    else:
        base["Title"] = "Breaking Bad"
        base["Year"] = "2008"
        base["Certification"] = "TV-MA"
        base["Genres"] = "Science Fiction, Adventure"
        base["Runtime"] = "47m"
        base["SeriesStatus"] = "Returning"
        base["ComingSoonOrNewEp"] = "New Ep. - Sep 30"
    return base


def serialize_layout_catalog() -> dict[str, Any]:
    """API payload for the designer (profiles, tokens, grids, sample strings)."""
    doc = get_layout_doc()
    token_meta = [
        {"name": "Banner", "label": "Banner (fill strip)"},
        {"name": "PLACEHOLDER", "label": "PLACEHOLDER"},
        {"name": "ComingSoonOrNewEp", "label": "Coming Soon / New Ep. / Airing"},
        {"name": "Custom", "label": "Custom text"},
        {"name": "Badge", "label": "Placeholdarr badge"},
        {"name": "SeriesStatus", "label": "Series status"},
        {"name": "Title", "label": "Title"},
        {"name": "Year", "label": "Year"},
        {"name": "Certification", "label": "Certification"},
        {"name": "Genres", "label": "Genres"},
        {"name": "Runtime", "label": "Runtime"},
        {"name": "TheatricalDate", "label": "Theatrical date"},
        {"name": "DigitalDate", "label": "Digital date"},
        {"name": "PhysicalDate", "label": "Physical date"},
        {"name": "AirDate", "label": "Air date"},
        {"name": "SeriesPremiereDate", "label": "Series premiere date"},
        {"name": "SXXEYY", "label": "SxxEyy"},
        {"name": "Rating", "label": "Rating"},
    ]
    grids = {key: {"cols": grid_size_for_profile(key)[0], "rows": grid_size_for_profile(key)[1]} for key in PROFILE_KEYS}
    allowed = {key: sorted(tokens_for_profile(key)) for key in PROFILE_KEYS}
    samples = {key: sample_tokens_for_profile(key) for key in PROFILE_KEYS}
    custom_tokens = {key: custom_template_tokens_for_profile(key) for key in PROFILE_KEYS}
    return {
        "version": LAYOUT_DOC_VERSION,
        "profiles": doc.get("profiles") or {},
        "profile_keys": list(PROFILE_KEYS),
        "tokens": token_meta,
        "allowed_tokens": allowed,
        "sample_tokens": samples,
        "custom_template_tokens": custom_tokens,
        "custom_text_max_length": _CUSTOM_TEXT_MAX_LEN,
        "grids": grids,
        "sizes": list(LABEL_SIZES),
        "styles": list(LABEL_STYLES),
        "banner_edges": list(BANNER_EDGES),
        "arted_tokens": sorted(_ARTED_TOKENS),
        "rating_sources": list(_RATING_SOURCES),
        "fingerprint": layout_fingerprint(doc),
    }
