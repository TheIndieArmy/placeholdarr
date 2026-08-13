"""HTTP routes for dashboard authentication."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from services import auth as auth_svc

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SetupBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=auth_svc.MIN_PASSWORD_LENGTH, max_length=256)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=auth_svc.MIN_PASSWORD_LENGTH, max_length=256)


@router.get("/status")
async def auth_status(request: Request) -> dict[str, Any]:
    return auth_svc.build_auth_status(request)


@router.post("/setup")
async def auth_setup(request: Request, body: SetupBody):
    if auth_svc.get_auth_mode() == "disabled":
        return auth_svc.json_error(400, "authentication is disabled")
    if auth_svc.get_auth_mode() == "forward_auth":
        return auth_svc.json_error(400, "builtin setup is not available in forward_auth mode")
    if not auth_svc.validate_csrf(request):
        return auth_svc.json_error(403, "CSRF token missing or invalid")
    if auth_svc.is_auth_configured():
        return auth_svc.json_error(409, "admin account already configured")
    try:
        auth_svc.create_admin_account(body.username, body.password)
    except LookupError as exc:
        return auth_svc.json_error(409, str(exc))
    except ValueError as exc:
        return auth_svc.json_error(400, str(exc))
    auth_svc.login_user(request, body.username.strip())
    return auth_svc.build_auth_status(request)


@router.post("/login")
async def auth_login(request: Request, body: LoginBody):
    if auth_svc.get_auth_mode() != "builtin":
        return auth_svc.json_error(400, "password login is only available in builtin mode")
    if not auth_svc.validate_csrf(request):
        return auth_svc.json_error(403, "CSRF token missing or invalid")
    if not auth_svc.is_auth_configured():
        return auth_svc.json_error(400, "admin account is not configured")
    rate_key = auth_svc.login_rate_limit_key(request, body.username)
    if auth_svc.login_rate_limited(rate_key):
        return auth_svc.json_error(429, "too many failed login attempts; try again later")
    if not auth_svc.authenticate_password(body.username, body.password):
        auth_svc.record_login_failure(rate_key)
        return auth_svc.json_error(401, "invalid username or password")
    auth_svc.clear_login_failures(rate_key)
    auth_svc.login_user(request, body.username.strip())
    return auth_svc.build_auth_status(request)


@router.post("/logout")
async def auth_logout(request: Request):
    if auth_svc.get_auth_mode() == "builtin" and auth_svc.session_username(request):
        if not auth_svc.validate_csrf(request):
            return auth_svc.json_error(403, "CSRF token missing or invalid")
    auth_svc.logout_user(request)
    return auth_svc.build_auth_status(request)


@router.post("/change-password")
async def auth_change_password(request: Request, body: ChangePasswordBody):
    if auth_svc.get_auth_mode() != "builtin":
        return auth_svc.json_error(400, "password changes are only available in builtin mode")
    user, _source = auth_svc.resolve_request_identity(request)
    if not user:
        return auth_svc.json_error(401, "authentication required")
    if not auth_svc.validate_csrf(request):
        return auth_svc.json_error(403, "CSRF token missing or invalid")
    try:
        auth_svc.change_admin_password(body.current_password, body.new_password)
    except PermissionError as exc:
        return auth_svc.json_error(401, str(exc))
    except ValueError as exc:
        return auth_svc.json_error(400, str(exc))
    return {"ok": True, "message": "password updated"}


@router.get("/webhook-key")
async def auth_webhook_key(request: Request):
    # Not allowlisted, so AuthGateMiddleware already required a valid
    # session to reach here.
    user, _source = auth_svc.resolve_request_identity(request)
    if not user:
        return auth_svc.json_error(401, "authentication required")
    return {"webhook_api_key": auth_svc.ensure_webhook_api_key()}


@router.post("/webhook-key/regenerate")
async def auth_webhook_key_regenerate(request: Request):
    # Rotating this breaks every already-configured Radarr/Sonarr/Tautulli/
    # Jellyfin/Emby webhook until the URLs are re-pasted with the new key —
    # the frontend action that calls this must warn before doing so.
    user, _source = auth_svc.resolve_request_identity(request)
    if not user:
        return auth_svc.json_error(401, "authentication required")
    if not auth_svc.validate_csrf(request):
        return auth_svc.json_error(403, "CSRF token missing or invalid")
    return {"webhook_api_key": auth_svc.regenerate_webhook_api_key()}
