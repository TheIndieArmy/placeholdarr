from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from services.postgres.db import get_session
from services.postgres.models import LibraryRefreshThrottle


_REFRESH_KEY_PREFIX = "refresh_throttle"


def _safe_now() -> datetime:
    return datetime.now(timezone.utc)


def try_acquire_refresh_lease(
    *,
    section_ids: list[int] | set[int],
    source: str,
    min_interval_seconds: int,
    lease_seconds: int,
) -> dict[str, object]:
    """Acquire a durable refresh lease for all target sections atomically.

    Returns:
      {
        "allowed": bool,
        "granted_section_ids": [int],
        "blocked_section_ids": [int],
        "reason": str,
      }
    """
    target_ids = sorted({int(x) for x in (section_ids or [])})
    if not target_ids:
        return {
            "allowed": True,
            "granted_section_ids": [],
            "blocked_section_ids": [],
            "reason": "no_sections",
        }

    now = _safe_now()
    min_interval = max(0, int(min_interval_seconds or 1))
    lease = max(0, int(lease_seconds or 1))

    session = get_session()
    try:
        rows_by_section: dict[int, LibraryRefreshThrottle] = {}
        blocked_ids: list[int] = []
        block_reason = ""

        # Atomically check all target sections
        for section_id in target_ids:
            row = (
                session.query(LibraryRefreshThrottle)
                .filter(LibraryRefreshThrottle.section_id == section_id)
                .with_for_update()
                .first()
            )
            if not row:
                row = LibraryRefreshThrottle(section_id=section_id, source=source)
                session.add(row)
                session.flush()

            # Check if current lease is still active
            expires_at = getattr(row, "expires_at", None)
            if expires_at and expires_at > now:
                blocked_ids.append(section_id)
                block_reason = "lease_active"
                continue

            # Check if min interval requirement is met
            last_acquired = getattr(row, "acquired_at", None)
            if last_acquired and min_interval > 0:
                elapsed = (now - last_acquired).total_seconds()
                if elapsed < float(min_interval):
                    blocked_ids.append(section_id)
                    block_reason = "throttle"
                    continue

            rows_by_section[section_id] = row

        if blocked_ids:
            session.rollback()
            return {
                "allowed": False,
                "granted_section_ids": [],
                "blocked_section_ids": blocked_ids,
                "reason": block_reason or "blocked",
            }

        # Grant leases
        expires_at = now + timedelta(seconds=lease)
        for section_id, row in rows_by_section.items():
            row.source = source
            row.acquired_at = now
            row.expires_at = expires_at
            row.updated_at = func.now()
            session.add(row)

        session.commit()
        return {
            "allowed": True,
            "granted_section_ids": target_ids,
            "blocked_section_ids": [],
            "reason": "granted",
        }
    except Exception as e:
        logger.error(f"Failed to acquire refresh lease: {e}", extra={"emoji_type": "error"})
        try:
            session.rollback()
        except Exception:
            pass
        return {
            "allowed": False,
            "granted_section_ids": [],
            "blocked_section_ids": target_ids,
            "reason": "error",
        }
    finally:
        session.close()
