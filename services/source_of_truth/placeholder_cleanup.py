from __future__ import annotations

import os
from typing import Any

from services.placeholders import remove_nfo_sidecar, remove_placeholder_file
from services.postgres.models import Episode, Placeholder, Season


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

	for path in paths:
		deleted_any = remove_placeholder_file(path) or deleted_any
		nfo_deleted_any = remove_nfo_sidecar(path) or nfo_deleted_any

	season_folders = {os.path.dirname(path) for path in paths if path}
	series_folder = _derive_series_folder(series, season, paths)
	series_has_placeholders = _series_has_active_episode_placeholders(session, series_id=int(series.id))

	if not series_has_placeholders:
		series_nfo_deleted = _remove_series_nfo(series_folder) or series_nfo_deleted
		directories_deleted += _prune_empty_tree(series_folder)
		refresh_path = _nearest_existing_dir(series_folder)
		if refresh_path:
			refresh_paths.add(refresh_path)
	else:
		for season_folder in season_folders:
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