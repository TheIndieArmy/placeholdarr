"""Watchlist roster discovery and relationship classification."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from core.config import settings
from core.logger import logger
from services.media_servers.plex_watchlist import (
    fetch_account_user,
    list_home_users,
    list_plex_friends,
    list_plex_server_users,
    plex_watchlist_token,
)
from services.postgres.db import get_session
from services.postgres.models import PlexWatchlistMembership, PlexWatchlistUser

# Relationship labels from intersecting friends, server shares, and Plex Home.
REL_OWNER = "owner"
REL_FRIEND = "friend"
REL_FRIEND_ONLY = "friend_only"
REL_SERVER_ONLY = "server_only"
REL_HOME = "home"
REL_UNKNOWN = "unknown"

BLOCK_NEEDS_FRIEND_OR_TOKEN = "needs_friend_or_token"
BLOCK_NEEDS_PIN_OR_TOKEN = "needs_pin_or_token"
BLOCK_SYNC_DISABLED = "sync_disabled"
BLOCK_HOME_SWITCH_OFF = "home_switch_disabled"
BLOCK_FRIENDS_SYNC_OFF = "friends_sync_disabled"
BLOCK_FRIEND_ONLY_OFF = "friend_only_disabled"

# Blocks where the People toggle must not look "on" (policy or no fetch path).
POLICY_OR_HARD_BLOCKS = frozenset(
    {
        BLOCK_NEEDS_FRIEND_OR_TOKEN,
        BLOCK_NEEDS_PIN_OR_TOKEN,
        BLOCK_HOME_SWITCH_OFF,
        BLOCK_FRIENDS_SYNC_OFF,
        BLOCK_FRIEND_ONLY_OFF,
    }
)

BLOCK_REASON_MESSAGES = {
    BLOCK_NEEDS_FRIEND_OR_TOKEN: "Needs Plex friend access or a user token",
    BLOCK_NEEDS_PIN_OR_TOKEN: "Needs Home PIN or a user token",
    BLOCK_HOME_SWITCH_OFF: "Not syncing: Home switch is off in Settings → Watching",
    BLOCK_FRIENDS_SYNC_OFF: "Not syncing: friends sync is off in Settings → Watching",
    BLOCK_FRIEND_ONLY_OFF: "Not syncing: friends without server access is off in Settings → Watching",
    BLOCK_SYNC_DISABLED: "Sync disabled for this user",
}

_HEX_UUID_RE = re.compile(r"^[0-9a-fA-F]{16,40}$")
_NUMERIC_ID_RE = re.compile(r"^\d{5,}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uid_key(*parts: Any) -> str | None:
    for p in parts:
        s = str(p or "").strip()
        if s:
            return s
    return None


def _norm_username(*parts: Any) -> str | None:
    for p in parts:
        s = str(p or "").strip().lower()
        if s:
            return s
    return None


def _is_hex_uuid(value: str | None) -> bool:
    return bool(value and _HEX_UUID_RE.match(str(value).strip()))


def _is_numeric_id(value: str | None) -> bool:
    return bool(value and _NUMERIC_ID_RE.match(str(value).strip()))


def classify_relationship(
    *,
    in_friends: bool,
    in_server: bool,
    in_home: bool,
) -> str:
    """Classify one roster identity (owner handled separately).

    Axes:
      - friends list (GraphQL)
      - Plex Home quick-switch list
      - server library shares (may overlap Home, or be share-only)
    """
    if in_friends and (in_home or in_server):
        return REL_FRIEND
    if in_friends:
        return REL_FRIEND_ONLY
    if in_home:
        # Any Home member who is not a friend (adult or managed).
        return REL_HOME
    if in_server:
        # Library share only; not on Home, so no user-switch path.
        return REL_SERVER_ONLY
    return REL_UNKNOWN


def home_profile_detail(
    *,
    in_home: bool,
    is_restricted: bool,
    is_protected: bool,
) -> str | None:
    """Short Home profile tag for UI (null when not on the Home list)."""
    if not in_home:
        return None
    if is_protected:
        return "pin"
    if is_restricted:
        return "managed"
    return "unlocked"


def default_sync_enabled(
    relationship: str,
    *,
    is_protected: bool = False,
) -> bool:
    """Default sync flag for a newly seen roster member."""
    if relationship == REL_OWNER:
        return True
    if relationship in {REL_FRIEND, REL_FRIEND_ONLY}:
        if relationship == REL_FRIEND_ONLY and not bool(
            getattr(settings, "WATCHLIST_SYNC_FRIEND_ONLY", True)
        ):
            return False
        return bool(getattr(settings, "WATCHLIST_SYNC_FRIENDS", True))
    if relationship == REL_HOME:
        if is_protected:
            return False
        return bool(getattr(settings, "WATCHLIST_SYNC_HOME_USERS", False))
    if relationship == REL_SERVER_ONLY:
        return False
    return False


def block_reason_for(
    relationship: str,
    *,
    is_protected: bool = False,
    sync_enabled: bool = True,
) -> str | None:
    """Return why this roster member cannot sync right now (policy or hard block first)."""
    if relationship == REL_SERVER_ONLY:
        return BLOCK_NEEDS_FRIEND_OR_TOKEN
    if relationship == REL_HOME and is_protected:
        return BLOCK_NEEDS_PIN_OR_TOKEN
    if relationship == REL_HOME and not bool(getattr(settings, "WATCHLIST_SYNC_HOME_USERS", False)):
        return BLOCK_HOME_SWITCH_OFF
    if relationship in {REL_FRIEND, REL_FRIEND_ONLY} and not bool(
        getattr(settings, "WATCHLIST_SYNC_FRIENDS", True)
    ):
        return BLOCK_FRIENDS_SYNC_OFF
    if relationship == REL_FRIEND_ONLY and not bool(
        getattr(settings, "WATCHLIST_SYNC_FRIEND_ONLY", True)
    ):
        return BLOCK_FRIEND_ONLY_OFF
    if not sync_enabled:
        return BLOCK_SYNC_DISABLED
    return None


def effective_sync_enabled(
    *,
    sync_enabled: bool,
    relationship: str | None,
    is_protected: bool = False,
    block_reason: str | None = None,
) -> bool:
    """True only when the stored toggle is on and a fetch path is actually available."""
    if not sync_enabled:
        return False
    reason = block_reason or block_reason_for(
        str(relationship or REL_UNKNOWN),
        is_protected=is_protected,
        sync_enabled=True,
    )
    if reason in POLICY_OR_HARD_BLOCKS:
        return False
    return True


def fetch_method_for(relationship: str, *, is_protected: bool = False) -> str:
    if relationship == REL_OWNER:
        return "discover"
    if relationship in {REL_FRIEND, REL_FRIEND_ONLY}:
        return "graphql"
    if relationship == REL_HOME and not is_protected:
        return "home_switch"
    if relationship == REL_HOME and is_protected:
        return "none"
    if relationship == REL_SERVER_ONLY:
        return "none"
    return "none"


def _alias_keys_for(raw: dict[str, Any] | None) -> list[str]:
    """Identity keys used to merge friend / server / Home rows for one person."""
    if not raw:
        return []
    keys: list[str] = []
    for part in (
        raw.get("plex_uuid"),
        raw.get("plex_user_id"),
        raw.get("plex_numeric_id"),
    ):
        s = str(part or "").strip()
        if not s:
            continue
        keys.append(s.lower() if _is_hex_uuid(s) else s)
    uname = _norm_username(raw.get("username"))
    if uname:
        keys.append(f"user:{uname}")
    # Deduplicate while preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def link_all(self, keys: list[str]) -> None:
        if not keys:
            return
        base = keys[0]
        self.find(base)
        for k in keys[1:]:
            self.union(base, k)


def _canonical_ids(
    *,
    friend: dict[str, Any] | None,
    server: dict[str, Any] | None,
    home: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """Prefer GraphQL/hex uuid for plex_user_id (needed for friend watchlist fetch)."""
    for raw in (friend, home, server):
        if not raw:
            continue
        for part in (raw.get("plex_uuid"), raw.get("plex_user_id")):
            s = str(part or "").strip()
            if _is_hex_uuid(s):
                return s.lower(), s.lower()
    for raw in (home, server, friend):
        if not raw:
            continue
        numeric = str(raw.get("plex_numeric_id") or raw.get("plex_user_id") or "").strip()
        if numeric:
            uuid = str(raw.get("plex_uuid") or "").strip() or None
            return numeric, (uuid.lower() if uuid and _is_hex_uuid(uuid) else uuid)
    return "", None


def discover_roster(*, token: str | None = None) -> list[dict[str, Any]]:
    """Build classified roster rows (no watchlist title fetch).

    Merges friends (GraphQL hex uuid), server shares (numeric id + thumb uuid),
    and Home users (numeric id + uuid) into one identity per person.
    """
    tok = (token or plex_watchlist_token() or "").strip()
    if not tok:
        raise RuntimeError("No Plex token configured for watchlist")

    owner = fetch_account_user(token=tok)
    owner_uuid = str(owner.get("plex_uuid") or owner.get("plex_user_id") or "").strip().lower()
    owner_uname = _norm_username(owner.get("username"), owner.get("title"))

    friends: list[dict[str, Any]] = []
    try:
        friends = list_plex_friends(token=tok)
    except Exception as exc:
        logger.warning(f"Roster friends list failed: {exc}", extra={"emoji_type": "warning"})

    server_users: list[dict[str, Any]] = []
    try:
        server_users = list_plex_server_users(token=tok)
    except Exception as exc:
        logger.warning(f"Roster server users list failed: {exc}", extra={"emoji_type": "warning"})

    home_users: list[dict[str, Any]] = []
    try:
        home_users = list_home_users(token=tok)
    except Exception as exc:
        logger.debug(f"Roster home users list skipped: {exc}", extra={"emoji_type": "debug"})

    uf = _UnionFind()
    owner_aliases = _alias_keys_for(owner)
    # Home admin often uses a different numeric id than the account uuid.
    for h in home_users:
        if h.get("is_admin") or _norm_username(h.get("username")) == owner_uname:
            owner_aliases.extend(_alias_keys_for(h))
        huuid = str(h.get("plex_uuid") or "").strip().lower()
        if owner_uuid and huuid == owner_uuid:
            owner_aliases.extend(_alias_keys_for(h))
    if owner_uname:
        owner_aliases.append(f"user:{owner_uname}")
    uf.link_all(owner_aliases)

    for raw in friends:
        uf.link_all(_alias_keys_for(raw))
    for raw in server_users:
        uf.link_all(_alias_keys_for(raw))
    for raw in home_users:
        uf.link_all(_alias_keys_for(raw))

    clusters: dict[str, dict[str, Any]] = {}

    def _bucket(raw: dict[str, Any], slot: str) -> None:
        keys = _alias_keys_for(raw)
        if not keys:
            return
        root = uf.find(keys[0])
        bucket = clusters.setdefault(root, {"friend": None, "server": None, "home": None})
        # Prefer first non-empty; Home may overwrite title/thumb later when merging fields.
        if bucket.get(slot) is None:
            bucket[slot] = raw

    for raw in friends:
        _bucket(raw, "friend")
    for raw in server_users:
        _bucket(raw, "server")
    for raw in home_users:
        _bucket(raw, "home")

    owner_roots: set[str] = set()
    if owner_aliases:
        owner_roots.add(uf.find(owner_aliases[0]))
        for k in owner_aliases[1:]:
            owner_roots.add(uf.find(k))

    rows: list[dict[str, Any]] = []
    owner_id = str(owner.get("plex_user_id") or owner_uuid or "owner").strip()
    owner_alias_ids: list[str] = []
    _seen_owner_alias: set[str] = set()
    for k in owner_aliases:
        if not k or str(k).startswith("user:") or k == owner_id or k in _seen_owner_alias:
            continue
        _seen_owner_alias.add(k)
        owner_alias_ids.append(k)
    owner_row = {
        **owner,
        "plex_user_id": owner_id,
        "plex_uuid": owner_uuid or owner.get("plex_uuid"),
        "kind": "owner",
        "relationship": REL_OWNER,
        "fetch_method": "discover",
        "block_reason": None,
        "sync_enabled": True,
        "on_plex_home": True,
        "home_profile": "unlocked",
        "_alias_ids": owner_alias_ids,
    }
    # Prefer Home thumb/title for owner when available.
    for h in home_users:
        if h.get("is_admin") or (
            owner_uuid and str(h.get("plex_uuid") or "").strip().lower() == owner_uuid
        ):
            if h.get("thumb_url") and not owner_row.get("thumb_url"):
                owner_row["thumb_url"] = h.get("thumb_url")
            if h.get("title"):
                owner_row["title"] = h.get("title")
            break
    rows.append(owner_row)

    for root, parts in sorted(clusters.items(), key=lambda kv: kv[0]):
        if root in owner_roots:
            continue
        # Also skip if any alias matches owner uuid/username.
        friend = parts.get("friend")
        server = parts.get("server")
        home = parts.get("home")
        if owner_uuid:
            for raw in (friend, server, home):
                if not raw:
                    continue
                if str(raw.get("plex_uuid") or "").strip().lower() == owner_uuid:
                    friend = server = home = None
                    break
                if str(raw.get("plex_user_id") or "").strip().lower() == owner_uuid:
                    friend = server = home = None
                    break
        if friend is None and server is None and home is None:
            continue
        if owner_uname and _norm_username(
            (friend or {}).get("username"),
            (server or {}).get("username"),
            (home or {}).get("username"),
        ) == owner_uname:
            continue

        in_friends = friend is not None
        in_server = server is not None
        in_home = home is not None

        relationship = classify_relationship(
            in_friends=in_friends,
            in_server=in_server,
            in_home=in_home,
        )
        plex_user_id, plex_uuid = _canonical_ids(friend=friend, server=server, home=home)
        if not plex_user_id:
            continue

        base = friend or server or home or {}
        is_protected = bool((home or {}).get("is_protected") or (base or {}).get("is_protected"))
        is_restricted = bool(
            (home or {}).get("is_restricted")
            or (
                # Managed Home profiles often have no username on the Home list.
                in_home and not str((home or {}).get("username") or "").strip()
            )
            or (server or {}).get("is_restricted")
            or (base or {}).get("is_restricted")
        )
        kind = (
            "friend"
            if relationship in {REL_FRIEND, REL_FRIEND_ONLY}
            else (
                "home"
                if relationship == REL_HOME
                else ("owner" if relationship == REL_OWNER else "unknown")
            )
        )
        if relationship == REL_SERVER_ONLY:
            kind = "unknown"

        sync_default = default_sync_enabled(relationship, is_protected=is_protected)
        block = block_reason_for(
            relationship,
            is_protected=is_protected,
            sync_enabled=sync_default,
        )
        profile_detail = home_profile_detail(
            in_home=in_home,
            is_restricted=is_restricted,
            is_protected=is_protected,
        )
        title = (
            str((home or {}).get("title") or "").strip()
            or str((friend or {}).get("title") or "").strip()
            or str((server or {}).get("title") or "").strip()
            or str((base or {}).get("username") or "").strip()
            or plex_user_id
        )
        rows.append(
            {
                "plex_user_id": plex_user_id,
                "plex_uuid": plex_uuid,
                "plex_numeric_id": _uid_key(
                    (home or {}).get("plex_user_id") if home and _is_numeric_id(str(home.get("plex_user_id") or "")) else None,
                    (server or {}).get("plex_numeric_id"),
                    (server or {}).get("plex_user_id") if server and _is_numeric_id(str(server.get("plex_user_id") or "")) else None,
                ),
                "username": (
                    str((friend or {}).get("username") or "").strip()
                    or str((home or {}).get("username") or "").strip()
                    or str((server or {}).get("username") or "").strip()
                    or None
                ),
                "title": title,
                "thumb_url": (
                    str((home or {}).get("thumb_url") or "").strip()
                    or str((server or {}).get("thumb_url") or "").strip()
                    or str((friend or {}).get("thumb_url") or "").strip()
                    or None
                ),
                "kind": kind,
                "relationship": relationship,
                "fetch_method": fetch_method_for(relationship, is_protected=is_protected),
                "block_reason": block,
                "on_plex_home": in_home,
                "home_profile": profile_detail,
                "is_admin": bool((home or {}).get("is_admin") or (base or {}).get("is_admin")),
                "is_restricted": is_restricted,
                "is_protected": is_protected,
                "is_guest": bool((home or {}).get("is_guest") or (base or {}).get("is_guest")),
                "sync_enabled": sync_default,
                "_alias_ids": [
                    k
                    for k in _alias_keys_for(
                        {
                            "plex_uuid": plex_uuid,
                            "plex_user_id": plex_user_id,
                            "plex_numeric_id": (server or {}).get("plex_numeric_id")
                            or ((home or {}).get("plex_user_id") if home else None),
                            "username": (friend or server or home or {}).get("username"),
                        }
                    )
                    if not k.startswith("user:")
                ],
            }
        )

    rows.sort(
        key=lambda r: (
            0 if r.get("relationship") == REL_OWNER else 1,
            str(r.get("title") or "").lower(),
        )
    )
    return rows


def _merge_alias_memberships(
    session,
    *,
    canonical_id: str,
    alias_ids: list[str],
) -> int:
    """Move memberships from alias plex_user_id rows onto the canonical id; delete aliases."""
    merged = 0
    aliases = [a for a in alias_ids if a and a != canonical_id]
    if not aliases:
        return 0
    # Memberships under alias ids.
    for alias in aliases:
        mems = (
            session.query(PlexWatchlistMembership)
            .filter(PlexWatchlistMembership.plex_user_id == alias)
            .all()
        )
        for mem in mems:
            existing = (
                session.query(PlexWatchlistMembership)
                .filter(
                    PlexWatchlistMembership.watchlist_item_id == mem.watchlist_item_id,
                    PlexWatchlistMembership.plex_user_id == canonical_id,
                )
                .first()
            )
            if existing is None:
                mem.plex_user_id = canonical_id
            else:
                if mem.first_seen_at and (
                    existing.first_seen_at is None or mem.first_seen_at < existing.first_seen_at
                ):
                    existing.first_seen_at = mem.first_seen_at
                if mem.last_seen_at and (
                    existing.last_seen_at is None or mem.last_seen_at > existing.last_seen_at
                ):
                    existing.last_seen_at = mem.last_seen_at
                session.delete(mem)
            merged += 1
        alias_user = (
            session.query(PlexWatchlistUser)
            .filter(PlexWatchlistUser.plex_user_id == alias)
            .first()
        )
        if alias_user is not None:
            # Preserve sync_enabled=True if either side had it on before drop.
            canon = (
                session.query(PlexWatchlistUser)
                .filter(PlexWatchlistUser.plex_user_id == canonical_id)
                .first()
            )
            if canon is not None and bool(alias_user.sync_enabled) and not bool(canon.sync_enabled):
                # Do not revive hard-blocked relationships.
                if str(getattr(canon, "relationship", "") or "") not in {
                    REL_SERVER_ONLY,
                } and not bool(canon.is_protected):
                    canon.sync_enabled = True
            session.delete(alias_user)
            merged += 1
    return merged


def persist_roster(rows: list[dict[str, Any]], *, preserve_sync_toggles: bool = True) -> dict[str, Any]:
    """Upsert roster classification into plex_watchlist_user without touching memberships."""
    now = _utcnow()
    seen: set[str] = set()
    created = updated = aliases_merged = 0
    session = get_session()
    try:
        existing = {u.plex_user_id: u for u in session.query(PlexWatchlistUser).all()}
        for raw in rows:
            uid = str(raw.get("plex_user_id") or "").strip()
            if not uid:
                continue
            seen.add(uid)
            user = existing.get(uid)
            is_new = user is None
            if is_new:
                user = PlexWatchlistUser(
                    plex_user_id=uid,
                    title=str(raw.get("title") or uid),
                    first_seen_at=now,
                    updated_at=now,
                    sync_enabled=bool(raw.get("sync_enabled", True)),
                )
                session.add(user)
                existing[uid] = user
                created += 1
            else:
                updated += 1

            user.plex_uuid = str(raw.get("plex_uuid") or "") or user.plex_uuid
            user.username = str(raw.get("username") or "") or user.username
            user.title = str(raw.get("title") or user.title or uid)
            user.thumb_url = str(raw.get("thumb_url") or "") or user.thumb_url
            kind = str(raw.get("kind") or "").strip().lower()
            if kind in {"owner", "friend", "home", "unknown"}:
                user.kind = kind
            rel = str(raw.get("relationship") or "").strip().lower()
            if rel:
                user.relationship = rel
            fm = str(raw.get("fetch_method") or "").strip().lower()
            if fm:
                user.fetch_method = fm
            br = raw.get("block_reason")
            user.block_reason = str(br) if br else None
            user.is_admin = bool(raw.get("is_admin"))
            user.is_restricted = bool(raw.get("is_restricted"))
            user.is_protected = bool(raw.get("is_protected"))
            user.is_guest = bool(raw.get("is_guest"))
            # Stash Home membership tags without a dedicated column (roster meta only).
            meta = dict(user.settings_override) if isinstance(user.settings_override, dict) else {}
            meta["on_plex_home"] = bool(raw.get("on_plex_home"))
            hp = raw.get("home_profile")
            if hp:
                meta["home_profile"] = str(hp)
            else:
                meta.pop("home_profile", None)
            user.settings_override = meta or None
            if is_new or not preserve_sync_toggles:
                user.sync_enabled = bool(raw.get("sync_enabled", user.sync_enabled))
            user.block_reason = block_reason_for(
                str(user.relationship or rel or REL_UNKNOWN),
                is_protected=bool(user.is_protected),
                sync_enabled=bool(user.sync_enabled),
            )
            if user.block_reason:
                user.last_sync_error = BLOCK_REASON_MESSAGES.get(
                    user.block_reason, user.last_sync_error
                )
            elif user.last_sync_error in set(BLOCK_REASON_MESSAGES.values()) | {
                "Home switch sync disabled",
                "Home switch sync disabled; use friends sync",
                "Protected Home user (PIN required to sync watchlist)",
            }:
                user.last_sync_error = None
            user.updated_at = now

            alias_ids = raw.get("_alias_ids") if isinstance(raw.get("_alias_ids"), list) else []
            aliases_merged += _merge_alias_memberships(
                session,
                canonical_id=uid,
                alias_ids=[str(a).strip() for a in alias_ids if str(a or "").strip()],
            )

        session.commit()
    finally:
        session.close()
    return {
        "ok": True,
        "created": created,
        "updated": updated,
        "seen": len(seen),
        "aliases_merged": aliases_merged,
    }


def refresh_roster(*, preserve_sync_toggles: bool = True) -> dict[str, Any]:
    rows = discover_roster()
    result = persist_roster(rows, preserve_sync_toggles=preserve_sync_toggles)
    result["users"] = len(rows)
    return result


def set_user_sync_enabled(plex_user_id: str, enabled: bool) -> dict[str, Any] | None:
    uid = str(plex_user_id or "").strip()
    if not uid:
        return None
    session = get_session()
    try:
        user = session.query(PlexWatchlistUser).filter(PlexWatchlistUser.plex_user_id == uid).first()
        if user is None:
            return None
        # Policy / hard blocks cannot be enabled while the gate is closed.
        rel = str(getattr(user, "relationship", None) or REL_UNKNOWN)
        hard = block_reason_for(rel, is_protected=bool(user.is_protected), sync_enabled=True)
        if enabled and hard in POLICY_OR_HARD_BLOCKS:
            user.sync_enabled = False
            user.block_reason = hard
        else:
            user.sync_enabled = bool(enabled)
            user.block_reason = block_reason_for(
                rel,
                is_protected=bool(user.is_protected),
                sync_enabled=bool(user.sync_enabled),
            )
        if user.block_reason:
            user.last_sync_error = BLOCK_REASON_MESSAGES.get(user.block_reason, user.last_sync_error)
        elif user.last_sync_error in set(BLOCK_REASON_MESSAGES.values()):
            user.last_sync_error = None
        user.updated_at = _utcnow()
        session.commit()
        from services.watchlist.sync import _serialize_user

        return _serialize_user(user)
    finally:
        session.close()
