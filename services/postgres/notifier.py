"""Postgres LISTEN/NOTIFY listener with reconnect-with-backoff.

Holds a dedicated psycopg2 connection in autocommit mode, separate from the
SQLAlchemy pool, and runs a single worker thread that blocks on select() for
incoming NOTIFY messages. Multiple channels can be registered; each has its
own callback that fires whenever any NOTIFY is received for that channel.

After every successful reconnect (including the initial connect), every
registered channel's callback is fired once with notify=None so callers can
"force drain" anything that may have been missed during the disconnected
window. Safety polls in callers (e.g., the worker loop) provide a second
backstop.

The listener is designed to be:
- Crash-resistant: every iteration is wrapped in try/except; the thread
  loops forever via an outer supervisor.
- Reconnect-safe: on connection failure, exponential backoff (1s -> 2s ->
  4s -> ... up to 30s), then re-LISTEN on every channel.
- Coalescing-friendly: NOTIFY is best-effort; callbacks should be idempotent
  drainers (i.e., "wake up and process whatever PENDING work exists").
"""

from __future__ import annotations

import select
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import psycopg2
import psycopg2.extensions

from core.config import settings
from core.logger import logger


_RECONNECT_BACKOFF_INITIAL = 1.0
_RECONNECT_BACKOFF_MAX = 30.0
_SELECT_TIMEOUT_SECONDS = 30.0


@dataclass
class _ChannelHandler:
    name: str
    callback: Callable[[Optional[str]], None]


@dataclass
class NotifierStats:
    started_at: Optional[datetime] = None
    last_notify_at: Optional[datetime] = None
    notifies_received: int = 0
    reconnect_count: int = 0
    last_reconnect_at: Optional[datetime] = None
    last_error: Optional[str] = None
    is_connected: bool = False
    channels: List[str] = field(default_factory=list)


