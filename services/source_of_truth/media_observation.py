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


def _build_plex_snapshot() -> dict[str, Any] | None:
	"""Build a one-attempt in-memory Plex snapshot for fast multi-item matching."""
	if not _is_enabled('plex'):
		return None
	plex = get_plex_server()
	if not plex:
		return None

	try:
		movie_section = plex.library.sectionByID(settings.PLEX_MOVIE_SECTION_ID)
		tv_section = plex.library.sectionByID(settings.PLEX_TV_SECTION_ID)
	except Exception:
		return None

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

	try:
		all_movies = movie_section.all()
	except Exception:
		all_movies = []
	for m in all_movies:
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

	try:
		all_shows = tv_section.all()
	except Exception:
		all_shows = []
	for show in all_shows:
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
			continue

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
	# Observation polling is intentionally Plex-only for now.
	if not _is_enabled('plex'):
		return True
	# Must have both identity (ratingKey) *and* confirmed metadata projection.
	has_id = bool(getattr(placeholder, 'plex_placeholder_id', None))
	if not has_id:
		return False
	return getattr(placeholder, 'media_lookup_error', None) != 'plex_metadata_pending'


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


def observe_placeholder_media_ids(
	session,
	placeholder: Placeholder,
	movie: Movie | None,
	episode: Episode | None,
	plex_snapshot: dict[str, Any] | None = None,
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
		'plex_progress': 0,
		'resolved_all': False,
	}

	season = None
	series = None
	if episode is not None:
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
			if movie is not None:
				if plex_snapshot is not None:
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
				if plex_snapshot is not None:
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
				if plex_ready and _should_send_status_updates():
					try:
						intents = []
						status = _placeholder_display_status(placeholder)
						if movie is not None:
							intents.append({'entity_type': Movie, 'entity_id': int(movie.id), 'status': status})
							result['status_updates_plex'] = 1
						elif episode is not None:
							intents.append({'entity_type': Episode, 'entity_id': int(episode.id), 'status': status})
							result['status_updates_plex'] = 1
						if intents:
							batch_update_plex_statuses(session, intents)
					except Exception:
						pass
				elif not plex_ready:
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
				if phase2_item is not None and _plex_item_metadata_ready(phase2_item):
					# Metadata is now available — apply status projection and clear pending state.
					result['plex_progress'] = 1
					result['observed_plex'] = 1
					placeholder.media_lookup_error = None
					if _should_send_status_updates():
						try:
							intents = []
							status = _placeholder_display_status(placeholder)
							if movie is not None:
								intents.append({'entity_type': Movie, 'entity_id': int(movie.id), 'status': status})
								result['status_updates_plex'] = 1
							elif episode is not None:
								intents.append({'entity_type': Episode, 'entity_id': int(episode.id), 'status': status})
								result['status_updates_plex'] = 1
							if intents:
								batch_update_plex_statuses(session, intents)
						except Exception:
							pass

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
) -> tuple[list[Placeholder], dict[str, Any], int, int]:
	"""Build a single Plex snapshot then probe every placeholder in the list.

	Returns:
	    (still_unresolved, pass_stats, resolved_delta, progress_delta)
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
	}
	still_unresolved: list[Placeholder] = []
	resolved_delta = 0

	plex_snapshot: dict[str, Any] | None = _build_plex_snapshot() if _is_enabled('plex') else None

	for placeholder in placeholders:
		movie: Movie | None = None
		episode: Episode | None = None
		movie_id = getattr(placeholder, 'movie_id', None)
		episode_id = getattr(placeholder, 'episode_id', None)
		if movie_id is not None:
			movie = session.query(Movie).filter(Movie.id == int(movie_id)).first()
		elif episode_id is not None:
			episode = session.query(Episode).filter(Episode.id == int(episode_id)).first()

		result = observe_placeholder_media_ids(
			session,
			placeholder,
			movie,
			episode,
			plex_snapshot=plex_snapshot,
			allow_title_fallback=allow_title_fallback,
		)
		pass_stats['observed_plex'] += result['observed_plex']
		pass_stats['observed_jellyfin'] += result['observed_jellyfin']
		pass_stats['observed_emby'] += result['observed_emby']
		pass_stats['status_updates_plex'] += result.get('status_updates_plex', 0)
		pass_stats['plex_progress'] += result.get('plex_progress', 0)

		if result['resolved_all']:
			resolved_delta += 1
		else:
			still_unresolved.append(placeholder)

	try:
		session.commit()
	except Exception:
		session.rollback()
		raise

	progress_delta = pass_stats['plex_progress']
	return still_unresolved, pass_stats, resolved_delta, progress_delta


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
	}
	if not placeholders:
		return stats
	if not _is_enabled('plex'):
		stats['stop_reason'] = 'plex_disabled'
		return stats

	unresolved: list[Placeholder] = [p for p in placeholders if not _is_placeholder_fully_resolved(p)]
	if not unresolved:
		stats['stop_reason'] = 'all_resolved'
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

	while unresolved:
		stats['attempts'] += 1
		unresolved, pass_stats, resolved_delta, progress_delta = _run_observation_pass(
			session,
			unresolved,
			allow_title_fallback=allow_title_fallback,
		)
		stats['observed_plex'] += pass_stats['observed_plex']
		stats['observed_jellyfin'] += pass_stats['observed_jellyfin']
		stats['observed_emby'] += pass_stats['observed_emby']
		stats['status_updates_plex'] += pass_stats['status_updates_plex']
		resolved_total += resolved_delta
		stats['resolved_total'] = resolved_total

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

