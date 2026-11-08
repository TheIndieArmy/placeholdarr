"""Poster / thumb layout profiles for the label designer.

Stores a versioned JSON document in AppConfig under ``PLACEHOLDER_POSTER_LAYOUTS``.
Twelve profiles: movie|tv|episode × placeholder|real × default|coming_soon.
Season posters use TV profiles. Bake uses placeholder today; real is designer-ready.
Chip placement is fractional ``x``/``y`` (0..1); legacy row/col cells migrate on load.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import uuid
from datetime import date, datetime
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
# ``FillRectangle`` is a textless fill box; place chips on top for copy.
# Placeable designer chips: Text (Custom), Fill, Badge, Rating. Metadata lives in Text templates.
LABEL_TOKENS: tuple[str, ...] = (
    "FillRectangle",
    "Custom",
    "Badge",
    "Rating",
)

MOVIE_TOKENS = frozenset(LABEL_TOKENS)
TV_TOKENS = frozenset(LABEL_TOKENS)
EPISODE_TOKENS = frozenset(LABEL_TOKENS)

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
# Insert-chip order for Text templates (media filters apply below).
_CUSTOM_TOKEN_ORDER: tuple[str, ...] = (
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
)
_MOVIE_CUSTOM_TOKENS = frozenset(
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
        "Rating",
        "Sep",
    }
)
_TV_CUSTOM_TOKENS = frozenset(
    {
        "PLACEHOLDER",
        "ComingSoonOrNewEp",
        "Title",
        "Year",
        "Certification",
        "Genres",
        "Runtime",
        "AirDate",
        "SeriesPremiereDate",
        "SXXEYY",
        "SeriesStatus",
        "Rating",
        "Sep",
    }
)
_EPISODE_CUSTOM_TOKENS = frozenset(
    {
        "PLACEHOLDER",
        "ComingSoonOrNewEp",
        "Title",
        "Year",
        "Certification",
        "Genres",
        "Runtime",
        "AirDate",
        "SeriesPremiereDate",
        "SXXEYY",
        "SeriesStatus",
        "Rating",
        "Sep",
    }
)
# Pre-cut placeables (and Coming Soon) → Text with ``{Token}`` on load.
_LEGACY_SINGLE_TOKEN_CHIPS: frozenset[str] = frozenset(
    {
        "ComingSoonOrNewEp",
        "PLACEHOLDER",
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
    }
)
# ``{Token}`` or ``{DateToken:format}`` (format is optional; date tokens only).
_CUSTOM_TOKEN_PATTERN = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)(?::([A-Za-z0-9_-]+))?\}")
_CUSTOM_DATE_TOKENS: frozenset[str] = frozenset(
    {
        "TheatricalDate",
        "DigitalDate",
        "PhysicalDate",
        "AirDate",
        "SeriesPremiereDate",
    }
)
# Composite ``Label - date`` stamps; ``:format`` rewrites only the date half.
_CUSTOM_STAMPED_DATE_TOKENS: frozenset[str] = frozenset({"ComingSoonOrNewEp"})
# Format ids in templates. Numeric uses regional patterns (not a sample date).
_CUSTOM_DATE_FORMATS: tuple[str, ...] = ("iso", "short", "medium", "long", "mdy", "dmy", "ymd")
_CUSTOM_DATE_FORMAT_DEFAULT = "iso"
# ``numeric`` was US-style during early drafts.
_CUSTOM_DATE_FORMAT_ALIASES: dict[str, str] = {"numeric": "mdy"}

_DEFAULT_TEXT = "#ffffff"
_DEFAULT_FILL = "#000000"
_DEFAULT_BORDER = "#000000"
_DEFAULT_FILL_OPACITY = 0.65
_DEFAULT_BORDER_OPACITY = 0.0
LABEL_SIZES: tuple[str, ...] = ("small", "medium", "large")
# Relative to small. Placeholdarr badge defaults to medium (matches legacy corner size).
_SIZE_MULT: dict[str, float] = {"small": 1.0, "medium": 1.5, "large": 2.0}
_SIZE_SCALE_MIN = 0.5
_SIZE_SCALE_MAX = 2.5
# Arted Rating logo placement relative to the score.
_RATING_ART_LAYOUTS: tuple[str, ...] = ("left", "right", "above", "below", "fill")
_RATING_ART_LAYOUT_DEFAULT = "left"
# Chip = content-sized pill. FillRectangle = textless fill box (style still ``banner``).
LABEL_STYLES: tuple[str, ...] = ("chip", "banner")
BANNER_EDGES: tuple[str, ...] = ("top", "bottom", "left", "right")  # legacy migrate only
BANNER_ORIENTATIONS: tuple[str, ...] = ("horizontal", "vertical")  # legacy migrate only
FILL_RECTANGLE_TOKEN = "FillRectangle"
BANNER_TOKEN = FILL_RECTANGLE_TOKEN  # alias
_LEGACY_BANNER_TOKEN = "Banner"
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


def _clamp_frac(value: Any, default: float = 0.5) -> float:
    """Clamp a poster-relative coordinate to ``[0, 1]``."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(n):
        return default
    return max(0.0, min(1.0, n))


