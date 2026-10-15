"""HTTP routes for uploading/resetting the placeholder ("dummy") video files
shown in Plex/Jellyfin/Emby for missing and coming-soon titles.
"""

from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from core.logger import logger
from services.dummy_media import (
    DummyMediaError,
    get_dummy_media_status,
    reset_dummy_media_to_default,
    save_uploaded_dummy_media,
)

router = APIRouter(prefix="/api/settings/dummy-media", tags=["dummy-media"])

_VALID_KINDS = {"primary", "coming_soon"}
_CHUNK_SIZE = 1024 * 1024  # 1MB


@router.get("/status")
async def dummy_media_status():
    return JSONResponse(content=get_dummy_media_status())


@router.post("/upload")
async def dummy_media_upload(kind: str, file: UploadFile = File(...)):
    if kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid kind '{kind}'. Expected one of {sorted(_VALID_KINDS)}.")

    suffix = os.path.splitext(file.filename or "")[1].lower() or ".mp4"
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="placeholdarr_dummy_upload_", suffix=suffix)
    size = 0
    try:
        with os.fdopen(tmp_fd, "wb") as tmp:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                tmp.write(chunk)
    finally:
        await file.close()

    try:
        info = save_uploaded_dummy_media(kind, tmp_path, size)
    except DummyMediaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Failed to save uploaded dummy media ({kind}): {exc}", extra={"emoji_type": "error"})
        raise HTTPException(status_code=500, detail="Failed to save uploaded file.") from exc

    logger.info(f"Custom dummy media uploaded for kind={kind} ({size} bytes)", extra={"emoji_type": "dummy"})
    return JSONResponse(content=info)


@router.post("/reset")
async def dummy_media_reset(kind: str):
    if kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid kind '{kind}'. Expected one of {sorted(_VALID_KINDS)}.")
    try:
        info = reset_dummy_media_to_default(kind)
    except DummyMediaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Failed to reset dummy media ({kind}): {exc}", extra={"emoji_type": "error"})
        raise HTTPException(status_code=500, detail="Failed to reset to default.") from exc

    logger.info(f"Dummy media reset to default for kind={kind}", extra={"emoji_type": "dummy"})
    return JSONResponse(content=info)
