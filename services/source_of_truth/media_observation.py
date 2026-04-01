from __future__ import annotations

"""Observer contract: discover media-server identity only.

This module enriches Placeholder rows with media-server identifiers such as
`plex_placeholder_id`, `jellyfin_placeholder_id`, and `emby_placeholder_id`.
It must never mutate `display_status`, `display_reason`, or `display_progress`.

Lifecycle sequencing requirement:
materialize -> observe -> status projection.
Observation is allowed to run repeatedly and via deferred trail retries because
it only enriches identity fields and does not own user-facing status.
"""

import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import requests

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.media_servers.plex_identity import persist_episode_hierarchy_plex_identity, persist_movie_plex_identity
from services.media_servers.plex_lookup import find_show_by_id, get_plex_server
from services.media_servers.plex_status_writer import batch_update_plex_statuses
from services.postgres.models import Episode, Movie, Placeholder, Season, Series



def _safe_now() -> datetime:
	return datetime.now(timezone.utc)


def _set_plex_observed(placeholder: Placeholder, rating_key: str | int) -> None:
	if not rating_key:
		return
	placeholder.plex_placeholder_id = str(rating_key)
	if not placeholder.plex_id_observed_at:
		placeholder.plex_id_observed_at = _safe_now()
	placeholder.media_lookup_error = None
	placeholder.updated_at = func.now()


def _set_jellyfin_observed(placeholder: Placeholder, item_id: str | int) -> None:
	if not item_id:
		return
	placeholder.jellyfin_placeholder_id = str(item_id)
	if not placeholder.jellyfin_id_observed_at:
		placeholder.jellyfin_id_observed_at = _safe_now()
	placeholder.media_lookup_error = None
	placeholder.updated_at = func.now()


def _set_emby_observed(placeholder: Placeholder, item_id: str | int) -> None:
	if not item_id:
		return
	placeholder.emby_placeholder_id = str(item_id)
	if not placeholder.emby_id_observed_at:
		placeholder.emby_id_observed_at = _safe_now()
	placeholder.media_lookup_error = None
	placeholder.updated_at = func.now()


def _is_enabled(service: str) -> bool:
	if service == 'plex':
		return bool(getattr(settings, 'ENABLE_PLEX', False))
	if service == 'jellyfin':
		return bool(getattr(settings, 'ENABLE_JELLYFIN', False))
	if service == 'emby':
		return bool(getattr(settings, 'ENABLE_EMBY', False))
	return False


def _status_updates_mode() -> str:
	mode = str(getattr(settings, 'PLACEHOLDER_STATUS_UPDATES', 'ALL') or 'ALL').strip().upper()
	return mode


def _should_send_status_updates() -> bool:
	return _status_updates_mode() in {'REQUEST', 'ALL'}


def _base_url(service: str) -> str:
	if service == 'jellyfin':
		return str(getattr(settings, 'JELLYFIN_URL', '') or '').rstrip('/')
	if service == 'emby':
		return str(getattr(settings, 'EMBY_URL', '') or '').rstrip('/')
	return ''


def _token(service: str) -> str:
	if service == 'jellyfin':
		return str(getattr(settings, 'JELLYFIN_TOKEN', '') or '')
	if service == 'emby':
		return str(getattr(settings, 'EMBY_TOKEN', '') or '')
	return ''


def _build_url(service: str, endpoint: str) -> str:
	base = _base_url(service)
	clean = endpoint.lstrip('/')
	if service == 'emby':
		if base.endswith('/emby'):
			return f"{base}/{clean}"
		return f"{base}/emby/{clean}"
	return f"{base}/{clean}"


def _session(service: str) -> requests.Session:
	s = requests.Session()
	s.headers.update({
		'X-Emby-Token': _token(service),
		'Accept': 'application/json',
		'Content-Type': 'application/json',
	})
	return s


def _normalize_title(title: str | None) -> str:
	if not title:
		return ''
	text = str(title).strip().lower()
	if text.endswith(')') and '(' in text:
		try:
			open_idx = text.rfind('(')
			year = text[open_idx + 1:-1]
			if len(year) == 4 and year.isdigit():
				return text[:open_idx].strip()
		except Exception:
			pass
	return text


def _extract_guid_numeric(item: Any, provider: str) -> str | None:
	"""Extract provider numeric ID from Plex GUID list.

	Example GUID shape: tmdb://12345, tvdb://67890.
	"""
	try:
		for guid in getattr(item, 'guids', []) or []:
			gid = str(getattr(guid, 'id', '') or '')
			needle = f"{provider.lower()}://"
			if gid.lower().startswith(needle):
				return gid.split('://', 1)[1]
	except Exception:
		return None
	return None


def _extract_path_numeric(item: Any, provider: str) -> str | None:
	"""Extract provider numeric ID from Plex item location paths."""
	needle = f"{provider.lower()}-"
	try:
		for location in getattr(item, 'locations', []) or []:
			loc = str(location or '').lower()
			idx = loc.find(needle)
			if idx < 0:
				continue
			rest = loc[idx + len(needle):]
			num = ''
			for ch in rest:
				if ch.isdigit():
					num += ch
				else:
					break
			if num:
				return num
	except Exception:
		return None
	return None


def _normalize_path(path: str | None) -> str:
	if not path:
		return ''
	text = str(path).strip().replace('\\', '/').lower()
	while '//' in text:
		text = text.replace('//', '/')
	return text.rstrip('/')


def _path_suffix_key(path: str | None, depth: int) -> str | None:
	norm = _normalize_path(path)
	if not norm or depth <= 0:
		return None
	parts = [p for p in norm.split('/') if p]
	if len(parts) < depth:
		return None
	return '/'.join(parts[-depth:])


def _append_item_to_suffix_index(index: dict[str, list[Any]], item: Any, location: str | None, depth: int) -> None:
	key = _path_suffix_key(location, depth)
	if not key:
		return
	index_key = f"{depth}:{key}"
	existing = index.setdefault(index_key, [])
	item_rating_key = str(getattr(item, 'ratingKey', '') or '')
	for current in existing:
		if str(getattr(current, 'ratingKey', '') or '') == item_rating_key:
			return
	existing.append(item)


def _single_match_from_suffix_index(
	index: dict[str, list[Any]],
	target_path: str | None,
	depths: tuple[int, ...],
) -> Any | None:
	for depth in depths:
		key = _path_suffix_key(target_path, depth)
		if not key:
			continue
		candidates = index.get(f"{depth}:{key}", [])
		if len(candidates) == 1:
			return candidates[0]
		if len(candidates) > 1:
			return None
	return None


def _single_match_from_item_locations(items: list[Any], target_path: str | None, depths: tuple[int, ...]) -> Any | None:
	for depth in depths:
		target_key = _path_suffix_key(target_path, depth)
		if not target_key:
			continue
		matches: list[Any] = []
		for item in items:
			for location in getattr(item, 'locations', []) or []:
				if _path_suffix_key(location, depth) == target_key:
					matches.append(item)
					break
		if len(matches) == 1:
			return matches[0]
		if len(matches) > 1:
			return None
	return None


def _plex_item_metadata_ready(item: Any) -> bool:
	"""Legacy-parity readiness gate for status-safe updates.

	We only treat a Plex item as ready when summary metadata is populated,
	which reduces the chance of immediate status updates being overwritten by
	later metadata hydration.
	"""
	try:
		summary = str(getattr(item, 'summary', '') or '').strip()
		# Must match the field we mutate via editSummary for status updates.
		return bool(summary)
	except Exception:
		return False


def _required_plex_metadata_confirm_polls() -> int:
	"""Return how many consecutive metadata-ready polls are required before status writes."""
	try:
		return max(1, int(getattr(settings, 'PLEX_METADATA_READY_CONFIRM_POLLS', 2)))
	except Exception:
		return 2


def _get_plex_metadata_ready_seen(placeholder: Placeholder) -> int:
	extra = getattr(placeholder, 'extra', None)
	if not isinstance(extra, dict):
		return 0
	try:
		return max(0, int(extra.get('plex_metadata_ready_seen') or 0))
	except Exception:
		return 0


