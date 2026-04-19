from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from core.logger import logger
from services.placeholders import remove_nfo_sidecar, remove_placeholder_file
from services.postgres.db import get_session
from services.postgres.models import Episode, Placeholder, Season
from services.source_of_truth.arr_share_guard import (
	active_sibling_series_exists,
	configured_instance_keys,
	filter_episode_disk_cleanup_paths,
)
from services.source_of_truth.filesystem import (
	is_path_under_configured_roots,
	is_path_under_movie_library_roots,
	is_path_under_tv_library_roots,
)
from services.media_servers.refresh import refresh_selected_sections


def _unique_paths(candidate_paths: list[str]) -> list[str]:
	seen: set[str] = set()
	ordered: list[str] = []
	for path in candidate_paths:
		if not path:
			continue
		normalized = os.path.abspath(path)
		if normalized in seen:
			continue
		seen.add(normalized)
		ordered.append(normalized)
	return ordered


def _nearest_existing_dir(path: str | None) -> str | None:
	if not path:
		return None
	current = os.path.abspath(path)
	while True:
		if os.path.isdir(current):
			return current
		parent = os.path.dirname(current)
		if parent == current:
			return None
		current = parent


def _remove_dir_if_empty(path: str | None) -> bool:
	if not path or not os.path.isdir(path):
		return False
	if os.listdir(path):
		return False
	os.rmdir(path)
	return True


def _remove_series_nfo(series_folder: str | None) -> bool:
	if not series_folder:
		return False
	nfo_path = os.path.join(series_folder, "tvshow.nfo")
	if not os.path.isfile(nfo_path):
		return False
	os.remove(nfo_path)
	return True


def _prune_empty_tree(root_dir: str | None) -> int:
	if not root_dir or not os.path.isdir(root_dir):
		return 0
	deleted = 0
	for dirpath, _, _ in os.walk(root_dir, topdown=False):
		if _remove_dir_if_empty(dirpath):
			deleted += 1
	return deleted


def _series_has_active_episode_placeholders(session, *, series_id: int) -> bool:
	count = (
		session.query(Placeholder.id)
		.join(Episode, Placeholder.episode_id == Episode.id)
		.join(Season, Episode.season_id == Season.id)
		.filter(Season.series_id == int(series_id))
		.filter(Placeholder.has_placeholder == True)  # noqa: E712
		.count()
	)
	return bool(count)


def _derive_series_folder(series: Any, season: Any, candidate_paths: list[str]) -> str | None:
	series_folder = getattr(series, "placeholder_folder", None)
	if series_folder:
		return os.path.abspath(series_folder)
	season_folder = getattr(season, "placeholder_folder", None)
	if season_folder:
		return os.path.dirname(os.path.abspath(season_folder))
	for path in _unique_paths(candidate_paths):
		season_folder = os.path.dirname(path)
		if season_folder:
			return os.path.dirname(season_folder)
	return None


def cleanup_movie_placeholder_files(*, candidate_paths: list[str]) -> dict[str, Any]:
	deleted_any = False
	nfo_deleted_any = False
	directories_deleted = 0
	refresh_paths: set[str] = set()
	paths = _unique_paths(candidate_paths)

	for path in paths:
		deleted_any = remove_placeholder_file(path) or deleted_any
		nfo_deleted_any = remove_nfo_sidecar(path) or nfo_deleted_any

	for path in paths:
		movie_folder = os.path.dirname(path)
		if _remove_dir_if_empty(movie_folder):
			directories_deleted += 1
		refresh_path = _nearest_existing_dir(movie_folder)
		if refresh_path:
			refresh_paths.add(refresh_path)

	return {
		"deleted": deleted_any,
		"nfo_deleted": nfo_deleted_any,
		"directories_deleted": directories_deleted,
		"series_nfo_deleted": False,
		"refresh_paths": sorted(refresh_paths),
	}


