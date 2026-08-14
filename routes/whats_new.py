"""What's new / upgrade notice API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from services import auth as auth_svc
from services.app_config import get_onboarding_status
from services import whats_new as whats_new_svc

router = APIRouter(tags=["whats-new"])


class DismissBody(BaseModel):
    ids: list[str] = Field(default_factory=list)


def _require_user(request: Request):
    user, _source = auth_svc.resolve_request_identity(request)
    if not user:
        return None, auth_svc.json_error(401, "authentication required")
    return user, None


@router.get("/api/whats-new")
async def get_whats_new(request: Request) -> dict[str, Any]:
    user, err = _require_user(request)
    if err:
        return err
    status = get_onboarding_status()
    catalog = str(request.query_params.get("catalog") or "").strip().lower() in {"1", "true", "yes"}
    if catalog:
        return whats_new_svc.catalog_notices()
    return whats_new_svc.pending_notices(setup_complete=bool(status.get("setup_complete")))


@router.post("/api/whats-new/dismiss")
async def dismiss_whats_new(request: Request, body: DismissBody) -> dict[str, Any]:
    user, err = _require_user(request)
    if err:
        return err
    if not auth_svc.validate_csrf(request):
        return auth_svc.json_error(403, "CSRF token missing or invalid")
    status = get_onboarding_status()
    return whats_new_svc.dismiss_notices(body.ids, setup_complete=bool(status.get("setup_complete")))
