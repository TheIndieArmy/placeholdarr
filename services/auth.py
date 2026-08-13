"""Dashboard authentication: builtin sessions, forward-auth, CSRF, rate limits."""

from __future__ import annotations

import ipaddress
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request
from starlette.responses import JSONResponse

from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig


AUTH_USERNAME_KEY = "AUTH_USERNAME"
AUTH_PASSWORD_HASH_KEY = "AUTH_PASSWORD_HASH"
AUTH_SESSION_SECRET_KEY = "AUTH_SESSION_SECRET"
AUTH_MODE_KEY = "AUTH_MODE"
AUTH_TRUSTED_PROXIES_KEY = "AUTH_TRUSTED_PROXIES"
AUTH_WEBHOOK_API_KEY_KEY = "AUTH_WEBHOOK_API_KEY"

SESSION_USER_KEY = "auth_user"
SESSION_CSRF_KEY = "csrf_token"

AUTH_MODES = frozenset({"builtin", "forward_auth", "disabled"})
DEFAULT_AUTH_MODE = "builtin"

FORWARD_IDENTITY_HEADERS = ("Remote-User", "X-Forwarded-User", "X-Remote-User")

MIN_PASSWORD_LENGTH = 8
LOGIN_RATE_LIMIT_MAX = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 15 * 60

CSRF_HEADER = "X-CSRF-Token"

_password_hasher = PasswordHasher()
_rate_lock = threading.Lock()
_login_failures: dict[str, list[float]] = {}


def _get_config_value(key: str, session=None) -> Any:
    owns = session is None
    session = session or get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == key).first()
        return row.value if row else None
    finally:
        if owns:
            session.close()


def _set_config_value(key: str, value: Any, *, value_type: str = "string", description: str = "", session=None) -> None:
    owns = session is None
    session = session or get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == key).first()
        if not row:
            row = AppConfig(
                key=key,
                value=value,
                value_type=value_type,
                restart_required=False,
                description=description or key,
            )
            session.add(row)
        else:
            row.value = value
            row.value_type = value_type
            session.add(row)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        if owns:
            session.close()


def ensure_session_secret() -> str:
    """Return a persistent signing secret, creating one on first boot if needed."""
    try:
        existing = _get_config_value(AUTH_SESSION_SECRET_KEY)
        if isinstance(existing, str) and existing.strip():
            return existing.strip()
    except Exception as exc:
        logger.warning(f"Could not read AUTH_SESSION_SECRET from DB: {exc}", extra={"emoji_type": "warning"})

    env_secret = str(os.getenv("AUTH_SESSION_SECRET", "") or "").strip()
    if env_secret:
        try:
            _set_config_value(
                AUTH_SESSION_SECRET_KEY,
                env_secret,
                description="Signed session cookie secret",
            )
        except Exception as exc:
            logger.warning(f"Failed to persist AUTH_SESSION_SECRET: {exc}", extra={"emoji_type": "warning"})
        return env_secret

    # File fallback so SessionMiddleware can start before DB is ready.
    try:
        from core.config import settings as _settings

        appdata = str(getattr(_settings, "APPDATA_PATH", "/config") or "/config").strip() or "/config"
        secret_path = Path(appdata) / ".auth_session_secret"
        if secret_path.is_file():
            file_secret = secret_path.read_text(encoding="utf-8").strip()
            if file_secret:
                try:
                    _set_config_value(
                        AUTH_SESSION_SECRET_KEY,
                        file_secret,
                        description="Signed session cookie secret",
                    )
                except Exception:
                    pass
                return file_secret
        generated = secrets.token_hex(32)
        try:
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret_path.write_text(generated, encoding="utf-8")
            try:
                os.chmod(secret_path, 0o600)
            except Exception:
                pass
        except Exception as exc:
            logger.warning(f"Failed to write session secret file: {exc}", extra={"emoji_type": "warning"})
        try:
            _set_config_value(
                AUTH_SESSION_SECRET_KEY,
                generated,
                description="Signed session cookie secret",
            )
        except Exception:
            pass
        return generated
    except Exception as exc:
        logger.warning(f"Session secret fallback failed: {exc}", extra={"emoji_type": "warning"})
        return secrets.token_hex(32)


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return bool(_password_hasher.verify(password_hash, password))
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def is_auth_configured(session=None) -> bool:
    username = _get_config_value(AUTH_USERNAME_KEY, session=session)
    password_hash = _get_config_value(AUTH_PASSWORD_HASH_KEY, session=session)
    return bool(str(username or "").strip() and str(password_hash or "").strip())


