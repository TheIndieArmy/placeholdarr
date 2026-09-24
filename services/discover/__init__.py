"""TMDB Discover catalog mode (movies + show-level TV)."""

from services.discover.mode import get_catalog_mode, is_tmdb_discover_mode
from services.discover.pipeline import run_discover_pipeline, run_discover_startup

__all__ = [
    "get_catalog_mode",
    "is_tmdb_discover_mode",
    "run_discover_pipeline",
    "run_discover_startup",
]
