"""In-process ring buffer for near-real-time log streaming in the dashboard."""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
_LEVEL_NAME_TO_NUM = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


@dataclass(frozen=True)
class LiveLogEntry:
    id: int
    line: str
    levelno: int


class LiveLogBuffer:
    def __init__(self, *, maxlen: int = 12_000) -> None:
        self._maxlen = max(500, int(maxlen))
        self._entries: deque[LiveLogEntry] = deque(maxlen=self._maxlen)
        self._next_id = 1
        self._lock = threading.Lock()

    def append(self, line: str, levelno: int) -> LiveLogEntry:
        text = str(line or "").rstrip("\n")
        with self._lock:
            entry = LiveLogEntry(id=self._next_id, line=text, levelno=int(levelno))
            self._next_id += 1
            self._entries.append(entry)
            return entry

    def latest_id(self) -> int:
        with self._lock:
            if not self._entries:
                return 0
            return self._entries[-1].id

    def _level_threshold(self, level: str) -> int | None:
        normalized = str(level or "all").strip().lower()
        if normalized == "all":
            return None
        return _LEVEL_NAME_TO_NUM.get(normalized)

    def _matches_level(self, entry: LiveLogEntry, level: str) -> bool:
        threshold = self._level_threshold(level)
        if threshold is None:
            return True
        return int(entry.levelno) >= int(threshold)

    def get_tail(self, *, tail: int, level: str = "all") -> list[LiveLogEntry]:
        limit = max(1, int(tail))
        with self._lock:
            rows = list(self._entries)
        matched = [row for row in rows if self._matches_level(row, level)]
        return matched[-limit:]

    def get_since(self, since_id: int, *, level: str = "all") -> list[LiveLogEntry]:
        sid = max(0, int(since_id))
        with self._lock:
            rows = [row for row in self._entries if row.id > sid]
        return [row for row in rows if self._matches_level(row, level)]


LIVE_LOG_BUFFER = LiveLogBuffer()


class LiveLogBufferHandler(logging.Handler):
    """Capture formatted log lines into ``LIVE_LOG_BUFFER``."""

    def __init__(self, formatter: logging.Formatter) -> None:
        super().__init__(logging.NOTSET)
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            LIVE_LOG_BUFFER.append(message, record.levelno)
        except Exception:
            self.handleError(record)


def install_live_log_capture(formatter: logging.Formatter) -> None:
    """Attach buffer handlers to the app logger and logging root (idempotent)."""
    targets = (logging.getLogger("core.logger"), logging.getLogger())
    for target in targets:
        if any(isinstance(handler, LiveLogBufferHandler) for handler in target.handlers):
            continue
        target.addHandler(LiveLogBufferHandler(formatter))


def live_log_payload(
    *,
    tail: int,
    level: str,
    since_id: int | None = None,
) -> dict:
    if since_id is not None:
        entries = LIVE_LOG_BUFFER.get_since(int(since_id), level=level)
    else:
        entries = LIVE_LOG_BUFFER.get_tail(tail=tail, level=level)
    return {
        "lines": [entry.line for entry in entries],
        "latest_id": LIVE_LOG_BUFFER.latest_id(),
        "capture_level": "LIVE",
        "source": "live",
    }

