"""Pre-discovery DB reconciliation for lite startup: collect row ids that need a determination pass."""

from __future__ import annotations

from sqlalchemy import or_

from core.config import settings
from core.logger import logger
from services.postgres.db import get_session
from services.postgres.models import AppConfig, Episode, Movie, Season, Series
from services.source_of_truth.arr_api import fetch_sonarr_series
from services.source_of_truth.sync_runner import sync_sonarr_series_by_ids

# Bound work per lite run; full passes still cover anything missed.
_LITE_RECON_PER_QUERY_CAP = 8000
_SPECIALS_BACKFILL_PENDING_KEY = "INCLUDE_SPECIALS_BACKFILL_PENDING"
_SPECIALS_CATALOG_BACKFILL_DONE_KEY = "SPECIALS_CATALOG_BACKFILL_DONE"


def _configured_instance_keys() -> list[str]:
    keys: list[str] = []
    for item in getattr(settings, "configured_arr_instances", []) or []:
        k = str(item.get("instance_key") or "").strip().lower()
        if k:
            keys.append(k)
    return keys


def run_lite_startup_reconciliation_pre_discovery() -> tuple[list[int], list[int], dict]:
    """Return (movie_row_ids, episode_row_ids, stats) for rows to include before ARR catalog sync writes.

    Patterns are intentionally conservative DB-side checks; determination + materialization
    remain authoritative for final outcomes.
    """
    keys = _configured_instance_keys()
    stats: dict = {
        "movies_placeholder_path_mismatch": 0,
        "episodes_placeholder_path_mismatch": 0,
        "movies_baseline_flags": 0,
        "episodes_baseline_flags": 0,
        "skipped_no_instances": 0,
    }
    if not keys:
        stats["skipped_no_instances"] = 1
        return [], [], stats

    include_specials = bool(getattr(settings, "INCLUDE_SPECIALS", False))

    session = get_session()
    movie_ids: set[int] = set()
    episode_ids: set[int] = set()
    try:
        m_ph = (
            session.query(Movie.id)
            .filter(
                Movie.instance_key.in_(keys),
                Movie.has_placeholder == True,  # noqa: E712
                or_(Movie.placeholder_filepath.is_(None), Movie.placeholder_filepath == ""),
                Movie.is_deleted == False,  # noqa: E712
            )
            .limit(_LITE_RECON_PER_QUERY_CAP)
            .all()
        )
        for (mid,) in m_ph:
            if mid is not None:
                movie_ids.add(int(mid))
        stats["movies_placeholder_path_mismatch"] = len(movie_ids)

        e_ph = (
            session.query(Episode.id)
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .filter(
                Series.instance_key.in_(keys),
                Episode.has_placeholder == True,  # noqa: E712
                or_(Episode.placeholder_filepath.is_(None), Episode.placeholder_filepath == ""),
                Episode.is_deleted == False,  # noqa: E712
            )
            .limit(_LITE_RECON_PER_QUERY_CAP)
            .all()
        )
        ep_ph = {int(eid) for (eid,) in e_ph if eid is not None}
        stats["episodes_placeholder_path_mismatch"] = len(ep_ph)
        episode_ids.update(ep_ph)

        m_triple = (
            session.query(Movie.id)
            .filter(
                Movie.instance_key.in_(keys),
                Movie.has_file == False,  # noqa: E712
                Movie.has_placeholder == False,  # noqa: E712
                Movie.is_deleted == False,  # noqa: E712
            )
            .limit(_LITE_RECON_PER_QUERY_CAP)
            .all()
        )
        m_triple_ids = {int(mid) for (mid,) in m_triple if mid is not None}
        stats["movies_baseline_flags"] = len(m_triple_ids)
        movie_ids.update(m_triple_ids)

        e_triple_q = (
            session.query(Episode.id, Season.season_number)
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .filter(
                Series.instance_key.in_(keys),
                Episode.has_file == False,  # noqa: E712
                Episode.has_placeholder == False,  # noqa: E712
                Episode.is_deleted == False,  # noqa: E712
            )
        )
        if not include_specials:
            e_triple_q = e_triple_q.filter(Season.season_number != 0)
        e_triple = e_triple_q.limit(_LITE_RECON_PER_QUERY_CAP).all()
        e_triple_ids = {int(eid) for (eid, _sn) in e_triple if eid is not None}
        stats["episodes_baseline_flags"] = len(e_triple_ids)
        episode_ids.update(e_triple_ids)

        logger.info(
            "Startup lite · reconciliation (pre-discovery): "
            f"movies={len(movie_ids)} episodes={len(episode_ids)} "
            f"(placeholder_path_mismatch: movies={stats['movies_placeholder_path_mismatch']} "
            f"episodes={stats['episodes_placeholder_path_mismatch']}; "
            f"has_file/has_placeholder/is_deleted all false: movies={stats['movies_baseline_flags']} "
            f"episodes={stats['episodes_baseline_flags']})",
            extra={"emoji_type": "info"},
        )
        return sorted(movie_ids), sorted(episode_ids), stats
    finally:
        session.close()