def _set_plex_metadata_ready_seen(placeholder: Placeholder, count: int) -> None:
	extra = getattr(placeholder, 'extra', None)
	data: dict[str, Any] = dict(extra) if isinstance(extra, dict) else {}
	if count <= 0:
		data.pop('plex_metadata_ready_seen', None)
	else:
		data['plex_metadata_ready_seen'] = int(count)
	placeholder.extra = data


def _record_plex_metadata_ready_observation(placeholder: Placeholder, is_ready: bool) -> bool:
	"""Track consecutive metadata-ready observations and return True once confirmed."""
	required = _required_plex_metadata_confirm_polls()
	if required <= 1:
		if not is_ready:
			_set_plex_metadata_ready_seen(placeholder, 0)
		return is_ready

	if not is_ready:
		_set_plex_metadata_ready_seen(placeholder, 0)
		return False

	seen = _get_plex_metadata_ready_seen(placeholder) + 1
	if seen >= required:
		_set_plex_metadata_ready_seen(placeholder, 0)
		return True

	_set_plex_metadata_ready_seen(placeholder, seen)
	return False


def _snapshot_scope_from_placeholders(session, placeholders: list[Placeholder]) -> dict[str, Any]:
	"""Build the minimal DB-backed scope needed for Plex snapshot indexing."""
	movie_ids: set[int] = set()
	episode_ids: set[int] = set()

	for placeholder in placeholders:
		movie_id = getattr(placeholder, 'movie_id', None)
		episode_id = getattr(placeholder, 'episode_id', None)
		if movie_id is not None:
			movie_ids.add(int(movie_id))
		if episode_id is not None:
			episode_ids.add(int(episode_id))

	movies: list[Movie] = []
	series: list[Series] = []

	if movie_ids:
		movies = session.query(Movie).filter(Movie.id.in_(movie_ids)).all()

	if episode_ids:
		episodes = session.query(Episode).filter(Episode.id.in_(episode_ids)).all()
		season_ids = {int(getattr(ep, 'season_id', 0) or 0) for ep in episodes if getattr(ep, 'season_id', None) is not None}
		if season_ids:
			seasons = session.query(Season).filter(Season.id.in_(season_ids)).all()
			series_ids = {int(getattr(season, 'series_id', 0) or 0) for season in seasons if getattr(season, 'series_id', None) is not None}
			if series_ids:
				series = session.query(Series).filter(Series.id.in_(series_ids)).all()

	return {
		'movies': movies,
		'series': series,
	}


def _index_movie_item(snapshot: dict[str, Any], m: Any) -> None:
	tmdb_guid = _extract_guid_numeric(m, 'tmdb')
	if tmdb_guid and tmdb_guid not in snapshot['movies_by_tmdb']:
		snapshot['movies_by_tmdb'][tmdb_guid] = m

	tmdb_path = _extract_path_numeric(m, 'tmdb')
	if tmdb_path and tmdb_path not in snapshot['movies_by_path_tmdb']:
		snapshot['movies_by_path_tmdb'][tmdb_path] = m

	imdb_guid = str(_extract_guid_numeric(m, 'imdb') or '').strip().lower()
	if imdb_guid and imdb_guid not in snapshot['movies_by_imdb']:
		snapshot['movies_by_imdb'][imdb_guid] = m

	norm_title = _normalize_title(getattr(m, 'title', None))
	year = int(getattr(m, 'year', 0) or 0)
	for location in getattr(m, 'locations', []) or []:
		_append_item_to_suffix_index(snapshot['movies_by_path_suffix'], m, location, 3)
		_append_item_to_suffix_index(snapshot['movies_by_path_suffix'], m, location, 2)
	if norm_title:
		snapshot['movies_by_title_year'][(norm_title, year)] = m
		snapshot['movies_by_title'].setdefault(norm_title, []).append(m)


def _index_show_episodes(snapshot: dict[str, Any], show: Any) -> None:
	tvdb_guid = _extract_guid_numeric(show, 'tvdb')
	tvdb_path = _extract_path_numeric(show, 'tvdb')
	show_title = _normalize_title(getattr(show, 'title', None))

	keys: list[str] = []
	if tvdb_guid:
		keys.append(tvdb_guid)
	if tvdb_path and tvdb_path not in keys:
		keys.append(tvdb_path)

	try:
		for plex_season in show.seasons():
			season_num = int(getattr(plex_season, 'index', -1) or -1)
			if season_num < 0:
				continue
			for plex_episode in plex_season.episodes():
				ep_num = int(getattr(plex_episode, 'index', -1) or -1)
				if ep_num < 0:
					continue
				for tvdb in keys:
					snapshot['episodes_by_tvdb_sxe'][(tvdb, season_num, ep_num)] = plex_episode
				if show_title:
					snapshot['episodes_by_series_title_sxe'][(show_title, season_num, ep_num)] = plex_episode
	except Exception:
		return


def _scoped_shows_for_series(tv_section: Any, series_row: Series) -> list[Any]:
	"""Find likely Plex show candidates for one scoped series without scanning the whole section."""
	target_tvdb = str(getattr(series_row, 'tvdbid', '') or '').strip()
	target_title = _normalize_title(getattr(series_row, 'title', None))

	search_results: list[Any] = []
	raw_title = str(getattr(series_row, 'title', '') or '').strip()
	if raw_title:
		try:
			search_results = list(tv_section.search(title=raw_title) or [])
		except Exception:
			search_results = []

	if not search_results and raw_title:
		try:
			candidate = tv_section.get(raw_title)
			if candidate is not None:
				search_results = [candidate]
		except Exception:
			pass

	if not search_results:
		return []

	if target_tvdb:
		matched = []
		for show in search_results:
			guid_id = _extract_guid_numeric(show, 'tvdb')
			if guid_id and str(guid_id) == target_tvdb:
				matched.append(show)
				continue
			path_id = _extract_path_numeric(show, 'tvdb')
			if path_id and str(path_id) == target_tvdb:
				matched.append(show)
		if matched:
			return matched

	if target_title:
		exact_title = [show for show in search_results if _normalize_title(getattr(show, 'title', None)) == target_title]
		if exact_title:
			return exact_title

	return search_results


def _build_plex_snapshot(session, placeholders: list[Placeholder]) -> dict[str, Any] | None:
	"""Build a scoped in-memory Plex snapshot for the passed placeholder set."""
	if not _is_enabled('plex'):
		return None
	plex = get_plex_server()
	if not plex:
		return None
	scope = _snapshot_scope_from_placeholders(session, placeholders)
	scope_movies: list[Movie] = scope.get('movies', [])
	scope_series: list[Series] = scope.get('series', [])

	movie_section = None
	tv_section = None
	if scope_movies:
		try:
			movie_section = plex.library.sectionByID(settings.PLEX_MOVIE_SECTION_ID)
		except Exception:
			movie_section = None
	if scope_series:
		try:
			tv_section = plex.library.sectionByID(settings.PLEX_TV_SECTION_ID)
		except Exception:
			tv_section = None

	snapshot: dict[str, Any] = {
		'movies_by_tmdb': {},
		'movies_by_path_tmdb': {},
		'movies_by_imdb': {},
		'movies_by_path_suffix': {},
		'movies_by_title_year': {},
		'movies_by_title': {},
		'episodes_by_tvdb_sxe': {},
		'episodes_by_series_title_sxe': {},
	}
	if scope_movies and movie_section is not None:
		try:
			all_movies = movie_section.all()
		except Exception:
			all_movies = []
		for m in all_movies:
			_index_movie_item(snapshot, m)

	if scope_series and tv_section is not None:
		seen_show_keys: set[str] = set()
		for series_row in scope_series:
			for show in _scoped_shows_for_series(tv_section, series_row):
				key = str(getattr(show, 'ratingKey', '') or '') or str(id(show))
				if key in seen_show_keys:
					continue
				seen_show_keys.add(key)
				_index_show_episodes(snapshot, show)

	return snapshot


