"""TV placeholder density: how many stub files we write on disk.

Orthogonal to ``TV_PLAY_MODE`` (search width on play). Resolve through these
helpers so a later per-source override is one function change, not a settings
scatter.
"""

from __future__ import annotations

from typing import Literal

from core.config import settings

TvPlaceholderDensity = Literal["episode", "season", "series"]
TvDensityRetireWhen = Literal["when_no_episode_needs_placeholder", "when_any_episode_has_file"]

PLACEHOLDER_KIND_EPISODE = "episode"
PLACEHOLDER_KIND_SERIES_STUB = "series_stub"
PLACEHOLDER_KIND_SEASON_STUB = "season_stub"

# User-facing episode title / filename suffix for season and series density files (Plex NFO + disk).
DENSITY_PLACEHOLDER_TITLE = "Placeholdarr Placeholder"
# Legacy title/filename fragment; keep matching for existing library files.
DENSITY_PLACEHOLDER_TITLE_LEGACY = "Placeholdarr Stub"

_DENSITY_VALUES = frozenset({"episode", "season", "series"})
_RETIRE_VALUES = frozenset({"when_no_episode_needs_placeholder", "when_any_episode_has_file"})


def text_marks_density_placeholder(text: str | None) -> bool:
    """True when Tracearr/Plex/path text marks a Placeholdarr season/series density file."""
    blob = str(text or "").lower()
    if "placeholdarr placeholder" in blob:
        return True
    if "placeholdarr stub" in blob:
        return True
    compact = blob.replace(" ", "")
    return "placeholdarrplaceholder" in compact or "placeholdarrstub" in compact


def tv_placeholder_density() -> TvPlaceholderDensity:
    """Global density (v1). Prefer :func:`tv_placeholder_density_for_series` at call sites."""
    raw = str(getattr(settings, "TV_PLACEHOLDER_DENSITY", "episode") or "episode").strip().lower()
    if raw in _DENSITY_VALUES:
        return raw  # type: ignore[return-value]
    return "episode"


def tv_density_retire_when() -> TvDensityRetireWhen:
    """Global retire-when (v1). Prefer :func:`tv_density_retire_when_for_series` at call sites."""
    raw = str(
        getattr(settings, "TV_DENSITY_RETIRE_WHEN", "when_no_episode_needs_placeholder")
        or "when_no_episode_needs_placeholder"
    ).strip().lower()
    if raw in _RETIRE_VALUES:
        return raw  # type: ignore[return-value]
    return "when_no_episode_needs_placeholder"


def tv_placeholder_density_for_series(series=None) -> TvPlaceholderDensity:
    """Resolve density for a series row. Today: global only; later source override."""
    _ = series
    return tv_placeholder_density()


def tv_density_retire_when_for_series(series=None) -> TvDensityRetireWhen:
    """Resolve retire-when for a series row. Today: global only; later source override."""
    _ = series
    return tv_density_retire_when()


def density_uses_episode_files(series=None) -> bool:
    return tv_placeholder_density_for_series(series) == "episode"


def density_uses_series_stub(series=None) -> bool:
    return tv_placeholder_density_for_series(series) == "series"


def density_uses_season_stub(series=None) -> bool:
    return tv_placeholder_density_for_series(series) == "season"


def density_suppresses_episode_files(series=None) -> bool:
    """True when density is season or series (no per-episode stub files)."""
    return tv_placeholder_density_for_series(series) in ("season", "series")
