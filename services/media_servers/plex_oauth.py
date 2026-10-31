"""Admin Plex OAuth (PIN / link) to obtain a plex.tv account token and pick a PMS.

Flow:
1. ``create_pin`` → pin id + code + auth URL
2. Admin opens URL and signs in with Plex
3. ``poll_pin`` until ``authToken`` is present
4. ``list_pms_servers`` → choose a Media Server
5. Persist ``PLEX_URL`` + server token (+ account token for watchlist)
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import quote

import requests

from core.config import settings
from core.logger import logger

_PLEX_PRODUCT = "Placeholdarr"
_PINS_URL = "https://plex.tv/api/v2/pins"
_RESOURCES_URL = "https://plex.tv/api/v2/resources"
_USER_URL = "https://plex.tv/api/v2/user"


def _client_id() -> str:
    existing = str(getattr(settings, "PLEX_OAUTH_CLIENT_ID", None) or "").strip()
    if existing:
        return existing
    return f"placeholdarr-{secrets.token_hex(8)}"


def _headers(client_id: str, *, token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": _PLEX_PRODUCT,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Version": "1.0",
        "X-Plex-Platform": "Linux",
        "X-Plex-Device": "Placeholdarr",
        "X-Plex-Device-Name": "Placeholdarr",
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def create_pin() -> dict[str, Any]:
    client_id = _client_id()
    resp = requests.post(
        _PINS_URL,
        headers=_headers(client_id),
        params={"strong": "true"},
        timeout=20,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Plex PIN create failed HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json() if resp.content else {}
    pin_id = data.get("id")
    code = data.get("code")
    if not pin_id or not code:
        raise RuntimeError("Plex PIN response missing id/code")
    auth_url = (
        "https://app.plex.tv/auth#?"
        f"clientID={quote(client_id)}&code={quote(str(code))}"
        f"&context%5Bdevice%5D%5Bproduct%5D={quote(_PLEX_PRODUCT)}"
    )
    return {
        "id": int(pin_id),
        "code": str(code),
        "client_id": client_id,
        "auth_url": auth_url,
        "expires_in": data.get("expiresIn"),
    }


def fetch_account_username(token: str, *, client_id: str | None = None) -> str | None:
    cid = (client_id or _client_id()).strip()
    try:
        acc = requests.get(_USER_URL, headers=_headers(cid, token=token), timeout=20)
        if acc.status_code >= 400:
            return None
        user = acc.json() if acc.content else {}
        return user.get("username") or user.get("title") or user.get("friendlyName")
    except Exception as exc:
        logger.debug(f"Plex user lookup: {exc}", extra={"emoji_type": "debug"})
        return None


def poll_pin(pin_id: int, *, client_id: str | None = None) -> dict[str, Any]:
    cid = (client_id or _client_id()).strip()
    resp = requests.get(
        f"{_PINS_URL}/{int(pin_id)}",
        headers=_headers(cid),
        timeout=20,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Plex PIN poll failed HTTP {resp.status_code}")
    data = resp.json() if resp.content else {}
    token = data.get("authToken")
    if not token:
        return {"authorized": False, "auth_token": None}
    username = fetch_account_username(str(token), client_id=cid)
    return {
        "authorized": True,
        "auth_token": str(token),
        "username": username,
        "client_id": cid,
    }


def _is_plex_direct(host_or_uri: str) -> bool:
    return "plex.direct" in str(host_or_uri or "").lower()


def _is_lan_host(host: str) -> bool:
    h = str(host or "").strip().lower().split("%")[0]
    if not h:
        return False
    if h in {"localhost", "127.0.0.1", "::1"} or h.endswith(".local"):
        return True
    # IPv4 private ranges
    if h.startswith("10.") or h.startswith("192.168.") or h.startswith("169.254."):
        return True
    if h.startswith("172."):
        try:
            second = int(h.split(".")[1])
        except (IndexError, ValueError):
            return False
        return 16 <= second <= 31
    return False


def _uri_score(uri: str, *, local: bool, relay: bool) -> tuple[int, int, int, int, int]:
    """Higher is better for Placeholdarr server-to-server access."""
    u = str(uri or "").rstrip("/")
    host = ""
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(u).hostname or "").lower()
    except Exception:
        host = ""
    return (
        0 if relay else 1,
        1 if local else 0,
        0 if _is_plex_direct(u) or _is_plex_direct(host) else 1,
        1 if _is_lan_host(host) else 0,
        # Prefer http for LAN IP (avoids plex.direct TLS / cert issues from the app host).
        1 if u.lower().startswith("http://") and _is_lan_host(host) else 0,
    )


def _candidate_uris_for_connection(conn: dict[str, Any]) -> list[str]:
    """Expand one Plex connection into URI candidates (LAN host:port before plex.direct)."""
    out: list[str] = []
    address = str(conn.get("address") or "").strip()
    port = conn.get("port")
    protocol = str(conn.get("protocol") or "http").strip().lower() or "http"
    published = str(conn.get("uri") or "").strip().rstrip("/")

    if address and port not in (None, ""):
        try:
            port_i = int(port)
        except (TypeError, ValueError):
            port_i = None
        if port_i:
            if _is_lan_host(address):
                # Server-to-server on LAN: plain IP/hostname beats plex.direct.
                out.append(f"http://{address}:{port_i}")
                out.append(f"https://{address}:{port_i}")
            else:
                out.append(f"{protocol}://{address}:{port_i}")
    if published:
        out.append(published)

    deduped: list[str] = []
    seen: set[str] = set()
    for uri in out:
        key = uri.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(uri.rstrip("/"))
    return deduped


def ranked_connection_uris(connections: list[Any]) -> list[str]:
    scored: list[tuple[tuple[int, int, int, int, int], str]] = []
    for raw in connections or []:
        if not isinstance(raw, dict):
            continue
        local = bool(raw.get("local"))
        relay = bool(raw.get("relay"))
        for uri in _candidate_uris_for_connection(raw):
            scored.append((_uri_score(uri, local=local, relay=relay), uri))
    if not scored:
        return []
    scored.sort(key=lambda item: item[0], reverse=True)
    # Keep order; drop duplicate URIs that appeared under multiple connections.
    out: list[str] = []
    seen: set[str] = set()
    for _score, uri in scored:
        key = uri.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(uri)
    return out


def probe_plex_identity(uri: str, token: str, *, client_id: str | None = None, timeout: float = 2.5) -> bool:
    """True when Placeholdarr can reach ``{uri}/identity`` with the token."""
    base = str(uri or "").rstrip("/")
    tok = str(token or "").strip()
    if not base or not tok:
        return False
    cid = (client_id or _client_id()).strip()
    try:
        resp = requests.get(
            f"{base}/identity",
            headers=_headers(cid, token=tok),
            timeout=timeout,
            verify=True,
        )
        return resp.status_code < 400
    except Exception:
        # plex.direct / LAN https often uses certs that fail default verify; retry once insecure.
        try:
            resp = requests.get(
                f"{base}/identity",
                headers=_headers(cid, token=tok),
                timeout=timeout,
                verify=False,
            )
            return resp.status_code < 400
        except Exception:
            return False


def pick_best_connection_uri(
    connections: list[Any],
    *,
    token: str | None = None,
    client_id: str | None = None,
) -> str | None:
    """Prefer a URI Placeholdarr can actually reach; fall back to ranked guess."""
    ranked = ranked_connection_uris(connections)
    if not ranked:
        return None
    tok = str(token or "").strip()
    if tok:
        for uri in ranked:
            if probe_plex_identity(uri, tok, client_id=client_id):
                return uri
    return ranked[0]


def list_pms_servers(token: str, *, client_id: str | None = None) -> list[dict[str, Any]]:
    """Return PMS resources the account can access (owned + shared)."""
    cid = (client_id or _client_id()).strip()
    resp = requests.get(
        _RESOURCES_URL,
        headers=_headers(cid, token=token),
        params={"includeHttps": "1", "includeRelay": "1", "includeIPv6": "1"},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Plex resources failed HTTP {resp.status_code}")
    data = resp.json() if resp.content else []
    if not isinstance(data, list):
        data = []
    out: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        provides = str(raw.get("provides") or "").lower()
        if "server" not in provides:
            continue
        machine_id = str(raw.get("clientIdentifier") or "").strip()
        name = str(raw.get("name") or machine_id or "Plex Media Server").strip()
        connections = raw.get("connections") if isinstance(raw.get("connections"), list) else []
        access = str(raw.get("accessToken") or token).strip()
        # Rank only here (fast). resolve_server_choice probes for a reachable URI.
        uri = pick_best_connection_uri(connections)
        if not uri and not machine_id:
            continue
        owned = bool(raw.get("owned"))
        out.append(
            {
                "machine_id": machine_id,
                "name": name,
                "owned": owned,
                "product": raw.get("product"),
                "platform": raw.get("platform"),
                "presence": bool(raw.get("presence")),
                "uri": uri,
                "access_token": access,
                "connection_count": len(connections or []),
                "connection_uris": ranked_connection_uris(connections),
            }
        )
    # Owned first, then name.
    out.sort(key=lambda s: (0 if s.get("owned") else 1, str(s.get("name") or "").lower()))
    return out


def resolve_server_choice(
    token: str,
    *,
    machine_id: str,
    client_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a PMS and pick a URI Placeholdarr can reach (probed)."""
    cid = (client_id or _client_id()).strip()
    # Re-fetch resources so we have raw connections for probing.
    resp = requests.get(
        _RESOURCES_URL,
        headers=_headers(cid, token=token),
        params={"includeHttps": "1", "includeRelay": "1", "includeIPv6": "1"},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Plex resources failed HTTP {resp.status_code}")
    data = resp.json() if resp.content else []
    if not isinstance(data, list):
        data = []
    want = str(machine_id or "").strip()
    for raw in data:
        if not isinstance(raw, dict):
            continue
        provides = str(raw.get("provides") or "").lower()
        if "server" not in provides:
            continue
        mid = str(raw.get("clientIdentifier") or "").strip()
        if mid != want:
            continue
        connections = raw.get("connections") if isinstance(raw.get("connections"), list) else []
        access = str(raw.get("accessToken") or token).strip()
        uri = pick_best_connection_uri(connections, token=access, client_id=cid)
        if not uri:
            raise RuntimeError(f"Plex server {raw.get('name')!r} has no reachable connection URI")
        return {
            "machine_id": mid,
            "name": str(raw.get("name") or mid or "Plex Media Server").strip(),
            "owned": bool(raw.get("owned")),
            "product": raw.get("product"),
            "platform": raw.get("platform"),
            "presence": bool(raw.get("presence")),
            "uri": uri,
            "access_token": access,
            "connection_count": len(connections or []),
            "connection_uris": ranked_connection_uris(connections),
        }
    raise RuntimeError("Selected Plex server was not found on this account")


def connection_status_from_settings() -> dict[str, Any]:
    """Non-secret snapshot for Media Integrations connected state."""
    url = str(getattr(settings, "PLEX_URL", None) or "").strip()
    token = str(getattr(settings, "PLEX_TOKEN", None) or "").strip()
    method = str(getattr(settings, "PLEX_AUTH_METHOD", None) or "").strip().lower()
    username = str(getattr(settings, "PLEX_ACCOUNT_USERNAME", None) or "").strip() or None
    server_name = str(getattr(settings, "PLEX_SERVER_NAME", None) or "").strip() or None
    machine_id = str(getattr(settings, "PLEX_SERVER_MACHINE_ID", None) or "").strip() or None
    connected = bool(url and (token or method == "oauth"))
    if not method and connected:
        method = "manual"
    return {
        "connected": connected,
        "auth_method": method or None,
        "username": username,
        "server_name": server_name,
        "machine_id": machine_id,
        "url": url or None,
        "enabled": bool(getattr(settings, "ENABLE_PLEX", False)),
    }
