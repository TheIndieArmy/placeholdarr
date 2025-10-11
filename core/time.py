from datetime import datetime, timezone


def now_utc():
    """Return a timezone-aware UTC datetime for storage/logic."""
    return datetime.now(timezone.utc)
