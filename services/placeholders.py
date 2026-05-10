from __future__ import annotations

import json
import os
import re
import shutil
import filecmp
import tempfile
from typing import Any
from xml.sax.saxutils import escape

from core.config import settings
from services.messages.context import build_projection_context
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


def _resolve_dummy_path(kind: str = "primary") -> str:
    """Resolve dummy media source path with runtime fallbacks.

    Preference order:
    1) Explicit configured setting if valid
    2) Docker `/config` defaults
    3) In-image `/app` defaults
    """
    configured = ""
    if kind == "coming_soon":
        configured = str(getattr(settings, "COMING_SOON_DUMMY_FILE_PATH", "") or "").strip()
    else:
        configured = str(getattr(settings, "DUMMY_FILE_PATH", "") or "").strip()

    candidates = []
    if configured:
        candidates.append(configured)
    if kind == "coming_soon":
        candidates.extend([
            "/config/coming_soon_dummy.mp4",
            "/app/coming_soon_dummy.mp4",
            "/config/dummy.mp4",
            "/app/dummy.mp4",
        ])
    else:
        candidates.extend([
            "/config/dummy.mp4",
            "/app/dummy.mp4",
        ])

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return configured


def ensure_runtime_dummy_files() -> dict[str, Any]:
    """Ensure default dummy media files exist in /config early at runtime.

    This is best-effort and intended to run during app startup so first-run
    onboarding has known-good dummy files available before any placeholder work.
    """
    created: list[str] = []
    existing: list[str] = []
    missing_sources: list[str] = []
    errors: list[str] = []

    service_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(service_dir, ".."))

    specs = [
        {
            "dest": "/config/dummy.mp4",
            "sources": [
                "/app/dummy.mp4",
                os.path.join(repo_root, "dummy.mp4"),
            ],
        },
        {
            "dest": "/config/coming_soon_dummy.mp4",
            "sources": [
                "/app/coming_soon_dummy.mp4",
                os.path.join(repo_root, "coming_soon_dummy.mp4"),
                "/app/dummy.mp4",
                os.path.join(repo_root, "dummy.mp4"),
            ],
        },
    ]

    config_dir = "/config"
    try:
        os.makedirs(config_dir, exist_ok=True)
        _ensure_open_permissions(config_dir, is_dir=True)
    except Exception as exc:
        errors.append(f"failed to prepare /config: {exc}")
        return {
            "created": created,
            "existing": existing,
            "missing_sources": missing_sources,
            "errors": errors,
        }

    for spec in specs:
        dest = spec["dest"]
        source = next((candidate for candidate in spec["sources"] if candidate and os.path.isfile(candidate)), "")

        try:
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                _ensure_open_permissions(dest)
                existing.append(dest)
                continue
        except Exception:
            # Continue and try to restore the file from source.
            pass

        if not source:
            missing_sources.append(dest)
            continue

        try:
            shutil.copy2(source, dest)
            _ensure_open_permissions(dest)
            created.append(dest)
        except Exception as exc:
            errors.append(f"failed to write {dest} from {source}: {exc}")

    return {
        "created": created,
        "existing": existing,
        "missing_sources": missing_sources,
        "errors": errors,
    }


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


def resolve_calendar_variant_dummy_path(variant: str) -> str:
    """Choose dummy media source for calendar-driven placeholder variants.

    Coming Soon variants use the Coming Soon dummy file when configured; otherwise the standard dummy.
    """
    primary = _resolve_dummy_path("primary")
    alt = _resolve_dummy_path("coming_soon")
    if variant != "coming_soon":
        return primary
    return alt if alt else primary


def sanitize_filename(value: str | None) -> str:
    text = (value or "unknown").strip()
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "unknown"


def _arr_instance_role(content_type: str, item: Any) -> str:
    arr_type = "radarr" if content_type == "movie" else "sonarr"
    row = settings.resolve_arr_instance(
        arr_type,
        instance_id=str(getattr(item, "instance_id", "") or "").strip().lower() or None,
        instance_key=str(getattr(item, "instance_key", "") or "").strip().lower() or None,
    ) or {}
    role = str(row.get("role") or "primary").strip().lower()
    return role if role in {"primary", "secondary", "additional"} else "primary"


