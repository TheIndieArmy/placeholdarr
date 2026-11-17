from datetime import date
from types import SimpleNamespace

from services.source_of_truth import determiner
from services.source_of_truth.determiner import (
    DETERMINATION_EXISTS,
    DETERMINATION_NEEDS,
    DETERMINATION_NOT_NEEDED,
    DETERMINATION_OBSOLETE,
)


def test_compute_determination_zero_lookahead_suppresses_future_placeholder():
    value = determiner._compute_determination(
        has_placeholder=True,
        has_file=False,
        is_deleted=False,
        target_date=date(2026, 4, 15),
        lookahead_days=0,
        placeholders_enabled=True,
        now_date=date(2026, 3, 28),
    )
    assert value == DETERMINATION_OBSOLETE


def test_compute_determination_disabled_coming_soon_suppresses_future_even_if_lookahead_coerced():
    # Settings wiring: coming_soon_placeholders_enabled is False when days=0.
    # A buggy ``or 30`` reader used to pass lookahead_days=30 with enabled=False and
    # skip the calendar guard, creating placeholders for every future title (#189).
    value = determiner._compute_determination(
        has_placeholder=False,
        has_file=False,
        is_deleted=False,
        target_date=date(2026, 4, 15),
        lookahead_days=30,
        placeholders_enabled=False,
        now_date=date(2026, 3, 28),
    )
    assert value == DETERMINATION_NOT_NEEDED


def test_compute_determination_disabled_coming_soon_keeps_past_due_needs():
    value = determiner._compute_determination(
        has_placeholder=False,
        has_file=False,
        is_deleted=False,
        target_date=date(2026, 3, 1),
        lookahead_days=0,
        placeholders_enabled=False,
        now_date=date(2026, 3, 28),
    )
    assert value == DETERMINATION_NEEDS


def test_compute_determination_zero_lookahead_suppresses_unknown_date():
    value = determiner._compute_determination(
        has_placeholder=False,
        has_file=False,
        is_deleted=False,
        target_date=None,
        lookahead_days=0,
        placeholders_enabled=True,
        now_date=date(2026, 3, 28),
    )
    assert value == DETERMINATION_NOT_NEEDED


def test_compute_determination_infinite_lookahead_keeps_unknown_date():
    value = determiner._compute_determination(
        has_placeholder=False,
        has_file=False,
        is_deleted=False,
        target_date=None,
        lookahead_days=-1,
        placeholders_enabled=True,
        now_date=date(2026, 3, 28),
    )
    assert value == DETERMINATION_NEEDS


def test_compute_determination_positive_lookahead_outside_window():
    value = determiner._compute_determination(
        has_placeholder=False,
        has_file=False,
        is_deleted=False,
        target_date=date(2026, 6, 1),
        lookahead_days=30,
        placeholders_enabled=True,
        now_date=date(2026, 3, 28),
    )
    assert value == DETERMINATION_NOT_NEEDED


def test_preferred_movie_release_date_strict_no_cross_type_fallback(monkeypatch):
    movie = SimpleNamespace(
        theater_release_date=date(2026, 4, 20),
        digital_release_date=None,
        physical_release_date=None,
    )

    monkeypatch.setattr(determiner.settings, "PREFERRED_MOVIE_DATE_TYPE", "digitalRelease")
    assert determiner._preferred_movie_release_date(movie) is None


def test_preferred_movie_release_date_returns_selected_type_date(monkeypatch):
    movie = SimpleNamespace(
        theater_release_date=None,
        digital_release_date=date(2026, 4, 20),
        physical_release_date=None,
    )

    monkeypatch.setattr(determiner.settings, "PREFERRED_MOVIE_DATE_TYPE", "digitalRelease")
    assert determiner._preferred_movie_release_date(movie) == date(2026, 4, 20)


def test_compute_determination_infinite_lookahead_keeps_existing_placeholder():
    value = determiner._compute_determination(
        has_placeholder=True,
        has_file=False,
        is_deleted=False,
        target_date=None,
        lookahead_days=-1,
        placeholders_enabled=True,
        now_date=date(2026, 3, 28),
    )
    assert value == DETERMINATION_EXISTS
