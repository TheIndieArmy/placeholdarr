"""Write placeholder poster/thumb JPEGs beside media files (decoupled from NFO)."""

from __future__ import annotations

import glob
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from services.poster_overlay import (
    OVERLAY_META_FILENAME,
    composite_poster_from_url,
    logo_asset_stamp,
    poster_overlay_mode,
    save_jpeg,
    save_raw_poster_from_url,
)

POSTER_JPEG = "poster.jpg"
SERIES_FOLDER_JPEG = "folder.jpg"
SEASON_POSTER_GLOB = "season*-poster.jpg"


@dataclass
class LocalArtPaths:
    poster: str | None = None
    thumb: str | None = None

    def as_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.poster:
            out["poster"] = self.poster
        if self.thumb:
            out["thumb"] = self.thumb
        return out


def _empty_art_counts() -> dict[str, int]:
    return {"movie": 0, "series": 0, "season": 0, "episode": 0}


@dataclass
class ArtResult:
    local_art: LocalArtPaths = field(default_factory=LocalArtPaths)
    wrote_any: bool = False
    art_counts: dict[str, int] = field(default_factory=_empty_art_counts)

    def merge_counts(self, other: "ArtResult") -> None:
        for key in ("movie", "series", "season", "episode"):
            self.art_counts[key] = int(self.art_counts.get(key, 0)) + int(other.art_counts.get(key, 0))
        if other.wrote_any:
            self.wrote_any = True


def _meta_path_for_output(output_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(output_path)), OVERLAY_META_FILENAME)


def _read_meta(meta_path: str) -> dict | None:
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _publish_series_folder_poster(poster_path: str) -> None:
    """Plex/Jellyfin often prefer folder.jpg for TV show posters in the series root."""
    folder = os.path.dirname(os.path.abspath(poster_path))
    dest = os.path.join(folder, SERIES_FOLDER_JPEG)
    if os.path.abspath(dest) == os.path.abspath(poster_path):
        return
    try:
        shutil.copy2(poster_path, dest)
        _apply_art_file_permissions(poster_path, dest)
    except OSError:
        pass


def _apply_art_file_permissions(anchor_path: str, *artifact_paths: str) -> None:
    """Match placeholder media/NFO open permissions on art files."""
    from services.placeholders import _apply_dir_chain_permissions, _ensure_open_permissions

    if anchor_path:
        _apply_dir_chain_permissions(anchor_path)
    for path in artifact_paths:
        if path and os.path.exists(path):
            _ensure_open_permissions(path)


def _normalize_art_url(url: str | None) -> str:
    return str(url or "").strip()