def mark_specials_backfill_pending(*, enabled: bool) -> None:
    """Persist one-time specials catalog backfill intent when INCLUDE_SPECIALS is toggled on."""
    session = get_session()
    try:
        row = session.query(AppConfig).filter(AppConfig.key == _SPECIALS_BACKFILL_PENDING_KEY).first()
        if not row:
            row = AppConfig(
                key=_SPECIALS_BACKFILL_PENDING_KEY,
                value=bool(enabled),
                value_type="bool",
                restart_required=False,
                description="Internal flag: run one-time Sonarr specials backfill after INCLUDE_SPECIALS enable.",
            )
            session.add(row)
        else:
            row.value = bool(enabled)
            row.value_type = "bool"
            session.add(row)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_specials_backfill_if_pending(*, instances: list[dict]) -> dict:
    """Run Sonarr-wide specials row capture when pending or never completed.

    Row capture is independent of INCLUDE_SPECIALS so DB counts converge with Sonarr.
    INCLUDE_SPECIALS still controls whether determination/materialization creates placeholders.
    """
    stats = {
        "pending": False,
        "done": False,
        "ran": False,
        "instances": 0,
        "series_requested": 0,
        "series_seen": 0,
        "episodes_seen": 0,
        "errors": 0,
        "touched_episode_row_ids": [],
        "determination_episode_row_ids": [],
    }

    session = get_session()
    try:
        pending_row = session.query(AppConfig).filter(AppConfig.key == _SPECIALS_BACKFILL_PENDING_KEY).first()
        done_row = session.query(AppConfig).filter(AppConfig.key == _SPECIALS_CATALOG_BACKFILL_DONE_KEY).first()
        pending = bool(pending_row and bool(pending_row.value))
        done = bool(done_row and bool(done_row.value))
        stats["pending"] = pending
        stats["done"] = done
        should_run = pending or (not done)
        if not should_run:
            return stats

        touched: set[int] = set()
        sonarr_instance_keys: set[str] = set()
        for inst in instances:
            if str(inst.get("arr_type") or "").strip().lower() != "sonarr":
                continue
            stats["instances"] += 1
            instance_key = str(inst.get("instance_key") or "")
            if instance_key:
                sonarr_instance_keys.add(instance_key)
            try:
                api_series = fetch_sonarr_series(inst["base_url"], inst["api_key"]) or []
                series_ids = {
                    int(s.get("id"))
                    for s in api_series
                    if isinstance(s, dict) and s.get("id") is not None
                }
                if not series_ids:
                    continue
                stats["series_requested"] += len(series_ids)
                logger.info(
                    f"Startup lite · specials backfill · {instance_key}: syncing {len(series_ids)} series",
                    extra={"emoji_type": "info"},
                )
                sync_stats = sync_sonarr_series_by_ids(
                    series_ids,
                    base_url=inst["base_url"],
                    api_key=inst["api_key"],
                    instance_key=instance_key,
                )
                stats["series_seen"] += int(sync_stats.get("series_seen", 0) or 0)
                stats["episodes_seen"] += int(sync_stats.get("episodes_seen", 0) or 0)
                for eid in (sync_stats.get("touched_episode_row_ids") or []):
                    if eid is not None:
                        touched.add(int(eid))
            except Exception as exc:
                stats["errors"] += 1
                logger.warning(
                    f"Startup lite · specials backfill failed for {instance_key}: {exc}",
                    extra={"emoji_type": "warning"},
                )

        stats["touched_episode_row_ids"] = sorted(touched)
        if sonarr_instance_keys and bool(getattr(settings, "INCLUDE_SPECIALS", False)):
            special_episode_rows = (
                session.query(Episode.id)
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
                .filter(
                    Series.instance_key.in_(sorted(sonarr_instance_keys)),
                    Season.season_number == 0,
                    Episode.is_deleted == False,  # noqa: E712
                    Episode.has_file == False,  # noqa: E712
                )
                .limit(_LITE_RECON_PER_QUERY_CAP)
                .all()
            )
            stats["determination_episode_row_ids"] = sorted(
                {int(eid) for (eid,) in special_episode_rows if eid is not None}
            )
        stats["ran"] = True
        if pending_row:
            pending_row.value = False
            pending_row.value_type = "bool"
            session.add(pending_row)
        else:
            session.add(
                AppConfig(
                    key=_SPECIALS_BACKFILL_PENDING_KEY,
                    value=False,
                    value_type="bool",
                    restart_required=False,
                    description="Internal flag: one-time specials placeholder backfill request.",
                )
            )
        if done_row:
            done_row.value = True
            done_row.value_type = "bool"
            session.add(done_row)
        else:
            session.add(
                AppConfig(
                    key=_SPECIALS_CATALOG_BACKFILL_DONE_KEY,
                    value=True,
                    value_type="bool",
                    restart_required=False,
                    description="Internal flag: one-time Sonarr specials catalog row capture has completed.",
                )
            )
        session.commit()
        logger.info(
            "Startup lite · specials backfill complete: "
            f"instances={stats['instances']} series_seen={stats['series_seen']} "
            f"episodes_seen={stats['episodes_seen']} errors={stats['errors']} "
            f"include_specials={bool(getattr(settings, 'INCLUDE_SPECIALS', False))}",
            extra={"emoji_type": "success"},
        )
        return stats
    finally:
        session.close()