def cleanup_episode_placeholder_files(
	session,
	*,
	season: Any,
	series: Any,
	candidate_paths: list[str],
) -> dict[str, Any]:
	deleted_any = False
	nfo_deleted_any = False
	series_nfo_deleted = False
	directories_deleted = 0
	refresh_paths: set[str] = set()
	paths = _unique_paths(candidate_paths)
	paths = filter_episode_disk_cleanup_paths(session, series=series, paths=paths)

	for path in paths:
		deleted_any = remove_placeholder_file(path) or deleted_any
		nfo_deleted_any = remove_nfo_sidecar(path) or nfo_deleted_any

	season_folders = {os.path.dirname(path) for path in paths if path}
	candidate_series_folders = {
		os.path.dirname(folder)
		for folder in season_folders
		if folder and os.path.dirname(folder)
	}
	series_folder = _derive_series_folder(series, season, paths)
	if series_folder:
		candidate_series_folders.add(series_folder)
	series_has_placeholders = _series_has_active_episode_placeholders(session, series_id=int(series.id))
	allowed_son = configured_instance_keys("sonarr")
	tvdbid = int(getattr(series, "tvdbid", 0) or 0)
	if allowed_son and tvdbid:
		if active_sibling_series_exists(
			session,
			exclude_series_id=int(series.id),
			tvdbid=tvdbid,
			allowed_instance_keys=allowed_son,
		):
			series_has_placeholders = True

	if not series_has_placeholders:
		for folder in sorted(candidate_series_folders):
			series_nfo_deleted = _remove_series_nfo(folder) or series_nfo_deleted
			directories_deleted += _prune_empty_tree(folder)
			refresh_path = _nearest_existing_dir(folder)
			if refresh_path:
				refresh_paths.add(refresh_path)
	else:
		for season_folder in season_folders:
			if _remove_dir_if_empty(season_folder):
				directories_deleted += 1
			refresh_path = _nearest_existing_dir(season_folder)
			if refresh_path:
				refresh_paths.add(refresh_path)

	return {
		"deleted": deleted_any,
		"nfo_deleted": nfo_deleted_any,
		"directories_deleted": directories_deleted,
		"series_nfo_deleted": series_nfo_deleted,
		"refresh_paths": sorted(refresh_paths),
	}


_ORPHAN_EPISODE_PLACEHOLDER_NAME_RE = re.compile(r" -\s*s\d+e\d+\s*-\s*", re.IGNORECASE)


def _looks_like_materialized_tv_placeholder_filename(filename: str) -> bool:
	"""Match episode placeholder filenames we create (… - s01e01 - Title.mp4)."""
	return bool(filename and _ORPHAN_EPISODE_PLACEHOLDER_NAME_RE.search(filename))


def _looks_like_materialized_movie_placeholder_path(path: str) -> bool:
	"""Movie placeholders live under a ``{tmdb-…}`` folder."""
	return "{tmdb-" in (path or "")


def _season_parent_is_named_season(folder: str) -> bool:
	base = os.path.basename(folder or "")
	return bool(re.match(r"^Season\s+\d+", base, re.IGNORECASE))


