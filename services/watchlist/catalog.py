"""Batched catalog join for watchlist rows vs Arr Movie/Series + watchlist targets."""

from __future__ import annotations

from typing import Any, Iterable

from services.postgres.models import Movie, Placeholder, Series


def effective_targets(
    media_type: str,
    *,
    plex_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Watchlist Arr targets for a media type.

    ``plex_user_id`` is reserved for per-user ``settings_override`` later; account
    defaults are used today.
    """
    _ = plex_user_id
    from services.watchlist.sync import arr_targets_for_media

    return arr_targets_for_media("movie" if media_type == "movie" else "show")


def _presence_state(
    *,
    has_file: bool,
    has_placeholder: bool,
    in_catalog: bool,
    is_future: bool = False,
) -> str:
    if not in_catalog:
        return "not_in_library"
    if has_file:
        return "file"
    if has_placeholder:
        return "placeholder"
    # No file and no placeholder: Future (outside Coming Soon window) vs truly Missing.
    if is_future:
        return "future"
    return "missing"


def _movie_instance_payload(m: Movie, *, target_keys: set[str]) -> dict[str, Any]:
    from services.library_future_semantics import movie_row_is_future_outside_lookahead

    key = str(m.instance_key or "").strip()
    has_file = bool(m.has_file)
    has_ph = bool(m.has_placeholder)
    is_future = (not has_file) and (not has_ph) and bool(movie_row_is_future_outside_lookahead(m))
    return {
        "instance_key": key,
        "item_id": int(m.id),
        "item_type": "movie",
        "is_watchlist_target": key in target_keys,
        "has_file": has_file,
        "has_placeholder": has_ph,
        "has_future": is_future,
        "monitored": bool(m.radarr_monitored),
        "state": _presence_state(
            has_file=has_file,
            has_placeholder=has_ph,
            in_catalog=True,
            is_future=is_future,
        ),
    }


def _series_instance_payload(s: Series, *, target_keys: set[str]) -> dict[str, Any]:
    key = str(s.instance_key or "").strip()
    files = int(s.episode_files or 0)
    placeholders = int(s.episode_placeholders or 0)
    missing = int(s.episode_missing or 0)
    future = int(s.episode_future or 0)
    has_file = files > 0 or bool(s.has_files)
    has_ph = placeholders > 0
    # Align with Library shelf: Future only when unresolved is all future (no missing eps).
    is_future = (not has_file) and (not has_ph) and missing == 0 and future > 0
    return {
        "instance_key": key,
        "item_id": int(s.id),
        "item_type": "series",
        "is_watchlist_target": key in target_keys,
        "has_file": has_file,
        "has_placeholder": has_ph,
        "has_missing": missing > 0,
        "has_future": is_future,
        "monitored": bool(s.sonarr_monitored),
        "state": _presence_state(
            has_file=has_file,
            has_placeholder=has_ph,
            in_catalog=True,
            is_future=is_future,
        ),
        "stats": {
            "files": files,
            "placeholders": placeholders,
            "missing": missing,
            "future": future,
            "total": int(s.episode_total or 0),
        },
    }


def _empty_enrichment(media_type: str, target_keys: list[str]) -> dict[str, Any]:
    return {
        "state": "not_in_library",
        "coverage": "none" if target_keys else "none",
        "target_keys": list(target_keys),
        "matched_target_keys": [],
        "missing_target_keys": list(target_keys),
        "other_instance_keys": [],
        "already_on_keys": [],
        "will_add_keys": list(target_keys),
        "instances": [],
        "item_id": None,
        "item_type": "movie" if media_type == "movie" else "series",
        "display_status": None,
        "poster_url": None,
        "stats": None,
    }


def _rollup_state(instances: list[dict[str, Any]]) -> str:
    if not instances:
        return "not_in_library"
    if any(i.get("has_file") for i in instances):
        return "file"
    if any(i.get("has_placeholder") for i in instances):
        return "placeholder"
    if any(i.get("has_future") or i.get("state") == "future" for i in instances):
        return "future"
    return "missing"


def _build_enrichment_for_hits(
    *,
    media_type: str,
    target_keys: list[str],
    instances: list[dict[str, Any]],
    display_status: str | None,
    poster_url: str | None,
) -> dict[str, Any]:
    target_set = {str(k).strip() for k in target_keys if str(k).strip()}
    matched = sorted(
        {str(i["instance_key"]) for i in instances if i.get("is_watchlist_target") and i.get("instance_key")}
    )
    other = sorted(
        {
            str(i["instance_key"])
            for i in instances
            if (not i.get("is_watchlist_target")) and i.get("instance_key")
        }
    )
    missing = [k for k in target_keys if k not in set(matched)]
    if not target_keys:
        coverage = "none"
        will_add: list[str] = []
    elif not matched:
        coverage = "none"
        will_add = list(target_keys)
    elif missing:
        coverage = "partial"
        will_add = list(missing)
    else:
        coverage = "complete"
        will_add = []

    # Presence: prefer matched targets; fall back to any catalog hit (wrong-instance).
    rollup_from = [i for i in instances if i.get("is_watchlist_target")] or instances
    state = _rollup_state(rollup_from)

    primary = None
    for i in instances:
        if i.get("is_watchlist_target"):
            primary = i
            break
    if primary is None and instances:
        primary = instances[0]

    stats = None
    if media_type == "show":
        files = sum(int((i.get("stats") or {}).get("files") or 0) for i in rollup_from)
        placeholders = sum(int((i.get("stats") or {}).get("placeholders") or 0) for i in rollup_from)
        miss = sum(int((i.get("stats") or {}).get("missing") or 0) for i in rollup_from)
        fut = sum(int((i.get("stats") or {}).get("future") or 0) for i in rollup_from)
        stats = {"files": files, "placeholders": placeholders, "missing": miss, "future": fut}
    elif rollup_from:
        stats = {
            "files": 1 if any(i.get("has_file") for i in rollup_from) else 0,
            "placeholders": 1 if any(i.get("has_placeholder") for i in rollup_from) else 0,
            "missing": 1 if state == "missing" else 0,
            "future": 1 if state == "future" else 0,
        }

    already_on = []
    seen: set[str] = set()
    for k in matched + other:
        if k not in seen:
            already_on.append(k)
            seen.add(k)

    return {
        "state": state,
        "coverage": coverage,
        "target_keys": list(target_keys),
        "matched_target_keys": matched,
        "missing_target_keys": missing,
        "other_instance_keys": other,
        "already_on_keys": already_on,
        "will_add_keys": will_add,
        "instances": instances,
        "item_id": int(primary["item_id"]) if primary else None,
        "item_type": "movie" if media_type == "movie" else "series",
        "display_status": display_status,
        "poster_url": poster_url,
        "stats": stats,
    }


def enrich_watchlist_items(session, items: Iterable[Any]) -> dict[int, dict[str, Any]]:
    """Return ``{watchlist_item_id: enrichment}`` for the given ORM rows or dict-like items."""
    rows = list(items)
    if not rows:
        return {}

    movie_tmdbs: set[int] = set()
    movie_imdbs: set[str] = set()
    show_tvdbs: set[int] = set()
    show_tmdbs: set[int] = set()
    show_imdbs: set[str] = set()

    normalized: list[tuple[int, str, int | None, int | None, str | None]] = []
    for row in rows:
        if isinstance(row, dict):
            rid = int(row["id"])
            media_type = str(row.get("media_type") or "movie")
            tmdb_id = row.get("tmdb_id")
            tvdb_id = row.get("tvdb_id")
            imdb_id = row.get("imdb_id")
        else:
            rid = int(row.id)
            media_type = str(row.media_type or "movie")
            tmdb_id = row.tmdb_id
            tvdb_id = row.tvdb_id
            imdb_id = row.imdb_id
        try:
            tmdb_i = int(tmdb_id) if tmdb_id not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            tmdb_i = None
        try:
            tvdb_i = int(tvdb_id) if tvdb_id not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            tvdb_i = None
        imdb_s = str(imdb_id or "").strip() or None
        normalized.append((rid, media_type, tmdb_i, tvdb_i, imdb_s))
        if media_type == "movie":
            if tmdb_i:
                movie_tmdbs.add(tmdb_i)
            if imdb_s:
                movie_imdbs.add(imdb_s)
        else:
            if tvdb_i:
                show_tvdbs.add(tvdb_i)
            if tmdb_i:
                show_tmdbs.add(tmdb_i)
            if imdb_s:
                show_imdbs.add(imdb_s)

    movies: list[Movie] = []
    if movie_tmdbs or movie_imdbs:
        q = session.query(Movie).filter(Movie.is_deleted.is_(False))
        clauses = []
        if movie_tmdbs:
            clauses.append(Movie.tmdbid.in_(list(movie_tmdbs)))
        if movie_imdbs:
            clauses.append(Movie.imdbid.in_(list(movie_imdbs)))
        if len(clauses) == 1:
            movies = q.filter(clauses[0]).all()
        elif clauses:
            from sqlalchemy import or_

            movies = q.filter(or_(*clauses)).all()

    series_rows: list[Series] = []
    if show_tvdbs or show_tmdbs or show_imdbs:
        q = session.query(Series).filter(Series.is_deleted.is_(False))
        clauses = []
        if show_tvdbs:
            clauses.append(Series.tvdbid.in_(list(show_tvdbs)))
        if show_tmdbs:
            clauses.append(Series.sonarr_tmdbid.in_(list(show_tmdbs)))
        if show_imdbs:
            clauses.append(Series.imdbid.in_(list(show_imdbs)))
        if len(clauses) == 1:
            series_rows = q.filter(clauses[0]).all()
        elif clauses:
            from sqlalchemy import or_

            series_rows = q.filter(or_(*clauses)).all()

    movies_by_tmdb: dict[int, list[Movie]] = {}
    movies_by_imdb: dict[str, list[Movie]] = {}
    for m in movies:
        movies_by_tmdb.setdefault(int(m.tmdbid), []).append(m)
        if m.imdbid:
            movies_by_imdb.setdefault(str(m.imdbid).strip(), []).append(m)

    series_by_tvdb: dict[int, list[Series]] = {}
    series_by_tmdb: dict[int, list[Series]] = {}
    series_by_imdb: dict[str, list[Series]] = {}
    for s in series_rows:
        series_by_tvdb.setdefault(int(s.tvdbid), []).append(s)
        if s.sonarr_tmdbid:
            series_by_tmdb.setdefault(int(s.sonarr_tmdbid), []).append(s)
        if s.imdbid:
            series_by_imdb.setdefault(str(s.imdbid).strip(), []).append(s)

    movie_ids = [int(m.id) for m in movies]
    display_by_movie: dict[int, str] = {}
    if movie_ids:
        for ph in (
            session.query(Placeholder)
            .filter(Placeholder.movie_id.in_(movie_ids), Placeholder.has_placeholder.is_(True))
            .all()
        ):
            mid = int(ph.movie_id) if ph.movie_id is not None else None
            if mid is None:
                continue
            status = str(ph.display_status_projected or ph.display_status or "").strip() or None
            if status and mid not in display_by_movie:
                display_by_movie[mid] = status

    movie_targets = [str(t.get("instance_key") or "").strip() for t in effective_targets("movie") if t.get("instance_key")]
    show_targets = [str(t.get("instance_key") or "").strip() for t in effective_targets("show") if t.get("instance_key")]
    movie_target_set = set(movie_targets)
    show_target_set = set(show_targets)

    out: dict[int, dict[str, Any]] = {}
    for rid, media_type, tmdb_i, tvdb_i, imdb_s in normalized:
        if media_type == "movie":
            target_keys = movie_targets
            target_set = movie_target_set
            hits: list[Movie] = []
            seen_ids: set[int] = set()
            for bucket in (
                movies_by_tmdb.get(tmdb_i or -1, []),
                movies_by_imdb.get(imdb_s or "", []),
            ):
                for m in bucket:
                    if int(m.id) in seen_ids:
                        continue
                    seen_ids.add(int(m.id))
                    hits.append(m)
            if not hits:
                out[rid] = _empty_enrichment("movie", target_keys)
                continue
            instances = [_movie_instance_payload(m, target_keys=target_set) for m in hits]
            primary = next((m for m in hits if str(m.instance_key or "") in target_set), hits[0])
            poster = None
            if primary:
                poster = f"/api/library/poster/movie/{int(primary.id)}"
            display = display_by_movie.get(int(primary.id)) if primary else None
            out[rid] = _build_enrichment_for_hits(
                media_type="movie",
                target_keys=target_keys,
                instances=instances,
                display_status=display,
                poster_url=poster,
            )
        else:
            target_keys = show_targets
            target_set = show_target_set
            hits_s: list[Series] = []
            seen_ids = set()
            for bucket in (
                series_by_tvdb.get(tvdb_i or -1, []),
                series_by_tmdb.get(tmdb_i or -1, []),
                series_by_imdb.get(imdb_s or "", []),
            ):
                for s in bucket:
                    if int(s.id) in seen_ids:
                        continue
                    seen_ids.add(int(s.id))
                    hits_s.append(s)
            if not hits_s:
                out[rid] = _empty_enrichment("show", target_keys)
                continue
            instances = [_series_instance_payload(s, target_keys=target_set) for s in hits_s]
            primary_s = next((s for s in hits_s if str(s.instance_key or "") in target_set), hits_s[0])
            poster = f"/api/library/poster/series/{int(primary_s.id)}" if primary_s else None
            out[rid] = _build_enrichment_for_hits(
                media_type="show",
                target_keys=target_keys,
                instances=instances,
                display_status=None,
                poster_url=poster,
            )
    return out


def coverage_for_row(session, row: Any) -> str:
    """Return coverage string for a single watchlist ORM row."""
    enrich = enrich_watchlist_items(session, [row]).get(int(row.id))
    if not enrich:
        return "none"
    return str(enrich.get("coverage") or "none")
