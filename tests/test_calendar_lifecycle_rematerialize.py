from services.app_config import CALENDAR_LIFECYCLE_REMATERIALIZE_SETTING_KEYS
from services.source_of_truth import calendar_lifecycle_rematerialize as cal_life


def test_calendar_lifecycle_setting_keys():
    assert CALENDAR_LIFECYCLE_REMATERIALIZE_SETTING_KEYS == frozenset(
        {"CALENDAR_LOOKAHEAD_DAYS", "PREFERRED_MOVIE_DATE_TYPE"}
    )


def test_execute_next_full_sync_marks_pending(monkeypatch):
    marked: list[bool] = []

    monkeypatch.setattr(cal_life, "mark_calendar_lifecycle_rematerialize_pending", lambda pending=True: marked.append(bool(pending)))
    monkeypatch.setattr(cal_life, "start_calendar_lifecycle_rematerialize_background", lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not start")))

    out = cal_life.execute_calendar_lifecycle_rematerialize_apply_scope(
        "next_full_sync",
        source="test",
    )
    assert out["ok"] is True
    assert out["scope"] == "next_full_sync"
    assert out["pending"] is True
    assert marked == [True]


def test_execute_now_marks_pending_and_starts(monkeypatch):
    marked: list[bool] = []
    started: list[str] = []

    monkeypatch.setattr(cal_life, "mark_calendar_lifecycle_rematerialize_pending", lambda pending=True: marked.append(bool(pending)))
    monkeypatch.setattr(
        cal_life,
        "start_calendar_lifecycle_rematerialize_background",
        lambda *, source="settings_save": started.append(source),
    )

    out = cal_life.execute_calendar_lifecycle_rematerialize_apply_scope("now", source="test")
    assert out["ok"] is True
    assert out["scope"] == "now"
    assert out["enqueued"] is True
    assert marked == [True]
    assert started == ["test:apply_now"]
