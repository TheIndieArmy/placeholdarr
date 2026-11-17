from datetime import date
from types import SimpleNamespace

from core.config import Settings
from services import library_future_semantics as future_sem
from services.source_of_truth.determiner import DETERMINATION_NOT_NEEDED


def test_calendar_lookahead_days_preserves_zero():
    s = Settings()
    s.CALENDAR_LOOKAHEAD_DAYS = 0
    assert s.calendar_lookahead_days == 0
    assert s.coming_soon_placeholders_enabled is False


def test_calendar_lookahead_days_preserves_unlimited():
    s = Settings()
    s.CALENDAR_LOOKAHEAD_DAYS = -1
    assert s.calendar_lookahead_days == -1
    assert s.coming_soon_placeholders_enabled is True


def test_movie_row_future_when_coming_soon_window_off(monkeypatch):
    monkeypatch.setattr(future_sem.settings, "CALENDAR_LOOKAHEAD_DAYS", 0)
    movie = SimpleNamespace(
        determination=DETERMINATION_NOT_NEEDED,
        has_file=False,
        has_placeholder=False,
        theater_release_date=date(2026, 5, 1),
        digital_release_date=None,
        physical_release_date=None,
        radarr_release_status=None,
    )
    assert future_sem.movie_row_is_future_outside_lookahead(movie, now_date=date(2026, 3, 28)) is True


def test_movie_row_not_future_when_past_due_and_window_off(monkeypatch):
    monkeypatch.setattr(future_sem.settings, "CALENDAR_LOOKAHEAD_DAYS", 0)
    movie = SimpleNamespace(
        determination=DETERMINATION_NOT_NEEDED,
        has_file=False,
        has_placeholder=False,
        theater_release_date=date(2026, 3, 1),
        digital_release_date=None,
        physical_release_date=None,
        radarr_release_status=None,
    )
    assert future_sem.movie_row_is_future_outside_lookahead(movie, now_date=date(2026, 3, 28)) is False