def movie_placeholder_path(movie: Any) -> str:
    title = sanitize_filename(getattr(movie, "title", None))
    year = getattr(movie, "year", None)

    movie_role = _arr_instance_role("movie", movie)
    root = settings.MOVIE_LIBRARY_4K_FOLDER if movie_role != "primary" else settings.MOVIE_LIBRARY_FOLDER
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

    series_role = _arr_instance_role("series", series)
    root = settings.TV_LIBRARY_4K_FOLDER if series_role != "primary" else settings.TV_LIBRARY_FOLDER
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
    dummy_path = dummy_file_path or _resolve_dummy_path("primary")
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

    fd, tmp_path = tempfile.mkstemp(
        dir=parent,
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        _ensure_open_permissions(tmp_path)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        raise
    _ensure_open_permissions(path)
    return True


def _to_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(',') if p.strip()]
        return parts
    return []


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _status_text(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    return text.replace('_', ' ').title()


def _ratings_entries(ratings: Any) -> list[tuple[str, str | None, str | None, bool]]:
    if not isinstance(ratings, dict):
        return []

    entries: list[tuple[str, str | None, str | None, bool]] = []
    for name, payload in ratings.items():
        if not isinstance(payload, dict):
            continue
        value = payload.get('value')
        votes = payload.get('votes')
        if value is None and votes is None:
            continue
        value_text = str(value) if value is not None else None
        votes_text = str(votes) if votes is not None else None
        normalized_name = str(name or '').strip().lower() or 'rating'
        entries.append((normalized_name, value_text, votes_text, normalized_name in ('imdb', 'tmdb', 'themoviedb')))
    return entries


def _append_actors(lines: list[str], actors: Any) -> None:
    for idx, actor in enumerate(_to_list(actors)):
        if isinstance(actor, dict):
            name = str(actor.get('name') or '').strip()
            role = str(actor.get('character') or actor.get('role') or '').strip()
            thumb = str(actor.get('images') or actor.get('image') or actor.get('thumb') or '').strip()
            order = actor.get('order', idx)
        else:
            name = str(actor or '').strip()
            role = ''
            thumb = ''
            order = idx
        if not name:
            continue
        lines.append('  <actor>')
        lines.append(f"    <name>{escape(name)}</name>")
        if role:
            lines.append(f"    <role>{escape(role)}</role>")
        if order is not None:
            lines.append(f"    <order>{escape(str(order))}</order>")
        if thumb:
            lines.append(f"    <thumb>{escape(thumb)}</thumb>")
        lines.append('  </actor>')


def _append_people_as_tag(lines: list[str], tag_name: str, values: Any) -> None:
    for value in _to_list(values):
        if isinstance(value, dict):
            text = str(value.get('name') or '').strip()
        else:
            text = str(value or '').strip()
        if text:
            lines.append(f"  <{tag_name}>{escape(text)}</{tag_name}>")


def _movie_nfo_xml(movie: Any) -> str:
    status = str(getattr(movie, "placeholder_status", "") or "REQUEST")
    raw_title = str(getattr(movie, "title", "") or "")
    year = getattr(movie, "year", None)
    runtime = _to_int(getattr(movie, "radarr_runtime", None))
    rm = runtime if runtime is not None and runtime > 0 else None
    media_ctx = build_projection_context(movie=movie, runtime_minutes=rm)
    title = escape(
        project_title(
            raw_title,
            status,
            suffix_template_key="title.suffix.movie",
            runtime_minutes=rm,
            media_context=media_ctx,
        )
    )
    overview = escape(
        project_summary(
            str(getattr(movie, "radarr_overview", "") or ""),
            status,
            runtime_minutes=rm,
            media_context=media_ctx,
        )
    )
    tmdbid = getattr(movie, "tmdbid", None)
    imdbid = getattr(movie, "imdbid", None)
    poster_url = escape(str(getattr(movie, "remote_poster", "") or ""))
    fanart_url = escape(str(getattr(movie, "remote_fanart", "") or ""))
    certification = str(getattr(movie, "radarr_certification", "") or "").strip()
    genres = _to_list(getattr(movie, "radarr_genres", None))
    studio = str(getattr(movie, "radarr_studio", "") or "").strip()
    ratings = _ratings_entries(getattr(movie, "radarr_ratings", None))
    collection = getattr(movie, "radarr_collection", None)
    release_status = _status_text(getattr(movie, "radarr_release_status", None))
    premiered = getattr(movie, "radarr_premiered", None)
    trailer = str(getattr(movie, "radarr_trailer", "") or "").strip()
    actors = getattr(movie, "radarr_actors", None)
    directors = getattr(movie, "radarr_directors", None)
    credits = getattr(movie, "radarr_credits", None)

    lines = [
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\" ?>",
        "<movie>",
        f"  <title>{title}</title>",
        f"  <originaltitle>{escape(raw_title)}</originaltitle>",
        "  <tag>placeholder</tag>",
        f"  <tag>status:{escape(status)}</tag>",
    ]
    yi = _to_int(year)
    if yi and yi > 1800:
        lines.append(f"  <year>{yi}</year>")
    if overview:
        lines.append(f"  <plot>{overview}</plot>")
    else:
        lines.append(f"  <plot>{escape(project_summary('', status, runtime_minutes=rm, media_context=media_ctx))}</plot>")
    if ratings:
        lines.append("  <ratings>")
        for name, value_text, votes_text, is_default in ratings:
            default_attr = ' default="true"' if is_default else ''
            lines.append(f"    <rating name=\"{escape(name)}\" max=\"10\"{default_attr}>")
            if value_text:
                lines.append(f"      <value>{escape(value_text)}</value>")
            if votes_text:
                lines.append(f"      <votes>{escape(votes_text)}</votes>")
            lines.append("    </rating>")
        lines.append("  </ratings>")
        top_rating = next((r for r in ratings if r[0] in ('tmdb', 'themoviedb')), ratings[0])
        if top_rating[1]:
            lines.append(f"  <rating>{escape(top_rating[1])}</rating>")
    if tmdbid:
        lines.append(f"  <id>{escape(str(tmdbid))}</id>")
        lines.append(f"  <tmdbid>{escape(str(tmdbid))}</tmdbid>")
        lines.append(f"  <uniqueid type=\"tmdb\" default=\"true\">{escape(str(tmdbid))}</uniqueid>")
    if imdbid:
        lines.append(f"  <imdbid>{escape(str(imdbid))}</imdbid>")
        lines.append(f"  <uniqueid type=\"imdb\">{escape(str(imdbid))}</uniqueid>")
    if runtime:
        lines.append(f"  <runtime>{runtime}</runtime>")
    if poster_url:
        lines.append(f"  <thumb aspect=\"poster\" preview=\"{poster_url}\">{poster_url}</thumb>")
    if fanart_url:
        lines.append("  <fanart>")
        lines.append(f"    <thumb preview=\"{fanart_url}\">{fanart_url}</thumb>")
        lines.append("  </fanart>")
    if poster_url or fanart_url:
        lines.append("  <art>")
        if poster_url:
            lines.append(f"    <poster>{poster_url}</poster>")
            lines.append(f"    <thumb>{poster_url}</thumb>")
        if fanart_url:
            lines.append(f"    <fanart>{fanart_url}</fanart>")
        lines.append("  </art>")
    if certification:
        lines.append(f"  <mpaa>{escape(certification)}</mpaa>")
    for genre in genres:
        text = str(genre or '').strip()
        if text:
            lines.append(f"  <genre>{escape(text)}</genre>")
    if isinstance(collection, dict) and collection.get('name'):
        collection_name = str(collection.get('name') or '').strip()
        collection_id = collection.get('tmdbId')
        if collection_name:
            if collection_id:
                lines.append(f"  <set tmdbcolid=\"{escape(str(collection_id))}\">")
            else:
                lines.append("  <set>")
            lines.append(f"    <name>{escape(collection_name)}</name>")
            lines.append("  </set>")
    if release_status:
        lines.append(f"  <status>{escape(release_status)}</status>")
    if premiered:
        lines.append(f"  <premiered>{escape(str(premiered))}</premiered>")
    if studio:
        lines.append(f"  <studio>{escape(studio)}</studio>")
    if trailer:
        lines.append(f"  <trailer>{escape(trailer)}</trailer>")
    _append_people_as_tag(lines, 'credits', credits)
    _append_people_as_tag(lines, 'director', directors)
    _append_actors(lines, actors)
    lines.append("</movie>")
    lines.append("")
    return "\n".join(lines)


def _episode_nfo_xml(episode: Any, season: Any, series: Any) -> str:
    status = str(getattr(episode, "placeholder_status", "") or "REQUEST")
    raw_series_title = str(getattr(series, "title", "") or "")
    raw_episode_title = str(getattr(episode, "title", "") or "")
    runtime = _to_int(getattr(episode, "sonarr_runtime", None)) or _to_int(getattr(series, "sonarr_runtime", None))
    rm = runtime if runtime is not None and runtime > 0 else None
    media_ctx = build_projection_context(episode=episode, season=season, series=series, runtime_minutes=rm)
    series_ctx = build_projection_context(series=series, runtime_minutes=rm)
    show_title = escape(
        project_title(
            raw_series_title,
            status,
            suffix_template_key="title.suffix.series",
            runtime_minutes=rm,
            media_context=series_ctx,
        )
    )
    episode_title = escape(
        project_title(
            raw_episode_title,
            status,
            suffix_template_key="title.suffix.episode",
            runtime_minutes=rm,
            media_context=media_ctx,
        )
    )
    plot = escape(
        project_summary(
            str(getattr(episode, "sonarr_episode_overview", "") or ""),
            status,
            runtime_minutes=rm,
            media_context=media_ctx,
        )
    )
    season_number = int(getattr(season, "season_number", 0) or 0)
    episode_number = int(getattr(episode, "episode_number", 0) or 0)
    aired = getattr(episode, "air_date", None)
    tvdbid = getattr(episode, "sonarr_episode_tvdbid", None) or getattr(series, "tvdbid", None)
    imdbid = getattr(series, "imdbid", None)
    sonarrid = getattr(episode, "sonarrid", None)
    still_url = escape(str(getattr(episode, "sonarr_episode_still", "") or ""))
    if not still_url:
        still_url = escape(str(getattr(series, "remote_fanart", "") or ""))
    certification = str(getattr(series, "sonarr_certification", "") or "").strip()
    network = str(getattr(series, "sonarr_network", "") or "").strip()
    directors = getattr(episode, "sonarr_episode_directors", None)
    credits = getattr(episode, "sonarr_episode_credits", None)

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
        lines.append(f"  <plot>{escape(project_summary('', status, runtime_minutes=rm, media_context=media_ctx))}</plot>")
    if tvdbid:
        lines.append(f"  <tvdbid>{escape(str(tvdbid))}</tvdbid>")
        # uniqueid lets Emby/Jellyfin match this episode to their databases
        lines.append(f"  <uniqueid type=\"tvdb\" default=\"true\">{escape(str(tvdbid))}</uniqueid>")
    if sonarrid:
        lines.append(f"  <uniqueid type=\"sonarr\">{escape(str(sonarrid))}</uniqueid>")
    if imdbid:
        lines.append(f"  <imdbid>{escape(str(imdbid))}</imdbid>")
        lines.append(f"  <uniqueid type=\"imdb\">{escape(str(imdbid))}</uniqueid>")
    if runtime:
        lines.append(f"  <runtime>{runtime}</runtime>")
    if certification:
        lines.append(f"  <mpaa>{escape(certification)}</mpaa>")
    if network:
        lines.append(f"  <studio>{escape(network)}</studio>")
    if still_url:
        lines.append(f"  <thumb>{still_url}</thumb>")
    _append_people_as_tag(lines, 'credits', credits)
    _append_people_as_tag(lines, 'director', directors)
    lines.append("  <watched>false</watched>")
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
    raw_title = str(getattr(series, "title", "") or "")
    runtime = _to_int(getattr(series, "sonarr_runtime", None))
    rm = runtime if runtime is not None and runtime > 0 else None
    media_ctx = build_projection_context(series=series, runtime_minutes=rm)
    title = escape(
        project_title(
            raw_title,
            status,
            suffix_template_key="title.suffix.series",
            runtime_minutes=rm,
            media_context=media_ctx,
        )
    )
    overview = escape(str(getattr(series, "sonarr_series_overview", "") or ""))
    tvdbid = getattr(series, "tvdbid", None)
    imdbid = getattr(series, "imdbid", None)
    tmdbid = getattr(series, "sonarr_tmdbid", None)
    tvmazeid = getattr(series, "sonarr_tvmazeid", None)
    first_aired = getattr(series, "sonarr_first_aired", None)
    network = str(getattr(series, "sonarr_network", "") or "").strip()
    certification = str(getattr(series, "sonarr_certification", "") or "").strip()
    ratings = _ratings_entries(getattr(series, "sonarr_ratings", None))
    genres = _to_list(getattr(series, "sonarr_genres", None))
    actors = getattr(series, "sonarr_actors", None)
    poster_url = escape(str(getattr(series, "remote_poster", "") or ""))
    fanart_url = escape(str(getattr(series, "remote_fanart", "") or ""))
    banner_url = escape(str(getattr(series, "remote_banner", "") or ""))

    lines = [
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\" ?>",
        "<tvshow>",
        f"  <title>{title}</title>",
        "  <tag>placeholder</tag>",
    ]
    year = getattr(series, "year", None)
    yi = _to_int(year)
    if yi and yi > 1800:
        lines.append(f"  <year>{yi}</year>")
    if ratings:
        top_rating = next((r for r in ratings if r[0] in ('tmdb', 'themoviedb')), ratings[0])
        if top_rating[1]:
            lines.append(f"  <rating>{escape(top_rating[1])}</rating>")
    if tvdbid:
        lines.append(f"  <id>{escape(str(tvdbid))}</id>")
    if overview:
        lines.append(f"  <plot>{overview}</plot>")
    if tvdbid:
        lines.append(f"  <tvdbid>{escape(str(tvdbid))}</tvdbid>")
        lines.append(f"  <uniqueid type=\"tvdb\" default=\"true\">{escape(str(tvdbid))}</uniqueid>")
    if imdbid:
        lines.append(f"  <imdbid>{escape(str(imdbid))}</imdbid>")
        lines.append(f"  <uniqueid type=\"imdb\">{escape(str(imdbid))}</uniqueid>")
    if tmdbid:
        lines.append(f"  <uniqueid type=\"tmdb\">{escape(str(tmdbid))}</uniqueid>")
    if tvmazeid:
        lines.append(f"  <uniqueid type=\"tvmaze\">{escape(str(tvmazeid))}</uniqueid>")
    if runtime:
        lines.append(f"  <runtime>{runtime}</runtime>")
    if poster_url:
        lines.append(f"  <thumb aspect=\"poster\" preview=\"{poster_url}\">{poster_url}</thumb>")
    if fanart_url:
        lines.append("  <fanart>")
        lines.append(f"    <thumb preview=\"{fanart_url}\">{fanart_url}</thumb>")
        lines.append("  </fanart>")
    if banner_url:
        lines.append(f"  <banner>{banner_url}</banner>")
    if poster_url or fanart_url or banner_url:
        lines.append("  <art>")
        if poster_url:
            lines.append(f"    <poster>{poster_url}</poster>")
            lines.append(f"    <thumb>{poster_url}</thumb>")
        if fanart_url:
            lines.append(f"    <fanart>{fanart_url}</fanart>")
        if banner_url:
            lines.append(f"    <banner>{banner_url}</banner>")
        lines.append("  </art>")
    if certification:
        lines.append(f"  <mpaa>{escape(certification)}</mpaa>")
    for genre in genres:
        text = str(genre or '').strip()
        if text:
            lines.append(f"  <genre>{escape(text)}</genre>")
    status_text = _status_text(getattr(series, 'sonarr_status', None))
    if status_text:
        lines.append(f"  <status>{escape(status_text)}</status>")
    if first_aired:
        lines.append(f"  <premiered>{escape(str(first_aired))}</premiered>")
    if network:
        lines.append(f"  <studio>{escape(network)}</studio>")
    _append_actors(lines, actors)
    guide: dict[str, str] = {}
    if tvdbid:
        guide['tvdb'] = str(tvdbid)
    if tvmazeid:
        guide['tvmaze'] = str(tvmazeid)
    if tmdbid:
        guide['tmdb'] = str(tmdbid)
    if imdbid:
        guide['imdb'] = str(imdbid)
    if guide:
        lines.append(f"  <episodeguide>{escape(json.dumps(guide, separators=(',', ':')))}</episodeguide>")
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
        series_role = _arr_instance_role("series", series)
        root = settings.TV_LIBRARY_4K_FOLDER if series_role != "primary" else settings.TV_LIBRARY_FOLDER
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
