"""Resolve sample art + layout tokens for the poster layout designer (TMDB or library)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.sql.expression import func

from services.postgres.db import get_session
from services.postgres.models import Episode, Movie, Season, Series
from services.poster_layouts import (
    _format_rating_value,
    _format_runtime_minutes,
    _genres_short,
    _token_date,
    resolve_label_tokens,
    series_status_label,
)


def _normalize_kind(kind: str | None) -> str:
    text = str(kind or "movie").strip().lower()
    if text in {"tv", "show", "series"}:
        return "series"
    if text == "episode":
        return "episode"
    return "movie"


def _movie_poster_url(row: Movie) -> str:
    return str(row.localized_poster or row.remote_poster or "").strip()


def _series_poster_url(row: Series) -> str:
    return str(row.localized_poster or row.remote_poster or "").strip()


def _episode_thumb_url(episode: Episode, series: Series | None) -> str:
    still = str(getattr(episode, "sonarr_episode_still", None) or "").strip()
    if still:
        return still
    if series is not None:
        fanart = str(getattr(series, "remote_fanart", None) or "").strip()
        if fanart:
            return fanart
        return _series_poster_url(series)
    return ""


def _year_from_date(raw: Any) -> str:
    text = str(raw or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        return text[:4]
    return ""


def _coming_soon_from_dates(*, kind: str, theatrical: str = "", digital: str = "", air: str = "") -> str:
    """Best-effort Coming Soon / New Ep. / Airing chip for designer previews."""
    want = _normalize_kind(kind)

    def _short(iso: str) -> str:
        text = str(iso or "").strip()
        if len(text) < 10:
            return ""
        try:
            from datetime import date

            d = date.fromisoformat(text[:10])
            return f"{d.strftime('%b')} {d.day}"
        except Exception:
            return text[:10]

    if want == "episode":
        label = _short(air)
        return f"Airing - {label}" if label else "Airing"
    if want == "series":
        label = _short(air or theatrical)
        return f"New Ep. - {label}" if label else "New Ep."
    dig = _short(digital)
    if dig:
        return f"Digital - {dig}"
    the = _short(theatrical)
    if the:
        return f"Theatrical - {the}"
    return ""


def _tokens_from_tmdb_movie(data: dict[str, Any]) -> dict[str, str]:
    title = str(data.get("title") or data.get("original_title") or "").strip()
    release = str(data.get("release_date") or "").strip()
    year = _year_from_date(release)
    genres = _genres_short([g.get("name") for g in (data.get("genres") or []) if isinstance(g, dict)])
    runtime = _format_runtime_minutes(data.get("runtime"))
    rating = _format_rating_value(data.get("vote_average"))
    theatrical = _token_date(release)
    coming = _coming_soon_from_dates(kind="movie", theatrical=theatrical, digital="")
    out: dict[str, str] = {
        "Title": title,
        "Year": year,
        "Certification": "",
        "Genres": genres,
        "Runtime": runtime,
        "TheatricalDate": theatrical,
        "DigitalDate": "",
        "PhysicalDate": "",
        "AirDate": "",
        "SeriesPremiereDate": "",
        "SXXEYY": "",
        "SeriesStatus": "",
        "Rating": rating,
        "RatingSource": "tmdb" if rating else "",
        "ComingSoonOrNewEp": coming,
    }
    if rating:
        out["Rating:tmdb"] = rating
    return out


def _tokens_from_tmdb_tv(data: dict[str, Any], *, episode_name: str = "", air_date: str = "", sxxeyy: str = "") -> dict[str, str]:
    title = str(data.get("name") or data.get("original_name") or "").strip()
    first_air = str(data.get("first_air_date") or "").strip()
    year = _year_from_date(air_date or first_air)
    genres = _genres_short([g.get("name") for g in (data.get("genres") or []) if isinstance(g, dict)])
    episode_run_times = data.get("episode_run_time") if isinstance(data.get("episode_run_time"), list) else []
    runtime_mins = episode_run_times[0] if episode_run_times else None
    runtime = _format_runtime_minutes(runtime_mins)
    rating = _format_rating_value(data.get("vote_average"))
    status = series_status_label(data.get("status"))
    premiere = _token_date(first_air)
    air = _token_date(air_date)
    coming = _coming_soon_from_dates(kind="episode" if (episode_name or sxxeyy) else "series", air=air or premiere)
    out: dict[str, str] = {
        "Title": title if not episode_name else title,
        "Year": year,
        "Certification": "",
        "Genres": genres,
        "Runtime": runtime,
        "TheatricalDate": "",
        "DigitalDate": "",
        "PhysicalDate": "",
        "AirDate": air,
        "SeriesPremiereDate": premiere,
        "SXXEYY": sxxeyy,
        "SeriesStatus": status,
        "Rating": rating,
        "RatingSource": "tmdb" if rating else "",
        "ComingSoonOrNewEp": coming,
    }
    if rating:
        out["Rating:tmdb"] = rating
    # Episode sample chips historically show the episode name as Title.
    if episode_name:
        out["Title"] = episode_name
    return out


def _payload(*, kind: str, tmdb_id: int | None, poster_url: str, title: str, source: str, tokens: dict[str, str]) -> dict[str, Any]:
    cleaned = {str(k): str(v) for k, v in (tokens or {}).items() if str(v or "").strip()}
    if title and "Title" not in cleaned:
        cleaned["Title"] = title
    return {
        "ok": True,
        "kind": kind,
        "tmdb_id": tmdb_id,
        "poster_url": poster_url,
        "title": title or cleaned.get("Title") or "",
        "source": source,
        "tokens": cleaned,
    }


def resolve_from_tmdb(*, kind: str, tmdb_id: int) -> dict[str, Any]:
    """Resolve designer board art + sample tokens from a TMDB id."""
    from services import tmdb_client

    want = _normalize_kind(kind)
    tid = int(tmdb_id)
    if tid <= 0:
        return {"ok": False, "message": "Enter a positive TMDB id"}
    if not tmdb_client.tmdb_configured():
        return {"ok": False, "message": "TMDB API key is not configured"}
    try:
        if want == "movie":
            data = tmdb_client._request(f"/movie/{tid}")  # noqa: SLF001
            url = tmdb_client.poster_image_url(data.get("poster_path"))
            tokens = _tokens_from_tmdb_movie(data if isinstance(data, dict) else {})
            title = tokens.get("Title") or ""
        else:
            data = tmdb_client._request(f"/tv/{tid}")  # noqa: SLF001
            title = str((data or {}).get("name") or (data or {}).get("original_name") or "").strip()
            episode_name = ""
            air_date = ""
            sxxeyy = ""
            url = tmdb_client.poster_image_url((data or {}).get("poster_path"))
            if want == "episode":
                try:
                    season = tmdb_client._request(f"/tv/{tid}/season/1")  # noqa: SLF001
                except tmdb_client.TmdbError:
                    season = {}
                episodes = season.get("episodes") if isinstance(season, dict) else None
                if isinstance(episodes, list):
                    for ep in episodes:
                        if not isinstance(ep, dict):
                            continue
                        still = tmdb_client.poster_image_url(ep.get("still_path"), size="w780")
                        if still:
                            url = still
                            episode_name = str(ep.get("name") or "").strip()
                            air_date = str(ep.get("air_date") or "").strip()
                            sn = int(ep.get("season_number") or 1)
                            en = int(ep.get("episode_number") or 0)
                            sxxeyy = f"S{sn:02d}E{en:02d}" if sn or en else ""
                            break
            tokens = _tokens_from_tmdb_tv(
                data if isinstance(data, dict) else {},
                episode_name=episode_name,
                air_date=air_date,
                sxxeyy=sxxeyy,
            )
            if not title:
                title = tokens.get("Title") or ""
    except tmdb_client.TmdbError as exc:
        return {"ok": False, "message": str(exc)}
    if not url:
        return {"ok": False, "message": "No poster art found for that TMDB id"}
    return _payload(kind=want, tmdb_id=tid, poster_url=url, title=title, source="tmdb", tokens=tokens)


def resolve_random_from_library(*, kind: str) -> dict[str, Any]:
    """Pick random library art + tokens matching movie / TV / episode."""
    want = _normalize_kind(kind)
    session = get_session()
    try:
        if want == "movie":
            row = (
                session.query(Movie)
                .filter(
                    Movie.is_deleted == False,  # noqa: E712
                    ((Movie.localized_poster.isnot(None)) & (Movie.localized_poster != ""))
                    | ((Movie.remote_poster.isnot(None)) & (Movie.remote_poster != "")),
                )
                .order_by(func.random())
                .first()
            )
            if row is None:
                return {"ok": False, "message": "No movie posters found in the library"}
            url = _movie_poster_url(row)
            theatrical = _token_date(getattr(row, "theater_release_date", None))
            digital = _token_date(getattr(row, "digital_release_date", None))
            coming = _coming_soon_from_dates(kind="movie", theatrical=theatrical, digital=digital)
            tokens = resolve_label_tokens(media_kind="movie", movie=row, coming_soon_label=coming)
            return _payload(
                kind=want,
                tmdb_id=int(row.tmdbid) if row.tmdbid else None,
                poster_url=url,
                title=str(row.title or "").strip(),
                source="library",
                tokens=tokens,
            )

        if want == "series":
            row = (
                session.query(Series)
                .filter(
                    Series.is_deleted == False,  # noqa: E712
                    ((Series.localized_poster.isnot(None)) & (Series.localized_poster != ""))
                    | ((Series.remote_poster.isnot(None)) & (Series.remote_poster != "")),
                )
                .order_by(func.random())
                .first()
            )
            if row is None:
                return {"ok": False, "message": "No TV posters found in the library"}
            url = _series_poster_url(row)
            premiere = _token_date(getattr(row, "sonarr_first_aired", None))
            coming = _coming_soon_from_dates(kind="series", air=premiere)
            tokens = resolve_label_tokens(media_kind="series", series=row, coming_soon_label=coming)
            tid = int(row.sonarr_tmdbid) if row.sonarr_tmdbid else None
            return _payload(
                kind=want,
                tmdb_id=tid,
                poster_url=url,
                title=str(row.title or "").strip(),
                source="library",
                tokens=tokens,
            )

        ep = (
            session.query(Episode)
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .filter(
                Episode.is_deleted == False,  # noqa: E712
                Season.is_deleted == False,  # noqa: E712
                Series.is_deleted == False,  # noqa: E712
                Episode.sonarr_episode_still.isnot(None),
                Episode.sonarr_episode_still != "",
            )
            .order_by(func.random())
            .first()
        )
        if ep is None:
            ep = (
                session.query(Episode)
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
                .filter(
                    Episode.is_deleted == False,  # noqa: E712
                    Season.is_deleted == False,  # noqa: E712
                    Series.is_deleted == False,  # noqa: E712
                )
                .order_by(func.random())
                .first()
            )
        if ep is None:
            return {"ok": False, "message": "No episode thumbs found in the library"}
        season = session.query(Season).filter(Season.id == ep.season_id).first()
        series = (
            session.query(Series).filter(Series.id == season.series_id).first()
            if season is not None
            else None
        )
        url = _episode_thumb_url(ep, series)
        if not url:
            return {"ok": False, "message": "No episode thumbs found in the library"}
        air = _token_date(getattr(ep, "air_date", None))
        coming = _coming_soon_from_dates(kind="episode", air=air)
        tokens = resolve_label_tokens(
            media_kind="episode",
            series=series,
            season=season,
            episode=ep,
            coming_soon_label=coming,
        )
        # Designer episode samples show episode title; bake uses series title. Prefer episode name here.
        ep_title = str(getattr(ep, "title", "") or "").strip()
        if ep_title:
            tokens["Title"] = ep_title
        tid = int(series.sonarr_tmdbid) if series is not None and series.sonarr_tmdbid else None
        display = ep_title
        if series is not None and series.title:
            display = f"{series.title}: {ep_title}".strip(": ")
        return _payload(
            kind=want,
            tmdb_id=tid,
            poster_url=url,
            title=display,
            source="library",
            tokens=tokens,
        )
    finally:
        session.close()
