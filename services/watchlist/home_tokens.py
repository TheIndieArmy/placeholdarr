"""Cached Plex Home switch tokens for watchlist pulls (opt-in setting)."""

from __future__ import annotations

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import PlexWatchlistUser


def cache_home_switch_tokens_enabled() -> bool:
    return bool(getattr(settings, "WATCHLIST_CACHE_HOME_SWITCH_TOKENS", False)) and bool(
        getattr(settings, "WATCHLIST_SYNC_HOME_USERS", False)
    )


def get_cached_home_token(plex_user_id: str) -> str | None:
    uid = str(plex_user_id or "").strip()
    if not uid or not cache_home_switch_tokens_enabled():
        return None
    session = get_session()
    try:
        row = (
            session.query(PlexWatchlistUser.cached_home_token)
            .filter(PlexWatchlistUser.plex_user_id == uid)
            .first()
        )
        if not row:
            return None
        tok = str(row[0] or "").strip()
        return tok or None
    finally:
        session.close()


def set_cached_home_token(plex_user_id: str, token: str) -> None:
    uid = str(plex_user_id or "").strip()
    tok = str(token or "").strip()
    if not uid or not tok or not cache_home_switch_tokens_enabled():
        return
    session = get_session()
    try:
        user = session.query(PlexWatchlistUser).filter(PlexWatchlistUser.plex_user_id == uid).first()
        if user is None:
            return
        user.cached_home_token = tok[:512]
        session.add(user)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning(
            f"Failed to cache Home switch token for {uid}: {exc}",
            extra={"emoji_type": "warning"},
        )
    finally:
        session.close()


def clear_cached_home_token(plex_user_id: str) -> None:
    uid = str(plex_user_id or "").strip()
    if not uid:
        return
    session = get_session()
    try:
        user = session.query(PlexWatchlistUser).filter(PlexWatchlistUser.plex_user_id == uid).first()
        if user is None or not user.cached_home_token:
            return
        user.cached_home_token = None
        session.add(user)
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.warning(
            f"Failed to clear Home switch token for {uid}: {exc}",
            extra={"emoji_type": "warning"},
        )
    finally:
        session.close()


def clear_all_cached_home_tokens() -> int:
    """Drop every stored Home switch token (setting off or Home sync disabled)."""
    session = get_session()
    try:
        rows = (
            session.query(PlexWatchlistUser)
            .filter(PlexWatchlistUser.cached_home_token.isnot(None))
            .all()
        )
        n = 0
        for user in rows:
            if user.cached_home_token:
                user.cached_home_token = None
                session.add(user)
                n += 1
        if n:
            session.commit()
            logger.info(
                f"Cleared {n} cached Plex Home switch token(s)",
                extra={"emoji_type": "info"},
            )
        return n
    except Exception as exc:
        session.rollback()
        logger.warning(
            f"Failed to clear cached Home switch tokens: {exc}",
            extra={"emoji_type": "warning"},
        )
        return 0
    finally:
        session.close()