def _resolve_plex_movie_item_from_snapshot(
	snapshot: dict[str, Any],
	movie: Movie,
	observed_path: str | None = None,
	allow_title_fallback: bool = True,
) -> Any | None:
	tmdb_target = str(getattr(movie, 'tmdbid', '') or '')
	if tmdb_target:
		item = snapshot.get('movies_by_tmdb', {}).get(tmdb_target)
		if item:
			return item
		item = snapshot.get('movies_by_path_tmdb', {}).get(tmdb_target)
		if item:
			return item

	imdb_target = str(getattr(movie, 'imdbid', '') or '').strip().lower()
	if imdb_target:
		item = snapshot.get('movies_by_imdb', {}).get(imdb_target)
		if item:
			return item

	path_item = _single_match_from_suffix_index(
		snapshot.get('movies_by_path_suffix', {}),
		observed_path,
		(3, 2),
	)
	if path_item is not None:
		return path_item

	if not allow_title_fallback:
		return None

	norm_title = _normalize_title(getattr(movie, 'title', None))
	target_year = int(getattr(movie, 'year', 0) or 0)
	if norm_title and target_year:
		item = snapshot.get('movies_by_title_year', {}).get((norm_title, target_year))
		if item:
			return item
		# Avoid cross-year title-only false positives for duplicate titles.
		return None

	if norm_title:
		candidates = snapshot.get('movies_by_title', {}).get(norm_title, [])
		if len(candidates) == 1:
			return candidates[0]

	return None


def _normalize_title_tokens(text: str | None) -> list[str]:
	norm = _normalize_title(text)
	if not norm:
		return []
	clean = ''.join(ch if ch.isalnum() else ' ' for ch in norm)
	return [tok for tok in clean.split() if tok]


def _resolve_plex_episode_item_from_snapshot(
	snapshot: dict[str, Any],
	episode: Episode,
	season: Season,
	series: Series,
) -> Any | None:
	season_num = int(getattr(season, 'season_number', 0) or 0)
	ep_num = int(getattr(episode, 'episode_number', 0) or 0)
	tvdb_target = str(getattr(series, 'tvdbid', '') or '')

	if tvdb_target:
		item = snapshot.get('episodes_by_tvdb_sxe', {}).get((tvdb_target, season_num, ep_num))
		if item:
			return item

	show_title = _normalize_title(getattr(series, 'title', None))
	if show_title:
		item = snapshot.get('episodes_by_series_title_sxe', {}).get((show_title, season_num, ep_num))
		if item:
			return item

	return None


def _provider_id(item: dict[str, Any], *keys: str) -> str | None:
	provider_ids = item.get('ProviderIds') or {}
	for key in keys:
		for candidate in (key, key.lower(), key.upper(), key.capitalize()):
			val = provider_ids.get(candidate)
			if val:
				return str(val)
	return None


def _admin_user_id(service: str) -> str | None:
	if not _is_enabled(service):
		return None
	if not _base_url(service) or not _token(service):
		return None
	try:
		sess = _session(service)
		resp = sess.get(_build_url(service, 'Users'), timeout=5)
		resp.raise_for_status()
		users = resp.json() or []
		for u in users:
			if (u.get('Policy') or {}).get('IsAdministrator'):
				uid = u.get('Id')
				if uid:
					return str(uid)
		if users and isinstance(users, list):
			uid = users[0].get('Id')
			return str(uid) if uid else None
		return None
	except Exception:
		return None


