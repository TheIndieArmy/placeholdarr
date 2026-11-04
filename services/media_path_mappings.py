"""Translate media-server (remote) paths into Placeholdarr-local paths.

Used when Plex/Jellyfin/Emby run on a different OS or mount layout than
Placeholdarr (e.g. Windows Plex ``K:\\...\\media\\...`` vs Docker ``/media/...``).
"""

from __future__ import annotations

import json
from typing import Any

from core.config import settings
from core.logger import logger

SETTING_KEY = "MEDIA_SERVER_PATH_MAPPINGS_JSON"


def normalize_mapping_path(path: str | None) -> str:
    """Normalize separators and trailing slash (keep Windows drive letters)."""
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    while "//" in text:
        text = text.replace("//", "/")
    # Keep "K:/" style roots; strip trailing slash otherwise.
    if len(text) > 1 and text.endswith("/") and not (len(text) == 3 and text[1] == ":"):
        text = text.rstrip("/")
    return text


def parse_media_server_path_mappings(raw: str | None = None) -> list[dict[str, str]]:
    """Parse ``MEDIA_SERVER_PATH_MAPPINGS_JSON`` into ``{remote, local}`` rows."""
    text = str(
        raw if raw is not None else getattr(settings, SETTING_KEY, "") or ""
    ).strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception as exc:
        logger.warning(
            f"Failed to parse {SETTING_KEY}: {exc}",
            extra={"emoji_type": "warning"},
        )
        return []
    if not isinstance(payload, list):
        return []

    out: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        remote = normalize_mapping_path(
            item.get("remote") or item.get("host") or item.get("remote_path") or ""
        )
        local = normalize_mapping_path(
            item.get("local") or item.get("container") or item.get("local_path") or ""
        )
        if not remote or not local:
            continue
        out.append({"remote": remote, "local": local})
    return out


def validate_media_server_path_mappings(rows: list[dict[str, Any]]) -> str | None:
    """Return an error message when rows are invalid; otherwise None."""
    seen_remote: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            return "each mapping must be an object"
        remote = normalize_mapping_path(
            item.get("remote") or item.get("host") or item.get("remote_path") or ""
        )
        local = normalize_mapping_path(
            item.get("local") or item.get("container") or item.get("local_path") or ""
        )
        if not remote:
            return "each mapping needs a remote path prefix"
        if not local:
            return "each mapping needs a local path prefix"
        key = remote.lower()
        if key in seen_remote:
            return f"duplicate remote path prefix: {remote}"
        seen_remote.add(key)
    return None


def apply_media_server_path_mappings(path: str | None) -> str | None:
    """Rewrite ``path`` using the longest matching remote→local prefix.

    Unmatched paths are returned unchanged (aside from no-op when mappings are empty).
    Matching is case-insensitive so Windows drive letters work from a Linux host.
    """
    if path is None:
        return None
    text = str(path).strip()
    if not text:
        return path

    mappings = parse_media_server_path_mappings()
    if not mappings:
        return path

    norm = normalize_mapping_path(text)
    norm_l = norm.lower()
    # Longest remote prefix wins (Radarr-style).
    ranked = sorted(mappings, key=lambda row: len(row["remote"]), reverse=True)
    for row in ranked:
        remote = row["remote"]
        local = row["local"]
        remote_l = remote.lower()
        if norm_l == remote_l:
            return local
        prefix = remote_l if remote_l.endswith("/") else remote_l + "/"
        if not norm_l.startswith(prefix):
            continue
        # Keep the original-cased suffix from the slash-normalized path.
        suffix = norm[len(remote) :]
        if not suffix.startswith("/"):
            suffix = "/" + suffix.lstrip("/")
        if local.endswith("/"):
            return local.rstrip("/") + suffix
        return local + suffix
    return path
