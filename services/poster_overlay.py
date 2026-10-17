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
_FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "SpaceGrotesk-Bold.ttf"
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
        f"corner-br-v1:{_TOP_BANNER_LAYOUT}:{_CORNER_BOTTOM_BANNER_LAYOUT}:"
        f"{_COMPOSE_LAYOUT}:prefer-local-nfo-v1"
    )
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
    """True when any compositing layer is on."""
    return (
        poster_grayscale_enabled()
        or poster_badge_enabled()
        or poster_placeholder_banner_enabled()
        or coming_soon_banner_enabled()
    )


def poster_overlay_mode() -> str:
    """Effective meta mode: ``compose`` when any layer is on, else ``off``.

    Legacy exclusive modes (`grayscale` / `top_banner` / `corner_logo`) remain
    readable from settings for migration only; drawing always uses layers.
    """
    if poster_overlay_enabled():
        return COMPOSE_MODE
    return "off"


def overlay_layer_fingerprint() -> dict[str, str | bool]:
    """Stable layer snapshot for ``.poster-overlay.json`` skip/regen."""
    return {
        "grayscale": bool(poster_grayscale_enabled()),
        "badge": bool(poster_badge_enabled()),
        "badge_position": poster_badge_position(),
        "placeholder_banner": bool(poster_placeholder_banner_enabled()),
        "coming_soon_banner": bool(coming_soon_banner_enabled()),
        "banner_position": poster_banner_position(),
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


def _build_corner_logo_badge(badge_w: int):
    """Square brand badge used for corner-logo mode (standalone or in the bottom banner)."""
    mods = _pillow()
    if mods is None:
        return None
    Image, _, _, _, ImageOps = mods
    badge_h = badge_w
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
):
    """Composite layered poster overlays.

    Drawing uses independent layers (grayscale / text bar / badge). The ``mode``
    argument is legacy: exclusive modes still work for older tests/callers when
    layer kwargs are omitted and settings have no layers enabled.
    """
    if _pillow() is None:
        _log_pillow_missing_once()
        return None

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
    return apply_overlay(img, mode, landscape=landscape, overlay_text=overlay_text)


def _preview_base_image(*, kind: str = "movie"):
    """Load a color sample for Settings live preview (not a pre-composited mode).

    ``kind``: ``movie`` / ``series`` (portrait) or ``episode`` (landscape thumb).
    """
    mods = _pillow()
    if mods is None:
        return None
    Image = mods[0]
    root = Path(__file__).resolve().parent.parent
    want = str(kind or "movie").strip().lower()
    if want not in {"movie", "series", "episode"}:
        want = "movie"
    if want == "episode":
        names = ("preview-episode.jpg",)
        fallback_size = LANDSCAPE_SIZE
        fallback_color = (36, 52, 78)
    elif want == "series":
        names = ("preview-series.jpg", "preview-base.jpg")
        fallback_size = PORTRAIT_SIZE
        fallback_color = (48, 72, 110)
    else:
        # Prefer clean color base. Never use grayscale.jpg (already desaturated).
        names = ("preview-base.jpg", "corner_logo.jpg")
        fallback_size = PORTRAIT_SIZE
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
    return Image.new("RGB", fallback_size, fallback_color)


def render_overlay_preview_jpeg(
    *,
    kind: str = "movie",
    grayscale: bool = False,
    badge: bool = False,
    badge_position: str = "bottom_right",
    placeholder_banner: bool = False,
    coming_soon_banner: bool = False,
    banner_position: str = "top",
    quality: int = 85,
) -> bytes | None:
    """Composite a sample poster/thumb for the live Settings preview.

    ``kind`` selects the sample and banner copy:
    - ``movie``: portrait, Coming Soon → ``Digital - Oct 22``
    - ``series``: portrait TV title, Coming Soon → ``New Ep. - Oct 22``
    - ``episode``: landscape thumb, Coming Soon → ``Airing - Oct 22``
    """
    mods = _pillow()
    if mods is None:
        return None
    want = str(kind or "movie").strip().lower()
    if want not in {"movie", "series", "episode"}:
        want = "movie"
    base = _preview_base_image(kind=want)
    if base is None:
        return None
    landscape = want == "episode"
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
