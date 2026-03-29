from __future__ import annotations

import os
import re
import shutil
import filecmp
from typing import Any
from xml.sax.saxutils import escape

from core.config import settings
from services.status_projection import project_summary, project_title


def _parse_mode(value: Any, default: int) -> int:
    """Parse octal mode values like '666', '0666', or '0o666'."""
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text.startswith("0o"):
        text = text[2:]
    if text.startswith("0") and len(text) > 1:
        text = text[1:]
    try:
        return int(text, 8)
    except ValueError:
        return default


_PLACEHOLDER_FILE_MODE = _parse_mode(getattr(settings, "PLACEHOLDER_FILE_MODE", "666"), 0o666)
_PLACEHOLDER_DIR_MODE = _parse_mode(getattr(settings, "PLACEHOLDER_DIR_MODE", "777"), 0o777)


def _ensure_open_permissions(path: str, *, is_dir: bool = False) -> None:
    mode = _PLACEHOLDER_DIR_MODE if is_dir else _PLACEHOLDER_FILE_MODE
    try:
        os.chmod(path, mode)
    except OSError:
        # Best effort: permission updates can fail on some mounts.
        return


def _apply_dir_chain_permissions(path: str) -> None:
    """Apply directory permissions from configured library root up to the parent of `path`.

    This ensures multi-level series/season directories are chmod'd so media servers
    can traverse into nested season folders.
    """
    try:
        target_parent = os.path.abspath(os.path.dirname(path))
        roots = []
        for r in (
            getattr(settings, "MOVIE_LIBRARY_FOLDER", None),
            getattr(settings, "MOVIE_LIBRARY_4K_FOLDER", None),
            getattr(settings, "TV_LIBRARY_FOLDER", None),
            getattr(settings, "TV_LIBRARY_4K_FOLDER", None),
        ):
            if r:
                roots.append(os.path.abspath(r))

        for root in roots:
            try:
                # Only consider this root if the path is under it
                common = os.path.commonpath([target_parent, root])
            except Exception:
                continue
            if common != root:
                continue

            # Walk from root -> target_parent, applying permissions to each directory
            if root == target_parent:
                _ensure_open_permissions(root, is_dir=True)
                return

            rel = os.path.relpath(target_parent, root)
            parts = [] if rel == "." else rel.split(os.sep)
            cur = root
            _ensure_open_permissions(cur, is_dir=True)
            for p in parts:
                cur = os.path.join(cur, p)
                # ensure directory exists before chmod attempt
                try:
                    if not os.path.isdir(cur):
                        os.makedirs(cur, exist_ok=True)
                except Exception:
                    pass
                _ensure_open_permissions(cur, is_dir=True)
            return
    except Exception:
        # Best-effort; don't let permission propagation break placeholder creation
        return


def sanitize_filename(value: str | None) -> str:
    text = (value or "unknown").strip()
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "unknown"


def movie_placeholder_path(movie: Any) -> str:
    title = sanitize_filename(getattr(movie, "title", None))
    year = getattr(movie, "year", None)

    root = settings.MOVIE_LIBRARY_4K_FOLDER if bool(getattr(movie, "is_4k", False)) else settings.MOVIE_LIBRARY_FOLDER
    tmdb_or_id = getattr(movie, "tmdbid", None) or getattr(movie, "id", None)
    default_folder = os.path.join(root, f"{title} ({year}) {{tmdb-{tmdb_or_id}}}" if year else f"{title} {{tmdb-{tmdb_or_id}}}")
    folder = getattr(movie, "placeholder_folder", None) or default_folder

    year_part = f" ({year})" if year else ""
    filename = f"{title}{year_part}.mp4"
    return os.path.join(folder, filename)


def episode_placeholder_path(episode: Any, season: Any, series: Any) -> str:
    series_title = sanitize_filename(getattr(series, "title", None))
    episode_title = sanitize_filename(getattr(episode, "title", None))
    year = getattr(series, "year", None)

    root = settings.TV_LIBRARY_4K_FOLDER if bool(getattr(series, "is_4k", False)) else settings.TV_LIBRARY_FOLDER
    tvdb_or_id = getattr(series, "tvdbid", None) or getattr(series, "id", None)
    series_folder = f"{series_title} ({year}) {{tvdb-{tvdb_or_id}}}" if year else f"{series_title} {{tvdb-{tvdb_or_id}}}"
    season_folder = f"Season {int(getattr(season, 'season_number', 0)):02d}"

    default_folder = os.path.join(root, series_folder, season_folder)
    folder = (
        getattr(episode, "placeholder_folder", None)
        or getattr(season, "placeholder_folder", None)
        or getattr(series, "placeholder_folder", None)
        or default_folder
    )

    year_part = f" ({year})" if year else ""
    filename = (
        f"{series_title}{year_part} - "
        f"s{int(getattr(season, 'season_number', 0)):02d}e{int(getattr(episode, 'episode_number', 0)):02d} - "
        f"{episode_title}.mp4"
    )
    return os.path.join(folder, filename)


