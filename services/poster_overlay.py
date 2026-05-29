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

OverlayMode = Literal["off", "grayscale", "top_banner", "corner_logo"]

PORTRAIT_SIZE = (1000, 1500)
LANDSCAPE_SIZE = (1920, 1080)
BRAND_BLUE = (11, 17, 27, 255)
BRAND_BLUE_HEX = "#0b111b"
OVERLAY_META_FILENAME = ".poster-overlay.json"

_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "placeholdarr_logo_yellow.png"
_FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "SpaceGrotesk-Bold.ttf"
_VALID_MODES = frozenset({"off", "grayscale", "top_banner", "corner_logo"})
_TOP_BANNER_LAYOUT = "top-banner-space-grotesk-v2"


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
    layout_stamp = f"corner-br-v1:{_TOP_BANNER_LAYOUT}:prefer-local-nfo-v1"
    logo_digest = _asset_digest(_LOGO_PATH)
    font_digest = _asset_digest(_FONT_PATH)
    parts = [layout_stamp]
    if logo_digest:
        parts.append(logo_digest)
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


def poster_overlay_mode() -> str:
    raw = str(getattr(settings, "PLACEHOLDER_POSTER_OVERLAY_MODE", "off") or "off").strip().lower()
    return raw if raw in _VALID_MODES else "off"


def poster_overlay_enabled() -> bool:
    return poster_overlay_mode() != "off"


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


def _apply_top_banner(img):
    mods = _pillow()
    if mods is None:
        return img
    Image, ImageDraw, _, ImageFont, _ = mods
    out = img.convert("RGBA")
    w, h = out.size
    bar_h = max(56, int(h * 0.11))
    overlay = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([0, 0, w, bar_h], fill=(0, 0, 0, int(255 * 0.65)))
    out = Image.alpha_composite(out, overlay)
    draw = ImageDraw.Draw(out)
    text = "PLACEHOLDER"
    font_size = max(26, int(bar_h * 0.50))
    font = _load_font(font_size)
    # anchor="mm" centers on the point; naive bbox math ignores font ascender offset.
    draw.text(
        (w // 2, bar_h // 2),
        text,
        fill=(255, 255, 255, 255),
        font=font,
        anchor="mm",
    )
    return out


def _rounded_rectangle_mask(size: tuple[int, int], radius: int):
    mods = _pillow()
    if mods is None:
        return None
    Image, ImageDraw, _, _, _ = mods
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def _apply_corner_logo(img):
    mods = _pillow()
    if mods is None:
        return img
    Image, _, _, _, ImageOps = mods
    out = img.convert("RGBA")
    w, h = out.size
    badge_w = max(64, int(w * 0.18))
    badge_h = badge_w
    inset = max(8, int(w * 0.02))
    x0 = w - badge_w - inset
    y0 = h - badge_h - inset

    badge = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
    blue_layer = Image.new("RGBA", (badge_w, badge_h), BRAND_BLUE)
    radius = max(6, int(badge_w * 0.12))
    mask = _rounded_rectangle_mask((badge_w, badge_h), radius)
    badge.paste(blue_layer, (0, 0), mask)

    if _LOGO_PATH.is_file():
        try:
            logo = Image.open(_LOGO_PATH).convert("RGBA")
            inner = int(badge_w * 0.7)
            logo = ImageOps.contain(logo, (inner, inner), method=Image.Resampling.LANCZOS)
            lx = (badge_w - logo.width) // 2
            ly = (badge_h - logo.height) // 2
            badge.paste(logo, (lx, ly), logo)
        except Exception as exc:
            logger.debug(f"Corner logo load failed: {exc}", extra={"emoji_type": "debug"})

    out.paste(badge, (x0, y0), badge)
    return out


def apply_overlay(img, mode: str, *, landscape: bool = False):
    if _pillow() is None:
        _log_pillow_missing_once()
        return None
    want = str(mode or "off").strip().lower()
    if want not in _VALID_MODES or want == "off":
        return img
    target = LANDSCAPE_SIZE if landscape else PORTRAIT_SIZE
    base = _fit_image(img, target)
    if want == "grayscale":
        return _apply_grayscale(base)
    if want == "top_banner":
        return _apply_top_banner(base)
    if want == "corner_logo":
        return _apply_corner_logo(base)
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


def composite_poster_from_url(url: str | None, mode: str, *, landscape: bool = False):
    if _pillow() is None:
        _log_pillow_missing_once()
        return None
    data = download_poster_bytes(url or "")
    if not data:
        return None
    img = load_image_from_bytes(data)
    if img is None:
        return None
    return apply_overlay(img, mode, landscape=landscape)


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
