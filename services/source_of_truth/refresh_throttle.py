from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from services.postgres.db import get_session
from services.postgres.models import ObservationFlight


_REFRESH_KEY_PREFIX = "refresh_throttle"


def _safe_now() -> datetime:
    return datetime.now(timezone.utc)


def _key_for_section(section_id: int) -> str:
    return f"{_REFRESH_KEY_PREFIX}:section:{int(section_id)}"


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
    min_interval = max(0, int(min_interval_seconds or 0))
    lease = max(0, int(lease_seconds or 0))

    session = get_session()
    try:
        rows_by_section: dict[int, ObservationFlight] = {}
        blocked_ids: list[int] = []
        block_reason = ""

        for section_id in target_ids:
            key = _key_for_section(section_id)
            row = (
                session.query(ObservationFlight)
                .filter(ObservationFlight.flight_key == key)
                .with_for_update()
                .first()
            )
            if not row:
                row = ObservationFlight(flight_key=key)
                session.add(row)
                session.flush()

            lease_until = getattr(row, "released_at", None)
            if lease_until and lease_until > now:
                blocked_ids.append(section_id)
                block_reason = "lease"
                continue

            last_refresh_at = getattr(row, "acquired_at", None)
            if last_refresh_at and min_interval > 0:
                elapsed = (now - last_refresh_at).total_seconds()
                if elapsed < float(min_interval):
                    blocked_ids.append(section_id)
                    block_reason = "min_interval"
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

        lease_until = now + timedelta(seconds=lease)
        for section_id, row in rows_by_section.items():
            row.holder = source
            row.source = source
            row.is_active = True
            row.lock_attempts = int(getattr(row, "lock_attempts", 0) or 0) + 1
            row.acquired_at = now
            row.heartbeat_at = now
            row.released_at = lease_until
            row.last_reason = f"refresh_lease:{source}"
            row.updated_at = func.now()
            session.add(row)

        session.commit()
        return {
            "allowed": True,
            "granted_section_ids": target_ids,
            "blocked_section_ids": [],
            "reason": "granted",
        }
    except Exception:
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
        try:
            session.close()
        except Exception:
            pass