def _normalize_outputs_map(raw: Any) -> dict[str, dict[str, str]]:
    """Per-artifact entries: meta_key -> {file, source_url, source_kind?}. Supports legacy string values."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            file_name = str(value.get("file") or "").strip()
            src = _normalize_art_url(value.get("source_url"))
            if file_name and src:
                entry = {"file": file_name, "source_url": src}
                kind = str(value.get("source_kind") or "").strip()
                if kind:
                    entry["source_kind"] = kind
                out[str(key)] = entry
        elif isinstance(value, str) and value.strip():
            out[str(key)] = {"file": value.strip(), "source_url": ""}
    return out


def _season_poster_source(season: Any, series: Any) -> tuple[str, str]:
    season_url = _normalize_art_url(getattr(season, "remote_poster", None))
    series_url = _normalize_art_url(getattr(series, "remote_poster", None))
    if season_url:
        return season_url, "season"
    if series_url:
        return series_url, "series_fallback"
    return "", "none"


def _episode_thumb_source(episode: Any, series: Any) -> tuple[str, str]:
    still_url = _normalize_art_url(getattr(episode, "sonarr_episode_still", None))
    fanart_url = _normalize_art_url(getattr(series, "remote_fanart", None))
    if still_url:
        return still_url, "still"
    if fanart_url:
        return fanart_url, "fanart"
    return "", "none"


def _output_entry(meta: dict | None, meta_key: str, *, legacy_source_url: str = "") -> dict[str, str] | None:
    if not meta:
        return None
    outputs = _normalize_outputs_map(meta.get("outputs"))
    entry = outputs.get(meta_key)
    if entry and entry.get("source_url"):
        return entry
    # Legacy meta: outputs[meta_key] was a bare filename + top-level source_url
    if entry and legacy_source_url:
        return {"file": entry["file"], "source_url": legacy_source_url}
    legacy_top = str(meta.get("source_url") or "").strip()
    raw = meta.get("outputs") if isinstance(meta.get("outputs"), dict) else {}
    bare = raw.get(meta_key) if isinstance(raw, dict) else None
    if isinstance(bare, str) and bare.strip() and legacy_top:
        return {"file": bare.strip(), "source_url": legacy_top}
    return entry


def _write_meta(
    meta_path: str,
    *,
    mode: str,
    meta_key: str,
    source_url: str,
    output_basename: str,
    source_kind: str = "",
) -> None:
    existing = _read_meta(meta_path) or {}
    outputs = _normalize_outputs_map(existing.get("outputs"))
    entry: dict[str, str] = {"file": output_basename, "source_url": _normalize_art_url(source_url)}
    if source_kind:
        entry["source_kind"] = source_kind
    outputs[meta_key] = entry
    payload = {
        "mode": mode,
        "outputs": outputs,
        "logo_stamp": logo_asset_stamp(),
    }
    parent = os.path.dirname(meta_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))


def _needs_regenerate(
    output_path: str,
    *,
    mode: str,
    source_url: str,
    meta_key: str,
    source_kind: str = "",
) -> bool:
    source_url = _normalize_art_url(source_url)
    if not source_url:
        return False
    if not os.path.isfile(output_path):
        return True
    meta_path = _meta_path_for_output(output_path)
    meta = _read_meta(meta_path)
    if not meta:
        return True
    if str(meta.get("mode") or "") != mode:
        return True
    if str(meta.get("logo_stamp") or "") != logo_asset_stamp():
        return True
    entry = _output_entry(meta, meta_key, legacy_source_url=str(meta.get("source_url") or ""))
    if not entry:
        return True
    if source_kind and str(entry.get("source_kind") or "") != source_kind:
        return True
    if _normalize_art_url(entry.get("source_url")) != source_url:
        return True
    expected_file = str(entry.get("file") or "").strip()
    if expected_file and os.path.basename(output_path) != expected_file:
        return True
    return False


def _save_image_to_path(output_path: str, url: str, *, mode: str, landscape: bool) -> bool:
    """Write JPEG using overlay mode (raw when off or compositing unavailable)."""
    if mode == "off":
        return save_raw_poster_from_url(url, output_path)
    img = composite_poster_from_url(url, mode, landscape=landscape)
    if img is not None:
        return save_jpeg(img, output_path)
    return save_raw_poster_from_url(url, output_path)


def _write_art_file(
    output_path: str,
    source_url: str | None,
    *,
    mode: str,
    landscape: bool,
    meta_key: str,
    source_kind: str = "",
) -> bool:
    url = _normalize_art_url(source_url)
    if not url:
        return False
    meta_path = _meta_path_for_output(output_path)
    if not _needs_regenerate(
        output_path, mode=mode, source_url=url, meta_key=meta_key, source_kind=source_kind
    ):
        artifacts = [output_path, meta_path]
        folder = os.path.dirname(os.path.abspath(output_path))
        folder_jpg = os.path.join(folder, SERIES_FOLDER_JPEG)
        if meta_key == "series_poster" and os.path.isfile(folder_jpg):
            artifacts.append(folder_jpg)
        _apply_art_file_permissions(output_path, *artifacts)
        return False
    if not _save_image_to_path(output_path, url, mode=mode, landscape=landscape):
        return False
    _write_meta(
        meta_path,
        mode=mode,
        meta_key=meta_key,
        source_url=url,
        output_basename=os.path.basename(output_path),
        source_kind=source_kind,
    )
    artifacts = [output_path, meta_path]
    if meta_key == "series_poster":
        _publish_series_folder_poster(output_path)
        folder_copy = os.path.join(os.path.dirname(os.path.abspath(output_path)), SERIES_FOLDER_JPEG)
        if os.path.isfile(folder_copy):
            artifacts.append(folder_copy)
    _apply_art_file_permissions(output_path, *artifacts)
    return True


def season_poster_filename(season_number: int) -> str:
    """Sonarr/Plex local season poster naming at the series root."""
    sn = int(season_number)
    if sn <= 0:
        return "season-specials-poster.jpg"
    return f"season{sn:02d}-poster.jpg"


def _season_poster_meta_key(season_number: int) -> str:
    sn = int(season_number)
    if sn <= 0:
        return "season_specials_poster"
    return f"season_poster_{sn:02d}"


def episode_thumb_filename(media_path: str) -> str:
    base, _ = os.path.splitext(os.path.basename(media_path))
    return f"{base}-thumb.jpg"


def remove_season_poster_art_in_series_folder(series_folder: str | None) -> bool:
    """Remove Sonarr-style season poster JPEGs from a TV series root."""
    if not series_folder:
        return False
    folder = os.path.abspath(series_folder)
    removed = False
    for path in glob.glob(os.path.join(folder, SEASON_POSTER_GLOB)):
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed = True
            except OSError:
                pass
    return removed


def remove_placeholder_art_in_dir(
    directory: str | None,
    *,
    media_path: str | None = None,
    series_folder: str | None = None,
) -> bool:
    """Remove local art and overlay metadata from a folder."""
    if not directory:
        return False
    folder = os.path.abspath(directory)
    removed = False
    candidates = [
        os.path.join(folder, POSTER_JPEG),
        os.path.join(folder, SERIES_FOLDER_JPEG),
        os.path.join(folder, OVERLAY_META_FILENAME),
    ]
    if media_path:
        candidates.append(os.path.join(folder, episode_thumb_filename(media_path)))
    for path in candidates:
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed = True
            except OSError:
                pass
    if series_folder:
        removed = remove_season_poster_art_in_series_folder(series_folder) or removed
    return removed




def _resolve_series_folder(series: Any, media_path: str | None = None) -> str | None:
    folder = getattr(series, "placeholder_folder", None)
    if folder:
        return os.path.abspath(str(folder))
    if media_path:
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(media_path))))
    return None


def ensure_movie_art(movie: Any, media_path: str) -> ArtResult:
    """Write movie poster.jpg (overlay or raw)."""
    result = ArtResult()
    mode = poster_overlay_mode()
    folder = os.path.dirname(os.path.abspath(media_path))
    out_path = os.path.join(folder, POSTER_JPEG)
    url = getattr(movie, "remote_poster", None)
    if _write_art_file(out_path, url, mode=mode, landscape=False, meta_key="poster"):
        result.local_art.poster = POSTER_JPEG
        result.wrote_any = True
        result.art_counts["movie"] = 1
    return result


def ensure_season_art(season: Any, series: Any, series_folder: str) -> ArtResult:
    """Write seasonNN-poster.jpg at the series root."""
    result = ArtResult()
    if not series_folder:
        return result
    mode = poster_overlay_mode()
    folder = os.path.abspath(series_folder)
    season_number = int(getattr(season, "season_number", 0) or 0)
    filename = season_poster_filename(season_number)
    out_path = os.path.join(folder, filename)
    url, kind = _season_poster_source(season, series)
    if _write_art_file(
        out_path,
        url,
        mode=mode,
        landscape=False,
        meta_key=_season_poster_meta_key(season_number),
        source_kind=kind,
    ):
        result.local_art.poster = filename
        result.wrote_any = True
        result.art_counts["season"] = 1
    return result


def ensure_series_art(
    series: Any,
    series_folder: str | None = None,
    seasons: list[Any] | None = None,
) -> ArtResult:
    """Write series poster/folder.jpg and all season posters for a show."""
    result = ArtResult()
    folder = series_folder or _resolve_series_folder(series)
    if not folder:
        return result
    mode = poster_overlay_mode()
    folder = os.path.abspath(folder)
    out_path = os.path.join(folder, POSTER_JPEG)
    url = _normalize_art_url(getattr(series, "remote_poster", None))
    kind = "series" if url else "none"
    if _write_art_file(out_path, url, mode=mode, landscape=False, meta_key="series_poster", source_kind=kind):
        result.local_art.poster = POSTER_JPEG
        result.wrote_any = True
        result.art_counts["series"] = 1

    rows = seasons
    if rows is None:
        from services.postgres.db import session_scope
        from services.postgres.models import Season

        series_id = getattr(series, "id", None)
        if series_id:
            with session_scope() as session:
                rows = (
                    session.query(Season)
                    .filter(Season.series_id == int(series_id), Season.is_deleted == False)  # noqa: E712
                    .all()
                )
    for season in rows or []:
        one = ensure_season_art(season, series, folder)
        if one.wrote_any:
            result.wrote_any = True
            result.merge_counts(one)
    return result


def ensure_episode_still_art(
    episode: Any,
    season: Any,
    series: Any,
    media_path: str,
) -> ArtResult:
    """Write episode *-thumb.jpg beside the placeholder file."""
    result = ArtResult()
    mode = poster_overlay_mode()
    folder = os.path.dirname(os.path.abspath(media_path))
    thumb_name = episode_thumb_filename(media_path)
    thumb_path = os.path.join(folder, thumb_name)
    still_url, kind = _episode_thumb_source(episode, series)
    if _write_art_file(thumb_path, still_url, mode=mode, landscape=True, meta_key="episode_thumb", source_kind=kind):
        result.local_art.thumb = thumb_name
        result.wrote_any = True
        result.art_counts["episode"] = 1
    return result


# Backward-compatible aliases (call sites being migrated)
ensure_movie_placeholder_art = ensure_movie_art
ensure_season_placeholder_art = ensure_season_art
ensure_series_season_placeholder_art = ensure_series_art
ensure_series_placeholder_art = ensure_series_art
ensure_episode_placeholder_art = ensure_episode_still_art


def _is_valid_art_file(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def discover_local_art(media_path: str | None, *, series_folder: str | None = None) -> LocalArtPaths:
    """Return relative local art paths when files exist on disk."""
    art = LocalArtPaths()
    if media_path:
        folder = os.path.dirname(os.path.abspath(media_path))
        poster_abs = os.path.join(folder, POSTER_JPEG)
        if _is_valid_art_file(poster_abs):
            art.poster = POSTER_JPEG
        thumb_name = episode_thumb_filename(media_path)
        thumb_abs = os.path.join(folder, thumb_name)
        if _is_valid_art_file(thumb_abs):
            art.thumb = thumb_name
    if series_folder:
        series_poster = os.path.join(os.path.abspath(series_folder), POSTER_JPEG)
        if _is_valid_art_file(series_poster) and not art.poster:
            art.poster = POSTER_JPEG
    return art