def run_orphan_placeholder_cleanup(
	*,
	grace_seconds: int = 900,
	dry_run: bool = False,
) -> dict[str, Any]:
	"""Remove placeholder files + DB rows with no movie/series/season/episode link.

	Rows must be under configured library roots, older than ``grace_seconds``,
	and match materialized naming (TV: ``… - s01e01 - …``; movies: path contains
	``{tmdb-``) so arbitrary user media is not deleted.

	After filesystem cleanup, issues a section refresh when anything was removed.
	"""
	stats: dict[str, Any] = {
		"candidates": 0,
		"removed_rows": 0,
		"removed_files": 0,
		"removed_nfos": 0,
		"skipped_outside_roots": 0,
		"skipped_too_new": 0,
		"skipped_name_guard": 0,
		"directories_deleted": 0,
		"series_nfo_deleted": 0,
		"prune_errors": 0,
		"row_errors": 0,
		"dry_run": dry_run,
	}
	cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(0, int(grace_seconds)))
	session = get_session()
	touched_tv = False
	touched_movie = False
	deleted_tv_paths: list[str] = []
	deleted_movie_paths: list[str] = []
	series_folders_to_prune: set[str] = set()
	movie_folders_to_prune: set[str] = set()

	try:
		q = (
			session.query(Placeholder)
			.filter(Placeholder.has_placeholder == True)  # noqa: E712
			.filter(Placeholder.movie_id.is_(None))
			.filter(Placeholder.series_id.is_(None))
			.filter(Placeholder.season_id.is_(None))
			.filter(Placeholder.episode_id.is_(None))
			.filter(Placeholder.path.isnot(None))
		)
		candidates = [row for row in q.all() if str(getattr(row, "path", "") or "").strip()]
		stats["candidates"] = len(candidates)

		for row in candidates:
			path = str(row.path or "").strip()
			try:
				if not is_path_under_configured_roots(path):
					stats["skipped_outside_roots"] += 1
					continue

				created = getattr(row, "created_at", None)
				if created is not None:
					ts = created
					if getattr(ts, "tzinfo", None) is None:
						ts = ts.replace(tzinfo=timezone.utc)
					if ts > cutoff:
						stats["skipped_too_new"] += 1
						continue

				base = os.path.basename(path)
				tv_ok = is_path_under_tv_library_roots(path) and _looks_like_materialized_tv_placeholder_filename(base)
				movie_ok = is_path_under_movie_library_roots(path) and _looks_like_materialized_movie_placeholder_path(path)
				if not tv_ok and not movie_ok:
					stats["skipped_name_guard"] += 1
					continue

				if dry_run:
					stats["removed_rows"] += 1
					continue

				had_media = os.path.isfile(path)
				if had_media:
					remove_placeholder_file(path)
				if remove_nfo_sidecar(path):
					stats["removed_nfos"] += 1
				if os.path.isfile(path):
					# File still present (e.g. permission); keep DB row.
					stats["row_errors"] += 1
					logger.warning(
						f"Orphan placeholder cleanup could not remove file; keeping row id={row.id} path={path!r}",
						extra={"emoji_type": "warning"},
					)
					continue

				if had_media:
					stats["removed_files"] += 1
				session.delete(row)
				stats["removed_rows"] += 1

				if tv_ok:
					touched_tv = True
					deleted_tv_paths.append(path)
					season_folder = os.path.dirname(path)
					if _season_parent_is_named_season(season_folder):
						sf = os.path.dirname(season_folder)
						if sf:
							series_folders_to_prune.add(os.path.abspath(sf))
				else:
					touched_movie = True
					deleted_movie_paths.append(path)
					mf = os.path.dirname(path)
					if mf:
						movie_folders_to_prune.add(os.path.abspath(mf))
			except Exception as exc:
				stats["row_errors"] += 1
				logger.warning(
					f"Orphan placeholder cleanup failed for id={getattr(row, 'id', None)} path={path!r}: {exc}",
					extra={"emoji_type": "warning"},
				)

		session.commit()

		for folder in sorted(series_folders_to_prune):
			try:
				norm = folder.replace("\\", "/").rstrip("/")
				like_lit = f"{norm}/%"
				remaining = (
					session.query(Placeholder.id)
					.filter(Placeholder.has_placeholder == True)  # noqa: E712
					.filter(Placeholder.path.isnot(None))
					.filter(Placeholder.path.like(like_lit))
					.first()
				)
				if remaining:
					continue
				if _remove_series_nfo(folder):
					stats["series_nfo_deleted"] += 1
				stats["directories_deleted"] += int(_prune_empty_tree(folder))
			except Exception as exc:
				stats["prune_errors"] += 1
				logger.warning(
					f"Orphan placeholder series-folder prune failed for {folder!r}: {exc}",
					extra={"emoji_type": "warning"},
				)

		for folder in sorted(movie_folders_to_prune):
			try:
				if _remove_dir_if_empty(folder):
					stats["directories_deleted"] += 1
			except Exception as exc:
				stats["prune_errors"] += 1
				logger.warning(
					f"Orphan placeholder movie-folder prune failed for {folder!r}: {exc}",
					extra={"emoji_type": "warning"},
				)

		session.commit()

		if not dry_run and (touched_tv or touched_movie):
			try:
				refresh_selected_sections(
					touched_movie,
					touched_tv,
					include_plex=True,
					include_jellyfin=True,
					include_emby=True,
					bypass_suppression=True,
				)
			except Exception as exc:
				logger.warning(
					f"Orphan placeholder cleanup: library refresh failed: {exc}",
					extra={"emoji_type": "warning"},
				)

		logger.info(f"Orphan placeholder cleanup complete: {stats}", extra={"emoji_type": "success"})
		return stats
	except Exception:
		session.rollback()
		raise
	finally:
		session.close()