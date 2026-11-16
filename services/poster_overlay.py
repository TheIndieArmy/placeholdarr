"""Download and composite placeholder poster overlays for media-player local art."""

from __future__ import annotations

import io
import math
import os
import re
from pathlib import Path
from typing import Literal

import requests

from core.config import settings
from core.logger import logger

OverlayMode = Literal["off", "grayscale", "top_banner", "corner_logo", "compose"]
BannerPosition = Literal["top", "bottom"]
BadgePosition = Literal["bottom_right", "bottom_left", "top_right", "top_left"]

PORTRAIT_SIZE = (1000, 1500)
LANDSCAPE_SIZE = (1920, 1080)
BRAND_BLUE = (11, 17, 27, 255)
BRAND_BLUE_HEX = "#0b111b"
OVERLAY_META_FILENAME = ".poster-overlay.json"
# Meta ``mode`` value for layered compositing (replaces exclusive overlay modes).
COMPOSE_MODE = "compose"

_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "placeholdarr_logo_yellow.png"
_LOGO_PATH_BLUE = Path(__file__).resolve().parent / "assets" / "placeholdarr_logo_blue.png"
_FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "SpaceGrotesk-Bold.ttf"
_RATING_LOGO_DIR = Path(__file__).resolve().parent / "assets" / "ratings"
_RATING_LOGO_FILES = {
    "imdb": "imdb.png",
    "tmdb": "tmdb.png",
    "trakt": "trakt.png",
    "rottentomatoes": "rottentomatoes.png",
    "metacritic": "metacritic.png",
}
# Badge size medium matches legacy corner logo edge length.
_BADGE_SIZE_BASE = "medium"
# Layout-designer badge: dark = yellow mark on navy; light = blue mark on brand yellow.
_BADGE_DARK_FILL = BRAND_BLUE_HEX
_BADGE_LIGHT_FILL = "#fbbf24"
_BADGE_LIGHT_PLATE = (251, 191, 36, 255)
_VALID_MODES = frozenset({"off", "grayscale", "top_banner", "corner_logo", COMPOSE_MODE})
_VALID_BANNER_POSITIONS = frozenset({"top", "bottom"})
_VALID_BADGE_POSITIONS = frozenset({"bottom_right", "bottom_left", "top_right", "top_left"})
# Bumped for Coming Soon / date-label banner text (auto-fit width).
_TOP_BANNER_LAYOUT = "top-banner-space-grotesk-v7"  # restore pre-compose bar opacity (0.65)
# Layered compose: independent grayscale / text bar / badge (badge corners).
_COMPOSE_LAYOUT = "compose-layers-v9"  # lighter banner bar; badge stays fully opaque
# Corner logo + Coming Soon uses a bottom strip with the logo embedded in the bar.
_CORNER_BOTTOM_BANNER_LAYOUT = "corner-bottom-banner-v1"

# Banner fill alpha (pre-compose top_banner used 0.65). Badge / logo stay fully opaque.
_BANNER_BAR_ALPHA = int(255 * 0.65)


def _asset_digest(path: Path) -> str | None:
    import hashlib

    try:
        data = path.read_bytes()
    except OSError:
        return None
    return f"{hashlib.sha256(data).hexdigest()[:16]}:{len(data)}"


def logo_asset_stamp() -> str:
    """Change when the bundled logo file or badge layout changes so posters can be regenerated.

    Uses file content hash (not mtime) so Docker restarts / image copies do not force a
    full-library art rebuild when the logo bytes are unchanged.
    """
    layout_stamp = (
        f"corner-br-v4:{_TOP_BANNER_LAYOUT}:{_CORNER_BOTTOM_BANNER_LAYOUT}:"
        f"{_COMPOSE_LAYOUT}:prefer-local-nfo-v1:chip-mm-v5"
    )
    logo_digest = _asset_digest(_LOGO_PATH)
    logo_blue_digest = _asset_digest(_LOGO_PATH_BLUE)
    font_digest = _asset_digest(_FONT_PATH)
    parts = [layout_stamp]
    if logo_digest:
        parts.append(logo_digest)
    if logo_blue_digest:
        parts.append(f"blue:{logo_blue_digest}")
    for key, name in sorted(_RATING_LOGO_FILES.items()):
        digest = _asset_digest(_RATING_LOGO_DIR / name)
        if digest:
            parts.append(f"rating-{key}:{digest}")
    if font_digest:
        parts.append(f"font:{font_digest}")
    return ":".join(parts)

_pillow_modules: tuple | bool | None = None
_pillow_missing_logged = False


def pillow_available() -> bool:
    """Return True when Pillow is importable (cached after first check)."""
    global _pillow_modules
    if _pillow_modules is False:
        return False
    if _pillow_modules is not None:
        return True
    try:
        from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

        _pillow_modules = (Image, ImageDraw, ImageEnhance, ImageFont, ImageOps)
        return True
    except ModuleNotFoundError:
        _pillow_modules = False
        return False


def _log_pillow_missing_once() -> None:
    global _pillow_missing_logged
    if _pillow_missing_logged:
        return
    _pillow_missing_logged = True
    logger.warning(
        "Placeholder poster overlay mode is enabled but Pillow is not installed. "
        "Falling back to raw poster downloads for local art files. "
        "Install with: pip install -r requirements.txt (or rebuild the Docker image).",
        extra={"emoji_type": "warning"},
    )


def _pillow():
    if pillow_available():
        return _pillow_modules
    return None


def poster_overlay_compositing_available() -> bool:
    """True when overlay mode is on and Pillow can composite images."""
    if not poster_overlay_enabled():
        return False
    if pillow_available():
        return True
    _log_pillow_missing_once()
    return False


def poster_grayscale_enabled() -> bool:
    return bool(getattr(settings, "PLACEHOLDER_POSTER_GRAYSCALE", False))


def poster_badge_enabled() -> bool:
    return bool(getattr(settings, "PLACEHOLDER_POSTER_BADGE", False))


def poster_badge_position() -> BadgePosition:
    raw = str(getattr(settings, "PLACEHOLDER_POSTER_BADGE_POSITION", "bottom_right") or "bottom_right")
    raw = raw.strip().lower().replace("-", "_")
    return raw if raw in _VALID_BADGE_POSITIONS else "bottom_right"  # type: ignore[return-value]


def poster_placeholder_banner_enabled() -> bool:
    """Draw PLACEHOLDER on the text bar when Coming Soon / New Ep. dates do not apply."""
    return bool(getattr(settings, "PLACEHOLDER_POSTER_PLACEHOLDER_BANNER", False))


def coming_soon_banner_enabled() -> bool:
    """Coming Soon date strips (movies/episodes) and New Ep. on series/season posters."""
    return bool(getattr(settings, "PLACEHOLDER_POSTER_COMING_SOON_BANNER", False))


def series_new_ep_banner_enabled() -> bool:
    """New Ep. follows the Coming Soon date-banner setting."""
    return coming_soon_banner_enabled()


def poster_banner_position() -> BannerPosition:
    """Where text bars sit (PLACEHOLDER / Coming Soon / New Ep.). Default top."""
    raw = str(getattr(settings, "PLACEHOLDER_POSTER_BANNER_POSITION", "top") or "top").strip().lower()
    return "bottom" if raw == "bottom" else "top"


def poster_overlay_enabled() -> bool:
    """True when any compositing layer is on (legacy toggles or layout labels)."""
    if (
        poster_grayscale_enabled()
        or poster_badge_enabled()
        or poster_placeholder_banner_enabled()
        or coming_soon_banner_enabled()
    ):
        return True
    try:
        from services.poster_layouts import layouts_have_any_labels

        return layouts_have_any_labels()
    except Exception:
        return False


def poster_overlay_mode() -> str:
    """Effective meta mode: ``compose`` when any layer is on, else ``off``.

    Legacy exclusive modes (`grayscale` / `top_banner` / `corner_logo`) remain
    readable from settings for migration only; drawing always uses layers.
    """
    if poster_overlay_enabled():
        return COMPOSE_MODE
    return "off"


def overlay_layer_fingerprint(*, profile_key: str | None = None) -> dict[str, str | bool]:
    """Stable layer snapshot for ``.poster-overlay.json`` skip/regen.

    When ``profile_key`` is set, ``layout_hash`` is that profile only so editing
    ``movie.placeholder.default`` does not invalidate TV/episode or Real sidecars.
    """
    layout_hash = ""
    try:
        from services.poster_layouts import layout_fingerprint, profile_layout_fingerprint

        key = str(profile_key or "").strip()
        layout_hash = profile_layout_fingerprint(key) if key else layout_fingerprint()
    except Exception:
        layout_hash = ""
    layout_locale = "en"
    try:
        from services.layout_locale import get_layout_locale

        layout_locale = get_layout_locale()
    except Exception:
        layout_locale = "en"
    return {
        "grayscale": bool(poster_grayscale_enabled()),
        "badge": bool(poster_badge_enabled()),
        "badge_position": poster_badge_position(),
        "placeholder_banner": bool(poster_placeholder_banner_enabled()),
        "coming_soon_banner": bool(coming_soon_banner_enabled()),
        "banner_position": poster_banner_position(),
        "layout_locale": layout_locale,
        "layout_hash": layout_hash,
    }


def normalize_poster_url(url: str | None) -> str | None:
    text = str(url or "").strip()
    if not text:
        return None
    # Prefer smaller TMDB assets for download size and speed.
    text = re.sub(
        r"(https?://image\.tmdb\.org/t/p/)original(/)",
        r"\1w500\2",
        text,
        flags=re.IGNORECASE,
    )
    return text


def download_poster_bytes(url: str, *, timeout: float = 20.0) -> bytes | None:
    normalized = normalize_poster_url(url)
    if not normalized:
        return None
    try:
        resp = requests.get(normalized, timeout=timeout)
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.content
    except Exception as exc:
        logger.warning(
            f"Poster download failed url={normalized!r}: {exc}",
            extra={"emoji_type": "warning"},
        )
        return None


