"""Curated defaults for Discover-style Collection Sets (issue #201)."""

from __future__ import annotations

# Common Radarr studio strings (include mode when catalog is still sparse).
MAJOR_MOVIE_STUDIOS: tuple[str, ...] = (
    "Warner Bros. Pictures",
    "Universal Pictures",
    "Paramount Pictures",
    "Walt Disney Pictures",
    "Sony Pictures",
    "20th Century Studios",
    "Lionsgate",
    "A24",
    "Metro-Goldwyn-Mayer",
    "DreamWorks Pictures",
)

# Common Sonarr network strings.
MAJOR_TV_NETWORKS: tuple[str, ...] = (
    "HBO",
    "Netflix",
    "Apple TV+",
    "Amazon",
    "Prime Video",
    "Disney+",
    "Hulu",
    "Paramount+",
    "Peacock",
    "AMC",
    "FX",
    "BBC One",
    "BBC",
    "National Geographic",
)

DISCOVER_AUTO_ADD_TAG = "placeholdarr-discover"
DISCOVER_PRESET_DEFAULT_LIMIT = 20

# TMDB-aligned genre starter sets (names must match TMDB genre/list).
DISCOVER_GENRE_PRESETS_MOVIE: dict[str, tuple[str, ...]] = {
    "popular": ("Action", "Comedy", "Drama", "Horror", "Science Fiction"),
    "latest": ("Thriller", "Romance", "Animation", "Action", "Science Fiction"),
    "upcoming": ("Family", "Animation", "Science Fiction", "Fantasy", "Adventure"),
    "all_time_greats": ("Drama", "History", "War", "Mystery", "Adventure", "Crime"),
}

DISCOVER_GENRE_PRESETS_SHOW: dict[str, tuple[str, ...]] = {
    "popular": ("Drama", "Comedy", "Documentary", "Crime", "Sci-Fi & Fantasy"),
    "latest": ("Reality", "Animation", "Mystery", "Drama", "Comedy"),
    "upcoming": ("Drama", "Comedy", "Sci-Fi & Fantasy", "Animation", "Mystery"),
    "all_time_greats": ("Drama", "Crime", "Mystery", "War & Politics", "Western"),
}

DISCOVER_STUDIO_PRESETS_MOVIE: dict[str, tuple[str, ...]] = {
    "popular": MAJOR_MOVIE_STUDIOS[:3],
    "latest": ("A24", "Neon", "Annapurna Pictures", "Legendary Pictures", "Blumhouse Productions"),
    "upcoming": ("Marvel Studios", "Lucasfilm", "Pixar", "Legendary Pictures", "Universal Pictures"),
    "all_time_greats": (
        "Warner Bros. Pictures",
        "Metro-Goldwyn-Mayer",
        "Universal Pictures",
        "Sony Pictures",
        "Walt Disney Pictures",
    ),
}

# Picker pools beyond catalog (TMDB-style company and network names).
EXPANDED_MOVIE_STUDIOS: tuple[str, ...] = MAJOR_MOVIE_STUDIOS + (
    "Columbia Pictures",
    "New Line Cinema",
    "Marvel Studios",
    "Lucasfilm",
    "Pixar",
    "Regency Enterprises",
    "Legendary Pictures",
    "Blumhouse Productions",
    "Studio Ghibli",
    "Focus Features",
    "Searchlight Pictures",
    "Annapurna Pictures",
    "Miramax",
    "Bron Studios",
    "Neon",
    "STX Entertainment",
    "Open Road Films",
)

EXPANDED_TV_NETWORKS: tuple[str, ...] = MAJOR_TV_NETWORKS + (
    "Showtime",
    "Starz",
    "Adult Swim",
    "Cartoon Network",
    "NBC",
    "CBS",
    "ABC",
    "FOX",
    "The CW",
    "ITV",
    "Sky",
    "Channel 4",
    "Crunchyroll",
    "Syfy",
    "TNT",
    "TBS",
    "Comedy Central",
    "MTV",
    "Nickelodeon",
    "Discovery Channel",
)


def merge_picker_values(*lists: list[str]) -> list[str]:
    """Union catalog values with curated/TMDB lists; case-insensitive dedupe, sorted for UI."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for raw in lst:
            text = str(raw or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
    return sorted(out, key=lambda s: s.casefold())


def tmdb_genre_id_for_label(section_type: str, label: str) -> int | None:
    """Map a display genre name to TMDB genre id (None when unknown or TMDB is off)."""
    from services import tmdb_client

    text = str(label or "").strip()
    if not text or not tmdb_client.tmdb_configured():
        return None
    try:
        kind = "movie" if section_type == "movie" else "tv"
        for row in tmdb_client.fetch_genres(kind):
            if str(row.get("name") or "").strip().casefold() == text.casefold():
                return int(row["id"])
    except (tmdb_client.TmdbError, TypeError, ValueError):
        return None
    return None


def picker_genre_names(section_type: str) -> list[str]:
    """TMDB genre names for movie or TV (empty when TMDB is not configured)."""
    from services import tmdb_client

    if not tmdb_client.tmdb_configured():
        return []
    try:
        kind = "movie" if section_type == "movie" else "tv"
        return [
            str(g.get("name") or "").strip()
            for g in tmdb_client.fetch_genres(kind)
            if str(g.get("name") or "").strip()
        ]
    except tmdb_client.TmdbError:
        return []


def discover_picker_studios() -> list[str]:
    return list(EXPANDED_MOVIE_STUDIOS)


def discover_picker_networks() -> list[str]:
    return list(EXPANDED_TV_NETWORKS)


def discover_genre_presets(section_type: str) -> dict[str, list[str]]:
    raw = DISCOVER_GENRE_PRESETS_SHOW if section_type == "show" else DISCOVER_GENRE_PRESETS_MOVIE
    return {key: list(names) for key, names in raw.items()}


def discover_studio_presets() -> dict[str, list[str]]:
    return {key: list(names) for key, names in DISCOVER_STUDIO_PRESETS_MOVIE.items()}