def ensure_placeholder_file(
    path: str,
    *,
    dummy_file_path: str | None = None,
    replace_existing: bool = False,
) -> bool:
    """Create or replace placeholder media file.

    Args:
        path: Destination media file path.
        dummy_file_path: Optional explicit source dummy file variant.
        replace_existing: When True, existing file can be replaced if variant differs.

    Returns:
        True when a file was created/replaced, False when no write was needed.
    """
    dummy_path = dummy_file_path or getattr(settings, "DUMMY_FILE_PATH", None)
    if not dummy_path or not os.path.isfile(dummy_path):
        raise RuntimeError(f"DUMMY_FILE_PATH missing or invalid: {dummy_path}")

    if os.path.isfile(path):
        if not replace_existing:
            _ensure_open_permissions(path)
            return False
        try:
            if filecmp.cmp(path, dummy_path, shallow=False):
                _ensure_open_permissions(path)
                return False
        except Exception:
            pass
        try:
            os.remove(path)
        except OSError:
            # Best-effort replacement path; continue and let write fail if needed.
            pass

    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    _apply_dir_chain_permissions(path)

    strategy = str(getattr(settings, "PLACEHOLDER_STRATEGY", "hardlink") or "hardlink").strip().lower()
    if strategy == "hardlink":
        try:
            os.link(dummy_path, path)
            os.utime(path, None)
            _ensure_open_permissions(path)
            return True
        except OSError:
            # Cross-device links can fail; copy is the safe fallback.
            shutil.copy2(dummy_path, path)
            os.utime(path, None)
            _ensure_open_permissions(path)
            return True

    shutil.copy2(dummy_path, path)
    os.utime(path, None)
    _ensure_open_permissions(path)
    return True


def remove_placeholder_file(path: str | None) -> bool:
    """Delete placeholder media if present. Returns True when file is deleted."""
    if not path or not os.path.isfile(path):
        return False
    os.remove(path)
    return True


def nfo_sidecar_path(media_path: str) -> str:
    base, _ = os.path.splitext(media_path)
    return f"{base}.nfo"


def _atomic_write_text(path: str, content: str) -> bool:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    _apply_dir_chain_permissions(path)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == content:
                _ensure_open_permissions(path)
                return False

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    _ensure_open_permissions(tmp_path)
    os.replace(tmp_path, path)
    _ensure_open_permissions(path)
    return True


def _movie_nfo_xml(movie: Any) -> str:
    status = str(getattr(movie, "placeholder_status", "") or "REQUEST")
    raw_title = str(getattr(movie, "title", "") or "")
    title = escape(project_title(raw_title, status))
    year = getattr(movie, "year", None)
    overview = escape(project_summary(str(getattr(movie, "radarr_overview", "") or ""), status))
    tmdbid = getattr(movie, "tmdbid", None)
    imdbid = getattr(movie, "imdbid", None)
    poster_url = escape(str(getattr(movie, "remote_poster", "") or ""))

    lines = [
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\" ?>",
        "<movie>",
        f"  <title>{title}</title>",
        f"  <originaltitle>{escape(raw_title)}</originaltitle>",
        "  <tag>placeholder</tag>",
        f"  <tag>status:{escape(status)}</tag>",
    ]
    if year:
        lines.append(f"  <year>{int(year)}</year>")
    if overview:
        lines.append(f"  <plot>{overview}</plot>")
    else:
        lines.append(f"  <plot>{escape(project_summary('', status))}</plot>")
    if tmdbid:
        lines.append(f"  <tmdbid>{escape(str(tmdbid))}</tmdbid>")
        lines.append(f"  <uniqueid type=\"tmdb\" default=\"true\">{escape(str(tmdbid))}</uniqueid>")
    if imdbid:
        lines.append(f"  <imdbid>{escape(str(imdbid))}</imdbid>")
        lines.append(f"  <uniqueid type=\"imdb\">{escape(str(imdbid))}</uniqueid>")
    if poster_url:
        lines.append(f"  <thumb aspect=\"poster\">{poster_url}</thumb>")
    lines.append("</movie>")
    lines.append("")
    return "\n".join(lines)