def load_image_from_bytes(data: bytes):
    mods = _pillow()
    if mods is None:
        _log_pillow_missing_once()
        return None
    Image = mods[0]
    try:
        img = Image.open(io.BytesIO(data))
        return img.convert("RGBA")
    except Exception as exc:
        logger.warning(f"Poster image decode failed: {exc}", extra={"emoji_type": "warning"})
        return None


def _fit_image(img, size: tuple[int, int]):
    mods = _pillow()
    if mods is None:
        return img
    Image, _, _, _, ImageOps = mods
    return ImageOps.fit(img, size, method=Image.Resampling.LANCZOS)


def _apply_grayscale(img):
    mods = _pillow()
    if mods is None:
        return img
    Image, _, ImageEnhance, _, ImageOps = mods
    gray = ImageOps.grayscale(img.convert("RGB"))
    rgb = gray.convert("RGB")
    dimmed = ImageEnhance.Brightness(rgb).enhance(0.85)
    out = dimmed.convert("RGBA")
    if img.mode == "RGBA":
        out.putalpha(img.split()[-1])
    return out


def _load_font(size: int):
    mods = _pillow()
    if mods is None:
        return None
    ImageFont = mods[3]
    if _FONT_PATH.is_file():
        try:
            return ImageFont.truetype(str(_FONT_PATH), size=size)
        except OSError as exc:
            logger.warning(
                f"Bundled overlay font failed to load ({_FONT_PATH}): {exc}",
                extra={"emoji_type": "warning"},
            )
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit_banner_font(text: str, *, max_width: int, preferred_size: int, min_size: int = 14):
    """Shrink font until ``text`` fits within ``max_width`` (with side padding)."""
    size = max(min_size, int(preferred_size))
    font = _load_font(size)
    if font is None:
        return font
    pad = max(16, int(max_width * 0.06))
    limit = max(1, max_width - pad)
    while size > min_size:
        try:
            bbox = font.getbbox(text)
            text_w = int(bbox[2] - bbox[0]) if bbox else 0
        except Exception:
            text_w = 0
        if text_w <= limit:
            break
        size = max(min_size, size - 2)
        font = _load_font(size)
    return font


def _banner_bar_height(width: int, height: int) -> int:
    """Text-bar thickness. Same value for top and bottom placement.

    Landscape (episode) thumbs use 18% of height; portrait posters use ~11%.
    """
    w, h = int(width), int(height)
    frac = 0.18 if w > h else 0.11
    return max(56, int(h * frac))


def _corner_badge_size(width: int, height: int) -> int:
    """Square badge edge length: always matches banner bar height so it fits the strip."""
    return _banner_bar_height(width, height)


def _corner_badge_inset(width: int, height: int) -> int:
    return max(8, int(min(int(width), int(height)) * 0.02))


def _badge_reserve_width(poster_w: int, poster_h: int) -> int:
    """Horizontal space reserved for a corner badge when it shares a banner edge."""
    badge_w = _corner_badge_size(poster_w, poster_h)
    return badge_w + 2 * _corner_badge_inset(poster_w, poster_h)


def _banner_badge_collision_side(
    banner_pos: str,
    badge_pos: str,
    *,
    badge_on: bool,
) -> str | None:
    """Return ``left`` / ``right`` when badge shares the banner edge; else None."""
    if not badge_on:
        return None
    bp = str(badge_pos or "").strip().lower().replace("-", "_")
    ban = str(banner_pos or "top").strip().lower()
    if ban == "bottom" and bp in {"bottom_left", "bottom_right"}:
        return "left" if bp == "bottom_left" else "right"
    if ban == "top" and bp in {"top_left", "top_right"}:
        return "left" if bp == "top_left" else "right"
    return None