def _search_user_items(service: str, user_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
	try:
		sess = _session(service)
		resp = sess.get(_build_url(service, f'Users/{user_id}/Items'), params=params, timeout=8)
		resp.raise_for_status()
		body = resp.json() or {}
		items = body.get('Items') or []
		if isinstance(items, list):
			return items
		return []
	except Exception:
		return []


def _resolve_movie_id(service: str, movie: Movie) -> str | None:
	user_id = _admin_user_id(service)
	if not user_id:
		return None

	params = {
		'searchTerm': getattr(movie, 'title', None) or '',
		'includeItemTypes': 'Movie',
		'recursive': 'true',
		'fields': 'ProviderIds,Name,ProductionYear,Path',
	}
	items = _search_user_items(service, user_id, params)
	if not items:
		return None

	tmdb_target = str(getattr(movie, 'tmdbid', '') or '')
	if tmdb_target:
		for item in items:
			tmdb = _provider_id(item, 'Tmdb')
			if tmdb and tmdb == tmdb_target:
				item_id = item.get('Id')
				return str(item_id) if item_id else None

	target_title = _normalize_title(getattr(movie, 'title', None))
	target_year = int(getattr(movie, 'year', 0) or 0)
	for item in items:
		name = _normalize_title(item.get('Name') or '')
		year = int(item.get('ProductionYear', 0) or 0)
		if name and name == target_title and (not target_year or year == target_year):
			item_id = item.get('Id')
			return str(item_id) if item_id else None

	return None


def _resolve_series_id(service: str, series: Series, user_id: str) -> str | None:
	params = {
		'searchTerm': getattr(series, 'title', None) or '',
		'includeItemTypes': 'Series',
		'recursive': 'true',
		'fields': 'ProviderIds,Name,Path',
	}
	items = _search_user_items(service, user_id, params)
	if not items:
		return None

	tvdb_target = str(getattr(series, 'tvdbid', '') or '')
	if tvdb_target:
		for item in items:
			tvdb = _provider_id(item, 'Tvdb')
			if tvdb and tvdb == tvdb_target:
				item_id = item.get('Id')
				return str(item_id) if item_id else None

	target_title = _normalize_title(getattr(series, 'title', None))
	for item in items:
		name = _normalize_title(item.get('Name') or '')
		if name and name == target_title:
			item_id = item.get('Id')
			return str(item_id) if item_id else None

	return None


def _resolve_episode_id(service: str, episode: Episode, season: Season, series: Series) -> str | None:
	user_id = _admin_user_id(service)
	if not user_id:
		return None

	series_id = _resolve_series_id(service, series, user_id)
	if not series_id:
		return None

	season_params = {
		'ParentId': series_id,
		'IncludeItemTypes': 'Season',
		'Recursive': 'false',
		'fields': 'Name,IndexNumber',
	}
	seasons = _search_user_items(service, user_id, season_params)
	target_season = int(getattr(season, 'season_number', 0) or 0)
	season_id = None
	for s in seasons:
		if int(s.get('IndexNumber', -1) or -1) == target_season:
			season_id = s.get('Id')
			break
	if not season_id:
		return None

	episode_params = {
		'ParentId': str(season_id),
		'IncludeItemTypes': 'Episode',
		'Recursive': 'false',
		'fields': 'Name,IndexNumber',
	}
	episodes = _search_user_items(service, user_id, episode_params)
	target_episode = int(getattr(episode, 'episode_number', 0) or 0)
	for e in episodes:
		if int(e.get('IndexNumber', -1) or -1) == target_episode:
			item_id = e.get('Id')
			return str(item_id) if item_id else None

	return None


def _resolve_jellyfin_movie_id(movie: Movie) -> str | None:
	return _resolve_movie_id('jellyfin', movie)


def _resolve_emby_movie_id(movie: Movie) -> str | None:
	return _resolve_movie_id('emby', movie)


def _resolve_jellyfin_episode_id(episode: Episode, season: Season, series: Series) -> str | None:
	return _resolve_episode_id('jellyfin', episode, season, series)


def _resolve_emby_episode_id(episode: Episode, season: Season, series: Series) -> str | None:
	return _resolve_episode_id('emby', episode, season, series)


def _resolve_plex_movie_id(movie: Movie) -> str | None:
	try:
		found = _resolve_plex_movie_item(movie)
		rating_key = getattr(found, 'ratingKey', None) if found else None
		return str(rating_key) if rating_key else None
	except Exception:
		return None


def _resolve_plex_episode_id(episode: Episode, season: Season, series: Series) -> str | None:
	tvdb_id = getattr(series, 'tvdbid', None)
	if not tvdb_id:
		return None

	try:
		show = find_show_by_id(tvdb_id, title=getattr(series, 'title', None))
		if not show:
			return None

		target_season = int(getattr(season, 'season_number', 0) or 0)
		target_episode = int(getattr(episode, 'episode_number', 0) or 0)

		for plex_season in show.seasons():
			if int(getattr(plex_season, 'index', -1)) != target_season:
				continue
			for plex_episode in plex_season.episodes():
				if int(getattr(plex_episode, 'index', -1)) == target_episode:
					rating_key = getattr(plex_episode, 'ratingKey', None)
					return str(rating_key) if rating_key else None
		return None
	except Exception:
		return None


def _resolve_plex_movie_item(
	movie: Movie,
	observed_path: str | None = None,
	allow_title_fallback: bool = True,
):
	"""Return the resolved Plex movie item (full object) or None."""
	plex = get_plex_server()
	if not plex:
		return None

	try:
		movie_section = plex.library.sectionByID(settings.PLEX_MOVIE_SECTION_ID)
		all_movies = movie_section.all()
	except Exception:
		return None

	tmdb_target = str(getattr(movie, 'tmdbid', '') or '')
	if tmdb_target:
		for item in all_movies:
			guid_tmdb = _extract_guid_numeric(item, 'tmdb')
			if guid_tmdb and guid_tmdb == tmdb_target:
				return item
		for item in all_movies:
			path_tmdb = _extract_path_numeric(item, 'tmdb')
			if path_tmdb and path_tmdb == tmdb_target:
				return item

	imdb_target = str(getattr(movie, 'imdbid', '') or '').strip().lower()
	if imdb_target:
		for item in all_movies:
			guid_imdb = str(_extract_guid_numeric(item, 'imdb') or '').strip().lower()
			if guid_imdb and guid_imdb == imdb_target:
				return item

	path_item = _single_match_from_item_locations(all_movies, observed_path, (3, 2))
	if path_item is not None:
		return path_item

	if not allow_title_fallback:
		return None

	norm_title = _normalize_title(getattr(movie, 'title', None))
	target_year = int(getattr(movie, 'year', 0) or 0)
	if norm_title and target_year:
		# Exact title+year first.
		for item in all_movies:
			item_year = int(getattr(item, 'year', 0) or 0)
			if item_year != target_year:
				continue
			if _normalize_title(getattr(item, 'title', None)) == norm_title:
				return item

		# Loose title token fallback constrained to same year only.
		tokens = set(_normalize_title_tokens(norm_title))
		if tokens:
			matches = []
			for item in all_movies:
				item_year = int(getattr(item, 'year', 0) or 0)
				if item_year != target_year:
					continue
				item_tokens = set(_normalize_title_tokens(getattr(item, 'title', None)))
				if tokens.issubset(item_tokens):
					matches.append(item)
			if len(matches) == 1:
				return matches[0]

		# Do not accept title-only matches across years for ambiguous titles.
		return None

	if norm_title:
		candidates = [item for item in all_movies if _normalize_title(getattr(item, 'title', None)) == norm_title]
		if len(candidates) == 1:
			return candidates[0]

	return None


def _resolve_plex_episode_item(episode: Episode, season: Season, series: Series):
	"""Return the resolved Plex episode item (full object) or None."""
	tvdb_id = getattr(series, 'tvdbid', None)
	if not tvdb_id:
		return None
	try:
		show = find_show_by_id(tvdb_id, title=getattr(series, 'title', None))
		if not show:
			return None
		target_season = int(getattr(season, 'season_number', 0) or 0)
		target_episode = int(getattr(episode, 'episode_number', 0) or 0)
		for plex_season in show.seasons():
			if int(getattr(plex_season, 'index', -1)) != target_season:
				continue
			for plex_ep in plex_season.episodes():
				if int(getattr(plex_ep, 'index', -1)) == target_episode:
					return plex_ep
		return None
	except Exception:
		return None


def observe_placeholder_plex_id(session, placeholder: Placeholder, movie: Movie | None, episode: Episode | None) -> bool:
	"""Attempt to observe and persist Plex ratingKey for the placeholder's media item."""
	placeholder.media_lookup_last_attempt_at = func.now()

	if not getattr(settings, 'ENABLE_PLEX', False):
		session.add(placeholder)
		return False

	rating_key: str | None = None
	try:
		if movie is not None:
			rating_key = _resolve_plex_movie_id(movie)
		elif episode is not None:
			season = session.query(Season).filter(Season.id == int(episode.season_id)).first()
			series = session.query(Series).filter(Series.id == int(season.series_id)).first() if season else None
			if season and series:
				rating_key = _resolve_plex_episode_id(episode, season, series)

		if rating_key:
			_set_plex_observed(placeholder, rating_key)
			# Best-effort summary/status projection in Plex once we have a stable item id.
			# Uses batch updater for efficiency and consistency.
			if _should_send_status_updates():
				try:
					intents = []
					status = _placeholder_display_status(placeholder)
					if movie is not None:
						intents.append({'entity_type': Movie, 'entity_id': int(movie.id), 'status': status})
					elif episode is not None:
						intents.append({'entity_type': Episode, 'entity_id': int(episode.id), 'status': status})
					if intents:
						batch_update_plex_statuses(session, intents)
				except Exception:
					# Keep observation resilient even when Plex summary update fails.
					pass
			session.add(placeholder)
			return True

		placeholder.media_lookup_error = 'plex_not_found'
		session.add(placeholder)
		return False
	except Exception as e:
		placeholder.media_lookup_error = f'plex_lookup_error:{str(e)[:200]}'
		placeholder.updated_at = func.now()
		session.add(placeholder)
		return False


def _is_placeholder_fully_resolved(placeholder: Placeholder) -> bool:
	if _is_enabled('plex'):
		# Must have both identity (ratingKey) and confirmed metadata projection.
		has_plex_id = bool(getattr(placeholder, 'plex_placeholder_id', None))
		if not has_plex_id:
			return False
		if getattr(placeholder, 'media_lookup_error', None) == 'plex_metadata_pending':
			return False

	if _is_enabled('jellyfin') and not bool(getattr(placeholder, 'jellyfin_placeholder_id', None)):
		return False

	if _is_enabled('emby') and not bool(getattr(placeholder, 'emby_placeholder_id', None)):
		return False

	return True


def _placeholder_display_status(placeholder: Placeholder) -> str | None:
	status = getattr(placeholder, 'display_status', None)
	if isinstance(status, str):
		status = status.strip() or None
	reason = getattr(placeholder, 'display_reason', None)
	if isinstance(reason, str):
		reason = reason.strip() or None

	if status in {
		'COMING_SOON',
		'COMING_SOON_30',
		'COMING_SOON_14',
		'COMING_SOON_7',
		'COMING_SOON_1',
		'COMING_SOON_TODAY',
	} and reason:
		return reason

	return status


def _plex_status_intent_for_entity(movie: Movie | None, episode: Episode | None, placeholder: Placeholder) -> dict[str, Any] | None:
	status = _placeholder_display_status(placeholder)
	if movie is not None:
		return {'entity_type': Movie, 'entity_id': int(movie.id), 'status': status}
	if episode is not None:
		return {'entity_type': Episode, 'entity_id': int(episode.id), 'status': status}
	return None


def _dedupe_plex_status_intents(intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
	seen: set[tuple[str, int, str | None]] = set()
	deduped: list[dict[str, Any]] = []
	for intent in intents:
		entity_type = intent.get('entity_type')
		entity_id = int(intent.get('entity_id'))
		status = intent.get('status')
		entity_name = getattr(entity_type, '__name__', str(entity_type))
		key = (entity_name, entity_id, status)
		if key in seen:
			continue
		seen.add(key)
		deduped.append(intent)
	return deduped


def _observation_pass_chunk_size() -> int:
	try:
		return max(1, int(getattr(settings, 'OBSERVATION_PASS_CHUNK_SIZE', 150) or 150))
	except Exception:
		return 150


def _observation_max_pass_seconds() -> float:
	try:
		return max(0.0, float(getattr(settings, 'OBSERVATION_MAX_PASS_SECONDS', 45) or 45))
	except Exception:
		return 45.0


def _timing_ms(started: float) -> int:
	return int(round((time.monotonic() - started) * 1000.0))


def _build_observation_prefetch(session, placeholders: list[Placeholder]) -> dict[str, Any]:
	"""Prefetch DB rows needed during a pass to avoid per-item queries."""
	movie_ids: set[int] = set()
	episode_ids: set[int] = set()
	for placeholder in placeholders:
		movie_id = getattr(placeholder, 'movie_id', None)
		episode_id = getattr(placeholder, 'episode_id', None)
		if movie_id is not None:
			movie_ids.add(int(movie_id))
		if episode_id is not None:
			episode_ids.add(int(episode_id))

	movies = session.query(Movie).filter(Movie.id.in_(movie_ids)).all() if movie_ids else []
	episodes = session.query(Episode).filter(Episode.id.in_(episode_ids)).all() if episode_ids else []

	season_ids = {
		int(getattr(ep, 'season_id', 0) or 0)
		for ep in episodes
		if getattr(ep, 'season_id', None) is not None
	}
	seasons = session.query(Season).filter(Season.id.in_(season_ids)).all() if season_ids else []

	series_ids = {
		int(getattr(season, 'series_id', 0) or 0)
		for season in seasons
		if getattr(season, 'series_id', None) is not None
	}
	series_list = session.query(Series).filter(Series.id.in_(series_ids)).all() if series_ids else []

	return {
		'movies_by_id': {int(m.id): m for m in movies},
		'episodes_by_id': {int(e.id): e for e in episodes},
		'seasons_by_id': {int(s.id): s for s in seasons},
		'series_by_id': {int(sr.id): sr for sr in series_list},
	}


def _build_reverse_snapshot_match_maps(
	prefetch: dict[str, Any],
	plex_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
	"""Build fast DB-targeted match maps by walking snapshot indexes once."""
	result: dict[str, Any] = {
		'movie_plex_item_by_movie_id': {},
		'episode_plex_item_by_episode_id': {},
	}
	if not plex_snapshot:
		return result

	movies_by_id: dict[int, Movie] = prefetch.get('movies_by_id', {})
	episodes_by_id: dict[int, Episode] = prefetch.get('episodes_by_id', {})
	seasons_by_id: dict[int, Season] = prefetch.get('seasons_by_id', {})
	series_by_id: dict[int, Series] = prefetch.get('series_by_id', {})

	# Build DB-side movie key maps.
	movies_by_tmdb: dict[str, list[int]] = {}
	movies_by_imdb: dict[str, list[int]] = {}
	movies_by_title_year: dict[tuple[str, int], list[int]] = {}
	movies_by_title: dict[str, list[int]] = {}
	for movie in movies_by_id.values():
		movie_id = int(movie.id)
		tmdb = str(getattr(movie, 'tmdbid', '') or '').strip()
		if tmdb:
			movies_by_tmdb.setdefault(tmdb, []).append(movie_id)
		imdb = str(getattr(movie, 'imdbid', '') or '').strip().lower()
		if imdb:
			movies_by_imdb.setdefault(imdb, []).append(movie_id)
		norm_title = _normalize_title(getattr(movie, 'title', None))
		year = int(getattr(movie, 'year', 0) or 0)
		if norm_title and year:
			movies_by_title_year.setdefault((norm_title, year), []).append(movie_id)
		if norm_title:
			movies_by_title.setdefault(norm_title, []).append(movie_id)

	# Walk unique movie items discovered in snapshot indexes.
	seen_movie_keys: set[str] = set()
	movie_candidates: list[Any] = []
	for bucket in (
		plex_snapshot.get('movies_by_tmdb', {}),
		plex_snapshot.get('movies_by_path_tmdb', {}),
		plex_snapshot.get('movies_by_imdb', {}),
		plex_snapshot.get('movies_by_title_year', {}),
	):
		for item in bucket.values():
			key = str(getattr(item, 'ratingKey', '') or '') or str(id(item))
			if key in seen_movie_keys:
				continue
			seen_movie_keys.add(key)
			movie_candidates.append(item)

	for item in movie_candidates:
		matched_movie_id: int | None = None
		tmdb = _extract_guid_numeric(item, 'tmdb') or _extract_path_numeric(item, 'tmdb')
		if tmdb:
			ids = movies_by_tmdb.get(str(tmdb), [])
			if len(ids) == 1:
				matched_movie_id = ids[0]

		if matched_movie_id is None:
			imdb = str(_extract_guid_numeric(item, 'imdb') or '').strip().lower()
			if imdb:
				ids = movies_by_imdb.get(imdb, [])
				if len(ids) == 1:
					matched_movie_id = ids[0]

		if matched_movie_id is None:
			norm_title = _normalize_title(getattr(item, 'title', None))
			year = int(getattr(item, 'year', 0) or 0)
			if norm_title and year:
				ids = movies_by_title_year.get((norm_title, year), [])
				if len(ids) == 1:
					matched_movie_id = ids[0]

		if matched_movie_id is None:
			norm_title = _normalize_title(getattr(item, 'title', None))
			if norm_title:
				ids = movies_by_title.get(norm_title, [])
				if len(ids) == 1:
					matched_movie_id = ids[0]

		if matched_movie_id is not None and matched_movie_id not in result['movie_plex_item_by_movie_id']:
			result['movie_plex_item_by_movie_id'][matched_movie_id] = item

	# Build DB-side episode key maps.
	episodes_by_tvdb_sxe: dict[tuple[str, int, int], list[int]] = {}
	episodes_by_title_sxe: dict[tuple[str, int, int], list[int]] = {}
	for episode in episodes_by_id.values():
		episode_id = int(episode.id)
		season = seasons_by_id.get(int(getattr(episode, 'season_id', 0) or 0))
		if season is None:
			continue
		series = series_by_id.get(int(getattr(season, 'series_id', 0) or 0))
		if series is None:
			continue
		season_num = int(getattr(season, 'season_number', 0) or 0)
		ep_num = int(getattr(episode, 'episode_number', 0) or 0)
		tvdb = str(getattr(series, 'tvdbid', '') or '').strip()
		if tvdb:
			episodes_by_tvdb_sxe.setdefault((tvdb, season_num, ep_num), []).append(episode_id)
		series_title = _normalize_title(getattr(series, 'title', None))
		if series_title:
			episodes_by_title_sxe.setdefault((series_title, season_num, ep_num), []).append(episode_id)

	for key, item in (plex_snapshot.get('episodes_by_tvdb_sxe', {}) or {}).items():
		ids = episodes_by_tvdb_sxe.get(key, [])
		if len(ids) == 1 and ids[0] not in result['episode_plex_item_by_episode_id']:
			result['episode_plex_item_by_episode_id'][ids[0]] = item

	for key, item in (plex_snapshot.get('episodes_by_series_title_sxe', {}) or {}).items():
		ids = episodes_by_title_sxe.get(key, [])
		if len(ids) == 1 and ids[0] not in result['episode_plex_item_by_episode_id']:
			result['episode_plex_item_by_episode_id'][ids[0]] = item

	return result


def observe_placeholder_media_ids(
	session,
	placeholder: Placeholder,
	movie: Movie | None,
	episode: Episode | None,
	plex_snapshot: dict[str, Any] | None = None,
	observation_context: dict[str, Any] | None = None,
	allow_title_fallback: bool = True,
) -> dict[str, Any]:
	"""Attempt to observe IDs for all enabled media servers for one placeholder.

	Contract: this function only enriches media-server identity fields and does
	not change Placeholder.display_status or any other user-facing display
	field. It is safe to call repeatedly on the same placeholder.
	"""
	placeholder.media_lookup_last_attempt_at = func.now()
	result = {
		'observed_plex': 0,
		'observed_jellyfin': 0,
		'observed_emby': 0,
		'status_updates_plex': 0,
		'plex_status_intents': [],
		'plex_progress': 0,
		'resolved_all': False,
	}

	season = None
	series = None
	if episode is not None:
		if observation_context:
			seasons_by_id = observation_context.get('seasons_by_id', {})
			series_by_id = observation_context.get('series_by_id', {})
			season = seasons_by_id.get(int(getattr(episode, 'season_id', 0) or 0))
			series = series_by_id.get(int(getattr(season, 'series_id', 0) or 0)) if season else None
		else:
			season = session.query(Season).filter(Season.id == int(episode.season_id)).first()
			series = session.query(Series).filter(Series.id == int(season.series_id)).first() if season else None

	if _is_enabled('plex'):
		has_plex_id = bool(getattr(placeholder, 'plex_placeholder_id', None))
		meta_pending = getattr(placeholder, 'media_lookup_error', None) == 'plex_metadata_pending'

		if not has_plex_id:
			# Phase 1: find Plex identity (ratingKey).
			plex_id = None
			plex_ready = False
			plex_item = None
			observed_path = str(getattr(placeholder, 'path', '') or '') or None
			movie_plex_item_by_movie_id = (observation_context or {}).get('movie_plex_item_by_movie_id', {})
			episode_plex_item_by_episode_id = (observation_context or {}).get('episode_plex_item_by_episode_id', {})
			if movie is not None:
				if int(movie.id) in movie_plex_item_by_movie_id:
					plex_item = movie_plex_item_by_movie_id.get(int(movie.id))
				elif plex_snapshot is not None:
					plex_item = _resolve_plex_movie_item_from_snapshot(
						plex_snapshot,
						movie,
						observed_path=observed_path,
						allow_title_fallback=allow_title_fallback,
					)
					if plex_item is None:
						# Keep snapshot as fast path, but fall back to direct resolver.
						plex_item = _resolve_plex_movie_item(
							movie,
							observed_path=observed_path,
							allow_title_fallback=allow_title_fallback,
						)
				else:
					plex_item = _resolve_plex_movie_item(
						movie,
						observed_path=observed_path,
						allow_title_fallback=allow_title_fallback,
					)
				if plex_item is not None:
					plex_id = str(getattr(plex_item, 'ratingKey', '') or '') or None
					plex_ready = _plex_item_metadata_ready(plex_item) if plex_snapshot is not None else bool(plex_id)
			elif episode is not None and season and series:
				if int(episode.id) in episode_plex_item_by_episode_id:
					plex_item = episode_plex_item_by_episode_id.get(int(episode.id))
				elif plex_snapshot is not None:
					plex_item = _resolve_plex_episode_item_from_snapshot(plex_snapshot, episode, season, series)
					if plex_item is None:
						# Snapshot miss fallback for naming/metadata-agent edge cases.
						plex_item = _resolve_plex_episode_item(episode, season, series)
				else:
					plex_item = _resolve_plex_episode_item(episode, season, series)
				if plex_item is not None:
					plex_id = str(getattr(plex_item, 'ratingKey', '') or '') or None
					plex_ready = _plex_item_metadata_ready(plex_item) if plex_snapshot is not None else bool(plex_id)
			if plex_id:
				_set_plex_observed(placeholder, plex_id)
				result['observed_plex'] = 1
				# Finding the ratingKey is always progress, regardless of metadata state.
				result['plex_progress'] = 1
				try:
					if movie is not None:
						persist_movie_plex_identity(session, movie, plex_item)
					elif episode is not None and season and series:
						persist_episode_hierarchy_plex_identity(session, series, season, episode, plex_item)
				except Exception:
					pass
				confirmed_ready = _record_plex_metadata_ready_observation(placeholder, plex_ready)
				if confirmed_ready and _should_send_status_updates():
					intent = _plex_status_intent_for_entity(movie, episode, placeholder)
					if intent is not None:
						result['plex_status_intents'].append(intent)
				if not confirmed_ready:
					placeholder.media_lookup_error = 'plex_metadata_pending'

		elif meta_pending:
			# Phase 2: identity is confirmed; re-check whether Plex metadata is now ready.
			# Fetch by ratingKey directly — always reflects the latest Plex state.
			stored_plex_id = str(getattr(placeholder, 'plex_placeholder_id', '') or '').strip()
			if stored_plex_id:
				phase2_item = None
				try:
					_plex_conn = get_plex_server()
					if _plex_conn:
						phase2_item = _plex_conn.fetchItem(f'/library/metadata/{stored_plex_id}')
				except Exception:
					pass
				phase2_ready = bool(phase2_item is not None and _plex_item_metadata_ready(phase2_item))
				confirmed_ready = _record_plex_metadata_ready_observation(placeholder, phase2_ready)
				if confirmed_ready:
					# Metadata is now available — apply status projection and clear pending state.
					result['plex_progress'] = 1
					result['observed_plex'] = 1
					placeholder.media_lookup_error = None
					if _should_send_status_updates():
						intent = _plex_status_intent_for_entity(movie, episode, placeholder)
						if intent is not None:
							result['plex_status_intents'].append(intent)
				else:
					if phase2_ready:
						result['plex_progress'] = 1
					placeholder.media_lookup_error = 'plex_metadata_pending'

	if _is_enabled('jellyfin') and not getattr(placeholder, 'jellyfin_placeholder_id', None):
		jellyfin_id: str | None = None
		try:
			if movie is not None:
				jellyfin_id = _resolve_jellyfin_movie_id(movie)
			elif episode is not None and season and series:
				jellyfin_id = _resolve_jellyfin_episode_id(episode, season, series)
		except Exception:
			jellyfin_id = None

		if jellyfin_id:
			_set_jellyfin_observed(placeholder, jellyfin_id)
			result['observed_jellyfin'] = 1

	if _is_enabled('emby') and not getattr(placeholder, 'emby_placeholder_id', None):
		emby_id: str | None = None
		try:
			if movie is not None:
				emby_id = _resolve_emby_movie_id(movie)
			elif episode is not None and season and series:
				emby_id = _resolve_emby_episode_id(episode, season, series)
		except Exception:
			emby_id = None

		if emby_id:
			_set_emby_observed(placeholder, emby_id)
			result['observed_emby'] = 1

	if _is_placeholder_fully_resolved(placeholder):
		placeholder.media_lookup_error = None
		result['resolved_all'] = True
	elif getattr(placeholder, 'media_lookup_error', None) == 'plex_metadata_pending':
		# Identity found; metadata projection still in progress — preserve this state.
		pass
	else:
		missing: list[str] = []
		if _is_enabled('plex') and not getattr(placeholder, 'plex_placeholder_id', None):
			missing.append('plex')
		if _is_enabled('jellyfin') and not getattr(placeholder, 'jellyfin_placeholder_id', None):
			missing.append('jellyfin')
		if _is_enabled('emby') and not getattr(placeholder, 'emby_placeholder_id', None):
			missing.append('emby')
		placeholder.media_lookup_error = f"not_found:{','.join(missing)}" if missing else None

	placeholder.updated_at = func.now()
	session.add(placeholder)
	return result


def _expected_plex_section_ids(placeholders: list[Placeholder]) -> set[int]:
	section_ids: set[int] = set()
	if any(getattr(placeholder, 'movie_id', None) for placeholder in placeholders):
		movie_section_id = getattr(settings, 'PLEX_MOVIE_SECTION_ID', None)
		if movie_section_id is not None:
			section_ids.add(int(movie_section_id))
	if any(getattr(placeholder, 'episode_id', None) for placeholder in placeholders):
		tv_section_id = getattr(settings, 'PLEX_TV_SECTION_ID', None)
		if tv_section_id is not None:
			section_ids.add(int(tv_section_id))
	return section_ids


def _run_plex_scan_gate(expected_section_ids: set[int], *, phase: str) -> dict[str, Any]:
	"""
	Gate for Plex section scan: always enabled, no activity checking.
	Busy/activity checker was removed in Phase 1.
	"""
	if not expected_section_ids:
		return {
			'ok': True,
			'reason': 'no_expected_sections',
			'waited_seconds': 0,
			'checks': 0,
			'scan_detected': False,
		}

	# Busy/activity checker removed: always proceed
	return {
		'ok': True,
		'reason': 'scan_gate_always_ok',
		'waited_seconds': 0,
		'checks': 0,
		'scan_detected': False,
	}


# Sleep durations (seconds) per consecutive no-progress poll.
# Schedule: 5 → 10 → 20 → 20, then stop on the 5th consecutive no-progress.
# Total worst-case sleep: 5 + 10 + 20 + 20 = 55 s.
_NO_PROGRESS_SLEEP_SCHEDULE = [5, 10, 20, 20]


def _run_observation_pass(
	session,
	placeholders: list[Placeholder],
	allow_title_fallback: bool = True,
	plex_snapshot: dict[str, Any] | None = None,
) -> tuple[list[Placeholder], dict[str, Any], int, int, dict[str, Any] | None]:
	"""Build a single Plex snapshot then probe every placeholder in the list.

	Returns:
	    (still_unresolved, pass_stats, resolved_delta, progress_delta, plex_snapshot)
	    resolved_delta — items that became fully resolved this pass.
	    progress_delta — items that made *any* Plex advancement (identity found
	                    or metadata confirmed), used to reset the sleep cadence.
	"""
	pass_stats: dict[str, Any] = {
		'observed_plex': 0,
		'observed_jellyfin': 0,
		'observed_emby': 0,
		'status_updates_plex': 0,
		'plex_progress': 0,
		'chunks_processed': 0,
		'pass_capped': False,
		'snapshot_build_ms': 0,
		'match_lookup_ms': 0,
		'status_projection_ms': 0,
		'db_commit_ms': 0,
	}
	still_unresolved: list[Placeholder] = []
	resolved_delta = 0
	chunk_size = _observation_pass_chunk_size()
	max_pass_seconds = _observation_max_pass_seconds()
	pass_started = time.monotonic()

	if _is_enabled('plex') and plex_snapshot is None:
		snapshot_started = time.monotonic()
		plex_snapshot = _build_plex_snapshot(session, placeholders)
		pass_stats['snapshot_build_ms'] += _timing_ms(snapshot_started)

	prefetch = _build_observation_prefetch(session, placeholders)
	reverse_matches = _build_reverse_snapshot_match_maps(prefetch, plex_snapshot)
	observation_context: dict[str, Any] = {
		**prefetch,
		**reverse_matches,
	}

	for chunk_start in range(0, len(placeholders), chunk_size):
		if max_pass_seconds > 0 and (time.monotonic() - pass_started) >= max_pass_seconds:
			still_unresolved.extend(placeholders[chunk_start:])
			pass_stats['pass_capped'] = True
			break

		chunk = placeholders[chunk_start:chunk_start + chunk_size]
		pending_status_intents: list[dict[str, Any]] = []
		match_started = time.monotonic()
		for placeholder in chunk:
			movie: Movie | None = None
			episode: Episode | None = None
			movie_id = getattr(placeholder, 'movie_id', None)
			episode_id = getattr(placeholder, 'episode_id', None)
			if movie_id is not None:
				movie = observation_context.get('movies_by_id', {}).get(int(movie_id))
			elif episode_id is not None:
				episode = observation_context.get('episodes_by_id', {}).get(int(episode_id))

			result = observe_placeholder_media_ids(
				session,
				placeholder,
				movie,
				episode,
				plex_snapshot=plex_snapshot,
				observation_context=observation_context,
				allow_title_fallback=allow_title_fallback,
			)
			pass_stats['observed_plex'] += result['observed_plex']
			pass_stats['observed_jellyfin'] += result['observed_jellyfin']
			pass_stats['observed_emby'] += result['observed_emby']
			pass_stats['plex_progress'] += result.get('plex_progress', 0)
			pending_status_intents.extend(result.get('plex_status_intents', []))

			if result['resolved_all']:
				resolved_delta += 1
			else:
				still_unresolved.append(placeholder)
		pass_stats['match_lookup_ms'] += _timing_ms(match_started)

		projection_started = time.monotonic()
		if pending_status_intents and _should_send_status_updates():
			try:
				projection_stats = batch_update_plex_statuses(session, _dedupe_plex_status_intents(pending_status_intents))
				pass_stats['status_updates_plex'] += int(projection_stats.get('status_updates', 0) or 0)
			except Exception as exc:
				logger.warning(
					f"Batched Plex status projection failed during observation pass: {exc}",
					extra={'emoji_type': 'warning'},
				)
		pass_stats['status_projection_ms'] += _timing_ms(projection_started)

		commit_started = time.monotonic()
		try:
			session.commit()
		except Exception:
			session.rollback()
			raise
		pass_stats['db_commit_ms'] += _timing_ms(commit_started)
		pass_stats['chunks_processed'] += 1

	progress_delta = pass_stats['plex_progress']
	return still_unresolved, pass_stats, resolved_delta, progress_delta, plex_snapshot


def observe_placeholders_with_polling(
	session,
	placeholders: list[Placeholder],
	allow_title_fallback: bool = False,
) -> dict[str, Any]:
	"""Observe placeholder media ids using direct polling with fixed intervals (no activity gating)."""
	stats: dict[str, Any] = {
		'observed_candidates': len(placeholders),
		'observed_plex': 0,
		'observed_jellyfin': 0,
		'observed_emby': 0,
		'status_updates_plex': 0,
		'observe_failed': 0,
		'attempts': 0,
		'resolved_total': 0,
		'stopped_early': False,
		'stop_reason': None,
		'passes_capped': 0,
		'timing': {
			'snapshot_build_ms': 0,
			'match_lookup_ms': 0,
			'status_projection_ms': 0,
			'db_commit_ms': 0,
		},
	}
	if not placeholders:
		return stats
	if not any((_is_enabled('plex'), _is_enabled('jellyfin'), _is_enabled('emby'))):
		stats['stop_reason'] = 'no_media_servers_enabled'
		return stats

	unresolved: list[Placeholder] = [p for p in placeholders if not _is_placeholder_fully_resolved(p)]
	if not unresolved:
		stats['stop_reason'] = 'all_resolved'
		return stats

	if not _is_enabled('plex'):
		stats['attempts'] = 1
		unresolved, pass_stats, resolved_delta, _, _ = _run_observation_pass(
			session,
			unresolved,
			allow_title_fallback=allow_title_fallback,
		)
		stats['observed_plex'] += pass_stats.get('observed_plex', 0)
		stats['observed_jellyfin'] += pass_stats.get('observed_jellyfin', 0)
		stats['observed_emby'] += pass_stats.get('observed_emby', 0)
		stats['status_updates_plex'] += pass_stats.get('status_updates_plex', 0)
		stats['timing']['snapshot_build_ms'] += int(pass_stats.get('snapshot_build_ms', 0) or 0)
		stats['timing']['match_lookup_ms'] += int(pass_stats.get('match_lookup_ms', 0) or 0)
		stats['timing']['status_projection_ms'] += int(pass_stats.get('status_projection_ms', 0) or 0)
		stats['timing']['db_commit_ms'] += int(pass_stats.get('db_commit_ms', 0) or 0)
		stats['resolved_total'] = resolved_delta
		stats['observe_failed'] = max(0, len(unresolved))
		stats['stop_reason'] = 'single_pass_non_plex'
		return stats

	total_expected = len(unresolved)
	resolved_total = 0
	expected_section_ids = _expected_plex_section_ids(unresolved)
	
	# Initial scan gate: no activity checking (always succeeds)
	initial_scan_gate = _run_plex_scan_gate(expected_section_ids, phase='initial')
	if not initial_scan_gate.get('ok', False):
		stats['observe_failed'] = len(unresolved)
		stats['stop_reason'] = str(initial_scan_gate.get('reason') or 'scan_gate_failed')
		logger.warning(
			f"Observation stopped before polling: reason={stats['stop_reason']}.",
			extra={'emoji_type': 'warning'},
		)
		return stats

	# Polling loop: adaptive interval 5→10→20→20, stop on 5th consecutive no-progress.
	# Any progress resets the interval back to 5 s.
	no_progress_count = 0
	plex_snapshot_cache: dict[str, Any] | None = None
	snapshot_reuse_streak = 0

	while unresolved:
		stats['attempts'] += 1
		pass_started = time.monotonic()
		logger.info(
			f"Observation poll pass #{stats['attempts']} starting with {len(unresolved)} unresolved item(s).",
			extra={'emoji_type': 'info'},
		)
		unresolved, pass_stats, resolved_delta, progress_delta, plex_snapshot_cache = _run_observation_pass(
			session,
			unresolved,
			allow_title_fallback=allow_title_fallback,
			plex_snapshot=plex_snapshot_cache,
		)
		pass_elapsed = time.monotonic() - pass_started
		logger.info(
			f"Observation poll pass #{stats['attempts']} finished in {pass_elapsed:.1f}s: "
			f"resolved_delta={resolved_delta}, progress_delta={progress_delta}, remaining={len(unresolved)}.",
			extra={'emoji_type': 'info'},
		)
		stats['observed_plex'] += pass_stats['observed_plex']
		stats['observed_jellyfin'] += pass_stats['observed_jellyfin']
		stats['observed_emby'] += pass_stats['observed_emby']
		stats['status_updates_plex'] += pass_stats['status_updates_plex']
		stats['timing']['snapshot_build_ms'] += int(pass_stats.get('snapshot_build_ms', 0) or 0)
		stats['timing']['match_lookup_ms'] += int(pass_stats.get('match_lookup_ms', 0) or 0)
		stats['timing']['status_projection_ms'] += int(pass_stats.get('status_projection_ms', 0) or 0)
		stats['timing']['db_commit_ms'] += int(pass_stats.get('db_commit_ms', 0) or 0)
		resolved_total += resolved_delta
		stats['resolved_total'] = resolved_total
		if pass_stats.get('pass_capped'):
			stats['passes_capped'] += 1
		logger.info(
			f"Observation pass timing: snapshot_build_ms={int(pass_stats.get('snapshot_build_ms', 0) or 0)} "
			f"match_lookup_ms={int(pass_stats.get('match_lookup_ms', 0) or 0)} "
			f"status_projection_ms={int(pass_stats.get('status_projection_ms', 0) or 0)} "
			f"db_commit_ms={int(pass_stats.get('db_commit_ms', 0) or 0)} "
			f"chunks={int(pass_stats.get('chunks_processed', 0) or 0)} "
			f"pass_capped={bool(pass_stats.get('pass_capped', False))}.",
			extra={'emoji_type': 'info'},
		)

		if not unresolved:
			stats['stop_reason'] = 'all_resolved'
			logger.info(
				f"Observation complete: all expected items are ready ({resolved_total}/{total_expected}).",
				extra={'emoji_type': 'success'},
			)
			if pass_stats['status_updates_plex'] > 0:
				logger.info(
					f"Plex status updates were applied for {pass_stats['status_updates_plex']} item(s) in this completion pass.",
					extra={'emoji_type': 'update'},
				)
			break

		if pass_stats['status_updates_plex'] > 0:
			logger.info(
				f"Plex status updates applied for {pass_stats['status_updates_plex']} item(s) this pass.",
				extra={'emoji_type': 'update'},
			)

		# Reuse scoped snapshots briefly for speed, but force periodic refresh so
		# newly scanned Plex content can be discovered on subsequent passes.
		if _is_enabled('plex'):
			if progress_delta <= 0:
				plex_snapshot_cache = None
				snapshot_reuse_streak = 0
			else:
				snapshot_reuse_streak += 1
				if snapshot_reuse_streak >= 2:
					plex_snapshot_cache = None
					snapshot_reuse_streak = 0

		if progress_delta > 0:
			# Progress: identity found or metadata confirmed — reset escalation state.
			no_progress_count = 0
			logger.info(
				f"Content polling progress: +{resolved_delta} fully resolved, +{progress_delta} advanced "
				f"this pass ({resolved_total}/{total_expected} done). Polling again in {_NO_PROGRESS_SLEEP_SCHEDULE[0]}s.",
				extra={'emoji_type': 'info'},
			)
			time.sleep(_NO_PROGRESS_SLEEP_SCHEDULE[0])
			continue

		# No progress this pass.
		if no_progress_count >= len(_NO_PROGRESS_SLEEP_SCHEDULE):
			# Exhausted all retries — stop without sleeping.
			stats['stopped_early'] = True
			stats['stop_reason'] = 'no_progress_max_polls'
			logger.warning(
				f"Observation made no progress for {no_progress_count + 1} consecutive polls "
				f"(schedule exhausted); finishing {len(unresolved)} unresolved.",
				extra={'emoji_type': 'warning'},
			)
			break

		sleep_seconds = _NO_PROGRESS_SLEEP_SCHEDULE[no_progress_count]
		no_progress_count += 1
		logger.info(
			f"No progress this pass ({resolved_total}/{total_expected} ready); "
			f"escalating to {sleep_seconds}s interval (no-progress streak: {no_progress_count}).",
			extra={'emoji_type': 'info'},
		)
		time.sleep(sleep_seconds)

	stats['observe_failed'] = len(unresolved)
	if not stats.get('stop_reason') and unresolved:
		stats['stop_reason'] = 'unresolved_after_observer'

	# When polling exhausts retries, handle two categories of remaining items:
	#   1. metadata_pending — ratingKey known but Plex hasn't populated metadata yet.
	#      Apply status projection now (using whatever Plex has at this moment) and
	#      clear the pending state.  No trail needed — we've resolved what we can.
	#   2. no_identity — Plex never found the item; enqueue a deferred 15-min trail.
	if unresolved and stats.get('stop_reason') == 'no_progress_max_polls':
		metadata_pending = [
			p for p in unresolved
			if bool(getattr(p, 'plex_placeholder_id', None))
			and getattr(p, 'media_lookup_error', None) == 'plex_metadata_pending'
		]
		no_identity = [
			p for p in unresolved
			if not bool(getattr(p, 'plex_placeholder_id', None))
		]

		if metadata_pending and _should_send_status_updates():
			fallback_intents: list[dict[str, Any]] = []
			for p in metadata_pending:
				status = _placeholder_display_status(p)
				movie_id = getattr(p, 'movie_id', None)
				episode_id = getattr(p, 'episode_id', None)
				if movie_id is not None:
					fallback_intents.append({'entity_type': Movie, 'entity_id': int(movie_id), 'status': status})
				elif episode_id is not None:
					fallback_intents.append({'entity_type': Episode, 'entity_id': int(episode_id), 'status': status})
			if fallback_intents:
				try:
					batch_update_plex_statuses(session, fallback_intents)
					logger.info(
						f"Applied fallback status update for {len(metadata_pending)} metadata-pending item(s) "
						f"after polling exhaustion.",
						extra={'emoji_type': 'info'},
					)
				except Exception as exc:
					logger.warning(
						f"Fallback status update failed for metadata-pending items: {exc}",
						extra={'emoji_type': 'warning'},
					)
			for p in metadata_pending:
				p.media_lookup_error = None
				p.updated_at = func.now()
				session.add(p)
			try:
				session.commit()
			except Exception:
				session.rollback()

		if no_identity:
			no_identity_ids = [int(p.id) for p in no_identity if getattr(p, 'id', None)]
			if no_identity_ids:
				try:
					# Local import to avoid circular dependency (observation_trail imports us).
					from services.source_of_truth.observation_trail import enqueue_observation_trail  # noqa: PLC0415
					enqueue_result = enqueue_observation_trail(
						session,
						placeholder_ids=no_identity_ids,
						source='poller_early_stop',
						delay_seconds=900,
					)
					logger.info(
						f"Enqueued 15-minute follow-up observation trail for {len(no_identity_ids)} item(s) with no Plex identity: "
						f"job_id={enqueue_result.get('job_id')}, enqueued={enqueue_result.get('enqueued')}.",
						extra={'emoji_type': 'info'},
					)
				except Exception as exc:
					logger.warning(
						f"Could not enqueue follow-up observation trail: {exc}",
						extra={'emoji_type': 'warning'},
					)

	logger.info(
		"Observation summary: "
		f"ready={stats['resolved_total']}/{total_expected}, "
		f"still_waiting={stats['observe_failed']}, "
		f"reason={stats['stop_reason'] or 'unknown'}.",
		extra={'emoji_type': 'info'},
	)
	return stats

