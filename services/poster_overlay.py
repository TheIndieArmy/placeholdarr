"""Download and composite placeholder poster overlays for media-player local art."""

from __future__ import annotations

import io
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
    return {
        "grayscale": bool(poster_grayscale_enabled()),
        "badge": bool(poster_badge_enabled()),
        "badge_position": poster_badge_position(),
        "placeholder_banner": bool(poster_placeholder_banner_enabled()),
        "coming_soon_banner": bool(coming_soon_banner_enabled()),
        "banner_position": poster_banner_position(),
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
        return max(28, min(56, int(img_h * 0.055)))
    return max(26, min(52, int(img_w * 0.048)))


def _label_size_mult(size: str | None) -> float:
    try:
        from services.poster_layouts import size_multiplier

        return float(size_multiplier(str(size or "small")))
    except Exception:
        key = str(size or "small").strip().lower()
        return {"small": 1.0, "medium": 1.5, "large": 2.0}.get(key, 1.0)


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


def _measure_text(font, text: str) -> tuple[int, int]:
    try:
        bbox = font.getbbox(text)
        if bbox:
            return max(1, int(bbox[2] - bbox[0])), max(1, int(bbox[3] - bbox[1]))
    except Exception:
        pass
    return max(1, len(text) * 10), 16


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
    size: str = "small",
    art_path: Path | None = None,
    landscape: bool = False,
):
    """Draw a content-sized chip centered on ``anchor`` (snap cell center).

    Grid cells only choose *where* the chip sits. Width/height follow the label
    text (and optional art logo). Font size scales with ``size``.
    """
    mods = _pillow()
    if mods is None:
        return out
    Image, ImageDraw, _, _, ImageOps = mods
    label = str(text or "").strip()
    if not label and art_path is None:
        return out

    img_w, img_h = out.size
    mult = _label_size_mult(size)
    preferred = max(16, int(round(_layout_chip_font_size(img_w, img_h, landscape=landscape) * mult)))
    # Cap width so a long string shrinks type instead of spanning the whole poster.
    max_text_w = max(80, int(img_w * 0.82))
    font = _fit_banner_font(label or "0", max_width=max_text_w, preferred_size=preferred, min_size=max(12, int(14 * mult)))
    if font is None:
        return out
    text_w, text_h = _measure_text(font, label) if label else (0, preferred)
    pad_y = max(8, int(preferred * 0.32))
    # Arted chips size to the logo+score cluster; keep side pad tight like text chips.
    if art_path is not None:
        pad_x = max(5, int(preferred * 0.16))
        gap = max(3, int(preferred * 0.10)) if label else 0
        icon_h = max(12, int(round(max(text_h, preferred * 0.78))))
    else:
        pad_x = max(14, int(preferred * 0.55))
        gap = 0
        icon_h = max(12, int(round(max(text_h, preferred * 0.85))))
    icon_w = 0
    logo = None
    if art_path is not None:
        # ``art_path`` may be a Path or a source key resolved earlier.
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
    chip_w = text_w + pad_x * 2 + icon_w + gap
    chip_h = max(text_h, icon_h) + pad_y * 2
    ax, ay = int(anchor[0]), int(anchor[1])
    box = _clamp_box_to_image(
        [ax - chip_w // 2, ay - chip_h // 2, ax - chip_w // 2 + chip_w, ay - chip_h // 2 + chip_h],
        img_w=img_w,
        img_h=img_h,
    )

    fill_opacity = max(0.0, min(1.0, float(fill_opacity)))
    border_opacity = max(0.0, min(1.0, float(border_opacity)))
    fill_rgb = _parse_hex_rgba(fill_color, alpha=255)[:3]
    border_rgb = _parse_hex_rgba(border_color, alpha=255)[:3]
    text_rgb = _parse_hex_rgba(text_color, alpha=255)[:3]
    # Fit / grayscale paths often leave RGB; alpha_composite requires RGBA.
    base = out.convert("RGBA") if out.mode != "RGBA" else out
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    radius = max(6, int(min(box[2] - box[0], box[3] - box[1]) * 0.22))
    stroke = max(3, int(round(preferred * 0.09)))
    fill_a = int(round(255 * fill_opacity))
    border_a = int(round(255 * border_opacity))
    has_border = (
        border_a > 0
        and box[2] - box[0] > stroke * 2
        and box[3] - box[1] > stroke * 2
    )
    if has_border:
        draw.rounded_rectangle(
            box,
            radius=radius,
            fill=(*border_rgb, border_a),
        )
        inner = [
            box[0] + stroke,
            box[1] + stroke,
            box[2] - stroke,
            box[3] - stroke,
        ]
        draw.rounded_rectangle(
            inner,
            radius=max(1, radius - stroke),
            fill=(*fill_rgb, fill_a),
        )
        content_box = inner
    else:
        draw.rounded_rectangle(box, radius=radius, fill=(*fill_rgb, fill_a))
        content_box = box

    content_w = content_box[2] - content_box[0]
    content_h = content_box[3] - content_box[1]
    cluster_w = icon_w + gap + text_w
    cluster_x = content_box[0] + max(0, (content_w - cluster_w) // 2)
    mid_y = content_box[1] + content_h // 2
    if logo is not None:
        lx = cluster_x
        ly = mid_y - logo.height // 2
        overlay.paste(logo, (lx, ly), logo)
        cluster_x += logo.width + gap
    if label:
        tx = cluster_x + text_w // 2
        draw.text((tx, mid_y), label, fill=(*text_rgb, 255), font=font, anchor="mm")
    return Image.alpha_composite(base, overlay)


def _layout_banner_thickness(
    img_w: int,
    img_h: int,
    *,
    edge: str,
    size: str = "medium",
    thickness: float | None = None,
) -> int:
    """Full-edge banner bar thickness from a continuous depth fraction (or legacy size)."""
    from services.poster_layouts import _normalize_banner_thickness

    w, h = int(img_w), int(img_h)
    landscape = w > h
    side = str(edge or "top").strip().lower() in {"left", "right"}
    span = w if side else h
    frac = _normalize_banner_thickness(
        thickness, size=str(size or "medium"), edge=str(edge or "top"), landscape=landscape
    )
    return max(36, int(round(span * frac)))


def _draw_layout_banner(
    out,
    *,
    edge: str = "top",
    fill_color: str,
    fill_opacity: float,
    size: str = "medium",
    thickness: float | None = None,
):
    """Draw a textless full-edge fill strip (chips supply any copy on top)."""
    mods = _pillow()
    if mods is None:
        return out
    Image, ImageDraw, _, _, _ = mods
    base = out.convert("RGBA") if out.mode != "RGBA" else out
    w, h = base.size
    side = str(edge or "top").strip().lower()
    if side not in {"top", "bottom", "left", "right"}:
        side = "top"
    bar = _layout_banner_thickness(w, h, edge=side, size=size, thickness=thickness)
    if side == "top":
        box = [0, 0, w, bar]
    elif side == "bottom":
        box = [0, max(0, h - bar), w, h]
    elif side == "left":
        box = [0, 0, bar, h]
    else:
        box = [max(0, w - bar), 0, w, h]

    fill_opacity = max(0.0, min(1.0, float(fill_opacity)))
    fill_rgb = _parse_hex_rgba(fill_color, alpha=255)[:3]
    fill_a = int(round(255 * fill_opacity))
    if fill_a <= 0:
        return base
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle(box, fill=(*fill_rgb, fill_a))
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
        # Arted chips pick an explicit source (default IMDb). Text chips keep Arr's preferred rating.
        if arted:
            if prefer in ("", "auto"):
                prefer = "imdb"
            keyed = str(token_map.get(f"Rating:{prefer}") or "").strip()
            text = keyed or text
            source = prefer
            art_path = _rating_logo_path(source)
        elif prefer not in ("", "auto"):
            keyed = str(token_map.get(f"Rating:{prefer}") or "").strip()
            if keyed:
                text = keyed
    return text, art_path


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
    """Composite grayscale + grid-anchored labels (banners, chips, badge)."""
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
    grid_cols = int(cols or (16 if landscape else 12))
    grid_rows = int(rows or (10 if landscape else 18))
    grid_cols = max(1, grid_cols)
    grid_rows = max(1, grid_rows)
    cell_w = w / grid_cols
    cell_h = h / grid_rows

    def _is_banner_label(lab: dict) -> bool:
        token = str(lab.get("token") or "").strip()
        style = str(lab.get("style") or "").strip().lower()
        return token == "Banner" or style == "banner"

    # Banners first (textless fill strips), then chips / badge on top.
    banners = [lab for lab in label_list if isinstance(lab, dict) and _is_banner_label(lab)]
    chips = [lab for lab in label_list if isinstance(lab, dict) and not _is_banner_label(lab)]

    for label in banners:
        edge = str(label.get("edge") or "top").strip().lower()
        if edge not in {"top", "bottom", "left", "right"}:
            edge = "top"
        label_size = str(label.get("size") or "medium").strip().lower()
        raw_thickness = label.get("thickness")
        try:
            thickness_val = float(raw_thickness) if raw_thickness is not None else None
        except (TypeError, ValueError):
            thickness_val = None
        out = _draw_layout_banner(
            out,
            edge=edge,
            fill_color=str(label.get("fill_color") or "#000000"),
            fill_opacity=float(label.get("fill_opacity") if label.get("fill_opacity") is not None else 0.65),
            size=label_size,
            thickness=thickness_val,
        )

    for label in chips:
        token = str(label.get("token") or "").strip()
        if token == "Banner":
            continue
        row = int(label.get("row") or 0)
        col = int(label.get("col") or 0)
        row = max(0, min(grid_rows - 1, row))
        col = max(0, min(grid_cols - 1, col))
        # Snap point = cell center. Size is independent of the cell.
        ax = int((col + 0.5) * cell_w)
        ay = int((row + 0.5) * cell_h)
        label_size = str(label.get("size") or ("medium" if token == "Badge" else "small")).strip().lower()
        if token == "Badge":
            mods = _pillow()
            if mods is None:
                continue
            # Medium matches legacy corner badge; small/large scale from that.
            base_mult = _label_size_mult(_BADGE_SIZE_BASE)
            badge_w = max(24, int(round(_corner_badge_size(w, h) * (_label_size_mult(label_size) / base_mult))))
            variant = _badge_logo_variant(label.get("fill_color"))
            badge = _build_corner_logo_badge(badge_w, variant=variant)
            if badge is None:
                continue
            if out.mode != "RGBA":
                out = out.convert("RGBA")
            bx = ax - badge_w // 2
            by = ay - badge_w // 2
            # Keep badge fully on-canvas.
            bx = max(8, min(w - badge_w - 8, bx))
            by = max(8, min(h - badge_w - 8, by))
            out.paste(badge, (bx, by), badge)
            continue
        text, art_path = _resolve_layout_label_text(label, token_map)
        if not text and art_path is None:
            continue
        border_opacity = label.get("border_opacity")
        if border_opacity is None:
            # Legacy ``#RRGGBBAA``: alpha 00 meant no stroke.
            legacy = _parse_hex_rgba(str(label.get("border_color") or "#00000000"))
            border_opacity = legacy[3] / 255.0
        out = _draw_layout_chip(
            out,
            text=text,
            anchor=(ax, ay),
            text_color=str(label.get("text_color") or "#ffffff"),
            fill_color=str(label.get("fill_color") or "#000000"),
            border_color=str(label.get("border_color") or "#000000"),
            fill_opacity=float(label.get("fill_opacity") if label.get("fill_opacity") is not None else 0.65),
            border_opacity=float(border_opacity),
            size=label_size,
            art_path=art_path,
            landscape=landscape,
        )
    if out.mode == "RGBA":
        out = out.convert("RGB")
    return out


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
        tokens = sample_tokens_for_profile(key)
        if isinstance(sample_token_overrides, dict):
            for raw_key, raw_val in sample_token_overrides.items():
                name = str(raw_key or "").strip()
                if not name:
                    continue
                text = str(raw_val or "").strip()
                if text:
                    tokens[name] = text
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