def _apply_banner_bar(
    img,
    text: str,
    *,
    position: str = "top",
    avoid_badge_side: str | None = None,
):
    """Draw a semi-opaque text bar at the top or bottom of the poster."""
    mods = _pillow()
    if mods is None:
        return img
    Image, ImageDraw, _, _, _ = mods
    out = img.convert("RGBA")
    w, h = out.size
    bar_h = _banner_bar_height(w, h)
    # Inclusive bottom row: Pillow's rectangle treats y1 as outside the fill, so use h
    # for bottom bars carefully (y0 + bar_h may equal h). Keep top/bottom spans equal.
    if position == "top":
        y0, y1 = 0, bar_h
    else:
        y0, y1 = max(0, h - bar_h), h
    overlay = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([0, y0, w, y1], fill=(0, 0, 0, _BANNER_BAR_ALPHA))
    out = Image.alpha_composite(out, overlay)
    draw = ImageDraw.Draw(out)
    label = str(text or "PLACEHOLDER").strip() or "PLACEHOLDER"
    side = str(avoid_badge_side or "").strip().lower()
    if side in {"left", "right"}:
        reserve = _badge_reserve_width(w, h)
        text_max_w = max(1, w - reserve)
        cx = (reserve + w) // 2 if side == "left" else text_max_w // 2
    else:
        text_max_w = w
        cx = w // 2
    preferred = max(26, int(bar_h * 0.50))
    font = _fit_banner_font(label, max_width=text_max_w, preferred_size=preferred)
    draw.text(
        (cx, y0 + (y1 - y0) // 2),
        label,
        fill=(255, 255, 255, 255),
        font=font,
        anchor="mm",
    )
    return out


def _apply_top_banner(
    img,
    text: str = "PLACEHOLDER",
    *,
    position: str | None = None,
    avoid_badge_side: str | None = None,
):
    pos = str(position or poster_banner_position() or "top").strip().lower()
    if pos not in _VALID_BANNER_POSITIONS:
        pos = "top"
    return _apply_banner_bar(img, text, position=pos, avoid_badge_side=avoid_badge_side)


def _rounded_rectangle_mask(size: tuple[int, int], radius: int):
    mods = _pillow()
    if mods is None:
        return None
    Image, ImageDraw, _, _, _ = mods
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def _badge_logo_variant(fill_color: str | None) -> str:
    """Map badge fill to logo variant: ``dark`` (yellow on navy) or ``light`` (blue on white)."""
    text = str(fill_color or "").strip().lower().lstrip("#")
    if len(text) >= 6:
        try:
            r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
            # Perceptual lightness: light plates use the blue mark.
            if (0.2126 * r + 0.7152 * g + 0.0722 * b) >= 160:
                return "light"
        except ValueError:
            pass
    return "dark"


def _build_corner_logo_badge(badge_w: int, *, variant: str = "dark"):
    """Square brand badge: Placeholdarr logo on a dark or light plate."""
    mods = _pillow()
    if mods is None:
        return None
    Image, _, _, _, ImageOps = mods
    badge_h = badge_w
    badge = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
    plate = _BADGE_LIGHT_PLATE if str(variant).strip().lower() == "light" else BRAND_BLUE
    logo_path = _LOGO_PATH_BLUE if str(variant).strip().lower() == "light" else _LOGO_PATH
    plate_layer = Image.new("RGBA", (badge_w, badge_h), plate)
    radius = max(6, int(badge_w * 0.12))
    mask = _rounded_rectangle_mask((badge_w, badge_h), radius)
    badge.paste(plate_layer, (0, 0), mask)

    if logo_path.is_file():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            inner = int(badge_w * 0.7)
            logo = ImageOps.contain(logo, (inner, inner), method=Image.Resampling.LANCZOS)
            lx = (badge_w - logo.width) // 2
            ly = (badge_h - logo.height) // 2
            badge.paste(logo, (lx, ly), logo)
        except Exception as exc:
            logger.debug(f"Corner logo load failed: {exc}", extra={"emoji_type": "debug"})
    return badge


def _apply_corner_logo(img, *, position: str | None = None, banner_edge: str | None = None):
    mods = _pillow()
    if mods is None:
        return img
    out = img.convert("RGBA")
    w, h = out.size
    pos = str(position or poster_badge_position() or "bottom_right").strip().lower().replace("-", "_")
    if pos not in _VALID_BADGE_POSITIONS:
        pos = "bottom_right"
    edge = str(banner_edge or "").strip().lower()
    share_top = edge == "top" and pos.startswith("top_")
    share_bottom = edge == "bottom" and pos.startswith("bottom_")
    share_edge = share_top or share_bottom
    badge_w = _corner_badge_size(w, h)
    inset = _corner_badge_inset(w, h)
    badge = _build_corner_logo_badge(badge_w)
    if badge is None:
        return out
    # Flush into the bar on the shared axis; otherwise keep a small corner inset.
    y_inset = 0 if share_edge else inset
    x0 = inset if pos.endswith("_left") else w - badge_w - inset
    y0 = y_inset if pos.startswith("top_") else h - badge_w - y_inset
    out.paste(badge, (x0, y0), badge)
    return out


def _parse_hex_rgba(color: str, *, alpha: int | None = None) -> tuple[int, int, int, int]:
    text = str(color or "").strip().lstrip("#")
    if len(text) == 6:
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
        a = 255 if alpha is None else max(0, min(255, int(alpha)))
        return (r, g, b, a)
    if len(text) == 8:
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
        a = int(text[6:8], 16) if alpha is None else max(0, min(255, int(alpha)))
        return (r, g, b, a)
    return (0, 0, 0, 255 if alpha is None else max(0, min(255, int(alpha or 255))))


def _layout_chip_font_size(img_w: int, img_h: int, *, landscape: bool) -> int:
    """Readable chip type sized from the poster, not from a snap cell (small tier)."""
    if landscape:
        return max(22, min(42, int(img_h * 0.042)))
    return max(20, min(38, int(img_w * 0.036)))


def _label_size_mult(size: str | float | None, size_scale: float | None = None) -> float:
    try:
        from services.poster_layouts import size_multiplier

        if size_scale is not None:
            return float(size_multiplier(size_scale))
        return float(size_multiplier(size if size is not None else "small"))
    except Exception:
        if size_scale is not None:
            try:
                return max(0.5, min(2.0, float(size_scale)))
            except (TypeError, ValueError):
                pass
        key = str(size or "small").strip().lower()
        return {"small": 0.85, "medium": 1.1, "large": 1.4}.get(key, 0.85)


def _rating_logo_path(source: str) -> Path | None:
    key = str(source or "").strip().lower()
    if key in ("themoviedb",):
        key = "tmdb"
    if key in ("rotten_tomatoes",):
        key = "rottentomatoes"
    name = _RATING_LOGO_FILES.get(key)
    if not name:
        return None
    path = _RATING_LOGO_DIR / name
    return path if path.is_file() else None


def _prepare_rating_logo(source: str, icon_h: int):
    """Load a rating mark and seat it on a brand well when the SVG is dark-on-transparent."""
    mods = _pillow()
    if mods is None:
        return None
    Image, ImageDraw, _, _, ImageOps = mods
    path = _rating_logo_path(source)
    if path is None:
        return None
    try:
        logo = Image.open(path).convert("RGBA")
    except Exception:
        return None
    key = str(source or "").strip().lower()
    if key in ("themoviedb",):
        key = "tmdb"
    if key in ("rotten_tomatoes",):
        key = "rottentomatoes"
    # IMDb mark is near-black; give it the yellow brand plate so it reads on dark chips.
    wells = {
        "imdb": (245, 197, 24, 255),
        "tmdb": (30, 36, 48, 255),
        "trakt": (30, 36, 48, 255),
        "rottentomatoes": (30, 36, 48, 255),
        "metacritic": (30, 36, 48, 255),
    }
    well = wells.get(key)
    inner = max(10, int(icon_h * 0.82))
    logo = ImageOps.contain(logo, (inner, inner), method=Image.Resampling.LANCZOS)
    if well is None:
        return logo
    plate = Image.new("RGBA", (icon_h, icon_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(plate)
    radius = max(3, icon_h // 6)
    draw.rounded_rectangle([0, 0, icon_h - 1, icon_h - 1], radius=radius, fill=well)
    lx = (icon_h - logo.width) // 2
    ly = (icon_h - logo.height) // 2
    plate.paste(logo, (lx, ly), logo)
    return plate


def _font_line_height(font) -> int:
    """Line box height used by multiline draw (ascent+descent), not ink-only getbbox."""
    try:
        ascent, descent = font.getmetrics()
        return max(1, int(ascent) + int(descent))
    except Exception:
        pass
    try:
        bbox = font.getbbox("Hg")
        if bbox:
            return max(1, int(bbox[3] - bbox[1]))
    except Exception:
        pass
    return 16


def _measure_text(font, text: str, *, line_spacing: int = 2) -> tuple[int, int]:
    """Measure text for chip layout; multiline height matches Pillow multiline_text."""
    raw = str(text or "")
    if "\n" not in raw:
        try:
            bbox = font.getbbox(raw)
            if bbox:
                # Prefer metric line height so vertical padding matches drawn glyphs.
                return max(1, int(bbox[2] - bbox[0])), max(_font_line_height(font), int(bbox[3] - bbox[1]))
        except Exception:
            pass
        return max(1, len(raw) * 10), _font_line_height(font)
    lines = raw.split("\n")
    width = 1
    gap = max(0, int(line_spacing))
    line_h = _font_line_height(font)
    for line in lines:
        try:
            bbox = font.getbbox(line if line else " ")
            if bbox:
                lw = max(1, int(bbox[2] - bbox[0]))
            else:
                lw = 1
        except Exception:
            lw = max(1, len(line) * 10)
        width = max(width, lw)
    n = max(1, len(lines))
    height = line_h * n + gap * max(0, n - 1)
    return width, max(1, height)


def _vertical_font_size(font) -> int:
    try:
        return max(1, int(getattr(font, "size", 16) or 16))
    except Exception:
        return 16


def _glyph_ink_bbox(font, ch: str) -> tuple[int, int, int, int]:
    """Return ``(left, top, right, bottom)`` ink box for one glyph (or a space slot)."""
    size = _vertical_font_size(font)
    # Word break: advance a readable blank slot; do not paint a space glyph.
    if ch and ch.isspace():
        slot = max(6, int(round(size * 0.62)))
        return (0, 0, max(1, size // 3), slot)
    try:
        bbox = font.getbbox(ch if ch else " ")
        if bbox:
            return (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
    except Exception:
        pass
    return (0, 0, max(1, size // 2), max(1, size))


def _measure_vertical_stack(font, text: str, *, gap: int = 1) -> tuple[int, int]:
    """Ink-based width/height for a vertical stack with letter and word gaps."""
    lines = str(text or "").split("\n")
    if not lines:
        return 1, 1
    width = 1
    height = 0
    g = max(0, int(gap))
    for i, line in enumerate(lines):
        left, top, right, bottom = _glyph_ink_bbox(font, line)
        # Width from painted glyphs only; spaces are vertical separators.
        if not (line and line.isspace()):
            width = max(width, max(1, right - left))
        height += max(1, bottom - top)
        if i < len(lines) - 1 and not (line and line.isspace()):
            # Letter gap after glyphs; word slots already include their own advance.
            next_line = lines[i + 1]
            if not (next_line and next_line.isspace()):
                height += g
    return width, max(1, height)


def _draw_vertical_stack(
    draw,
    *,
    mid_x: int,
    top_y: int,
    text: str,
    font,
    fill,
    gap: int = 1,
) -> None:
    """Paint stacked glyphs from ink boxes with readable letter/word spacing."""
    lines = str(text or "").split("\n")
    y = int(top_y)
    g = max(0, int(gap))
    for i, line in enumerate(lines):
        left, top, right, bottom = _glyph_ink_bbox(font, line)
        ink_h = max(1, bottom - top)
        is_space = bool(line and line.isspace())
        if not is_space:
            # Pen so the ink box is centered on mid_x and starts at y.
            pen_x = int(mid_x - (left + right) / 2)
            pen_y = int(y - top)
            draw.text((pen_x, pen_y), line, fill=fill, font=font)
        y += ink_h
        if i < len(lines) - 1 and not is_space:
            next_line = lines[i + 1]
            if not (next_line and next_line.isspace()):
                y += g


def _fit_layout_chip_font(
    text: str,
    *,
    preferred_size: int,
    min_size: int,
    max_width: int,
    max_height: int | None = None,
    line_gap_ratio: float = 0.06,
    vertical: bool = False,
    pad_x_ratio: float = 0.0,
    pad_x_min: int = 0,
    pad_y_ratio: float = 0.0,
    pad_y_min: int = 0,
):
    """Shrink chip type until plate size (ink + pad) fits the poster budget.

    Prefers ``preferred_size`` and only steps down when the chip would exceed
    ``max_width`` / ``max_height``. Never stops early while still overflowing
    until ``min_size`` (callers should set a low floor so text does not clip).
    """
    size = max(min_size, int(preferred_size))
    font = _load_font(size)
    if font is None:
        return font
    limit_w = max(1, int(max_width))
    limit_h = max(1, int(max_height)) if max_height is not None else None
    while True:
        line_gap = max(2 if vertical else 1, int(round(size * line_gap_ratio)))
        if vertical:
            tw, th = _measure_vertical_stack(font, text, gap=line_gap)
        else:
            tw, th = _measure_text(font, text, line_spacing=line_gap)
        pad_x = max(int(pad_x_min), int(round(size * pad_x_ratio))) if (pad_x_ratio or pad_x_min) else 0
        pad_y = max(int(pad_y_min), int(round(size * pad_y_ratio))) if (pad_y_ratio or pad_y_min) else 0
        chip_w = tw + pad_x * 2
        chip_h = th + pad_y * 2
        fits_w = chip_w <= limit_w
        fits_h = limit_h is None or chip_h <= limit_h
        if fits_w and fits_h:
            break
        if size <= min_size:
            break
        size = max(min_size, size - 2)
        font = _load_font(size)
        if font is None:
            break
    return font


def _verticalize_chip_text(text: str) -> str:
    """Stack characters top-to-bottom for vertical chip orientation."""
    chars: list[str] = []
    for ch in str(text or ""):
        if ch == "\n":
            continue
        chars.append(" " if ch.isspace() else ch)
    return "\n".join(chars) if chars else ""


def _clamp_box_to_image(
    box: list[int],
    *,
    img_w: int,
    img_h: int,
    margin: int = 8,
) -> list[int]:
    """Shift a chip box so it stays on-canvas without changing its size."""
    x0, y0, x1, y1 = box
    bw = x1 - x0
    bh = y1 - y0
    max_w = max(margin + 1, img_w - margin)
    max_h = max(margin + 1, img_h - margin)
    if bw > max_w - margin:
        x0 = margin
        x1 = max_w
    else:
        if x0 < margin:
            x1 += margin - x0
            x0 = margin
        if x1 > max_w:
            shift = x1 - max_w
            x0 -= shift
            x1 -= shift
    if bh > max_h - margin:
        y0 = margin
        y1 = max_h
    else:
        if y0 < margin:
            y1 += margin - y0
            y0 = margin
        if y1 > max_h:
            shift = y1 - max_h
            y0 -= shift
            y1 -= shift
    return [int(x0), int(y0), int(x1), int(y1)]


def _draw_layout_chip(
    out,
    *,
    text: str,
    anchor: tuple[int, int],
    text_color: str,
    fill_color: str,
    border_color: str,
    fill_opacity: float,
    border_opacity: float = 0.0,
    border_thickness: int | None = None,
    text_opacity: float = 1.0,
    logo_opacity: float = 1.0,
    size: str = "small",
    size_scale: float | None = None,
    art_path: Path | None = None,
    art_layout: str = "left",
    landscape: bool = False,
    rotation: int = 0,
    text_orientation: str = "horizontal",
):
    """Draw a content-sized chip centered on ``anchor``.

    Returns ``(image, box)`` where ``box`` is ``[x0, y0, x1, y1]`` in bake pixels,
    or ``(out, None)`` when nothing was drawn.

    Placement is independent of any snap grid. Width/height follow the label
    text (and optional art logo). Font size scales with ``size`` / ``size_scale``.

    ``art_layout`` (arted Rating): ``left`` / ``right`` of the score, ``above``,
    ``below``, or ``fill`` (logo as background; fill color paints the margin
    around it). ``fill_opacity`` fades the plate; ``text_opacity`` fades the score;
    ``logo_opacity`` fades the logo. ``rotation`` is CSS-clockwise degrees.
    ``text_orientation`` ``vertical`` stacks characters top-to-bottom.
    For ``above`` / ``below``, logo width matches an ``8.8`` score at the chip font
    (percentage scores like ``88%`` stay wider on purpose).
    """
    mods = _pillow()
    if mods is None:
        return out, None
    Image, ImageDraw, _, _, ImageOps = mods
    label = str(text or "").strip()
    if not label and art_path is None:
        return out, None

    layout = str(art_layout or "left").strip().lower()
    if layout == "beside":
        layout = "left"
    if layout not in {"left", "right", "above", "below", "fill"}:
        layout = "left"
    from services.poster_layouts import normalize_text_orientation

    vertical = normalize_text_orientation(text_orientation) == "vertical" and bool(label)
    if vertical:
        # Art layouts assume a horizontal score; keep logo beside stacked text as left.
        if layout not in {"left", "right", "fill"}:
            layout = "left"
        label = _verticalize_chip_text(label)

    img_w, img_h = out.size
    mult = _label_size_mult(size, size_scale)
    preferred = max(14, int(round(_layout_chip_font_size(img_w, img_h, landscape=landscape) * mult)))
    # Prefer size_scale type size; shrink only when the plate would leave the poster.
    # Low floor so long copy can still fit instead of clipping at the edges.
    min_font = max(11, int(round(preferred * 0.40)))
    # Vertical: ink boxes + letter gap (avoid metric line slots that inflate end pad).
    line_gap_ratio = 0.16 if vertical else 0.06
    if label:
        if vertical:
            font = _fit_layout_chip_font(
                label,
                preferred_size=preferred,
                min_size=min_font,
                max_width=max(24, int(img_w * min(0.42, 0.22 + 0.08 * mult))),
                max_height=img_h,
                line_gap_ratio=line_gap_ratio,
                vertical=True,
                pad_x_ratio=0.32,
                pad_x_min=7,
                pad_y_ratio=0.10,
                pad_y_min=3,
            )
        else:
            font = _fit_layout_chip_font(
                label,
                preferred_size=preferred,
                min_size=min_font,
                max_width=img_w,
                max_height=None,
                line_gap_ratio=line_gap_ratio,
                vertical=False,
                pad_x_ratio=0.38,
                pad_x_min=10,
                pad_y_ratio=0.28,
                pad_y_min=6,
            )
    else:
        font = _load_font(preferred)
    if font is None:
        return out, None
    # Use the fitted font size for padding/gaps so the plate hugs the glyphs.
    try:
        fitted_size = int(getattr(font, "size", preferred) or preferred)
    except Exception:
        fitted_size = preferred
    fitted_size = max(min_font, fitted_size)
    line_gap = max(2 if vertical else 1, int(round(fitted_size * line_gap_ratio)))
    if label and vertical:
        text_w, text_h = _measure_vertical_stack(font, label, gap=line_gap)
    elif label:
        text_w, text_h = _measure_text(font, label, line_spacing=line_gap)
    else:
        text_w, text_h = 0, fitted_size
    # Vertical spines use less end-cap pad so the plate hugs the stack like horizontal side pad.
    if vertical:
        pad_y = max(3, int(fitted_size * 0.10))
        pad_x = max(7, int(fitted_size * 0.32))
    else:
        pad_y = max(6, int(fitted_size * 0.28))
        pad_x = max(10, int(fitted_size * 0.38))
    # Arted chips size to the logo+score cluster; keep side pad tight like text chips.
    if art_path is not None:
        pad_x = max(5, int(fitted_size * 0.16))
        gap = max(3, int(fitted_size * 0.10)) if label else 0
        if layout in {"above", "below"}:
            # Logo width matches an "8.8" score at this type size (not "88%").
            ref_w, _ = _measure_text(font, "8.8")
            icon_h = max(12, int(ref_w))
        else:
            icon_h = max(12, int(round(max(text_h, fitted_size * 0.78))))
            if layout == "fill":
                # Larger mark so it reads as the chip background.
                icon_h = max(icon_h, int(round(fitted_size * 1.35)))
                pad_x = max(6, int(fitted_size * 0.22))
                pad_y = max(6, int(fitted_size * 0.22))
        if vertical and layout != "fill":
            pad_y = max(3, int(fitted_size * 0.10))
    else:
        gap = 0
        icon_h = max(12, int(round(max(text_h, fitted_size * 0.85))))
        layout = "left"
    icon_w = 0
    logo = None
    if art_path is not None:
        source_hint = ""
        if isinstance(art_path, Path):
            source_hint = art_path.stem
            logo = _prepare_rating_logo(source_hint, icon_h)
        if logo is None and isinstance(art_path, Path) and art_path.is_file():
            try:
                logo = Image.open(art_path).convert("RGBA")
                logo = ImageOps.contain(logo, (icon_h, icon_h), method=Image.Resampling.LANCZOS)
            except Exception as exc:
                logger.debug(f"Rating logo load failed: {exc}", extra={"emoji_type": "debug"})
                logo = None
        if logo is not None:
            icon_w = logo.width
            icon_h = logo.height
        else:
            art_path = None
            gap = 0
            layout = "left"

    if layout in {"above", "below"} and logo is not None:
        chip_w = max(text_w, icon_w) + pad_x * 2
        chip_h = text_h + icon_h + (gap if label else 0) + pad_y * 2
    elif layout == "fill" and logo is not None:
        # Score sits on the logo; chip hugs the logo with a thin color margin.
        chip_w = max(icon_w, text_w) + pad_x * 2
        chip_h = max(icon_h, text_h) + pad_y * 2
    else:
        chip_w = text_w + pad_x * 2 + icon_w + gap
        chip_h = max(text_h, icon_h) + pad_y * 2

    ax, ay = int(anchor[0]), int(anchor[1])
    # Draw the full chip on its own tile first. Drawing straight onto the poster
    # clipped leading glyphs when the unrotated plate hung past the canvas, and
    # rotate-then-crop made corner ribbons lose the first letter (e.g. PLACEHOLDER → LACEHOLDER).
    local_box = [0, 0, int(chip_w), int(chip_h)]

    fill_opacity = max(0.0, min(1.0, float(fill_opacity)))
    border_opacity = max(0.0, min(1.0, float(border_opacity)))
    text_opacity = max(0.0, min(1.0, float(text_opacity)))
    logo_opacity = max(0.0, min(1.0, float(logo_opacity)))
    fill_rgb = _parse_hex_rgba(fill_color, alpha=255)[:3]
    border_rgb = _parse_hex_rgba(border_color, alpha=255)[:3]
    text_rgb = _parse_hex_rgba(text_color, alpha=255)[:3]
    base = out.convert("RGBA") if out.mode != "RGBA" else out
    chip_layer = Image.new("RGBA", (int(chip_w), int(chip_h)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(chip_layer)
    radius = max(6, int(min(chip_w, chip_h) * 0.22))
    auto_stroke = max(2, int(round(fitted_size * 0.09)))
    if border_thickness is None:
        stroke = auto_stroke
    else:
        from services.poster_layouts import normalize_border_thickness

        stroke = normalize_border_thickness(border_thickness, default=0)
    fill_a = int(round(255 * fill_opacity))
    text_a = int(round(255 * text_opacity))
    logo_a = int(round(255 * logo_opacity)) if logo is not None else 255
    border_a = int(round(255 * border_opacity))
    text_fill = (*text_rgb, text_a)
    has_border = (
        border_a > 0
        and stroke > 0
        and chip_w > stroke * 2
        and chip_h > stroke * 2
    )
    if has_border:
        draw.rounded_rectangle(
            local_box,
            radius=radius,
            fill=(*border_rgb, border_a),
        )
        inner = [
            stroke,
            stroke,
            chip_w - stroke,
            chip_h - stroke,
        ]
        draw.rounded_rectangle(
            inner,
            radius=max(1, radius - stroke),
            fill=(*fill_rgb, fill_a),
        )
        content_box = inner
    else:
        draw.rounded_rectangle(local_box, radius=radius, fill=(*fill_rgb, fill_a))
        content_box = local_box

    content_w = content_box[2] - content_box[0]
    content_h = content_box[3] - content_box[1]
    mid_x = content_box[0] + content_w // 2
    mid_y = content_box[1] + content_h // 2

    def _paste_logo(lx: int, ly: int) -> None:
        """Composite the logo onto the plate so logo_opacity reveals fill, not the poster."""
        nonlocal logo, chip_layer, draw
        if logo is None:
            return
        mark = logo
        if logo_a < 255:
            mark = logo.copy()
            # Scale only the alpha channel; keep RGB.
            r, g, b, a = mark.split()
            a = a.point(lambda p: int(p * logo_a / 255))
            mark = Image.merge("RGBA", (r, g, b, a))
        # paste() replaces destination pixels; alpha_composite keeps the fill under a faded mark.
        layer = Image.new("RGBA", chip_layer.size, (0, 0, 0, 0))
        layer.paste(mark, (lx, ly), mark)
        chip_layer = Image.alpha_composite(chip_layer, layer)
        draw = ImageDraw.Draw(chip_layer)

    def _draw_label(cx: int, cy: int) -> None:
        if not label:
            return
        if vertical:
            _draw_vertical_stack(
                draw,
                mid_x=cx,
                top_y=cy - text_h // 2,
                text=label,
                font=font,
                fill=text_fill,
                gap=line_gap,
            )
        elif "\n" in label:
            draw.multiline_text(
                (cx, cy),
                label,
                fill=text_fill,
                font=font,
                anchor="mm",
                align="center",
                spacing=line_gap,
            )
        else:
            draw.text((cx, cy), label, fill=text_fill, font=font, anchor="mm")

    if layout == "fill" and logo is not None:
        lx = mid_x - logo.width // 2
        ly = mid_y - logo.height // 2
        _paste_logo(lx, ly)
        _draw_label(mid_x, mid_y)
    elif layout == "above" and logo is not None:
        cluster_h = icon_h + (gap if label else 0) + text_h
        top = content_box[1] + max(0, (content_h - cluster_h) // 2)
        _paste_logo(mid_x - logo.width // 2, top)
        if label:
            ty = top + icon_h + gap + text_h // 2
            _draw_label(mid_x, ty)
    elif layout == "below" and logo is not None:
        cluster_h = text_h + (gap if label else 0) + icon_h
        top = content_box[1] + max(0, (content_h - cluster_h) // 2)
        if label:
            _draw_label(mid_x, top + text_h // 2)
            _paste_logo(mid_x - logo.width // 2, top + text_h + gap)
        else:
            _paste_logo(mid_x - logo.width // 2, top)
    elif layout == "right" and logo is not None:
        cluster_w = text_w + gap + icon_w
        cluster_x = content_box[0] + max(0, (content_w - cluster_w) // 2)
        if label:
            tx = cluster_x + text_w // 2
            _draw_label(tx, mid_y)
            cluster_x += text_w + gap
        _paste_logo(cluster_x, mid_y - logo.height // 2)
    else:
        # left (default): logo then score
        cluster_w = icon_w + gap + text_w
        cluster_x = content_box[0] + max(0, (content_w - cluster_w) // 2)
        if logo is not None:
            _paste_logo(cluster_x, mid_y - logo.height // 2)
            cluster_x += logo.width + gap
        if label:
            tx = cluster_x + text_w // 2
            _draw_label(tx, mid_y)

    from services.poster_layouts import normalize_rotation

    rot = normalize_rotation(rotation)
    tile = chip_layer
    if rot % 360 != 0:
        # CSS clockwise ↔ Pillow counter-clockwise.
        tile = chip_layer.rotate(-rot, expand=True, resample=Image.Resampling.BICUBIC)
    rw, rh = tile.size
    # Same center as the desk sprite placement. Do not nudge on-canvas: if the user
    # parks a chip off the edge, bake should clip the same way. The offscreen tile
    # above is what fixes corner ribbons that used to lose leading glyphs while the
    # plate still looked on-poster (desk sprites render centered, then move).
    px = ax - rw // 2
    py = ay - rh // 2
    out_box = [int(px), int(py), int(px + rw), int(py + rh)]
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    _paste_rgba_clipped(overlay, tile, (px, py))
    return Image.alpha_composite(base, overlay), out_box


def _frac_axis_px(start: float, end: float, dim: int) -> tuple[int, int]:
    """Map axis fractions to bake pixels (may extend past the canvas for off-edge fills)."""
    d = max(1, int(dim))
    try:
        a = float(start)
    except (TypeError, ValueError):
        a = 0.0
    try:
        b = float(end)
    except (TypeError, ValueError):
        b = 1.0
    if not math.isfinite(a):
        a = 0.0
    if not math.isfinite(b):
        b = 1.0
    if b < a:
        a, b = b, a
    x0 = int(round(d * a))
    x1 = max(x0 + 1, int(round(d * b)))
    return x0, x1


def _paste_rgba_clipped(dest, src, origin: tuple[int, int]) -> None:
    """Paste ``src`` onto ``dest`` at ``origin``, clipping to dest bounds.

    Uses alpha compositing so source alpha is applied once. ``Image.paste`` with
    an RGBA mask multiplies alpha again, which made translucent fill rectangles
    look lighter in bake than the desk CSS ``rgba()`` overlay.
    """
    if src is None or dest is None:
        return
    dx, dy = int(origin[0]), int(origin[1])
    sw, sh = src.size
    dw, dh = dest.size
    sx0 = max(0, -dx)
    sy0 = max(0, -dy)
    sx1 = min(sw, dw - dx)
    sy1 = min(sh, dh - dy)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    crop = src.crop((sx0, sy0, sx1, sy1))
    if crop.mode != "RGBA":
        crop = crop.convert("RGBA")
    if dest.mode != "RGBA":
        # RGB destinations (opaque badges): mask paste is fine; no partial alpha to keep.
        dest.paste(crop, (dx + sx0, dy + sy0), crop)
        return
    dest.alpha_composite(crop, dest=(dx + sx0, dy + sy0))


def _draw_fill_frame_borders(
    draw,
    box: list[int],
    *,
    border_rgba: tuple[int, int, int, int],
    border_thickness_px: int,
    sides: tuple[bool, bool, bool, bool] | None = None,
) -> None:
    """Stroke sides of an axis-aligned fill box.

    ``sides`` is ``(top, right, bottom, left)``; omitted sides default to all on.
    """
    if border_rgba[3] <= 0:
        return
    x0, y0, x1, y1 = (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
    bw = max(1, x1 - x0)
    bh = max(1, y1 - y0)
    bt = max(0, int(border_thickness_px))
    if bt <= 0:
        return
    bt = min(bt, max(1, min(bw, bh) // 2))
    top_on, right_on, bottom_on, left_on = sides if sides is not None else (True, True, True, True)
    if top_on:
        draw.rectangle([x0, y0, x1, y0 + bt], fill=border_rgba)
    if bottom_on:
        draw.rectangle([x0, y1 - bt, x1, y1], fill=border_rgba)
    if left_on:
        draw.rectangle([x0, y0, x0 + bt, y1], fill=border_rgba)
    if right_on:
        draw.rectangle([x1 - bt, y0, x1, y1], fill=border_rgba)


def _fill_fade_ramp_alpha(i: int, span: int) -> int:
    """Alpha along one fade edge: 0 at the outer pixel → 255 at the inner edge.

    Uses Hermite smoothstep so the onset against bright art is softer than a
    linear ramp (which reads as a hard shelf).
    """
    n = max(1, int(span))
    if n <= 1:
        return 0
    t = max(0.0, min(1.0, float(i) / float(n - 1)))
    t = t * t * (3.0 - 2.0 * t)
    return int(round(255 * t))


def _apply_fill_edge_fade_mask(
    tile,
    *,
    fade_top: float,
    fade_right: float,
    fade_bottom: float,
    fade_left: float,
    Image,
):
    """Multiply tile alpha by per-edge smoothstep ramps (0 at outer → 1 inward)."""
    w, h = tile.size
    if w <= 0 or h <= 0:
        return tile
    from PIL import ImageChops
    from services.poster_layouts import normalize_fade_edge

    ft = normalize_fade_edge(fade_top)
    fr = normalize_fade_edge(fade_right)
    fb = normalize_fade_edge(fade_bottom)
    fl = normalize_fade_edge(fade_left)
    if ft <= 0 and fr <= 0 and fb <= 0 and fl <= 0:
        return tile
    mask = Image.new("L", (w, h), 255)
    px = mask.load()
    top_px = int(round(ft * h)) if ft > 0 else 0
    bottom_px = int(round(fb * h)) if fb > 0 else 0
    left_px = int(round(fl * w)) if fl > 0 else 0
    right_px = int(round(fr * w)) if fr > 0 else 0
    if top_px > 0:
        for y in range(min(top_px, h)):
            v = _fill_fade_ramp_alpha(y, top_px)
            for x in range(w):
                if px[x, y] > v:
                    px[x, y] = v
    if bottom_px > 0:
        for i in range(min(bottom_px, h)):
            y = h - 1 - i
            v = _fill_fade_ramp_alpha(i, bottom_px)
            for x in range(w):
                if px[x, y] > v:
                    px[x, y] = v
    if left_px > 0:
        for x in range(min(left_px, w)):
            v = _fill_fade_ramp_alpha(x, left_px)
            for y in range(h):
                if px[x, y] > v:
                    px[x, y] = v
    if right_px > 0:
        for i in range(min(right_px, w)):
            x = w - 1 - i
            v = _fill_fade_ramp_alpha(i, right_px)
            for y in range(h):
                if px[x, y] > v:
                    px[x, y] = v
    r, g, b, a = tile.split()
    a = ImageChops.multiply(a, mask)
    return Image.merge("RGBA", (r, g, b, a))


def _draw_layout_banner(
    out,
    *,
    fill_color: str,
    fill_opacity: float,
    x_start: float | None = None,
    x_end: float | None = None,
    y_start: float | None = None,
    y_end: float | None = None,
    # Legacy fields (orientation/span/depth or edge+thickness) when x/y absent.
    orientation: str | None = None,
    span_start: float | None = None,
    span_end: float | None = None,
    depth_start: float | None = None,
    depth_end: float | None = None,
    edge: str | None = None,
    size: str = "medium",
    thickness: float | None = None,
    rotation: int = 0,
    border_color: str = "#ffffff",
    border_opacity: float = 0.0,
    border_thickness: int = 3,
    fade_top: float = 0.0,
    fade_right: float = 0.0,
    fade_bottom: float = 0.0,
    fade_left: float = 0.0,
):
    """Draw a textless fill rectangle; chips supply copy.

    Shape-first: size the local width×height box, fade edges, paint remaining
    borders, then rotate around its center (CSS-clockwise). Clipped to the poster.
    """
    mods = _pillow()
    if mods is None:
        return out
    Image, ImageDraw, _, _, _ = mods
    from services.poster_layouts import (
        _fill_rect_geometry_from_raw,
        normalize_border_thickness,
        normalize_fade_edge,
        normalize_rotation,
    )

    base = out.convert("RGBA") if out.mode != "RGBA" else out
    w, h = base.size
    landscape = w > h
    xa, xb, ya, yb = _fill_rect_geometry_from_raw(
        {
            "x_start": x_start,
            "x_end": x_end,
            "y_start": y_start,
            "y_end": y_end,
            "orientation": orientation,
            "edge": edge,
            "thickness": thickness,
            "span_start": span_start,
            "span_end": span_end,
            "depth_start": depth_start,
            "depth_end": depth_end,
        },
        size=str(size or "medium"),
        landscape=landscape,
        row=0,
        col=0,
        cols=1,
        rows=1,
    )
    x0, x1 = _frac_axis_px(xa, xb, w)
    y0, y1 = _frac_axis_px(ya, yb, h)
    rect_w = max(1, abs(x1 - x0))
    rect_h = max(1, abs(y1 - y0))
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0

    fill_opacity = max(0.0, min(1.0, float(fill_opacity)))
    border_opacity = max(0.0, min(1.0, float(border_opacity)))
    fill_rgb = _parse_hex_rgba(fill_color, alpha=255)[:3]
    border_rgb = _parse_hex_rgba(border_color, alpha=255)[:3]
    fill_a = int(round(255 * fill_opacity))
    border_a = int(round(255 * border_opacity))
    border_px = normalize_border_thickness(border_thickness)
    ft = normalize_fade_edge(fade_top)
    fr = normalize_fade_edge(fade_right)
    fb = normalize_fade_edge(fade_bottom)
    fl = normalize_fade_edge(fade_left)
    if fill_a <= 0 and border_a <= 0:
        return base

    tile = Image.new("RGBA", (rect_w, rect_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    local_box = [0, 0, rect_w, rect_h]
    if fill_a > 0:
        draw.rectangle(local_box, fill=(*fill_rgb, fill_a))
        tile = _apply_fill_edge_fade_mask(
            tile,
            fade_top=ft,
            fade_right=fr,
            fade_bottom=fb,
            fade_left=fl,
            Image=Image,
        )
        draw = ImageDraw.Draw(tile)
    # Faded edges skip their border stroke.
    side_on = (ft <= 0, fr <= 0, fb <= 0, fl <= 0)
    if border_a > 0 and border_px > 0 and any(side_on):
        _draw_fill_frame_borders(
            draw,
            local_box,
            border_rgba=(*border_rgb, border_a),
            border_thickness_px=border_px,
            sides=side_on,
        )

    rot = normalize_rotation(rotation)
    if rot % 360 != 0:
        # CSS clockwise ↔ Pillow counter-clockwise.
        tile = tile.rotate(-rot, expand=True, resample=Image.Resampling.BICUBIC)
    tw, th = tile.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    _paste_rgba_clipped(
        overlay,
        tile,
        (int(round(cx - tw / 2)), int(round(cy - th / 2))),
    )
    return Image.alpha_composite(base, overlay)


def _resolve_layout_label_text(label: dict, token_map: dict) -> tuple[str, Path | None]:
    """Return (display text, optional rating art path) for a layout label."""
    token = str(label.get("token") or "").strip()
    art_path = None
    text = str(token_map.get(token, "") or "").strip()
    if token == "Custom":
        from services.poster_layouts import render_custom_chip_text

        text = render_custom_chip_text(str(label.get("custom_text") or ""), token_map)
    elif token == "Rating":
        prefer = str(label.get("rating_source") or "").strip().lower()
        arted = bool(label.get("arted")) and str(label.get("style") or "chip").strip().lower() != "banner"
        # Arted chips pick an explicit source (default IMDb). Missing source → dash (no IMDb fallback).
        if arted:
            if prefer in ("", "auto"):
                prefer = "imdb"
            keyed = str(token_map.get(f"Rating:{prefer}") or "").strip()
            text = keyed if keyed else "—"
            source = prefer
            art_path = _rating_logo_path(source)
        elif prefer not in ("", "auto"):
            keyed = str(token_map.get(f"Rating:{prefer}") or "").strip()
            # Explicit source with no value: dash instead of another provider's score.
            text = keyed if keyed else "—"
    return text, art_path


def _layout_anchor_xy(
    label: dict,
    *,
    width: int,
    height: int,
    cols: int,
    rows: int,
) -> tuple[int, int]:
    """Chip/badge center in pixels from fractional ``x``/``y`` (legacy row/col fallback)."""
    grid_cols = max(1, int(cols))
    grid_rows = max(1, int(rows))
    has_x = label.get("x") is not None
    has_y = label.get("y") is not None
    if has_x or has_y:
        try:
            fx = float(label.get("x")) if has_x else 0.5
        except (TypeError, ValueError):
            fx = 0.5
        try:
            fy = float(label.get("y")) if has_y else 0.5
        except (TypeError, ValueError):
            fy = 0.5
        if not math.isfinite(fx):
            fx = 0.5
        if not math.isfinite(fy):
            fy = 0.5
        # Centers stay on-canvas; half the chip may still hang past the edge.
        fx = max(0.0, min(1.0, fx))
        fy = max(0.0, min(1.0, fy))
        return int(round(fx * width)), int(round(fy * height))
    row = int(label.get("row") or 0)
    col = int(label.get("col") or 0)
    row = max(0, min(grid_rows - 1, row))
    col = max(0, min(grid_cols - 1, col))
    cell_w = width / grid_cols
    cell_h = height / grid_rows
    return int((col + 0.5) * cell_w), int((row + 0.5) * cell_h)


def apply_layout_overlay(
    img,
    *,
    landscape: bool = False,
    labels: list | None = None,
    tokens: dict | None = None,
    grayscale: bool | None = None,
    cols: int | None = None,
    rows: int | None = None,
):
    """Composite grayscale + fraction-anchored labels (banners, chips, badge)."""
    if _pillow() is None:
        _log_pillow_missing_once()
        return None
    target = LANDSCAPE_SIZE if landscape else PORTRAIT_SIZE
    out = _fit_image(img, target)
    gs = poster_grayscale_enabled() if grayscale is None else bool(grayscale)
    if gs:
        out = _apply_grayscale(out)
    label_list = list(labels or [])
    token_map = dict(tokens or {})
    if not label_list:
        return out
    w, h = out.size
    grid_cols = max(1, int(cols or (16 if landscape else 12)))
    grid_rows = max(1, int(rows or (10 if landscape else 18)))

    def _is_banner_label(lab: dict) -> bool:
        token = str(lab.get("token") or "").strip()
        style = str(lab.get("style") or "").strip().lower()
        return token in {"FillRectangle", "Banner"} or style == "banner"

    # Fill rectangles first, then chips / badge on top.
    banners = [lab for lab in label_list if isinstance(lab, dict) and _is_banner_label(lab)]
    chips = [lab for lab in label_list if isinstance(lab, dict) and not _is_banner_label(lab)]
    from services.poster_layouts import normalize_border_thickness, normalize_rotation

    for label in banners:
        label_size = str(label.get("size") or "medium").strip().lower()
        raw_thickness = label.get("thickness")
        try:
            thickness_val = float(raw_thickness) if raw_thickness is not None else None
        except (TypeError, ValueError):
            thickness_val = None
        orient_raw = str(label.get("orientation") or "").strip().lower() or None
        edge_raw = str(label.get("edge") or "").strip().lower() or None
        border_opacity = label.get("border_opacity")
        if border_opacity is None:
            border_opacity = 0.0

        out = _draw_layout_banner(
            out,
            fill_color=str(label.get("fill_color") or "#000000"),
            fill_opacity=float(label.get("fill_opacity") if label.get("fill_opacity") is not None else 0.65),
            x_start=label.get("x_start"),
            x_end=label.get("x_end"),
            y_start=label.get("y_start"),
            y_end=label.get("y_end"),
            orientation=orient_raw,
            span_start=label.get("span_start"),
            span_end=label.get("span_end"),
            depth_start=label.get("depth_start"),
            depth_end=label.get("depth_end"),
            edge=edge_raw,
            size=label_size,
            thickness=thickness_val,
            rotation=normalize_rotation(label.get("rotation")),
            border_color=str(label.get("border_color") or "#ffffff"),
            border_opacity=float(border_opacity),
            border_thickness=normalize_border_thickness(label.get("border_thickness")),
            fade_top=float(label.get("fade_top") or 0),
            fade_right=float(label.get("fade_right") or 0),
            fade_bottom=float(label.get("fade_bottom") or 0),
            fade_left=float(label.get("fade_left") or 0),
        )

    for label in chips:
        token = str(label.get("token") or "").strip()
        if token in {"FillRectangle", "Banner"}:
            continue
        ax, ay = _layout_anchor_xy(label, width=w, height=h, cols=grid_cols, rows=grid_rows)
        label_size = str(label.get("size") or ("medium" if token == "Badge" else "small")).strip().lower()
        raw_scale = label.get("size_scale")
        try:
            size_scale = float(raw_scale) if raw_scale is not None else None
        except (TypeError, ValueError):
            size_scale = None
        if token == "Badge":
            mods = _pillow()
            if mods is None:
                continue
            # Medium matches legacy corner badge; size_scale / small/large scale from that.
            base_mult = _label_size_mult(_BADGE_SIZE_BASE)
            badge_w = max(
                24,
                int(round(_corner_badge_size(w, h) * (_label_size_mult(label_size, size_scale) / base_mult))),
            )
            variant = _badge_logo_variant(label.get("fill_color"))
            badge = _build_corner_logo_badge(badge_w, variant=variant)
            if badge is None:
                continue
            Image, _, _, _, _ = mods
            rot = normalize_rotation(label.get("rotation"))
            if rot % 360 != 0:
                badge = badge.rotate(-rot, expand=True, resample=Image.Resampling.BICUBIC)
            bw, bh = badge.size
            if out.mode != "RGBA":
                out = out.convert("RGBA")
            if out.mode != "RGBA":
                out = out.convert("RGBA")
            bx = ax - bw // 2
            by = ay - bh // 2
            _paste_rgba_clipped(out, badge, (bx, by))
            continue
        text, art_path = _resolve_layout_label_text(label, token_map)
        if not text and art_path is None:
            continue
        border_opacity = label.get("border_opacity")
        if border_opacity is None:
            # Legacy ``#RRGGBBAA``: alpha 00 meant no stroke.
            legacy = _parse_hex_rgba(str(label.get("border_color") or "#00000000"))
            border_opacity = legacy[3] / 255.0
        art_layout = "left"
        if token == "Rating" and art_path is not None:
            art_layout = str(label.get("rating_art_layout") or "left").strip().lower() or "left"
            if art_layout == "beside":
                art_layout = "left"
        painted, _box = _draw_layout_chip(
            out,
            text=text,
            anchor=(ax, ay),
            text_color=str(label.get("text_color") or "#ffffff"),
            fill_color=str(label.get("fill_color") or "#000000"),
            border_color=str(label.get("border_color") or "#000000"),
            fill_opacity=float(label.get("fill_opacity") if label.get("fill_opacity") is not None else 0.65),
            border_opacity=float(border_opacity),
            border_thickness=normalize_border_thickness(label.get("border_thickness")),
            text_opacity=float(label.get("text_opacity") if label.get("text_opacity") is not None else 1.0),
            logo_opacity=float(label.get("logo_opacity") if label.get("logo_opacity") is not None else 1.0),
            size=label_size,
            size_scale=size_scale,
            art_path=art_path,
            art_layout=art_layout,
            landscape=landscape,
            rotation=normalize_rotation(label.get("rotation")),
            text_orientation=str(label.get("text_orientation") or "horizontal"),
        )
        out = painted
    if out.mode == "RGBA":
        out = out.convert("RGB")
    return out


def _paint_one_layout_label(
    out,
    label: dict,
    *,
    tokens: dict | None = None,
    cols: int = 12,
    rows: int = 18,
    landscape: bool = False,
):
    """Draw a single layout label onto ``out``.

    Returns ``(image, crop_box)`` where ``crop_box`` is ``[x0, y0, x1, y1]`` for
    chips/badges (exact bake paste box), or ``None`` for fills / empty draws.
    """
    if not isinstance(label, dict):
        return out, None
    mods = _pillow()
    if mods is None:
        return out, None
    token_map = dict(tokens or {})
    w, h = out.size
    grid_cols = max(1, int(cols))
    grid_rows = max(1, int(rows))
    token = str(label.get("token") or "").strip()
    style = str(label.get("style") or "").strip().lower()
    is_banner = token in {"FillRectangle", "Banner"} or style == "banner"
    if is_banner:
        label_size = str(label.get("size") or "medium").strip().lower()
        raw_thickness = label.get("thickness")
        try:
            thickness_val = float(raw_thickness) if raw_thickness is not None else None
        except (TypeError, ValueError):
            thickness_val = None
        orient_raw = str(label.get("orientation") or "").strip().lower() or None
        edge_raw = str(label.get("edge") or "").strip().lower() or None
        from services.poster_layouts import normalize_border_thickness, normalize_rotation

        border_opacity = label.get("border_opacity")
        if border_opacity is None:
            border_opacity = 0.0
        painted = _draw_layout_banner(
            out,
            fill_color=str(label.get("fill_color") or "#000000"),
            fill_opacity=float(label.get("fill_opacity") if label.get("fill_opacity") is not None else 0.65),
            x_start=label.get("x_start"),
            x_end=label.get("x_end"),
            y_start=label.get("y_start"),
            y_end=label.get("y_end"),
            orientation=orient_raw,
            span_start=label.get("span_start"),
            span_end=label.get("span_end"),
            depth_start=label.get("depth_start"),
            depth_end=label.get("depth_end"),
            edge=edge_raw,
            size=label_size,
            thickness=thickness_val,
            rotation=normalize_rotation(label.get("rotation")),
            border_color=str(label.get("border_color") or "#ffffff"),
            border_opacity=float(border_opacity),
            border_thickness=normalize_border_thickness(label.get("border_thickness")),
            fade_top=float(label.get("fade_top") or 0),
            fade_right=float(label.get("fade_right") or 0),
            fade_bottom=float(label.get("fade_bottom") or 0),
            fade_left=float(label.get("fade_left") or 0),
        )
        return painted, None
    ax, ay = _layout_anchor_xy(label, width=w, height=h, cols=grid_cols, rows=grid_rows)
    label_size = str(label.get("size") or ("medium" if token == "Badge" else "small")).strip().lower()
    raw_scale = label.get("size_scale")
    try:
        size_scale = float(raw_scale) if raw_scale is not None else None
    except (TypeError, ValueError):
        size_scale = None
    from services.poster_layouts import normalize_border_thickness, normalize_rotation

    if token == "Badge":
        Image, _, _, _, _ = mods
        base_mult = _label_size_mult(_BADGE_SIZE_BASE)
        badge_w = max(
            24,
            int(round(_corner_badge_size(w, h) * (_label_size_mult(label_size, size_scale) / base_mult))),
        )
        variant = _badge_logo_variant(label.get("fill_color"))
        badge = _build_corner_logo_badge(badge_w, variant=variant)
        if badge is None:
            return out, None
        rot = normalize_rotation(label.get("rotation"))
        if rot % 360 != 0:
            badge = badge.rotate(-rot, expand=True, resample=Image.Resampling.BICUBIC)
        bw, bh = badge.size
        base = out.convert("RGBA") if out.mode != "RGBA" else out
        bx = ax - bw // 2
        by = ay - bh // 2
        _paste_rgba_clipped(base, badge, (bx, by))
        # Crop box is the unclipped AABB (desk places by center + sprite size).
        return base, [bx, by, bx + bw, by + bh]
    text, art_path = _resolve_layout_label_text(label, token_map)
    if not text and art_path is None:
        return out, None
    border_opacity = label.get("border_opacity")
    if border_opacity is None:
        legacy = _parse_hex_rgba(str(label.get("border_color") or "#00000000"))
        border_opacity = legacy[3] / 255.0
    art_layout = "left"
    if token == "Rating" and art_path is not None:
        art_layout = str(label.get("rating_art_layout") or "left").strip().lower() or "left"
        if art_layout == "beside":
            art_layout = "left"
    return _draw_layout_chip(
        out,
        text=text,
        anchor=(ax, ay),
        text_color=str(label.get("text_color") or "#ffffff"),
        fill_color=str(label.get("fill_color") or "#000000"),
        border_color=str(label.get("border_color") or "#000000"),
        fill_opacity=float(label.get("fill_opacity") if label.get("fill_opacity") is not None else 0.65),
        border_opacity=float(border_opacity),
        border_thickness=normalize_border_thickness(label.get("border_thickness")),
        text_opacity=float(label.get("text_opacity") if label.get("text_opacity") is not None else 1.0),
        logo_opacity=float(label.get("logo_opacity") if label.get("logo_opacity") is not None else 1.0),
        size=label_size,
        size_scale=size_scale,
        art_path=art_path,
        art_layout=art_layout,
        landscape=landscape,
        rotation=normalize_rotation(label.get("rotation")),
        text_orientation=str(label.get("text_orientation") or "horizontal"),
    )


def render_layout_label_sprite_png(
    label: dict,
    *,
    landscape: bool = False,
    tokens: dict | None = None,
    cols: int | None = None,
    rows: int | None = None,
) -> tuple[bytes, int, int] | None:
    """Render one layout label to a cropped transparent PNG for the designer desk.

    Draws the label centered on a bake-sized canvas, then crops to the **exact**
    bake paste box (badge edge length / chip clamp box), not an alpha getbbox.
    """
    mods = _pillow()
    if mods is None:
        _log_pillow_missing_once()
        return None
    if not isinstance(label, dict):
        return None
    Image = mods[0]
    target = LANDSCAPE_SIZE if landscape else PORTRAIT_SIZE
    canvas = Image.new("RGBA", target, (0, 0, 0, 0))
    token = str(label.get("token") or "").strip()
    style = str(label.get("style") or "").strip().lower()
    is_banner = token in {"FillRectangle", "Banner"} or style == "banner"
    lab = dict(label)
    if not is_banner:
        # Size-only render: center so edge clamp does not shrink the crop.
        lab["x"] = 0.5
        lab["y"] = 0.5
    grid_cols = max(1, int(cols or (16 if landscape else 12)))
    grid_rows = max(1, int(rows or (10 if landscape else 18)))
    painted, crop_box = _paint_one_layout_label(
        canvas,
        lab,
        tokens=tokens,
        cols=grid_cols,
        rows=grid_rows,
        landscape=landscape,
    )
    if painted is None:
        return None
    if painted.mode != "RGBA":
        painted = painted.convert("RGBA")
    if crop_box and len(crop_box) == 4:
        x0, y0, x1, y1 = (int(crop_box[0]), int(crop_box[1]), int(crop_box[2]), int(crop_box[3]))
        if x1 > x0 and y1 > y0:
            cropped = painted.crop((x0, y0, x1, y1))
        else:
            return None
    else:
        bbox = painted.getbbox()
        if not bbox:
            return None
        cropped = painted.crop(bbox)
    buf = io.BytesIO()
    cropped.save(buf, format="PNG", optimize=True)
    cw, ch = cropped.size
    return buf.getvalue(), int(cw), int(ch)


def apply_overlay(
    img,
    mode: str | None = None,
    *,
    landscape: bool = False,
    overlay_text: str | None = None,
    grayscale: bool | None = None,
    badge: bool | None = None,
    badge_position: str | None = None,
    banner_position: str | None = None,
    layout_labels: list | None = None,
    layout_tokens: dict | None = None,
    layout_cols: int | None = None,
    layout_rows: int | None = None,
):
    """Composite layered poster overlays.

    When ``layout_labels`` is provided, draws the grid layout path (plus optional
    grayscale). Otherwise uses independent layers (grayscale / text bar / badge).
    The ``mode`` argument is legacy for exclusive modes when layer kwargs are omitted.
    """
    if _pillow() is None:
        _log_pillow_missing_once()
        return None

    if layout_labels is not None:
        return apply_layout_overlay(
            img,
            landscape=landscape,
            labels=layout_labels,
            tokens=layout_tokens,
            grayscale=grayscale,
            cols=layout_cols,
            rows=layout_rows,
        )

    want = str(mode or "").strip().lower()
    use_layers = (
        grayscale is not None
        or badge is not None
        or poster_overlay_enabled()
        or want in {"", COMPOSE_MODE}
    )

    if use_layers:
        gs = poster_grayscale_enabled() if grayscale is None else bool(grayscale)
        badge_on = poster_badge_enabled() if badge is None else bool(badge)
        badge_pos = (
            poster_badge_position()
            if badge_position is None
            else str(badge_position).strip().lower().replace("-", "_")
        )
        if badge_pos not in _VALID_BADGE_POSITIONS:
            badge_pos = "bottom_right"
        ban_pos = (
            poster_banner_position()
            if banner_position is None
            else str(banner_position).strip().lower()
        )
        if ban_pos not in _VALID_BANNER_POSITIONS:
            ban_pos = "top"
        if not gs and not badge_on and not str(overlay_text or "").strip():
            # No layers and no text: return fitted image unchanged (raw look).
            target = LANDSCAPE_SIZE if landscape else PORTRAIT_SIZE
            return _fit_image(img, target)
        target = LANDSCAPE_SIZE if landscape else PORTRAIT_SIZE
        out = _fit_image(img, target)
        if gs:
            out = _apply_grayscale(out)
        label = str(overlay_text or "").strip() or None
        if label:
            avoid = _banner_badge_collision_side(ban_pos, badge_pos, badge_on=badge_on)
            out = _apply_top_banner(out, text=label, position=ban_pos, avoid_badge_side=avoid)
        if badge_on:
            shared_edge = ban_pos if label else None
            out = _apply_corner_logo(out, position=badge_pos, banner_edge=shared_edge)
        return out

    # Legacy exclusive-mode path (tests / pre-migration).
    if want not in _VALID_MODES or want == "off":
        return img
    target = LANDSCAPE_SIZE if landscape else PORTRAIT_SIZE
    base = _fit_image(img, target)
    label = str(overlay_text or "").strip() or None
    banner_pos = poster_banner_position() if banner_position is None else str(banner_position)
    if want == "grayscale":
        out = _apply_grayscale(base)
        if label:
            out = _apply_top_banner(out, text=label, position=banner_pos)
        return out
    if want == "top_banner":
        return _apply_top_banner(base, text=label or "PLACEHOLDER", position=banner_pos)
    if want == "corner_logo":
        out = base
        if label:
            avoid = _banner_badge_collision_side(
                banner_pos, "bottom_right", badge_on=True
            )
            out = _apply_top_banner(out, text=label, position=banner_pos, avoid_badge_side=avoid)
        return _apply_corner_logo(
            out, position="bottom_right", banner_edge=banner_pos if label else None
        )
    return base


def save_raw_poster_from_url(url: str | None, path: str, *, quality: int = 88) -> bool:
    """Download remote art and save as JPEG without compositing."""
    data = download_poster_bytes(url or "")
    if not data:
        return False
    mods = _pillow()
    if mods is None:
        try:
            from services.placeholders import _apply_dir_chain_permissions, _ensure_open_permissions

            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            _apply_dir_chain_permissions(path)
            with open(path, "wb") as f:
                f.write(data)
            _ensure_open_permissions(path)
            return os.path.isfile(path) and os.path.getsize(path) > 0
        except OSError as exc:
            logger.warning(f"Failed to write raw poster {path!r}: {exc}", extra={"emoji_type": "warning"})
            return False
    img = load_image_from_bytes(data)
    if img is None:
        return False
    return save_jpeg(img, path, quality=quality)


def composite_poster_from_url(
    url: str | None,
    mode: str,
    *,
    landscape: bool = False,
    overlay_text: str | None = None,
    layout_labels: list | None = None,
    layout_tokens: dict | None = None,
    layout_cols: int | None = None,
    layout_rows: int | None = None,
    grayscale: bool | None = None,
):
    if _pillow() is None:
        _log_pillow_missing_once()
        return None
    data = download_poster_bytes(url or "")
    if not data:
        return None
    img = load_image_from_bytes(data)
    if img is None:
        return None
    return apply_overlay(
        img,
        mode,
        landscape=landscape,
        overlay_text=overlay_text,
        layout_labels=layout_labels,
        layout_tokens=layout_tokens,
        layout_cols=layout_cols,
        layout_rows=layout_rows,
        grayscale=grayscale,
    )


def _preview_base_image(*, kind: str = "movie", base_url: str | None = None):
    """Load a color sample for Settings live preview (not a pre-composited mode).

    ``kind``: ``movie`` / ``series`` (portrait) or ``episode`` (landscape thumb).
    ``base_url``: optional remote poster/thumb URL (TMDB or library).
    """
    mods = _pillow()
    if mods is None:
        return None
    Image = mods[0]
    root = Path(__file__).resolve().parent.parent
    want = str(kind or "movie").strip().lower()
    if want not in {"movie", "series", "episode"}:
        want = "movie"
    target = LANDSCAPE_SIZE if want == "episode" else PORTRAIT_SIZE
    url = str(base_url or "").strip()
    if url:
        data = download_poster_bytes(url)
        if data:
            img = load_image_from_bytes(data)
            if img is not None:
                return _fit_image(img, target).convert("RGB")
    if want == "episode":
        names = ("preview-episode.jpg",)
        fallback_color = (36, 52, 78)
    elif want == "series":
        names = ("preview-series.jpg", "preview-base.jpg")
        fallback_color = (48, 72, 110)
    else:
        # Prefer clean color base. Never use grayscale.jpg (already desaturated).
        names = ("preview-base.jpg", "corner_logo.jpg")
        fallback_color = (48, 72, 110)
    candidates: list[Path] = []
    for name in names:
        candidates.append(root / "frontend" / "public" / "overlay-examples" / name)
        candidates.append(root / "frontend" / "dist" / "overlay-examples" / name)
    for path in candidates:
        if path.is_file():
            try:
                return Image.open(path).convert("RGB")
            except Exception:
                continue
    return Image.new("RGB", target, fallback_color)


def _dim_preview_base(img, *, amount: float = 0.42):
    """Darken sample art before labels so chips stay readable (labels drawn after)."""
    mods = _pillow()
    if mods is None or img is None:
        return img
    ImageEnhance = mods[2]
    factor = max(0.15, min(1.0, float(amount)))
    try:
        return ImageEnhance.Brightness(img.convert("RGB")).enhance(factor)
    except Exception:
        return img


def render_overlay_preview_jpeg(
    *,
    kind: str = "movie",
    grayscale: bool = False,
    badge: bool = False,
    badge_position: str = "bottom_right",
    placeholder_banner: bool = False,
    coming_soon_banner: bool = False,
    banner_position: str = "top",
    profile_key: str | None = None,
    layout_labels: list | None = None,
    dim_base: bool = False,
    base_url: str | None = None,
    sample_token_overrides: dict | None = None,
    quality: int = 85,
) -> bytes | None:
    """Composite a sample poster/thumb for the live Settings preview.

    ``kind`` selects the sample and banner copy:
    - ``movie``: portrait, Coming Soon → ``Digital - Oct 22``
    - ``series``: portrait TV title, Coming Soon → ``New Ep. - Oct 22``
    - ``episode``: landscape thumb, Coming Soon → ``Airing - Oct 22``

    When ``profile_key`` or ``layout_labels`` is set, uses the grid layout path.
    ``dim_base`` darkens the sample art before labels (designer readability).
    ``base_url`` swaps the bundled sample for TMDB/library art.
    ``sample_token_overrides`` merges Title/Year/dates/ratings from preview-art.
    """
    mods = _pillow()
    if mods is None:
        return None
    want = str(kind or "movie").strip().lower()
    if want not in {"movie", "series", "episode"}:
        want = "movie"
    base = _preview_base_image(kind=want, base_url=base_url)
    if base is None:
        return None
    if dim_base:
        base = _dim_preview_base(base, amount=0.38)
    landscape = want == "episode"

    use_layout = layout_labels is not None or bool(str(profile_key or "").strip())
    if use_layout:
        from services.poster_layouts import (
            apply_sample_token_overrides,
            get_profile,
            grid_size_for_profile,
            sample_tokens_for_profile,
        )

        key = str(profile_key or "").strip()
        if not key:
            key = (
                f"{'episode' if want == 'episode' else ('tv' if want == 'series' else 'movie')}"
                f".placeholder.default"
            )
        profile = get_profile(key)
        labels = layout_labels if layout_labels is not None else list((profile.get("labels") or []))
        # Draft previews pass grayscale explicitly; saved-profile previews use the profile flag.
        gs = bool(grayscale) if layout_labels is not None else bool(profile.get("grayscale"))
        tokens = apply_sample_token_overrides(
            sample_tokens_for_profile(key),
            sample_token_overrides if isinstance(sample_token_overrides, dict) else None,
        )
        cols, rows = grid_size_for_profile(key)
        out = apply_overlay(
            base,
            COMPOSE_MODE,
            landscape=landscape,
            grayscale=gs,
            layout_labels=labels,
            layout_tokens=tokens,
            layout_cols=cols,
            layout_rows=rows,
        )
    else:
        overlay_text = None
        if coming_soon_banner:
            if want == "series":
                overlay_text = "New Ep. - Oct 22"
            elif want == "episode":
                overlay_text = "Airing - Oct 22"
            else:
                overlay_text = "Digital - Oct 22"
        elif placeholder_banner:
            overlay_text = "PLACEHOLDER"
        out = apply_overlay(
            base,
            COMPOSE_MODE,
            landscape=landscape,
            overlay_text=overlay_text,
            grayscale=grayscale,
            badge=badge,
            badge_position=badge_position,
            banner_position=banner_position,
        )
    if out is None:
        return None
    buf = io.BytesIO()
    out.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def save_jpeg(img, path: str, *, quality: int = 88) -> bool:
    mods = _pillow()
    if mods is None:
        _log_pillow_missing_once()
        return False
    Image = mods[0]
    try:
        from services.placeholders import _apply_dir_chain_permissions, _ensure_open_permissions

        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _apply_dir_chain_permissions(path)
        rgb = img.convert("RGB")
        rgb.save(path, format="JPEG", quality=quality, optimize=True)
        _ensure_open_permissions(path)
        return True
    except Exception as exc:
        logger.warning(f"Failed to write poster JPEG {path!r}: {exc}", extra={"emoji_type": "warning"})
        return False