def _episode_nfo_xml(episode: Any, season: Any, series: Any) -> str:
    show_title = escape(str(getattr(series, "title", "") or ""))
    status = str(getattr(episode, "placeholder_status", "") or "REQUEST")
    raw_episode_title = str(getattr(episode, "title", "") or "")
    episode_title = escape(project_title(raw_episode_title, status))
    plot = escape(project_summary(str(getattr(episode, "sonarr_episode_overview", "") or ""), status))
    season_number = int(getattr(season, "season_number", 0) or 0)
    episode_number = int(getattr(episode, "episode_number", 0) or 0)
    aired = getattr(episode, "air_date", None)
    tvdbid = getattr(series, "tvdbid", None)
    imdbid = getattr(series, "imdbid", None)

    lines = [
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\" ?>",
        "<episodedetails>",
        f"  <title>{episode_title}</title>",
        f"  <showtitle>{show_title}</showtitle>",
        f"  <season>{season_number}</season>",
        f"  <episode>{episode_number}</episode>",
        "  <tag>placeholder</tag>",
        f"  <tag>status:{escape(status)}</tag>",
    ]
    if aired:
        lines.append(f"  <aired>{escape(str(aired))}</aired>")
    if plot:
        lines.append(f"  <plot>{plot}</plot>")
    else:
        lines.append(f"  <plot>{escape(project_summary('', status))}</plot>")
    if tvdbid:
        lines.append(f"  <tvdbid>{escape(str(tvdbid))}</tvdbid>")
        # uniqueid lets Emby/Jellyfin match this episode to their databases
        lines.append(f"  <uniqueid type=\"tvdb\" default=\"true\">{escape(str(tvdbid))}</uniqueid>")
    if imdbid:
        lines.append(f"  <imdbid>{escape(str(imdbid))}</imdbid>")
        lines.append(f"  <uniqueid type=\"imdb\">{escape(str(imdbid))}</uniqueid>")
    lines.append("</episodedetails>")
    lines.append("")
    return "\n".join(lines)


def ensure_movie_nfo(media_path: str, movie: Any) -> bool:
    return _atomic_write_text(nfo_sidecar_path(media_path), _movie_nfo_xml(movie))


def ensure_episode_nfo(media_path: str, episode: Any, season: Any, series: Any) -> bool:
    return _atomic_write_text(nfo_sidecar_path(media_path), _episode_nfo_xml(episode, season, series))


def _series_nfo_xml(series: Any) -> str:
    """Render a tvshow.nfo XML for a series object."""
    status = str(getattr(series, "placeholder_status", "") or "REQUEST")
    title = escape(project_title(str(getattr(series, "title", "") or ""), status))
    overview = escape(project_summary(str(getattr(series, "sonarr_series_overview", "") or ""), status))
    tvdbid = getattr(series, "tvdbid", None)
    imdbid = getattr(series, "imdbid", None)
    poster_url = escape(str(getattr(series, "remote_poster", "") or ""))

    lines = [
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\" ?>",
        "<tvshow>",
        f"  <title>{title}</title>",
        "  <tag>placeholder</tag>",
        f"  <tag>status:{escape(status)}</tag>",
    ]
    if overview:
        lines.append(f"  <plot>{overview}</plot>")
    else:
        lines.append(f"  <plot>{escape(project_summary('', status))}</plot>")
    if tvdbid:
        lines.append(f"  <tvdbid>{escape(str(tvdbid))}</tvdbid>")
        lines.append(f"  <uniqueid type=\"tvdb\" default=\"true\">{escape(str(tvdbid))}</uniqueid>")
    if imdbid:
        lines.append(f"  <imdbid>{escape(str(imdbid))}</imdbid>")
        lines.append(f"  <uniqueid type=\"imdb\">{escape(str(imdbid))}</uniqueid>")
    if poster_url:
        lines.append(f"  <thumb aspect=\"poster\">{poster_url}</thumb>")
    lines.append("</tvshow>")
    lines.append("")
    return "\n".join(lines)


def ensure_series_nfo(series: Any, folder: str | None = None) -> bool:
    """Write a series-level tvshow.nfo into the series folder.

    If `folder` is provided it will be used as the target folder; otherwise the
    function will attempt to derive the folder from `series.placeholder_folder`
    or the configured TV library root and series title.
    """
    # Determine folder to write into
    target_folder = folder
    if not target_folder:
        target_folder = getattr(series, "placeholder_folder", None)
    if not target_folder:
        # Fallback to a best-effort folder using configured TV root + sanitized title
        root = settings.TV_LIBRARY_4K_FOLDER if bool(getattr(series, "is_4k", False)) else settings.TV_LIBRARY_FOLDER
        title = sanitize_filename(getattr(series, "title", None))
        year = getattr(series, "year", None)
        tvdb_or_id = getattr(series, "tvdbid", None) or getattr(series, "id", None)
        series_folder = f"{title} ({year}) {{tvdb-{tvdb_or_id}}}" if year else f"{title} {{tvdb-{tvdb_or_id}}}"
        if not root:
            return False
        target_folder = os.path.join(root, series_folder)

    # Ensure directory exists and permissions are applied
    try:
        os.makedirs(target_folder, exist_ok=True)
    except Exception:
        pass
    # Build path for tvshow.nfo
    nfo_path = os.path.join(target_folder, "tvshow.nfo")
    return _atomic_write_text(nfo_path, _series_nfo_xml(series))


def remove_nfo_sidecar(media_path: str | None) -> bool:
    if not media_path:
        return False
    nfo_path = nfo_sidecar_path(media_path)
    if not os.path.isfile(nfo_path):
        return False
    os.remove(nfo_path)
    return True
