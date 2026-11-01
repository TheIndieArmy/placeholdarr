"""Fetch Plex Universal Watchlists (owner + Plex Home users) via plex.tv APIs.

Uses the configured Plex account token (``PLEX_TOKEN`` or ``PLEX_WATCHLIST_TOKEN``).
Server library URL is not required for watchlist reads.

Home users are listed via ``/api/v2/home/users``. Managed users are switched with
``/api/home/users/{id}/switch`` (v1; v2 /switch returns 404). PIN-protected users
are skipped until PIN support.

RSS (Plex Pass) is used only as an optional change hint; the metadata API is the
source of truth for the full list and guids.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import requests

from core.config import settings
from core.logger import logger

_PLEX_PRODUCT = "Placeholdarr"
_PLEX_CLIENT_ID_DEFAULT = "placeholdarr-watchlist"
_WATCHLIST_PAGE_SIZE = 100
_HOME_USERS_URL = "https://plex.tv/api/v2/home/users"
# Switch still lives on the legacy home API (v2 /switch returns 404).
_HOME_USERS_SWITCH_BASE = "https://plex.tv/api/home/users"
_HOME_SWITCH_GAP_SEC = 0.75
_HOME_SWITCH_429_RETRIES = 4


def plex_watchlist_token() -> str | None:
    """Prefer dedicated watchlist/account token; fall back to PLEX_TOKEN."""
    for key in ("PLEX_WATCHLIST_TOKEN", "PLEX_TOKEN"):
        raw = getattr(settings, key, None)
        tok = str(raw or "").strip()
        if tok:
            return tok
    return None


def _client_identifier() -> str:
    raw = str(getattr(settings, "PLEX_OAUTH_CLIENT_ID", None) or "").strip()
    return raw or _PLEX_CLIENT_ID_DEFAULT


def _plex_headers(token: str, *, accept_json: bool = True) -> dict[str, str]:
    headers = {
        "X-Plex-Token": token,
        "X-Plex-Product": _PLEX_PRODUCT,
        "X-Plex-Client-Identifier": _client_identifier(),
        "X-Plex-Version": "1.0",
        "X-Plex-Platform": "Linux",
        "X-Plex-Device": "Placeholdarr",
        "X-Plex-Device-Name": "Placeholdarr",
    }
    if accept_json:
        headers["Accept"] = "application/json"
    return headers


def _parse_guid_ids(guids: list[Any] | None, *, top_guid: str | None = None) -> dict[str, Any]:
    """Extract tmdb/tvdb/imdb ids from Plex Guid entries."""
    out: dict[str, Any] = {"tmdb_id": None, "tvdb_id": None, "imdb_id": None, "plex_guid": None}
    blobs: list[str] = []
    if top_guid:
        blobs.append(str(top_guid))
    for g in guids or []:
        if isinstance(g, dict):
            blobs.append(str(g.get("id") or g.get("tag") or ""))
        else:
            blobs.append(str(g or ""))
    for blob in blobs:
        s = blob.strip()
        if not s:
            continue
        if out["plex_guid"] is None and s.startswith("plex://"):
            out["plex_guid"] = s
        low = s.lower()
        m = re.search(r"(?:tmdb|themoviedb)://(?:movie|show|tv)?/?(\d+)", low)
        if m and out["tmdb_id"] is None:
            out["tmdb_id"] = int(m.group(1))
        m = re.search(r"tvdb://(\d+)", low)
        if m and out["tvdb_id"] is None:
            out["tvdb_id"] = int(m.group(1))
        m = re.search(r"imdb://(tt\d+)", low)
        if m and out["imdb_id"] is None:
            out["imdb_id"] = m.group(1)
    return out


def _media_type_from_item(item: dict[str, Any]) -> str | None:
    t = str(item.get("type") or item.get("librarySectionType") or "").strip().lower()
    if t in {"movie", "1"}:
        return "movie"
    if t in {"show", "series", "tv", "2"}:
        return "show"
    # Discover watchlist sometimes uses subtype
    st = str(item.get("subtype") or "").strip().lower()
    if st in {"movie"}:
        return "movie"
    if st in {"show", "series"}:
        return "show"
    return None


def _normalize_watchlist_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    media_type = _media_type_from_item(raw)
    if media_type not in {"movie", "show"}:
        return None
    title = str(raw.get("title") or raw.get("titleSort") or "").strip()
    if not title:
        return None
    year = raw.get("year")
    try:
        year_i = int(year) if year is not None else None
    except (TypeError, ValueError):
        year_i = None
    rating_key = str(raw.get("ratingKey") or raw.get("key") or "").strip() or None
    guids = raw.get("Guid") or raw.get("guid") or []
    if isinstance(guids, str):
        guids = [guids]
    ids = _parse_guid_ids(guids if isinstance(guids, list) else [], top_guid=str(raw.get("guid") or "") or None)
    plex_guid = ids.get("plex_guid") or (str(raw.get("guid") or "").strip() or None)
    return {
        "title": title,
        "year": year_i,
        "media_type": media_type,
        "rating_key": rating_key,
        "plex_guid": plex_guid,
        "tmdb_id": ids.get("tmdb_id"),
        "tvdb_id": ids.get("tvdb_id"),
        "imdb_id": ids.get("imdb_id"),
        "thumb": raw.get("thumb") or raw.get("composite"),
    }


def ensure_watchlist_rss(token: str, *, feed_type: str = "watchlist") -> str | None:
    """Create/return a watchlist RSS URL (Plex Pass). Returns None if unavailable."""
    try:
        resp = requests.post(
            "https://discover.provider.plex.tv/rss",
            params={"X-Plex-Token": token},
            json={"feedType": feed_type},
            headers=_plex_headers(token),
            timeout=20,
        )
        if resp.status_code >= 400:
            logger.debug(
                f"Plex watchlist RSS unavailable status={resp.status_code}",
                extra={"emoji_type": "debug"},
            )
            return None
        data = resp.json() if resp.content else {}
        info = data.get("RSSInfo") if isinstance(data, dict) else None
        if isinstance(info, list) and info:
            url = info[0].get("url") if isinstance(info[0], dict) else None
            return str(url) if url else None
    except Exception as exc:
        logger.debug(f"Plex watchlist RSS error: {exc}", extra={"emoji_type": "debug"})
    return None


def fetch_owner_watchlist(*, token: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """Fetch a Universal Watchlist for the given account token (normalized)."""
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        raise RuntimeError("No Plex token configured for watchlist (set PLEX_TOKEN or PLEX_WATCHLIST_TOKEN)")

    items: list[dict[str, Any]] = []
    start = 0
    cap = max(1, min(int(limit or 500), 2000))
    while start < cap:
        size = min(_WATCHLIST_PAGE_SIZE, cap - start)
        # includeGuids=1 is required for TMDB/TVDB/IMDB Guid entries (plain list omits them).
        url = (
            "https://discover.provider.plex.tv/library/sections/watchlist/all"
            f"?X-Plex-Container-Start={start}&X-Plex-Container-Size={size}&includeGuids=1"
        )
        resp = requests.get(url, headers=_plex_headers(tok), timeout=45)
        if resp.status_code == 401:
            raise RuntimeError("Plex rejected the watchlist token (unauthorized)")
        if resp.status_code >= 400:
            # Fallback metadata host
            alt = (
                "https://metadata.provider.plex.tv/library/sections/watchlist/all"
                f"?X-Plex-Container-Start={start}&X-Plex-Container-Size={size}&includeGuids=1"
            )
            resp = requests.get(alt, headers=_plex_headers(tok), timeout=45)
            if resp.status_code >= 400:
                raise RuntimeError(f"Plex watchlist fetch failed HTTP {resp.status_code}")
        data = resp.json() if resp.content else {}
        media = []
        if isinstance(data, dict):
            container = data.get("MediaContainer") if isinstance(data.get("MediaContainer"), dict) else data
            media = container.get("Metadata") or container.get("Directory") or []
        if not isinstance(media, list) or not media:
            break
        for raw in media:
            if not isinstance(raw, dict):
                continue
            norm = _normalize_watchlist_item(raw)
            if norm:
                items.append(norm)
        if len(media) < size:
            break
        start += size
    return items


def _parse_home_user_node(raw: dict[str, Any] | ET.Element) -> dict[str, Any] | None:
    if isinstance(raw, ET.Element):
        get = raw.attrib.get
    elif isinstance(raw, dict):
        get = lambda k, d=None: raw.get(k, d)  # noqa: E731
    else:
        return None
    plex_user_id = str(get("id") or "").strip()
    if not plex_user_id:
        return None
    title = (
        str(get("friendlyName") or "").strip()
        or str(get("title") or "").strip()
        or str(get("username") or "").strip()
        or plex_user_id
    )
    return {
        "plex_user_id": plex_user_id,
        "plex_uuid": str(get("uuid") or "").strip() or None,
        "username": str(get("username") or "").strip() or None,
        "title": title,
        "thumb_url": str(get("thumb") or "").strip() or None,
        "is_admin": str(get("admin") or "0") in {"1", "true", "True"},
        "is_restricted": str(get("restricted") or "0") in {"1", "true", "True"},
        "is_protected": str(get("protected") or "0") in {"1", "true", "True"},
        "is_guest": str(get("guest") or "0") in {"1", "true", "True"},
    }


def list_home_users(*, token: str | None = None) -> list[dict[str, Any]]:
    """List Plex Home users for the signed-in account (admin token)."""
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        raise RuntimeError("No Plex token configured for watchlist")
    resp = requests.get(
        _HOME_USERS_URL,
        headers=_plex_headers(tok),
        params={"X-Plex-Client-Identifier": _client_identifier()},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Plex home users failed HTTP {resp.status_code}")

    users: list[dict[str, Any]] = []
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "json" in ctype and resp.content:
        data = resp.json()
        raw_users: list[Any] = []
        if isinstance(data, dict):
            if isinstance(data.get("users"), list):
                raw_users = data["users"]
            elif isinstance(data.get("Users"), list):
                raw_users = data["Users"]
            else:
                # Some payloads nest under home / MediaContainer.
                home = data.get("home") if isinstance(data.get("home"), dict) else data
                nested = home.get("users") if isinstance(home, dict) else None
                if isinstance(nested, dict) and isinstance(nested.get("user"), list):
                    raw_users = nested["user"]
                elif isinstance(nested, list):
                    raw_users = nested
                elif isinstance(home, dict) and isinstance(home.get("user"), list):
                    raw_users = home["user"]
        for raw in raw_users:
            parsed = _parse_home_user_node(raw if isinstance(raw, dict) else {})
            if parsed:
                users.append(parsed)
    else:
        # XML fallback
        root = ET.fromstring(resp.content or b"<home/>")
        for node in root.iter():
            tag = str(node.tag or "").lower()
            if tag.endswith("user") and node.attrib.get("id"):
                parsed = _parse_home_user_node(node)
                if parsed:
                    users.append(parsed)

    # Deduplicate by plex_user_id; admin / title sort.
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for u in users:
        key = u["plex_user_id"]
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    out.sort(key=lambda u: (0 if u.get("is_admin") else 1, str(u.get("title") or "").lower()))
    return out


def switch_home_user(
    plex_user_id: str,
    *,
    token: str | None = None,
    pin: str | None = None,
) -> str:
    """Return an auth token for the given Home user (admin token required)."""
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        raise RuntimeError("No Plex token configured for watchlist")
    uid = str(plex_user_id or "").strip()
    if not uid:
        raise RuntimeError("plex_user_id required")
    url = f"{_HOME_USERS_SWITCH_BASE}/{quote(uid, safe='')}/switch"
    params: dict[str, str] = {"X-Plex-Client-Identifier": _client_identifier()}
    if pin:
        params["pin"] = str(pin)
    # Prefer XML from legacy switch; drop Accept: application/json for this call.
    headers = {k: v for k, v in _plex_headers(tok, accept_json=False).items()}
    headers["Accept"] = "application/xml"

    last_status = 0
    last_body = ""
    for attempt in range(_HOME_SWITCH_429_RETRIES + 1):
        resp = requests.post(
            url,
            headers=headers,
            params=params,
            timeout=30,
        )
        last_status = resp.status_code
        last_body = (resp.text or "")[:180]
        if resp.status_code == 429:
            # plex.tv rate-limits rapid home switches; back off and retry.
            delay = min(8.0, 1.5 * (2 ** attempt))
            time.sleep(delay)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"Plex home user switch failed HTTP {resp.status_code}: {last_body}")
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "json" in ctype and resp.content:
            data = resp.json()
            if isinstance(data, dict):
                auth = data.get("authToken") or data.get("authenticationToken") or data.get("auth_token")
                if auth:
                    return str(auth)
        try:
            root = ET.fromstring(resp.content or b"<root/>")
            auth = root.attrib.get("authToken") or root.attrib.get("authenticationToken")
            if not auth:
                for node in root.iter():
                    auth = node.attrib.get("authToken") or node.attrib.get("authenticationToken")
                    if auth:
                        break
            if auth:
                return str(auth)
        except ET.ParseError:
            pass
        raise RuntimeError(f"Plex home user switch returned no auth token for {uid}")

    raise RuntimeError(f"Plex home user switch failed HTTP {last_status}: {last_body}")


def fetch_watchlists_for_home(
    *,
    token: str | None = None,
    limit_per_user: int = 1000,
) -> dict[str, Any]:
    """Fetch Home users and each reachable user's watchlist.

    Returns ``{users: [...], by_user: {plex_user_id: [items]}, errors: {id: msg}}``.
    Admin / owner watchlist uses the account token; others use switch tokens.
    PIN-protected users are listed but not switched (error recorded).
    """
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        raise RuntimeError("No Plex token configured for watchlist")

    try:
        users = list_home_users(token=tok)
    except Exception as exc:
        logger.warning(f"Plex home users list failed ({exc}); falling back to owner watchlist only", extra={"emoji_type": "warning"})
        users = []

    by_user: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}

    if not users:
        # Single-account / no Home: treat owner token as one synthetic user from /user.
        owner_id = "owner"
        try:
            from services.media_servers.plex_oauth import fetch_account_username

            uname = fetch_account_username(tok) or "Owner"
            users = [
                {
                    "plex_user_id": owner_id,
                    "plex_uuid": None,
                    "username": uname,
                    "title": uname,
                    "thumb_url": None,
                    "is_admin": True,
                    "is_restricted": False,
                    "is_protected": False,
                    "is_guest": False,
                }
            ]
            by_user[owner_id] = fetch_owner_watchlist(token=tok, limit=limit_per_user)
        except Exception as exc:
            errors[owner_id] = str(exc)
        return {"users": users, "by_user": by_user, "errors": errors}

    switched = 0
    for user in users:
        uid = str(user.get("plex_user_id") or "")
        if not uid:
            continue
        if user.get("is_protected"):
            errors[uid] = "Protected Home user (PIN required to sync watchlist)"
            by_user[uid] = []
            continue
        try:
            if user.get("is_admin"):
                user_token = tok
            else:
                if switched:
                    time.sleep(_HOME_SWITCH_GAP_SEC)
                user_token = switch_home_user(uid, token=tok)
                switched += 1
            by_user[uid] = fetch_owner_watchlist(token=user_token, limit=limit_per_user)
        except Exception as exc:
            errors[uid] = str(exc)
            by_user[uid] = []
            logger.warning(
                f"Watchlist fetch skipped for Home user {user.get('title')!r}: {exc}",
                extra={"emoji_type": "warning"},
            )

    return {"users": users, "by_user": by_user, "errors": errors}


def test_watchlist_access(token: str | None = None) -> dict[str, Any]:
    """Lightweight probe for Settings / OAuth completion."""
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        return {"ok": False, "error": "No Plex token configured"}
    try:
        items = fetch_owner_watchlist(token=tok, limit=5)
        rss = ensure_watchlist_rss(tok)
        home_count = 0
        try:
            home_count = len(list_home_users(token=tok))
        except Exception:
            home_count = 0
        return {
            "ok": True,
            "sample_count": len(items),
            "rss_available": bool(rss),
            "home_user_count": home_count,
            "titles": [i["title"] for i in items[:5]],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