def get_auth_username(session=None) -> str | None:
    value = _get_config_value(AUTH_USERNAME_KEY, session=session)
    text = str(value or "").strip()
    return text or None


def get_password_hash(session=None) -> str | None:
    value = _get_config_value(AUTH_PASSWORD_HASH_KEY, session=session)
    text = str(value or "").strip()
    return text or None


def normalize_auth_mode(raw: Any) -> str:
    mode = str(raw or "").strip().lower()
    if mode in AUTH_MODES:
        return mode
    return DEFAULT_AUTH_MODE


def get_auth_mode(session=None) -> str:
    env_mode = str(os.getenv("AUTH_MODE", "") or "").strip().lower()
    if env_mode in AUTH_MODES:
        return env_mode
    try:
        from core.config import settings

        runtime = getattr(settings, AUTH_MODE_KEY, None)
        if runtime is not None and str(runtime).strip():
            return normalize_auth_mode(runtime)
    except Exception:
        pass
    stored = _get_config_value(AUTH_MODE_KEY, session=session)
    if stored is not None and str(stored).strip():
        return normalize_auth_mode(stored)
    return DEFAULT_AUTH_MODE


def get_trusted_proxies_raw(session=None) -> str:
    env_raw = str(os.getenv("AUTH_TRUSTED_PROXIES", "") or "").strip()
    if env_raw:
        return env_raw
    try:
        from core.config import settings

        runtime = getattr(settings, AUTH_TRUSTED_PROXIES_KEY, None)
        if runtime is not None and str(runtime).strip():
            return str(runtime).strip()
    except Exception:
        pass
    stored = _get_config_value(AUTH_TRUSTED_PROXIES_KEY, session=session)
    return str(stored or "").strip()


def parse_trusted_proxies(raw: str | None = None) -> list[ipaddress._BaseNetwork]:
    text = raw if raw is not None else get_trusted_proxies_raw()
    networks: list[ipaddress._BaseNetwork] = []
    for part in str(text or "").split(","):
        token = part.strip()
        if not token:
            continue
        try:
            if "/" in token:
                networks.append(ipaddress.ip_network(token, strict=False))
            else:
                ip = ipaddress.ip_address(token)
                networks.append(ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}", strict=False))
        except Exception:
            logger.warning(f"Ignoring invalid AUTH_TRUSTED_PROXIES entry: {token}", extra={"emoji_type": "warning"})
    return networks


def request_client_ip(request: Request) -> str | None:
    if request.client and request.client.host:
        return str(request.client.host).strip() or None
    return None


def client_ip_trusted(request: Request, *, networks: list[ipaddress._BaseNetwork] | None = None) -> bool:
    host = request_client_ip(request)
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except Exception:
        return False
    nets = networks if networks is not None else parse_trusted_proxies()
    if not nets:
        return False
    return any(ip in net for net in nets)


def identity_from_forward_headers(request: Request) -> str | None:
    for header in FORWARD_IDENTITY_HEADERS:
        value = request.headers.get(header)
        if value and str(value).strip():
            return str(value).strip()
    return None


def request_is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    if client_ip_trusted(request):
        proto = str(request.headers.get("X-Forwarded-Proto") or "").strip().lower()
        if proto == "https":
            return True
    return False