class Notifier:
    """Single-process Postgres LISTEN/NOTIFY consumer.

    Usage:
        notifier = Notifier()
        notifier.listen('placeholdarr_jobs', on_jobs_notify)
        notifier.start()
        ...
        notifier.stop()

    Callbacks receive an optional payload string (or None for synthetic
    "force drain" wakes after reconnect). Callbacks must NOT block for long;
    treat them as wake signals that flip a threading.Event.
    """

    def __init__(self, name: str = 'placeholdarr_notifier'):
        self.name = str(name)
        self._handlers: Dict[str, _ChannelHandler] = {}
        self._handlers_lock = threading.Lock()
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._conn_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started = False
        self.stats = NotifierStats()

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def listen(self, channel: str, callback: Callable[[Optional[str]], None]) -> None:
        """Register a channel + callback. May be called before or after start()."""
        ch = self._normalize_channel(channel)
        with self._handlers_lock:
            self._handlers[ch] = _ChannelHandler(name=ch, callback=callback)
        if self._started:
            self._issue_listen(ch)
        self.stats.channels = list(self._handlers.keys())

    def unlisten(self, channel: str) -> None:
        ch = self._normalize_channel(channel)
        with self._handlers_lock:
            self._handlers.pop(ch, None)
        if self._started and self._conn is not None:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(f'UNLISTEN "{ch}"')
            except Exception as exc:
                logger.debug(
                    f"notifier({self.name}) UNLISTEN {ch} failed: {exc}",
                    extra={'emoji_type': 'debug'},
                )
        self.stats.channels = list(self._handlers.keys())

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_supervisor,
            name=f'{self.name}-listener',
            daemon=True,
        )
        self.stats.started_at = datetime.now(timezone.utc)
        self._thread.start()
        logger.info(
            f"Notifier({self.name}) started",
            extra={'emoji_type': 'gear'},
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        try:
            with self._conn_lock:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
        except Exception:
            pass
        if self._thread is not None:
            try:
                self._thread.join(timeout=5.0)
            except Exception:
                pass
        self._started = False
        self.stats.is_connected = False
        logger.info(
            f"Notifier({self.name}) stopped",
            extra={'emoji_type': 'info'},
        )

    def is_alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    # ------------------------------------------------------------
    # Internal: connection lifecycle
    # ------------------------------------------------------------

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        cleaned = ''.join(ch if (ch.isalnum() or ch == '_') else '_' for ch in str(channel or ''))
        if not cleaned:
            raise ValueError('notifier_channel_empty')
        return cleaned

    def _build_connection(self) -> psycopg2.extensions.connection:
        conn = psycopg2.connect(
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USER,
            password=settings.DB_PASS,
            dbname=settings.DB_NAME,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            application_name=f'placeholdarr_{self.name}',
        )
        conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        return conn

    def _issue_listen(self, channel: str) -> None:
        with self._conn_lock:
            if self._conn is None:
                logger.debug(
                    f"notifier({self.name}) LISTEN {channel} deferred: connection not ready yet "
                    f"(will run with the next _listen_all after connect)",
                    extra={'emoji_type': 'debug'},
                )
                return
            try:
                with self._conn.cursor() as cur:
                    cur.execute(f'LISTEN "{channel}"')
            except Exception as exc:
                logger.warning(
                    f"notifier({self.name}) LISTEN {channel} failed: "
                    f"{type(exc).__name__} {exc!r}",
                    extra={'emoji_type': 'warning'},
                )

    def _listen_all(self) -> None:
        with self._handlers_lock:
            channels = list(self._handlers.keys())
        for ch in channels:
            self._issue_listen(ch)

    def _force_drain_all(self) -> None:
        with self._handlers_lock:
            handlers = list(self._handlers.values())
        for handler in handlers:
            try:
                handler.callback(None)
            except Exception as exc:
                logger.warning(
                    f"notifier({self.name}) force-drain callback failed channel={handler.name}: {exc}",
                    extra={'emoji_type': 'warning'},
                )

    def _close_conn(self) -> None:
        with self._conn_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        self.stats.is_connected = False

    # ------------------------------------------------------------
    # Internal: main loop
    # ------------------------------------------------------------

    def _run_supervisor(self) -> None:
        """Outer supervisor: never exits except on stop_event. Wraps _run_one_cycle."""
        backoff = _RECONNECT_BACKOFF_INITIAL
        while not self._stop_event.is_set():
            try:
                self._run_one_cycle()
                # _run_one_cycle returned normally (e.g., disconnect loop ended),
                # reset backoff and try again.
                backoff = _RECONNECT_BACKOFF_INITIAL
            except Exception as exc:
                self.stats.last_error = str(exc)
                logger.warning(
                    f"notifier({self.name}) supervisor caught exception: {exc}; sleeping {backoff:.1f}s",
                    extra={'emoji_type': 'warning'},
                )
                self._close_conn()
                if self._stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2.0, _RECONNECT_BACKOFF_MAX)

    def _run_one_cycle(self) -> None:
        """Open a connection, register all channels, fire force-drain, then poll."""
        try:
            conn = self._build_connection()
        except Exception as exc:
            self.stats.last_error = f'connect_failed: {exc}'
            raise

        with self._conn_lock:
            self._conn = conn
        self.stats.is_connected = True
        self.stats.reconnect_count += 1
        self.stats.last_reconnect_at = datetime.now(timezone.utc)

        try:
            self._listen_all()
            with self._handlers_lock:
                channel_count = len(self._handlers)
            logger.info(
                f"notifier({self.name}) connected; LISTENing on {channel_count} channel(s)",
                extra={'emoji_type': 'success'},
            )
            # After (re)connect, force-drain so callers process anything we may have
            # missed while disconnected. Synthetic notify with payload=None.
            self._force_drain_all()

            # Main loop: select() with timeout, then poll for notifies.
            while not self._stop_event.is_set():
                try:
                    rlist, _wlist, _xlist = select.select(
                        [conn], [], [], _SELECT_TIMEOUT_SECONDS,
                    )
                except (OSError, ValueError) as exc:
                    # File descriptor closed or invalid (e.g., during stop()); treat as disconnect.
                    raise RuntimeError(f'select_failed: {exc}')
                if self._stop_event.is_set():
                    break
                if not rlist:
                    # Timeout — connection still healthy; loop back.
                    # Touch the connection lightly to surface stale-conn errors quickly.
                    try:
                        conn.poll()
                    except Exception as exc:
                        raise RuntimeError(f'poll_after_timeout_failed: {exc}')
                    continue
                try:
                    conn.poll()
                except Exception as exc:
                    raise RuntimeError(f'poll_failed: {exc}')

                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    self.stats.notifies_received += 1
                    self.stats.last_notify_at = datetime.now(timezone.utc)
                    channel = notify.channel
                    payload = notify.payload if notify.payload else None
                    with self._handlers_lock:
                        handler = self._handlers.get(channel)
                    if handler is None:
                        # Stale subscription or unknown channel; ignore.
                        continue
                    try:
                        handler.callback(payload)
                    except Exception as exc:
                        logger.warning(
                            f"notifier({self.name}) callback failed channel={channel}: {exc}",
                            extra={'emoji_type': 'warning'},
                        )
        finally:
            self._close_conn()

    # ------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------

    def snapshot_stats(self) -> dict:
        return {
            'name': self.name,
            'started_at': self.stats.started_at.isoformat() if self.stats.started_at else None,
            'last_notify_at': self.stats.last_notify_at.isoformat() if self.stats.last_notify_at else None,
            'notifies_received': int(self.stats.notifies_received),
            'reconnect_count': int(self.stats.reconnect_count),
            'last_reconnect_at': self.stats.last_reconnect_at.isoformat() if self.stats.last_reconnect_at else None,
            'last_error': self.stats.last_error,
            'is_connected': bool(self.stats.is_connected),
            'channels': list(self.stats.channels),
            'thread_alive': self.is_alive(),
        }

    def healthy(self) -> bool:
        """Return True if the listener is connected and the supervisor thread is alive.

        Phase 3 of the holistic NOTIFY audit: callers (and the diagnostics
        endpoint) use this to decide whether NOTIFY-driven wakes are
        currently working. A False here means the system is relying on the
        worker safety-poll backstop rather than NOTIFY.
        """
        try:
            return bool(self.stats.is_connected and self.is_alive())
        except Exception:
            return False


