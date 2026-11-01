"""Fetch Plex Universal Watchlists (owner + friends) via plex.tv APIs.

Uses the configured Plex account token (``PLEX_TOKEN`` or ``PLEX_WATCHLIST_TOKEN``).
Server library URL is not required for watchlist reads.

Primary sync:
  - Owner watchlist via discover/metadata API
  - Friends via community.plex.tv GraphQL (``allFriendsV2`` / ``userV2.watchlist``)
  - RSS (Plex Pass) as a change hint only (``watchlist`` + ``friendsWatchlist``)

Optional: Home user-switch for managed profiles not covered as friends
(``WATCHLIST_SYNC_HOME_USERS``). Home listing alone is used for avatar overlays.
"""

from __future__ import annotations

import hashlib
import json
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
_COMMUNITY_GRAPHQL_URL = "https://community.plex.tv/api"
_PLEX_USER_URL = "https://plex.tv/api/v2/user"


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
                f"Plex watchlist RSS unavailable status={resp.status_code} feedType={feed_type}",
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


def ensure_watchlist_rss_feeds(token: str | None = None) -> dict[str, str | None]:
    """Ensure owner + friendsWatchlist RSS URLs (Plex Pass)."""
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        return {"watchlist": None, "friendsWatchlist": None}
    return {
        "watchlist": ensure_watchlist_rss(tok, feed_type="watchlist"),
        "friendsWatchlist": ensure_watchlist_rss(tok, feed_type="friendsWatchlist"),
    }