def create_admin_account(username: str, password: str, session=None) -> None:
    username = str(username or "").strip()
    password = str(password or "")
    if not username:
        raise ValueError("username is required")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if is_auth_configured(session=session):
        raise LookupError("admin account already configured")
    owns = session is None
    session = session or get_session()
    try:
        _set_config_value(
            AUTH_USERNAME_KEY,
            username,
            description="Dashboard admin username",
            session=session,
        )
        _set_config_value(
            AUTH_PASSWORD_HASH_KEY,
            hash_password(password),
            description="Dashboard admin password hash (Argon2id)",
            session=session,
        )
        # Ensure mode defaults to builtin when first account is created.
        if not _get_config_value(AUTH_MODE_KEY, session=session):
            _set_config_value(
                AUTH_MODE_KEY,
                DEFAULT_AUTH_MODE,
                description="Dashboard authentication mode",
                session=session,
            )
        try:
            from core.config import settings

            setattr(settings, AUTH_MODE_KEY, DEFAULT_AUTH_MODE)
        except Exception:
            pass
        # Provision a webhook key alongside the account so it's ready the
        # moment onboarding shows Radarr/Sonarr/Tautulli/Jellyfin webhook URLs.
        ensure_webhook_api_key(session=session)
    finally:
        if owns:
            session.close()


def generate_webhook_api_key() -> str:
    return secrets.token_hex(20)  # 40 hex chars, matches a typical Radarr/Sonarr API key's shape


def get_webhook_api_key(session=None) -> str | None:
    value = _get_config_value(AUTH_WEBHOOK_API_KEY_KEY, session=session)
    text = str(value or "").strip()
    return text or None


def ensure_webhook_api_key(session=None) -> str:
    """Return the current webhook API key, creating one on first use.

    Kept separate from AUTH_SESSION_SECRET / the password hash: this key
    authenticates POST /webhook (called by Radarr/Sonarr/Tautulli/Jellyfin/
    Emby, not a browser — see is_auth_allowlisted, which exempts /webhook
    from the cookie/CSRF checks entirely since those callers can't log in).
    Never rotated implicitly by create_admin_account/change_admin_password —
    only regenerate_webhook_api_key() rotates it, since doing so silently
    would break every already-configured webhook connection.
    """
    existing = get_webhook_api_key(session=session)
    if existing:
        return existing
    generated = generate_webhook_api_key()
    _set_config_value(
        AUTH_WEBHOOK_API_KEY_KEY,
        generated,
        description="API key required on POST /webhook (?apikey=) for Radarr/Sonarr/Tautulli/Jellyfin/Emby callers",
        session=session,
    )
    return generated


def regenerate_webhook_api_key(session=None) -> str:
    """Explicit rotation, e.g. from a Settings action. Breaks every existing
    webhook connection until the URLs are re-pasted with the new key — the
    caller is responsible for warning about that before invoking this."""
    generated = generate_webhook_api_key()
    _set_config_value(
        AUTH_WEBHOOK_API_KEY_KEY,
        generated,
        description="API key required on POST /webhook (?apikey=) for Radarr/Sonarr/Tautulli/Jellyfin/Emby callers",
        session=session,
    )
    return generated


def verify_webhook_api_key(provided: str | None, session=None) -> bool:
    if not provided:
        return False
    stored = get_webhook_api_key(session=session)
    if not stored:
        return False
    return secrets.compare_digest(str(provided), stored)


def change_admin_password(current_password: str, new_password: str, session=None) -> None:
    new_password = str(new_password or "")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    stored_hash = get_password_hash(session=session)
    if not stored_hash or not verify_password(stored_hash, str(current_password or "")):
        raise PermissionError("current password is incorrect")
    _set_config_value(
        AUTH_PASSWORD_HASH_KEY,
        hash_password(new_password),
        description="Dashboard admin password hash (Argon2id)",
        session=session,
    )


def authenticate_password(username: str, password: str, session=None) -> bool:
    expected_user = get_auth_username(session=session)
    stored_hash = get_password_hash(session=session)
    if not expected_user or not stored_hash:
        return False
    provided_user = str(username or "").strip()
    if provided_user != expected_user:
        # Still hash-verify to reduce username-oracle timing differences.
        verify_password(stored_hash, str(password or ""))
        return False
    return verify_password(stored_hash, str(password or ""))


def login_rate_limit_key(request: Request, username: str) -> str:
    ip = request_client_ip(request) or "unknown"
    return f"{ip}|{str(username or '').strip().lower()}"


