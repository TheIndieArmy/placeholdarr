"""Series episode stats hooks must not re-enqueue on their own commits."""

from __future__ import annotations

from types import SimpleNamespace

from services import series_episode_stats_hooks as hooks


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeSession:
    def __init__(self):
        self.info = {}
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.executed = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(True)
        if "pg_advisory_unlock" in sql:
            return _FakeResult(True)
        return _FakeResult(None)

    def commit(self):
        self.commits += 1
        # Mimic SQLAlchemy: after_commit listeners see session.info as-is.
        hooks._after_commit(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_applying_stats_after_commit_does_not_reenqueue(monkeypatch):
    """Refreshing Series.stats_* must not schedule another refresh via after_commit."""
    fake = _FakeSession()
    # Import path used inside _execute_refresh_batch
    monkeypatch.setattr("services.postgres.db.get_session", lambda: fake)

    calls = {"refresh": 0, "bump": 0}

    def _fake_refresh(session, series_ids):
        calls["refresh"] += 1
        assert hooks._is_applying_stats(session)
        # Simulate ORM after_update marking dirty the way a buggy loop would.
        hooks._mark_series_id(session, 690)
        session.info[hooks._BUMP_SERIES_KEY] = True

    def _fake_bump(session):
        calls["bump"] += 1

    monkeypatch.setattr(hooks, "refresh_series_episode_stats", _fake_refresh)
    monkeypatch.setattr(hooks, "bump_series_version", _fake_bump)
    monkeypatch.setattr(hooks, "should_full_refresh", lambda n: False)
    monkeypatch.setattr(hooks, "_full_sync_is_active", lambda: False)

    # Clear any leftover pending work from other tests.
    hooks._take_pending_refresh()

    batch = hooks._RefreshBatch(series_ids={690}, bump_series=True)
    assert hooks._execute_refresh_batch(batch) is True
    assert calls["refresh"] == 1
    assert calls["bump"] == 1
    assert fake.commits >= 1

    leftover = hooks._take_pending_refresh()
    assert leftover is None or leftover.is_empty()


def test_after_series_change_skipped_while_applying(monkeypatch):
    session = SimpleNamespace(info={})
    target = SimpleNamespace(id=42)
    monkeypatch.setattr(hooks, "object_session", lambda _t: session)

    with hooks._applying_stats(session):
        hooks._after_series_change(None, None, target)  # type: ignore[arg-type]

    assert hooks._DIRTY_SERIES_KEY not in session.info
    assert not session.info.get(hooks._BUMP_SERIES_KEY)


def test_after_series_change_marks_dirty_normally(monkeypatch):
    session = SimpleNamespace(info={})
    target = SimpleNamespace(id=42)
    monkeypatch.setattr(hooks, "object_session", lambda _t: session)

    hooks._after_series_change(None, None, target)  # type: ignore[arg-type]

    assert session.info[hooks._DIRTY_SERIES_KEY] == {42}
    assert session.info[hooks._BUMP_SERIES_KEY] is True
