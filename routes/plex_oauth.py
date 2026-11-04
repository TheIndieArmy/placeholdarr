"""Admin Plex OAuth + PMS server selection (Media Integrations / onboarding)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.logger import logger
from services.app_config import save_settings
from services.media_servers import plex_oauth

router = APIRouter(tags=["plex-oauth"])


class PlexOAuthPollRequest(BaseModel):
    pin_id: int
    client_id: str | None = None


class PlexOAuthCompleteRequest(BaseModel):
    auth_token: str | None = None
    client_id: str | None = None
    machine_id: str = Field(..., min_length=1)
    username: str | None = None


class PlexOAuthServersRequest(BaseModel):
    auth_token: str | None = None
    client_id: str | None = None


@router.post("/api/plex/oauth/pin")
async def plex_oauth_create_pin():
    try:
        pin = plex_oauth.create_pin()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    client_id = str(pin.get("client_id") or "").strip()
    if client_id:
        try:
            save_settings({"PLEX_OAUTH_CLIENT_ID": client_id}, partial=True)
        except Exception as exc:
            logger.debug(f"Plex OAuth client id persist skipped: {exc}", extra={"emoji_type": "debug"})
    return {"ok": True, **pin}


@router.post("/api/plex/oauth/poll")
async def plex_oauth_poll(body: PlexOAuthPollRequest):
    """Wait for PIN claim. Does not persist PLEX_URL/TOKEN until ``/complete``."""
    try:
        result = plex_oauth.poll_pin(int(body.pin_id), client_id=body.client_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not result.get("authorized") or not result.get("auth_token"):
        return {"ok": True, "authorized": False}

    token = str(result["auth_token"])
    client_id = str(result.get("client_id") or body.client_id or "").strip()
    if client_id:
        try:
            save_settings({"PLEX_OAUTH_CLIENT_ID": client_id}, partial=True)
        except Exception:
            pass

    servers: list[dict[str, Any]] = []
    try:
        servers = plex_oauth.list_pms_servers(token, client_id=client_id or None)
    except Exception as exc:
        logger.warning(f"Plex server list after OAuth failed: {exc}", extra={"emoji_type": "warning"})

    # Never return raw access tokens in the server list over the wire for listing UI
    # except we need access_token only at complete time server-side. Strip here.
    safe_servers = [
        {
            "machine_id": s.get("machine_id"),
            "name": s.get("name"),
            "owned": s.get("owned"),
            "product": s.get("product"),
            "platform": s.get("platform"),
            "presence": s.get("presence"),
            "uri": s.get("uri"),
            "connection_count": s.get("connection_count"),
        }
        for s in servers
    ]
    return {
        "ok": True,
        "authorized": True,
        "auth_token": token,
        "username": result.get("username"),
        "client_id": client_id or None,
        "servers": safe_servers,
    }


@router.post("/api/plex/oauth/servers")
async def plex_oauth_servers(body: PlexOAuthServersRequest | None = None):
    """List PMS for an in-progress OAuth token or the stored account/watchlist token."""
    body = body or PlexOAuthServersRequest()
    token = str(body.auth_token or "").strip()
    if not token:
        from core.config import settings

        for key in ("PLEX_WATCHLIST_TOKEN", "PLEX_TOKEN"):
            token = str(getattr(settings, key, None) or "").strip()
            if token:
                break
    if not token:
        raise HTTPException(status_code=400, detail="No Plex account token available")
    try:
        servers = plex_oauth.list_pms_servers(token, client_id=body.client_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "servers": [
            {
                "machine_id": s.get("machine_id"),
                "name": s.get("name"),
                "owned": s.get("owned"),
                "product": s.get("product"),
                "platform": s.get("platform"),
                "presence": s.get("presence"),
                "uri": s.get("uri"),
                "connection_count": s.get("connection_count"),
            }
            for s in servers
        ],
    }


@router.post("/api/plex/oauth/complete")
async def plex_oauth_complete(body: PlexOAuthCompleteRequest):
    """Persist chosen PMS URL/token + account identity after OAuth."""
    from core.config import settings

    token = str(body.auth_token or "").strip()
    if not token:
        for key in ("PLEX_WATCHLIST_TOKEN", "PLEX_TOKEN"):
            token = str(getattr(settings, key, None) or "").strip()
            if token:
                break
    if not token:
        raise HTTPException(status_code=400, detail="Sign in with Plex again to select a server")
    client_id = str(body.client_id or "").strip() or None
    try:
        server = plex_oauth.resolve_server_choice(token, machine_id=body.machine_id, client_id=client_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    uri = str(server.get("uri") or "").strip()
    server_token = str(server.get("access_token") or token).strip()
    username = str(body.username or "").strip() or plex_oauth.fetch_account_username(token, client_id=client_id)
    values: dict[str, Any] = {
        "ENABLE_PLEX": True,
        "PLEX_URL": uri,
        "PLEX_TOKEN": server_token,
        "PLEX_WATCHLIST_TOKEN": token,
        "PLEX_AUTH_METHOD": "oauth",
        "PLEX_ACCOUNT_USERNAME": username or "",
        "PLEX_SERVER_NAME": str(server.get("name") or ""),
        "PLEX_SERVER_MACHINE_ID": str(server.get("machine_id") or ""),
    }
    if client_id:
        values["PLEX_OAUTH_CLIENT_ID"] = client_id

    # Connection-only: section IDs are collected on Folders / Paths, not here.
    save_result = save_settings(
        values,
        partial=True,
        context={"source": "plex_oauth", "stepKey": "media", "stepName": "Players"},
    )
    if not save_result.get("ok"):
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save Plex connection: {save_result.get('errors')}",
        )

    # Same path as Settings → Test: clear sticky connection ! when the saved URI works.
    connection: dict[str, Any] | None = None
    try:
        from services.integration_status import record_media_status
        from services.integrations import test_plex_connection

        connection = test_plex_connection(uri, server_token)
        if connection.get("ok"):
            record_media_status(
                "plex",
                ok=True,
                message=str(connection.get("message") or "Connected"),
                source="test",
            )
    except Exception as exc:
        connection = {"ok": False, "message": str(exc), "service": "plex"}

    probe = None
    try:
        from services.media_servers.plex_watchlist import test_watchlist_access

        probe = test_watchlist_access(token)
    except Exception as exc:
        probe = {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "username": username,
        "server_name": server.get("name"),
        "machine_id": server.get("machine_id"),
        "url": uri,
        "connection": connection,
        "watchlist": probe,
        "status": plex_oauth.connection_status_from_settings(),
    }


@router.get("/api/plex/oauth/status")
async def plex_oauth_status():
    return {"ok": True, **plex_oauth.connection_status_from_settings()}
