"""ASGI middleware enforcing dashboard API authentication."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from services import auth as auth_svc


class AuthGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        method = request.method.upper()
        path = request.url.path

        if auth_svc.is_auth_allowlisted(method, path):
            return await call_next(request)

        # SPA HTML and static files are public; the React gate handles UX.
        if not path.startswith("/api/"):
            return await call_next(request)

        mode = auth_svc.get_auth_mode()
        if mode == "disabled":
            return await call_next(request)

        username, _source = auth_svc.resolve_request_identity(request)
        if not username:
            return auth_svc.json_error(401, "authentication required")

        # CSRF for builtin cookie sessions on mutating API calls.
        if mode == "builtin" and auth_svc.mutating_method(method):
            # change-password / logout validate inside routes too; still enforce here.
            if not auth_svc.validate_csrf(request):
                return auth_svc.json_error(403, "CSRF token missing or invalid")

        request.state.auth_username = username
        return await call_next(request)