def _xy_from_raw(raw: dict[str, Any], *, cols: int, rows: int) -> tuple[float, float]:
    """Prefer stored ``x``/``y``; otherwise migrate from snap-cell centers."""
    has_x = raw.get("x") is not None
    has_y = raw.get("y") is not None
    if has_x or has_y:
        return _clamp_frac(raw.get("x"), 0.5), _clamp_frac(raw.get("y"), 0.5)
    c = max(1, int(cols))
    r = max(1, int(rows))
    row = _clamp_int(raw.get("row"), 0, r - 1, 0)
    col = _clamp_int(raw.get("col"), 0, c - 1, 0)
    return (col + 0.5) / c, (row + 0.5) / r


def _cell_from_xy(x: float, y: float, *, cols: int, rows: int) -> tuple[int, int]:
    """Nearest snap cell for legacy row/col fields (banners / occupancy helpers)."""
    c = max(1, int(cols))
    r = max(1, int(rows))
    col = _clamp_int(int(round(float(x) * c - 0.5)), 0, c - 1, 0)
    row = _clamp_int(int(round(float(y) * r - 0.5)), 0, r - 1, 0)
    return row, col


def _edge_anchor_xy(edge: str) -> tuple[float, float]:
    """Representative center for a banner edge (chips use free ``x``/``y`` instead)."""
    e = _normalize_edge(edge)
    if e == "bottom":
        return 0.5, 1.0
    if e == "left":
        return 0.0, 0.5
    if e == "right":
        return 1.0, 0.5
    return 0.5, 0.0


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
    return "medium" if token in {"Badge", FILL_RECTANGLE_TOKEN, _LEGACY_BANNER_TOKEN} else "small"