def poll_watchlist_rss_feed(
    url: str,
    *,
    etag: str | None = None,
    content_hash: str | None = None,
) -> dict[str, Any]:
    """Poll an RSS URL for changes. Returns changed/etag/hash/status (hint only)."""
    out: dict[str, Any] = {
        "changed": True,
        "etag": etag,
        "content_hash": content_hash,
        "status": 0,
        "available": False,
    }
    feed = str(url or "").strip()
    if not feed:
        return out
    headers: dict[str, str] = {"Accept": "application/json, application/rss+xml, */*"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        resp = requests.get(
            feed,
            headers=headers,
            params={"format": "json", "cache_buster": str(int(time.time()))},
            timeout=25,
        )
    except Exception as exc:
        out["error"] = str(exc)
        return out
    out["status"] = int(resp.status_code)
    out["available"] = True
    new_etag = str(resp.headers.get("ETag") or resp.headers.get("etag") or "").strip() or None
    if resp.status_code == 304:
        out["changed"] = False
        out["etag"] = new_etag or etag
        return out
    if resp.status_code >= 400:
        out["error"] = f"HTTP {resp.status_code}"
        return out
    body = resp.content or b""
    digest = hashlib.sha256(body).hexdigest()
    out["etag"] = new_etag
    out["content_hash"] = digest
    if content_hash and digest == content_hash:
        out["changed"] = False
    else:
        out["changed"] = True
    return out


def _community_graphql(token: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    resp = requests.post(
        _COMMUNITY_GRAPHQL_URL,
        headers={
            **_plex_headers(token),
            "Content-Type": "application/json",
        },
        data=json.dumps({"query": query, "variables": variables or {}}),
        timeout=45,
    )
    if resp.status_code == 429:
        raise RuntimeError("Plex community GraphQL rate limited (HTTP 429)")
    if resp.status_code >= 400:
        raise RuntimeError(f"Plex community GraphQL HTTP {resp.status_code}: {(resp.text or '')[:180]}")
    data = resp.json() if resp.content else {}
    if not isinstance(data, dict):
        raise RuntimeError("Plex community GraphQL returned non-object JSON")
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        msg = str((errors[0] or {}).get("message") if isinstance(errors[0], dict) else errors[0])
        raise RuntimeError(f"Plex community GraphQL error: {msg}")
    payload = data.get("data")
    return payload if isinstance(payload, dict) else {}


def fetch_account_user(*, token: str | None = None) -> dict[str, Any]:
    """Resolve the signed-in Plex account as a watchlist user row (owner)."""
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        raise RuntimeError("No Plex token configured for watchlist")
    resp = requests.get(
        _PLEX_USER_URL,
        headers=_plex_headers(tok),
        params={"X-Plex-Client-Identifier": _client_identifier()},
        timeout=30,
    )
    if resp.status_code >= 400:
        # Fall back to username-only identity.
        from services.media_servers.plex_oauth import fetch_account_username

        uname = fetch_account_username(tok) or "Owner"
        return {
            "plex_user_id": "owner",
            "plex_uuid": None,
            "username": uname,
            "title": uname,
            "thumb_url": None,
            "is_admin": True,
            "is_restricted": False,
            "is_protected": False,
            "is_guest": False,
            "kind": "owner",
        }
    data = resp.json() if resp.content else {}
    if not isinstance(data, dict):
        data = {}
    uuid = str(data.get("uuid") or data.get("id") or "").strip() or "owner"
    uname = str(data.get("username") or data.get("title") or "Owner").strip() or "Owner"
    title = str(data.get("friendlyName") or data.get("title") or uname).strip() or uname
    return {
        "plex_user_id": uuid,
        "plex_uuid": uuid if uuid != "owner" else None,
        "username": uname,
        "title": title,
        "thumb_url": str(data.get("thumb") or "").strip() or None,
        "is_admin": True,
        "is_restricted": False,
        "is_protected": False,
        "is_guest": False,
        "kind": "owner",
    }


def list_plex_friends(*, token: str | None = None) -> list[dict[str, Any]]:
    """List Plex friends via community GraphQL ``allFriendsV2``."""
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        raise RuntimeError("No Plex token configured for watchlist")
    data = _community_graphql(
        tok,
        """
        query GetAllFriends {
          allFriendsV2 {
            user { id username }
          }
        }
        """,
    )
    rows = data.get("allFriendsV2") if isinstance(data.get("allFriendsV2"), list) else []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        uid = str(user.get("id") or "").strip()
        if not uid:
            continue
        uid_l = uid.lower()
        uname = str(user.get("username") or "").strip() or uid
        out.append(
            {
                "plex_user_id": uid_l,
                "plex_uuid": uid_l,
                "username": uname,
                "title": uname,
                "thumb_url": None,
                "is_admin": False,
                "is_restricted": False,
                "is_protected": False,
                "is_guest": False,
                "kind": "friend",
            }
        )
    return out


def _plex_machine_identifier(*, token: str) -> str | None:
    """Best-effort PMS machineIdentifier for shared_servers lookups."""
    plex_url = str(getattr(settings, "PLEX_URL", None) or "").strip().rstrip("/")
    if not plex_url:
        return None
    try:
        resp = requests.get(
            f"{plex_url}/identity",
            headers=_plex_headers(token, accept_json=True),
            timeout=20,
        )
        if resp.status_code >= 400:
            return None
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "json" in ctype and resp.content:
            data = resp.json() if resp.content else {}
            if isinstance(data, dict):
                mc = data.get("MediaContainer") if isinstance(data.get("MediaContainer"), dict) else data
                mid = str((mc or {}).get("machineIdentifier") or "").strip()
                if mid:
                    return mid
        root = ET.fromstring(resp.content or b"<root/>")
        mid = root.attrib.get("machineIdentifier")
        if not mid:
            for node in root.iter():
                mid = node.attrib.get("machineIdentifier")
                if mid:
                    break
        return str(mid).strip() or None
    except Exception:
        return None


def _plex_uuid_from_thumb(thumb: str | None) -> str | None:
    """Extract account uuid from plex.tv avatar URLs (``/users/<uuid>/avatar``)."""
    s = str(thumb or "").strip()
    if not s:
        return None
    m = re.search(r"/users/([0-9a-fA-F]{16,40})(?:/|$|\?)", s)
    return m.group(1).lower() if m else None


def _parse_server_share_user(raw: dict[str, Any] | ET.Element) -> dict[str, Any] | None:
    """Parse a plex.tv share user.

    ``plex.tv/api/users`` exposes numeric ``id`` and often omits ``uuid``; the hex
    account uuid is recoverable from the avatar thumb URL.
    """
    if isinstance(raw, ET.Element):
        get = raw.attrib.get
    elif isinstance(raw, dict):
        get = lambda k, d=None: raw.get(k, d)  # noqa: E731
    else:
        return None
    numeric_id = str(get("id") or "").strip() or None
    thumb = str(get("thumb") or "").strip() or None
    uuid = str(get("uuid") or "").strip() or _plex_uuid_from_thumb(thumb)
    if uuid:
        uuid = uuid.lower()
    if not numeric_id and not uuid:
        return None
    uname = str(get("username") or get("title") or get("email") or "").strip() or None
    title = (
        str(get("title") or "").strip()
        or str(get("friendlyName") or "").strip()
        or uname
        or uuid
        or numeric_id
    )
    # Prefer hex uuid as the stable identity when present (matches GraphQL / Home).
    plex_user_id = uuid or numeric_id or ""
    return {
        "plex_user_id": plex_user_id,
        "plex_uuid": uuid,
        "plex_numeric_id": numeric_id,
        "username": uname,
        "title": title,
        "thumb_url": thumb,
        "is_admin": str(get("admin") or "0") in {"1", "true", "True"},
        "is_restricted": str(get("restricted") or "0") in {"1", "true", "True"},
        "is_protected": str(get("protected") or "0") in {"1", "true", "True"},
        "is_guest": str(get("guest") or "0") in {"1", "true", "True"},
        "kind": "unknown",
    }


def list_plex_server_users(*, token: str | None = None) -> list[dict[str, Any]]:
    """Users with access to this Plex server (friends ∩ shares directory).

    Prefers ``plex.tv/api/users`` (account shares). Falls back to
    ``shared_servers`` when a PMS machine id is available, then local ``/accounts``.
    """
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        raise RuntimeError("No Plex token configured for watchlist")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(parsed: dict[str, Any] | None) -> None:
        if not parsed:
            return
        key = str(parsed.get("plex_uuid") or parsed.get("plex_user_id") or "").strip()
        if not key or key in seen:
            return
        seen.add(key)
        out.append(parsed)

    # 1) Account-level users who share / have been shared with (XML or JSON).
    try:
        resp = requests.get(
            "https://plex.tv/api/users",
            headers=_plex_headers(tok, accept_json=False),
            timeout=30,
        )
        if resp.status_code < 400 and resp.content:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                data = resp.json()
                rows: list[Any] = []
                if isinstance(data, dict):
                    if isinstance(data.get("users"), list):
                        rows = data["users"]
                    elif isinstance(data.get("MediaContainer"), dict):
                        mc = data["MediaContainer"]
                        rows = mc.get("User") or mc.get("user") or []
                        if isinstance(rows, dict):
                            rows = [rows]
                for raw in rows:
                    if isinstance(raw, dict):
                        _add(_parse_server_share_user(raw))
            else:
                root = ET.fromstring(resp.content or b"<users/>")
                for node in root.iter():
                    tag = str(node.tag or "").lower()
                    if tag.endswith("user") and (node.attrib.get("id") or node.attrib.get("uuid")):
                        # Include users with a Server child when present; otherwise keep the
                        # row so friend ∩ server classification can still use uuid matches.
                        _add(_parse_server_share_user(node))
    except Exception as exc:
        logger.debug(f"plex.tv/api/users failed: {exc}", extra={"emoji_type": "debug"})

    # 2) shared_servers for this machine (invited shares).
    if not out:
        mid = _plex_machine_identifier(token=tok)
        if mid:
            try:
                resp = requests.get(
                    f"https://plex.tv/api/servers/{quote(mid, safe='')}/shared_servers",
                    headers=_plex_headers(tok, accept_json=False),
                    timeout=30,
                )
                if resp.status_code < 400 and resp.content:
                    root = ET.fromstring(resp.content or b"<root/>")
                    for node in root.iter():
                        tag = str(node.tag or "").lower()
                        if tag.endswith("user") or tag.endswith("sharedserver"):
                            # SharedServer often has username / userID attrs.
                            if tag.endswith("sharedserver"):
                                uid = str(
                                    node.attrib.get("userID")
                                    or node.attrib.get("id")
                                    or ""
                                ).strip()
                                uname = str(node.attrib.get("username") or "").strip() or None
                                if uid:
                                    _add(
                                        {
                                            "plex_user_id": uid,
                                            "plex_uuid": uid,
                                            "username": uname,
                                            "title": uname or uid,
                                            "thumb_url": None,
                                            "is_admin": False,
                                            "is_restricted": False,
                                            "is_protected": False,
                                            "is_guest": False,
                                            "kind": "unknown",
                                        }
                                    )
                            else:
                                _add(_parse_server_share_user(node))
            except Exception as exc:
                logger.debug(f"shared_servers list failed: {exc}", extra={"emoji_type": "debug"})

    # 3) Local PMS accounts (numeric ids; still useful when uuids overlap via title).
    plex_url = str(getattr(settings, "PLEX_URL", None) or "").strip().rstrip("/")
    if plex_url and not out:
        try:
            resp = requests.get(
                f"{plex_url}/accounts",
                headers=_plex_headers(tok),
                timeout=20,
            )
            if resp.status_code < 400 and resp.content:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "json" in ctype:
                    data = resp.json()
                    mc = data.get("MediaContainer") if isinstance(data, dict) else {}
                    rows = (mc or {}).get("Account") or (mc or {}).get("account") or []
                    if isinstance(rows, dict):
                        rows = [rows]
                    for raw in rows if isinstance(rows, list) else []:
                        if isinstance(raw, dict):
                            _add(_parse_server_share_user(raw))
                else:
                    root = ET.fromstring(resp.content or b"<root/>")
                    for node in root.iter():
                        tag = str(node.tag or "").lower()
                        if tag.endswith("account") and node.attrib.get("id"):
                            _add(_parse_server_share_user(node))
        except Exception as exc:
            logger.debug(f"PMS /accounts failed: {exc}", extra={"emoji_type": "debug"})

    out.sort(key=lambda u: str(u.get("title") or "").lower())
    return out


def _normalize_graphql_watchlist_node(node: dict[str, Any]) -> dict[str, Any] | None:
    """Map a community GraphQL watchlist node into our normalized item dict."""
    typ = str(node.get("type") or "").strip().lower()
    if typ in {"movie", "1"}:
        media_type = "movie"
    elif typ in {"show", "series", "tv", "2"}:
        media_type = "show"
    else:
        return None
    title = str(node.get("title") or "").strip()
    if not title:
        return None
    year = node.get("year")
    try:
        year_i = int(year) if year is not None else None
    except (TypeError, ValueError):
        year_i = None
    guid = str(node.get("guid") or node.get("id") or "").strip() or None
    key = str(node.get("key") or "").strip() or None
    rating_key = None
    if key:
        m = re.search(r"/(\d+)(?:\?|$)", key)
        if m:
            rating_key = m.group(1)
    ids = _parse_guid_ids([guid] if guid else [], top_guid=guid)
    thumb = None
    images = node.get("images")
    if isinstance(images, dict):
        thumb = images.get("thumbnail") or images.get("coverPoster")
    elif isinstance(images, list):
        for img in images:
            if isinstance(img, dict) and (img.get("thumbnail") or img.get("url")):
                thumb = img.get("thumbnail") or img.get("url")
                break
    return {
        "title": title,
        "year": year_i,
        "media_type": media_type,
        "rating_key": rating_key,
        "plex_guid": ids.get("plex_guid") or guid,
        "tmdb_id": ids.get("tmdb_id"),
        "tvdb_id": ids.get("tvdb_id"),
        "imdb_id": ids.get("imdb_id"),
        "thumb": thumb,
        "discover_key": key,
    }


def _enrich_item_guids(token: str, item: dict[str, Any]) -> dict[str, Any]:
    """Fill TMDB/TVDB/IMDB from discover metadata when GraphQL omitted them."""
    if item.get("tmdb_id") or item.get("tvdb_id") or item.get("imdb_id"):
        return item
    key = str(item.get("discover_key") or "").strip()
    if not key:
        return item
    path = key if key.startswith("/") else f"/{key}"
    try:
        resp = requests.get(
            f"https://discover.provider.plex.tv{path}",
            headers=_plex_headers(token),
            params={"includeGuids": "1"},
            timeout=30,
        )
        if resp.status_code >= 400:
            return item
        data = resp.json() if resp.content else {}
        meta = None
        if isinstance(data, dict):
            container = data.get("MediaContainer") if isinstance(data.get("MediaContainer"), dict) else data
            rows = container.get("Metadata") or []
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                meta = rows[0]
            elif isinstance(container, dict) and container.get("guid"):
                meta = container
        if not isinstance(meta, dict):
            return item
        ids = _parse_guid_ids(
            meta.get("Guid") if isinstance(meta.get("Guid"), list) else [],
            top_guid=str(meta.get("guid") or "") or None,
        )
        if ids.get("plex_guid"):
            item["plex_guid"] = ids["plex_guid"]
        if ids.get("tmdb_id"):
            item["tmdb_id"] = ids["tmdb_id"]
        if ids.get("tvdb_id"):
            item["tvdb_id"] = ids["tvdb_id"]
        if ids.get("imdb_id"):
            item["imdb_id"] = ids["imdb_id"]
        if not item.get("thumb") and meta.get("thumb"):
            item["thumb"] = meta.get("thumb")
    except Exception:
        return item
    return item


def fetch_friend_watchlist(
    friend_uuid: str,
    *,
    token: str | None = None,
    limit: int = 1000,
    enrich_guids: bool = True,
) -> list[dict[str, Any]]:
    """Fetch one friend's Universal Watchlist via ``userV2.watchlist``."""
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        raise RuntimeError("No Plex token configured for watchlist")
    uid = str(friend_uuid or "").strip()
    if not uid:
        return []
    items: list[dict[str, Any]] = []
    after: str | None = None
    cap = max(1, min(int(limit or 1000), 2000))
    # userV2 returns UserBase; watchlist lives on the User concrete type.
    query = """
    query GetFriendWatchlist($user: UserInput!, $first: PaginationInt!, $after: String) {
      userV2(user: $user) {
        ... on User {
          watchlist(first: $first, after: $after) {
            nodes {
              id
              title
              type
              year
              guid
              key
              images { thumbnail coverPoster }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
    """
    while len(items) < cap:
        page_size = min(100, cap - len(items))
        data = _community_graphql(
            tok,
            query,
            {"user": {"id": uid}, "first": page_size, "after": after},
        )
        user = data.get("userV2") if isinstance(data.get("userV2"), dict) else {}
        wl = user.get("watchlist") if isinstance(user.get("watchlist"), dict) else {}
        nodes = wl.get("nodes") if isinstance(wl.get("nodes"), list) else []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            norm = _normalize_graphql_watchlist_node(node)
            if not norm:
                continue
            if enrich_guids:
                norm = _enrich_item_guids(tok, norm)
            items.append(norm)
        page_info = wl.get("pageInfo") if isinstance(wl.get("pageInfo"), dict) else {}
        if not page_info.get("hasNextPage"):
            break
        after = str(page_info.get("endCursor") or "").strip() or None
        if not after:
            break
        time.sleep(0.2)
    return items


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


def _overlay_home_avatars(users: list[dict[str, Any]], *, token: str) -> None:
    """Best-effort: copy Home thumb/title onto owner/friend rows with matching uuid."""
    try:
        home_users = list_home_users(token=token)
    except Exception:
        return
    by_uuid: dict[str, dict[str, Any]] = {}
    for hu in home_users:
        uuid = str(hu.get("plex_uuid") or "").strip()
        if uuid:
            by_uuid[uuid] = hu
    for user in users:
        uuid = str(user.get("plex_uuid") or user.get("plex_user_id") or "").strip()
        home = by_uuid.get(uuid)
        if not home:
            continue
        if not user.get("thumb_url") and home.get("thumb_url"):
            user["thumb_url"] = home.get("thumb_url")
        home_title = str(home.get("title") or "").strip()
        if home_title and (
            not user.get("title")
            or str(user.get("title")) == str(user.get("username") or "")
        ):
            user["title"] = home_title


def fetch_watchlists_for_account(
    *,
    token: str | None = None,
    limit_per_user: int = 1000,
    include_friends: bool | None = None,
    include_home_switch: bool | None = None,
    enabled_user_ids: set[str] | None = None,
    roster_users: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch watchlists per roster relationship (owner Discover, friends GraphQL, Home switch).

    ``enabled_user_ids`` gates title pulls (DB ``sync_enabled``). Roster rows still
    return in ``users`` for classification upsert. PIN-protected Home and
    ``server_only`` are never fetched in Phase 1.

    Returns ``{users, by_user, errors, source}``.
    """
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        raise RuntimeError("No Plex token configured for watchlist")

    if include_friends is None:
        include_friends = bool(getattr(settings, "WATCHLIST_SYNC_FRIENDS", True))
    if include_home_switch is None:
        include_home_switch = bool(getattr(settings, "WATCHLIST_SYNC_HOME_USERS", False))
    include_friend_only = bool(getattr(settings, "WATCHLIST_SYNC_FRIEND_ONLY", True))

    users: list[dict[str, Any]]
    if roster_users is not None:
        users = [dict(u) for u in roster_users if isinstance(u, dict)]
    else:
        try:
            from services.watchlist.roster import discover_roster

            users = discover_roster(token=tok)
        except Exception as exc:
            logger.warning(f"Roster discover failed, falling back to owner: {exc}", extra={"emoji_type": "warning"})
            users = [fetch_account_user(token=tok)]

    by_user: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    switched = 0

    for user in users:
        uid = str(user.get("plex_user_id") or "").strip()
        if not uid:
            continue
        relationship = str(user.get("relationship") or "").strip().lower()
        if not relationship:
            kind = str(user.get("kind") or "").strip().lower()
            relationship = kind if kind in {"owner", "friend", "home"} else "unknown"
            user["relationship"] = relationship
        fetch_method = str(user.get("fetch_method") or "").strip().lower()
        is_protected = bool(user.get("is_protected"))

        # Hard blocks (Phase 1).
        if relationship == "server_only":
            errors[uid] = "Needs Plex friend access or a user token"
            continue
        if relationship == "home" and is_protected:
            errors[uid] = "Needs Home PIN or a user token"
            continue
        if relationship == "home" and not include_home_switch:
            errors[uid] = "Home switch sync disabled"
            continue
        if relationship in {"friend", "friend_only"} and not include_friends:
            errors[uid] = "Friends sync disabled"
            continue
        if relationship == "friend_only" and not include_friend_only:
            errors[uid] = "Friends without server access sync disabled"
            continue

        if enabled_user_ids is not None and uid not in enabled_user_ids:
            # Still listed on roster; skip title pull.
            continue
        if user.get("sync_enabled") is False and enabled_user_ids is None:
            continue

        try:
            if relationship == "owner" or fetch_method == "discover":
                by_user[uid] = fetch_owner_watchlist(token=tok, limit=limit_per_user)
            elif relationship in {"friend", "friend_only"} or fetch_method == "graphql":
                friend_key = str(user.get("plex_uuid") or uid).strip()
                by_user[uid] = fetch_friend_watchlist(friend_key, token=tok, limit=limit_per_user)
                time.sleep(0.15)
            elif relationship == "home" or fetch_method == "home_switch":
                if switched:
                    time.sleep(_HOME_SWITCH_GAP_SEC)
                user_token = switch_home_user(uid, token=tok)
                switched += 1
                by_user[uid] = fetch_owner_watchlist(token=user_token, limit=limit_per_user)
            else:
                errors[uid] = "No sync method available for this relationship"
        except Exception as exc:
            errors[uid] = str(exc)
            by_user[uid] = []
            logger.warning(
                f"Watchlist fetch skipped for {user.get('title')!r} ({relationship}): {exc}",
                extra={"emoji_type": "warning"},
            )

    _overlay_home_avatars(users, token=tok)
    return {
        "users": users,
        "by_user": by_user,
        "errors": errors,
        "source": "roster",
    }


def fetch_watchlists_for_home(
    *,
    token: str | None = None,
    limit_per_user: int = 1000,
) -> dict[str, Any]:
    """Backward-compatible alias: account sync with optional Home switch from settings."""
    return fetch_watchlists_for_account(token=token, limit_per_user=limit_per_user)


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