def login_rate_limited(key: str) -> bool:
    now = time.time()
    with _rate_lock:
        stamps = [t for t in _login_failures.get(key, []) if now - t < LOGIN_RATE_LIMIT_WINDOW_SECONDS]
        _login_failures[key] = stamps
        return len(stamps) >= LOGIN_RATE_LIMIT_MAX


def record_login_failure(key: str) -> None:
    now = time.time()
    with _rate_lock:
        stamps = [t for t in _login_failures.get(key, []) if now - t < LOGIN_RATE_LIMIT_WINDOW_SECONDS]
        stamps.append(now)
        _login_failures[key] = stamps


def clear_login_failures(key: str) -> None:
    with _rate_lock:
        _login_failures.pop(key, None)


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF_KEY)
    if isinstance(token, str) and token.strip():
        return token
    token = secrets.token_urlsafe(32)
    request.session[SESSION_CSRF_KEY] = token
    return token


def validate_csrf(request: Request) -> bool:
    expected = request.session.get(SESSION_CSRF_KEY)
    provided = request.headers.get(CSRF_HEADER) or request.headers.get(CSRF_HEADER.lower())
    if not isinstance(expected, str) or not expected.strip():
        return False
    if not isinstance(provided, str) or not provided.strip():
        return False
    return secrets.compare_digest(expected, provided)


def login_user(request: Request, username: str) -> None:
    request.session[SESSION_USER_KEY] = str(username).strip()
    ensure_csrf_token(request)


def logout_user(request: Request) -> None:
    request.session.pop(SESSION_USER_KEY, None)
    # Rotate CSRF on logout.
    request.session[SESSION_CSRF_KEY] = secrets.token_urlsafe(32)


def session_username(request: Request) -> str | None:
    value = request.session.get(SESSION_USER_KEY)
    text = str(value or "").strip()
    return text or None


def resolve_request_identity(request: Request) -> tuple[str | None, str]:
    """Return (username_or_none, auth_source) for the current request."""
    mode = get_auth_mode()
    if mode == "disabled":
        return ("anonymous", "disabled")
    if mode == "forward_auth":
        if client_ip_trusted(request):
            identity = identity_from_forward_headers(request)
            if identity:
                return (identity, "forward_auth")
        return (None, "forward_auth")
    # builtin
    user = session_username(request)
    if user:
        return (user, "builtin")
    return (None, "builtin")


def build_auth_status(request: Request) -> dict[str, Any]:
    mode = get_auth_mode()
    configured = is_auth_configured()
    username, source = resolve_request_identity(request)
    if mode == "disabled":
        authenticated = True
        display_user = None
    else:
        authenticated = bool(username)
        display_user = username if authenticated else None
    csrf_token = ensure_csrf_token(request)
    trusted = client_ip_trusted(request)
    return {
        "mode": mode,
        "configured": configured,
        "authenticated": authenticated,
        "username": display_user,
        "csrf_token": csrf_token,
        "forward_auth_ready": bool(parse_trusted_proxies()) if mode == "forward_auth" else None,
        "trusted_proxy": trusted if mode == "forward_auth" else None,
        "auth_source": source,
    }


def json_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, "message": detail})


# Paths that never require authentication (method-aware where needed).
_AUTH_ALLOWLIST_EXACT = {
    ("GET", "/api/health"),
    ("GET", "/api/ready"),
    ("GET", "/api/auth/status"),
    ("POST", "/api/auth/setup"),
    ("POST", "/api/auth/login"),
    ("POST", "/webhook"),
}


def is_auth_allowlisted(method: str, path: str) -> bool:
    method_u = method.upper()
    path_n = path.rstrip("/") or "/"
    if path_n == "/webhook" and method_u == "POST":
        return True
    if (method_u, path_n) in _AUTH_ALLOWLIST_EXACT:
        return True
    # Also accept trailing-slash variants already normalized.
    if path_n.startswith("/assets/") or path_n in {
        "/favicon.ico",
        "/favicon-16x16.png",
        "/favicon-32x32.png",
        "/apple-touch-icon.png",
    }:
        return True
    return False


def mutating_method(method: str) -> bool:
    return method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