def _normalize_custom_text(raw: Any, *, token: str) -> str:
    """User-entered chip template for the Custom token; ignored for every other token."""
    if token != CUSTOM_TOKEN:
        return ""
    text = str(raw or "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) > _CUSTOM_TEXT_MAX_LEN:
        text = text[:_CUSTOM_TEXT_MAX_LEN].rstrip()
    return text


def custom_template_tokens_for_profile(profile_key: str) -> list[str]:
    """Ordered `{Token}` names allowed in Text (Custom) chips for this profile."""
    media, _, _ = parse_profile_key(profile_key)
    if media == "movie":
        allowed = _MOVIE_CUSTOM_TOKENS
    elif media == "episode":
        allowed = _EPISODE_CUSTOM_TOKENS
    else:
        allowed = _TV_CUSTOM_TOKENS
    return [name for name in _CUSTOM_TOKEN_ORDER if name in allowed]


def _parse_token_date_value(raw: Any) -> date | None:
    """Parse an ISO date (or date-like) from a layout token string."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    # Prefer YYYY-MM-DD (token map / samples).
    head = text[:10]
    try:
        return date.fromisoformat(head)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _normalize_custom_date_format(fmt: str | None) -> str:
    key = str(fmt or "").strip().lower() or _CUSTOM_DATE_FORMAT_DEFAULT
    key = _CUSTOM_DATE_FORMAT_ALIASES.get(key, key)
    if key not in _CUSTOM_DATE_FORMATS:
        return _CUSTOM_DATE_FORMAT_DEFAULT
    return key


def format_coming_soon_or_new_ep_token(label: Any, date_raw: Any, fmt: str | None = None) -> str:
    """Rewrite the date half of a ``Theatrical - Sep 30`` / ``New Ep. - Sep 30`` stamp."""
    text = str(label or "").strip()
    if not text:
        return ""
    if not fmt:
        return text
    iso = str(date_raw or "").strip()
    if not iso:
        return text
    formatted = format_custom_date_token(iso, fmt)
    if not formatted:
        return text
    if " - " in text:
        return f"{text.split(' - ', 1)[0]} - {formatted}"
    return formatted


def format_custom_date_token(raw: Any, fmt: str | None = None) -> str:
    """Format a date token value; blank input stays blank.

    Formats: ``iso`` (default), ``short``, ``medium``, ``long``, ``mdy`` (MM/DD/YYYY),
    ``dmy`` (DD/MM/YYYY), ``ymd`` (YYYY/MM/DD). Legacy ``numeric`` aliases to ``mdy``.
    """
    parsed = _parse_token_date_value(raw)
    if parsed is None:
        return ""
    key = _normalize_custom_date_format(fmt)
    if key == "iso":
        return parsed.isoformat()
    from services.layout_locale import format_layout_month_day, format_layout_month_day_year

    if key == "short":
        # Sep 30 (layout locale tables; not container LANG)
        return format_layout_month_day(parsed)
    if key == "medium":
        # Sep 30, 2026
        return format_layout_month_day_year(parsed)
    if key == "long":
        # September 30, 2026
        return format_layout_month_day_year(parsed, long_month=True)
    if key == "mdy":
        # 9/30/2026 (US)
        return f"{parsed.month}/{parsed.day}/{parsed.year}"
    if key == "dmy":
        # 30/9/2026 (day-first)
        return f"{parsed.day}/{parsed.month}/{parsed.year}"
    if key == "ymd":
        # 2026/09/30 (year-first, zero-padded like ISO)
        return f"{parsed.year}/{parsed.month:02d}/{parsed.day:02d}"
    return parsed.isoformat()


def custom_date_format_options(*, sample: date | None = None) -> list[dict[str, str]]:
    """UI catalog: format id, label, and example/pattern for the date-token picker.

    Wordy formats show a sample date. Numeric formats show ``MM/DD/YYYY`` / ``DD/MM/YYYY`` /
    ``YYYY/MM/DD``.
    """
    sample_date = sample or date(2026, 9, 30)
    # Numeric rows: pattern string (not a filled-in sample).
    patterns = {
        "iso": None,
        "short": None,
        "medium": None,
        "long": None,
        "mdy": "MM/DD/YYYY",
        "dmy": "DD/MM/YYYY",
        "ymd": "YYYY/MM/DD",
    }
    labels = {
        "iso": "ISO",
        "short": "Short",
        "medium": "Medium",
        "long": "Long",
        "mdy": "Numeric",
        "dmy": "Numeric",
        "ymd": "Numeric",
    }
    out: list[dict[str, str]] = []
    for fmt in _CUSTOM_DATE_FORMATS:
        pattern = patterns.get(fmt)
        out.append(
            {
                "id": fmt,
                "label": labels.get(fmt, fmt),
                "example": pattern if pattern else format_custom_date_token(sample_date, fmt),
            }
        )
    return out


def render_custom_chip_text(
    template: str,
    token_map: dict[str, str] | None = None,
    *,
    separator: str = _CUSTOM_SEP_DEFAULT,
) -> str:
    """Fill a Custom chip template from static layout tokens; empty values collapse.

    Supports ``{Title}``, ``{Year}``, ``{Sep}``, and the other poster metadata tokens.
    Date tokens may include a format: ``{TheatricalDate:short}``.
    Missing / blank tokens drop out (same idea as Status Updates).
    """
    text = str(template or "")
    if not text.strip():
        return ""
    ctx = dict(token_map or {})
    sep = str(separator or _CUSTOM_SEP_DEFAULT).strip() or _CUSTOM_SEP_DEFAULT

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        fmt = match.group(2)
        if name == "Sep":
            return _CUSTOM_SEP_SENTINEL
        if name not in _CUSTOM_TEMPLATE_TOKENS:
            return ""
        raw = str(ctx.get(name) or "").strip()
        if not raw:
            return ""
        if name in _CUSTOM_STAMPED_DATE_TOKENS:
            # Bare stamp keeps overlay short style; ``:format`` rewrites the date half.
            return format_coming_soon_or_new_ep_token(raw, ctx.get("ComingSoonOrNewEpDate"), fmt)
        if name in _CUSTOM_DATE_TOKENS:
            # Bare ``{TheatricalDate}`` keeps ISO (legacy); ``:format`` reformats.
            if fmt:
                return format_custom_date_token(raw, fmt)
            return format_custom_date_token(raw, _CUSTOM_DATE_FORMAT_DEFAULT)
        return raw

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
_BANNER_THICKNESS_MAX = 0.45  # legacy edge-thickness clamp only
_BANNER_SIZE_MULT = {"small": 0.72, "medium": 1.0, "large": 1.35}
_BANNER_SPAN_MIN_GAP = 0.01


def _normalize_banner_span(start: Any, end: Any) -> tuple[float, float]:
    """Axis coverage as fractions 0..1 (full axis is 0..1)."""

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


def banner_base_frac(edge: str = "top", *, landscape: bool, orientation: str | None = None) -> float:
    """Medium-tier default strip depth (legacy edge or orientation)."""
    orient = str(orientation or "").strip().lower()
    if orient == "vertical" or str(edge or "top").strip().lower() in {"left", "right"}:
        return 0.14 if landscape else 0.16
    return 0.18 if landscape else 0.11


def _normalize_banner_thickness(
    raw: Any,
    *,
    size: str,
    edge: str = "top",
    landscape: bool,
    orientation: str | None = None,
) -> float:
    """Legacy single-depth clamp (migration from edge banners)."""
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
    base = banner_base_frac(edge, landscape=landscape, orientation=orientation)
    mult = float(_BANNER_SIZE_MULT.get(str(size or "medium").strip().lower(), 1.0))
    return max(_BANNER_THICKNESS_MIN, min(_BANNER_THICKNESS_MAX, base * mult))


def _fill_rect_geometry_from_raw(
    raw: dict[str, Any],
    *,
    size: str,
    landscape: bool,
    row: int,
    col: int,
    cols: int,
    rows: int,
) -> tuple[float, float, float, float]:
    """Return x_start/end, y_start/end (migrate x/y, orientation+span/depth, or edge+thickness)."""
    has_xy = (
        raw.get("x_start") is not None
        or raw.get("x_end") is not None
        or raw.get("y_start") is not None
        or raw.get("y_end") is not None
    )
    if has_xy:
        x0, x1 = _normalize_banner_span(raw.get("x_start"), raw.get("x_end"))
        y0, y1 = _normalize_banner_span(raw.get("y_start"), raw.get("y_end"))
        return x0, x1, y0, y1

    span_start, span_end = _normalize_banner_span(raw.get("span_start"), raw.get("span_end"))
    has_depth = raw.get("depth_start") is not None or raw.get("depth_end") is not None
    orient_raw = str(raw.get("orientation") or "").strip().lower()
    has_orient = orient_raw in BANNER_ORIENTATIONS
    edge_raw = raw.get("edge")
    has_legacy_edge = edge_raw is not None and str(edge_raw).strip() != ""

    if has_depth or (has_orient and not has_legacy_edge):
        orientation = orient_raw if has_orient else "horizontal"
        if has_depth:
            depth_start, depth_end = _normalize_banner_span(raw.get("depth_start"), raw.get("depth_end"))
        else:
            thick = _normalize_banner_thickness(
                raw.get("thickness"),
                size=size,
                edge="top",
                landscape=landscape,
                orientation=orientation,
            )
            depth_start, depth_end = 0.0, thick
        if orientation == "vertical":
            return depth_start, depth_end, span_start, span_end
        return span_start, span_end, depth_start, depth_end

    edge = (
        _normalize_edge(edge_raw)
        if has_legacy_edge
        else infer_banner_edge(row, col, cols=cols, rows=rows)
    )
    thick = _normalize_banner_thickness(raw.get("thickness"), size=size, edge=edge, landscape=landscape)
    if edge == "bottom":
        return span_start, span_end, max(0.0, 1.0 - thick), 1.0
    if edge == "left":
        return 0.0, thick, span_start, span_end
    if edge == "right":
        return max(0.0, 1.0 - thick), 1.0, span_start, span_end
    return span_start, span_end, 0.0, thick


def _fill_rect_center_xy(x_start: float, x_end: float, y_start: float, y_end: float) -> tuple[float, float]:
    return (x_start + x_end) / 2.0, (y_start + y_end) / 2.0


# Back-compat alias for overlay callers that still import the old name.
def _banner_geometry_from_raw(
    raw: dict[str, Any],
    *,
    size: str,
    landscape: bool,
    row: int,
    col: int,
    cols: int,
    rows: int,
) -> tuple[float, float, float, float]:
    return _fill_rect_geometry_from_raw(
        raw, size=size, landscape=landscape, row=row, col=col, cols=cols, rows=rows
    )


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


def _normalize_fill_rectangle_token(token: str) -> str:
    """Map legacy ``Banner`` token to ``FillRectangle``."""
    if token in {_LEGACY_BANNER_TOKEN, FILL_RECTANGLE_TOKEN, "Fill rectangle", "fill_rectangle"}:
        return FILL_RECTANGLE_TOKEN
    return token


def _normalize_style(raw: Any, *, token: str) -> str:
    """FillRectangle is always a fill box; every other token is a chip."""
    if token == FILL_RECTANGLE_TOKEN or token == _LEGACY_BANNER_TOKEN:
        return "banner"
    text = str(raw or "").strip().lower()
    if text in {"banner", "fill", "fill_rectangle", "fillrectangle"}:
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


def size_multiplier(size: Any) -> float:
    """Resolve a size enum or numeric scale to a chip/badge multiplier."""
    if isinstance(size, (int, float)) and not isinstance(size, bool):
        return max(_SIZE_SCALE_MIN, min(_SIZE_SCALE_MAX, float(size)))
    text = str(size or "small").strip().lower()
    if text in _SIZE_MULT:
        return float(_SIZE_MULT[text])
    try:
        return max(_SIZE_SCALE_MIN, min(_SIZE_SCALE_MAX, float(text)))
    except (TypeError, ValueError):
        return float(_SIZE_MULT["small"])


def nearest_size_preset(scale: float) -> str:
    """Map a continuous scale to the closest small/medium/large preset id."""
    target = max(_SIZE_SCALE_MIN, min(_SIZE_SCALE_MAX, float(scale)))
    return min(_SIZE_MULT.items(), key=lambda item: abs(item[1] - target))[0]


def normalize_size_scale(raw: Any, *, size: str) -> float:
    """Continuous size scale; falls back to the enum multiplier when unset."""
    if raw is not None and raw != "":
        try:
            return max(_SIZE_SCALE_MIN, min(_SIZE_SCALE_MAX, float(raw)))
        except (TypeError, ValueError):
            pass
    return float(_SIZE_MULT.get(str(size or "small").strip().lower(), 1.0))


def normalize_rating_art_layout(raw: Any, *, arted: bool) -> str:
    if not arted:
        return _RATING_ART_LAYOUT_DEFAULT
    key = str(raw or "").strip().lower()
    if key in _RATING_ART_LAYOUTS:
        return key
    # Unreleased draft used ``beside``; treat as left-of-score.
    if key in {"beside", "inline", "side"}:
        return "left"
    if key in {"top"}:
        return "above"
    if key in {"bottom"}:
        return "below"
    if key in {"background", "bg"}:
        return "fill"
    return _RATING_ART_LAYOUT_DEFAULT


def normalize_label(raw: Any, *, cols: int, rows: int, landscape: bool = False) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    token = _normalize_fill_rectangle_token(str(raw.get("token") or "").strip())
    custom_text_raw = raw.get("custom_text")
    # Former single-value placeables → Text with ``{Token}`` (Coming Soon included).
    if token in _LEGACY_SINGLE_TOKEN_CHIPS:
        if not str(custom_text_raw or "").strip():
            custom_text_raw = f"{{{token}}}"
        token = CUSTOM_TOKEN
    if token not in LABEL_TOKENS:
        return None
    label_id = str(raw.get("id") or "").strip() or _new_label_id()
    border_color, border_opacity = _border_color_and_opacity(raw)
    arted = bool(raw.get("arted")) and token in _ARTED_TOKENS
    style = _normalize_style(raw.get("style"), token=token)
    size = _normalize_size(raw.get("size"), token=token)
    size_scale = normalize_size_scale(raw.get("size_scale"), size=size)
    # Keep enum aligned with the continuous scale for fill-rectangle thickness presets.
    size = nearest_size_preset(size_scale) if raw.get("size_scale") is not None else size
    rating_art_layout = normalize_rating_art_layout(raw.get("rating_art_layout"), arted=arted)
    x, y = _xy_from_raw(raw, cols=cols, rows=rows)
    row, col = _cell_from_xy(x, y, cols=cols, rows=rows)
    if style == "banner":
        arted = False
        rating_art_layout = _RATING_ART_LAYOUT_DEFAULT
        border_opacity = 0.0
        x_start, x_end, y_start, y_end = _fill_rect_geometry_from_raw(
            raw, size=size, landscape=landscape, row=row, col=col, cols=cols, rows=rows
        )
        x, y = _fill_rect_center_xy(x_start, x_end, y_start, y_end)
        row, col = _cell_from_xy(x, y, cols=cols, rows=rows)
        thickness = max(_BANNER_SPAN_MIN_GAP, y_end - y_start)
    else:
        thickness = banner_base_frac("top", landscape=landscape)
        x_start, x_end = 0.0, 1.0
        y_start, y_end = 0.0, thickness
    return {
        "id": label_id,
        "token": token,
        "x": x,
        "y": y,
        "row": row,
        "col": col,
        "text_color": _hex_color(raw.get("text_color"), _DEFAULT_TEXT),
        "fill_color": _hex_color(raw.get("fill_color"), _DEFAULT_FILL),
        "border_color": border_color,
        "border_opacity": border_opacity,
        "fill_opacity": _opacity(raw.get("fill_opacity"), _DEFAULT_FILL_OPACITY),
        "size": size,
        "size_scale": size_scale,
        "thickness": thickness,
        "x_start": x_start,
        "x_end": x_end,
        "y_start": y_start,
        "y_end": y_end,
        "style": style,
        "arted": arted,
        "rating_source": _rating_source_for_label(token=token, arted=arted, raw=raw.get("rating_source")),
        "rating_art_layout": rating_art_layout if token == "Rating" else _RATING_ART_LAYOUT_DEFAULT,
        "custom_text": _normalize_custom_text(custom_text_raw, token=token),
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
        if style == "banner" and token and token not in {FILL_RECTANGLE_TOKEN, _LEGACY_BANNER_TOKEN, "Badge"}:
            row = _clamp_int(item.get("row"), 0, rows - 1, 0)
            col = _clamp_int(item.get("col"), 0, cols - 1, 0)
            edge = (
                _normalize_edge(item.get("edge"))
                if item.get("edge") is not None
                else infer_banner_edge(row, col, cols=cols, rows=rows)
            )
            anchor_r, anchor_c = _edge_anchor_cell(edge, cols=cols, rows=rows)
            ax, ay = _edge_anchor_xy(edge)
            banner = {
                **item,
                "id": _new_label_id(),
                "token": FILL_RECTANGLE_TOKEN,
                "style": "banner",
                "edge": edge,
                "x": ax,
                "y": ay,
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
                "x": ax,
                "y": ay,
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
    if isinstance(labels_in, list):
        for item in _expand_legacy_text_banners(labels_in, cols=cols, rows=rows):
            norm = normalize_label(item, cols=cols, rows=rows, landscape=landscape)
            if norm is None:
                continue
            if norm["token"] not in allowed:
                continue
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
        "art_library_synced": False,
    }


def _banner_cell(profile_key: str, position: str) -> tuple[int, int]:
    cols, rows = grid_size_for_profile(profile_key)
    return _edge_anchor_cell(_normalize_edge(position), cols=cols, rows=rows)


def _badge_xy(profile_key: str, position: str) -> tuple[float, float]:
    """Legacy corner badge → fractional center of the old corner snap cell."""
    cols, rows = grid_size_for_profile(profile_key)
    pos = str(position or "bottom_right").strip().lower().replace("-", "_")
    top = pos.startswith("top_")
    left = pos.endswith("_left")
    row = 0 if top else rows - 1
    col = 0 if left else cols - 1
    return (col + 0.5) / cols, (row + 0.5) / rows


def _make_label(token: str, row: int = 0, col: int = 0, **style: Any) -> dict[str, Any]:
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
    size = _normalize_size(style.get("size"), token=token)
    landscape = bool(style.get("landscape"))
    cols = LANDSCAPE_COLS if landscape else PORTRAIT_COLS
    rows = LANDSCAPE_ROWS if landscape else PORTRAIT_ROWS
    token = _normalize_fill_rectangle_token(token)
    if label_style == "banner":
        x_start, x_end, y_start, y_end = _fill_rect_geometry_from_raw(
            {
                "orientation": style.get("orientation"),
                "edge": style.get("edge"),
                "thickness": style.get("thickness"),
                "span_start": style.get("span_start"),
                "span_end": style.get("span_end"),
                "depth_start": style.get("depth_start"),
                "depth_end": style.get("depth_end"),
                "x_start": style.get("x_start"),
                "x_end": style.get("x_end"),
                "y_start": style.get("y_start"),
                "y_end": style.get("y_end"),
            },
            size=size,
            landscape=landscape,
            row=row,
            col=col,
            cols=cols,
            rows=rows,
        )
        thickness = max(_BANNER_SPAN_MIN_GAP, y_end - y_start)
        x, y = _fill_rect_center_xy(x_start, x_end, y_start, y_end)
        row, col = _cell_from_xy(x, y, cols=cols, rows=rows)
    else:
        thickness = banner_base_frac("top", landscape=landscape)
        x_start, x_end = 0.0, 1.0
        y_start, y_end = 0.0, thickness
        if style.get("x") is not None or style.get("y") is not None:
            x = _clamp_frac(style.get("x"), 0.5)
            y = _clamp_frac(style.get("y"), 0.5)
        else:
            x = (col + 0.5) / max(1, cols)
            y = (row + 0.5) / max(1, rows)
    return {
        "id": _new_label_id(),
        "token": token,
        "x": x,
        "y": y,
        "row": row,
        "col": col,
        "text_color": style.get("text_color", _DEFAULT_TEXT),
        "fill_color": style.get("fill_color", _DEFAULT_FILL),
        "border_color": border_color[:7] if str(border_color).startswith("#") else border_color,
        "border_opacity": float(border_opacity) if label_style != "banner" else 0.0,
        "fill_opacity": style.get("fill_opacity", _DEFAULT_FILL_OPACITY),
        "size": size,
        "thickness": thickness,
        "x_start": x_start,
        "x_end": x_end,
        "y_start": y_start,
        "y_end": y_end,
        "style": label_style,
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
            bx, by = _edge_anchor_xy(edge)
            labels.append(
                _make_label(
                    BANNER_TOKEN,
                    style="banner",
                    edge=edge,
                    size="medium",
                    fill_opacity=0.65,
                    landscape=landscape,
                )
            )
            labels.append(
                _make_label(
                    CUSTOM_TOKEN,
                    size="medium",
                    fill_opacity=0.0,
                    border_opacity=0.0,
                    x=bx,
                    y=by,
                    landscape=landscape,
                    custom_text="{ComingSoonOrNewEp}",
                )
            )
        elif variant == "default" and ph_on:
            edge = _normalize_edge(banner_pos)
            bx, by = _edge_anchor_xy(edge)
            labels.append(
                _make_label(
                    BANNER_TOKEN,
                    style="banner",
                    edge=edge,
                    size="medium",
                    fill_opacity=0.65,
                    landscape=landscape,
                )
            )
            labels.append(
                _make_label(
                    CUSTOM_TOKEN,
                    size="medium",
                    fill_opacity=0.0,
                    border_opacity=0.0,
                    x=bx,
                    y=by,
                    landscape=landscape,
                    custom_text="{PLACEHOLDER}",
                )
            )
        if badge_on:
            bx, by = _badge_xy(key, badge_pos)
            labels.append(
                _make_label(
                    "Badge",
                    fill_color="#0b111b",  # dark logo variant (yellow on navy)
                    border_color="#000000",
                    border_opacity=0.0,
                    fill_opacity=1.0,
                    x=bx,
                    y=by,
                    landscape=landscape,
                )
            )
        profiles[key] = normalize_profile(key, {"labels": labels, "grayscale": gs_on})
    return {
        "version": LAYOUT_DOC_VERSION,
        "profiles": profiles,
        "art_library_synced": False,
    }


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
    """Map prior-grid row/col into portable ``x``/``y`` fractions (run before normalize)."""
    if not labels:
        return labels
    landscape = str(profile_key).startswith("episode.")
    source = _source_grid_for_prior_version(prior_version, landscape=landscape)
    if source is None:
        return labels
    old_c, old_r = source
    out: list[dict[str, Any]] = []
    for label in labels:
        if not isinstance(label, dict):
            continue
        item = dict(label)
        if item.get("x") is not None or item.get("y") is not None:
            item["x"] = _clamp_frac(item.get("x"), 0.5)
            item["y"] = _clamp_frac(item.get("y"), 0.5)
            out.append(item)
            continue
        old_row = _clamp_int(item.get("row"), 0, max(0, old_r - 1), 0)
        old_col = _clamp_int(item.get("col"), 0, max(0, old_c - 1), 0)
        if old_c > 0 and old_r > 0:
            item["x"] = (old_col + 0.5) / old_c
            item["y"] = (old_row + 0.5) / old_r
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
        raw_prof = profiles_raw.get(key)
        if prior_version != LAYOUT_DOC_VERSION and isinstance(raw_prof, dict):
            raw_labels = raw_prof.get("labels")
            if isinstance(raw_labels, list) and raw_labels:
                raw_prof = {
                    **raw_prof,
                    "labels": _scale_label_coords_from_version(
                        key, raw_labels, prior_version=prior_version
                    ),
                }
        prof = normalize_profile(key, raw_prof)
        if prof["labels"]:
            any_labels = True
        profiles[key] = prof
    # First-time / empty doc: seed from legacy toggles so visual parity holds.
    if seed_if_empty and not any_labels and not any(
        isinstance(profiles_raw.get(k), dict) and "labels" in (profiles_raw.get(k) or {})
        for k in PROFILE_KEYS
    ):
        seeded = seed_profiles_from_legacy_settings()
        seeded["art_library_synced"] = bool(payload.get("art_library_synced")) if isinstance(payload, dict) else False
        return seeded
    return {
        "version": LAYOUT_DOC_VERSION,
        "profiles": profiles,
        # False until the first layout-driven art pass finishes enqueue (legacy JPEG migration).
        "art_library_synced": bool(payload.get("art_library_synced")),
    }


def layout_fingerprint(doc: dict[str, Any] | None = None) -> str:
    """Stable hash of the full layout document (settings dirty / catalog)."""
    data = doc if isinstance(doc, dict) else get_layout_doc()
    payload = {
        "version": data.get("version"),
        "profiles": data.get("profiles") or {},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def layout_art_library_synced(doc: dict[str, Any] | None = None) -> bool:
    data = doc if isinstance(doc, dict) else get_layout_doc()
    return bool(data.get("art_library_synced"))


def mark_layout_art_library_synced(synced: bool = True) -> dict[str, Any]:
    """Record that placeholder JPEGs have been through a layout-driven art pass."""
    doc = get_layout_doc()
    doc["art_library_synced"] = bool(synced)
    return save_layout_doc(doc)


def layout_art_targets_from_changed_profiles(
    changed_keys: list[str] | tuple[str, ...] | None,
    *,
    doc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide which placeholder art to enqueue after a layout save.

    First library sync (``art_library_synced`` false) always targets the full
    library so pre-designer overlay JPEGs migrate once. After that, only media
    kinds / variants present in ``changed_keys`` are queued.
    """
    data = doc if isinstance(doc, dict) else get_layout_doc()
    keys = [str(k).strip() for k in (changed_keys or []) if str(k).strip()]
    if not layout_art_library_synced(data):
        return {
            "full": True,
            "kinds": ["movie", "tv", "episode"],
            "variants": ["default", "coming_soon"],
            "reason": "first_layout_art_sync",
            "changed_profiles": keys,
        }
    kinds: set[str] = set()
    variants: set[str] = set()
    for key in keys:
        media, _availability, variant = parse_profile_key(key)
        kinds.add(media)
        variants.add(variant)
    if not kinds:
        return {
            "full": False,
            "kinds": [],
            "variants": [],
            "reason": "nothing_changed",
            "changed_profiles": [],
        }
    return {
        "full": False,
        "kinds": sorted(kinds),
        "variants": sorted(variants) if variants else ["default", "coming_soon"],
        "reason": "changed_profiles",
        "changed_profiles": keys,
    }


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


_DECIMAL_RATING_SOURCES = frozenset({"imdb", "tmdb", "trakt"})


def _format_rating_value(value: Any, *, source: str | None = None) -> str:
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    key = _normalize_rating_key(source) if source else ""
    # IMDb / TMDB / Trakt (and bare Arr scores): one decimal, except a perfect 10.
    if key in _DECIMAL_RATING_SOURCES or not key:
        if abs(num - 10.0) < 1e-6:
            return "10"
        return f"{num:.1f}"
    # RT / Metacritic stay whole when whole; keep one decimal if fractional.
    if abs(num - round(num)) < 1e-6:
        return str(int(round(num)))
    return f"{num:.1f}"


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
        text = _format_rating_value(raw, source="imdb")
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
            canon = _normalize_rating_key(str(name))
            text = _format_rating_value(val, source=canon)
            if not text:
                continue
            by_source[canon] = text
        return by_source

    for key in ("value", "Value", "rating", "Rating"):
        if raw.get(key) is not None:
            text = _format_rating_value(raw.get(key), source="imdb")
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


def _coming_soon_or_new_ep_date_iso(
    *,
    media_kind: str,
    movie: Any = None,
    series: Any = None,
    season: Any = None,
    episode: Any = None,
) -> str:
    """ISO date behind ``ComingSoonOrNewEp`` (preferred movie date, air date, or next unaired)."""
    kind = str(media_kind or "movie").strip().lower()
    if kind == "movie" and movie is not None:
        try:
            from services.source_of_truth.calendar_phase import _preferred_movie_release_date

            target, _release_type, _preferred = _preferred_movie_release_date(movie)
            return _token_date(target)
        except Exception:
            return ""
    if episode is not None:
        return _token_date(getattr(episode, "air_date", None))
    if series is not None:
        try:
            from services.poster_overlay_text import next_unaired_episode_air_date

            sid = getattr(series, "id", None)
            if sid is None:
                return ""
            season_id = getattr(season, "id", None) if season is not None else None
            return _token_date(
                next_unaired_episode_air_date(
                    series_id=int(sid),
                    season_id=int(season_id) if season_id is not None else None,
                )
            )
        except Exception:
            return ""
    return ""


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
        "FillRectangle": "",  # textless fill box; chips supply copy
        "PLACEHOLDER": "PLACEHOLDER",
        "ComingSoonOrNewEp": str(coming_soon_label or "").strip(),
        "ComingSoonOrNewEpDate": _coming_soon_or_new_ep_date_iso(
            media_kind=kind,
            movie=movie,
            series=series,
            season=season,
            episode=episode,
        ),
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
        "FillRectangle": "",
        "PLACEHOLDER": "PLACEHOLDER",
        "ComingSoonOrNewEp": "Theatrical - Sep 30",
        "ComingSoonOrNewEpDate": "2026-09-30",
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
        # 8.8 is as wide as typical Arr ratings; 0–10 scores keep one decimal except 10.
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
        base["ComingSoonOrNewEpDate"] = "2026-09-30"
    elif kind == "episode":
        base["Title"] = "Ozymandias"
        base["Year"] = "2013"
        base["Certification"] = "TV-MA"
        base["Genres"] = "Science Fiction, Adventure"
        base["Runtime"] = "47m"
        base["ComingSoonOrNewEp"] = "Airing - Sep 30"
        base["ComingSoonOrNewEpDate"] = "2026-09-30"
    else:
        base["Title"] = "Breaking Bad"
        base["Year"] = "2008"
        base["Certification"] = "TV-MA"
        base["Genres"] = "Science Fiction, Adventure"
        base["Runtime"] = "47m"
        base["SeriesStatus"] = "Returning"
        base["ComingSoonOrNewEp"] = "New Ep. - Sep 30"
        base["ComingSoonOrNewEpDate"] = "2026-09-30"
    return base


def serialize_layout_catalog() -> dict[str, Any]:
    """API payload for the designer (profiles, tokens, grids, sample strings)."""
    from services.layout_locale import get_layout_locale, layout_locale_catalog

    doc = get_layout_doc()
    token_meta = [
        {"name": "FillRectangle", "label": "Fill rectangle"},
        {"name": "Custom", "label": "Text"},
        {"name": "Badge", "label": "Placeholdarr badge"},
        {"name": "Rating", "label": "Rating"},
    ]
    grids = {key: {"cols": grid_size_for_profile(key)[0], "rows": grid_size_for_profile(key)[1]} for key in PROFILE_KEYS}
    allowed = {key: [t for t in LABEL_TOKENS if t in tokens_for_profile(key)] for key in PROFILE_KEYS}
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
        "custom_date_tokens": sorted(_CUSTOM_DATE_TOKENS | _CUSTOM_STAMPED_DATE_TOKENS),
        "custom_date_formats": custom_date_format_options(),
        "grids": grids,
        "sizes": list(LABEL_SIZES),
        "styles": list(LABEL_STYLES),
        "arted_tokens": sorted(_ARTED_TOKENS),
        "rating_sources": list(_RATING_SOURCES),
        "rating_art_layouts": list(_RATING_ART_LAYOUTS),
        "size_scale_min": _SIZE_SCALE_MIN,
        "size_scale_max": _SIZE_SCALE_MAX,
        "size_scale_presets": dict(_SIZE_MULT),
        "layout_locale": get_layout_locale(),
        "layout_locales": layout_locale_catalog(),
        "fingerprint": layout_fingerprint(doc),
        "art_library_synced": layout_art_library_synced(doc),
    }
