from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any

from services.placeholders import _ensure_open_permissions  # reuse existing perms helper

# Destinations mirror services.placeholders._resolve_dummy_path()'s highest-priority
# runtime fallback, so an uploaded file is picked up automatically without needing
# to touch DUMMY_FILE_PATH / COMING_SOON_DUMMY_FILE_PATH settings.
_DEST = {
    "primary": "/config/dummy.mp4",
    "coming_soon": "/config/coming_soon_dummy.mp4",
}

_MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500MB safety cap
_ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v"}


class DummyMediaError(Exception):
    """Raised for invalid uploads / unknown kinds."""


def _repo_default_source(kind: str) -> str:
    service_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(service_dir, ".."))
    filename = "coming_soon_dummy.mp4" if kind == "coming_soon" else "dummy.mp4"
    for candidate in (
        os.path.join("/app", filename),
        os.path.join(repo_root, filename),
    ):
        if os.path.isfile(candidate):
            return candidate
    return ""


def _dest_for(kind: str) -> str:
    if kind not in _DEST:
        raise DummyMediaError(f"Unknown dummy media kind: {kind!r}")
    return _DEST[kind]


def get_dummy_media_status() -> dict[str, Any]:
    """Report current state (custom vs default, size, mtime) for both dummy kinds."""
    result: dict[str, Any] = {}
    for kind, dest in _DEST.items():
        default_source = _repo_default_source(kind)
        info: dict[str, Any] = {
            "kind": kind,
            "path": dest,
            "exists": False,
            "size_bytes": 0,
            "modified_at": None,
            "is_default": True,
        }
        try:
            if os.path.isfile(dest):
                stat = os.stat(dest)
                info["exists"] = True
                info["size_bytes"] = stat.st_size
                info["modified_at"] = stat.st_mtime
                if default_source and os.path.isfile(default_source):
                    import filecmp

                    info["is_default"] = filecmp.cmp(dest, default_source, shallow=False)
                else:
                    info["is_default"] = False
        except Exception:
            pass
        result[kind] = info
    return result


def save_uploaded_dummy_media(kind: str, tmp_path: str, size_bytes: int) -> dict[str, Any]:
    """Atomically replace the dummy media file for `kind` with the uploaded file at tmp_path."""
    dest = _dest_for(kind)

    if size_bytes <= 0:
        raise DummyMediaError("Uploaded file is empty.")
    if size_bytes > _MAX_UPLOAD_BYTES:
        raise DummyMediaError(f"Uploaded file exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit.")

    config_dir = os.path.dirname(dest)
    os.makedirs(config_dir, exist_ok=True)
    _ensure_open_permissions(config_dir, is_dir=True)

    # Write into the destination directory first, then atomic rename, so a failed/partial
    # upload never clobbers a working dummy file mid-write.
    fd, staged_path = tempfile.mkstemp(prefix=".dummy_upload_", dir=config_dir)
    os.close(fd)
    try:
        shutil.copyfile(tmp_path, staged_path)
        os.replace(staged_path, dest)
    except Exception:
        try:
            os.unlink(staged_path)
        except OSError:
            pass
        raise
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    _ensure_open_permissions(dest)
    return get_dummy_media_status()[kind]


def reset_dummy_media_to_default(kind: str) -> dict[str, Any]:
    """Restore the bundled default video for `kind`, discarding any custom upload."""
    dest = _dest_for(kind)
    source = _repo_default_source(kind)
    if not source:
        raise DummyMediaError(f"No bundled default video available for {kind!r}.")

    config_dir = os.path.dirname(dest)
    os.makedirs(config_dir, exist_ok=True)
    _ensure_open_permissions(config_dir, is_dir=True)

    shutil.copy2(source, dest)
    _ensure_open_permissions(dest)
    return get_dummy_media_status()[kind]