# ----------------------------------------------------------------
# Process-wide singleton accessors (one Notifier per process)
# ----------------------------------------------------------------

_shared_notifier: Optional[Notifier] = None
_shared_notifier_lock = threading.Lock()


def get_shared_notifier() -> Notifier:
    """Return the process-wide Notifier singleton, creating it lazily.

    Multiple consumers (worker loop, queue monitor) share the same Notifier
    so we maintain at most one dedicated listener connection.
    """
    global _shared_notifier
    with _shared_notifier_lock:
        if _shared_notifier is None:
            _shared_notifier = Notifier(name='placeholdarr_notifier')
        return _shared_notifier


def start_shared_notifier() -> Notifier:
    notifier = get_shared_notifier()
    if not notifier.is_alive():
        notifier.start()
    return notifier


def stop_shared_notifier() -> None:
    global _shared_notifier
    with _shared_notifier_lock:
        if _shared_notifier is None:
            return
        try:
            _shared_notifier.stop()
        finally:
            _shared_notifier = None


def get_shared_notifier_health() -> dict:
    """Return a JSON-friendly health snapshot for the shared notifier.

    Used by ``GET /api/diagnostics/db`` and any future health probes. Safe
    to call when the notifier has not been started: returns
    ``{"healthy": False, "started": False}`` instead of raising.
    """
    with _shared_notifier_lock:
        notifier = _shared_notifier
    if notifier is None:
        return {"healthy": False, "started": False}
    snapshot = notifier.snapshot_stats()
    snapshot["healthy"] = bool(notifier.healthy())
    snapshot["started"] = True
    return snapshot


JOBS_CHANNEL = 'placeholdarr_jobs'
QUEUE_MONITOR_CHANNEL = 'placeholdarr_queue_monitor_signal'
