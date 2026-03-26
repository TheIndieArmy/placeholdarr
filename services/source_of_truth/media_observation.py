from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import requests

from sqlalchemy import func

from core.config import settings
from core.logger import logger
from services.postgres.models import Episode, Movie, Placeholder, Season, Series
from services.source_of_truth.plex_busy_check import (
	get_active_expected_scan_sections,
	has_any_active_plex_scan,
)


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
	try:
		from services.services_old import plex_client
	except Exception:
		return None

	plex = getattr(plex_client, 'plex', None)
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

		norm_title = _normalize_title(getattr(m, 'title', None))
		year = int(getattr(m, 'year', 0) or 0)
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


def _resolve_plex_movie_item_from_snapshot(snapshot: dict[str, Any], movie: Movie) -> Any | None:
	tmdb_target = str(getattr(movie, 'tmdbid', '') or '')
	if tmdb_target:
		item = snapshot.get('movies_by_tmdb', {}).get(tmdb_target)
		if item:
			return item
		item = snapshot.get('movies_by_path_tmdb', {}).get(tmdb_target)
		if item:
			return item

	norm_title = _normalize_title(getattr(movie, 'title', None))
	target_year = int(getattr(movie, 'year', 0) or 0)
	if norm_title and target_year:
		item = snapshot.get('movies_by_title_year', {}).get((norm_title, target_year))
		if item:
			return item

	if norm_title:
		candidates = snapshot.get('movies_by_title', {}).get(norm_title, [])
		if candidates:
			return candidates[0]

	return None


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
		from services.services_old.plex_client import find_movie_by_id
	except Exception as e:
		logger.debug(f"Plex helper import failed (movie): {e}", extra={'emoji_type': 'debug'})
		return None

	tmdb_id = getattr(movie, 'tmdbid', None)
	if not tmdb_id:
		return None

	try:
		found = find_movie_by_id(tmdb_id, title=getattr(movie, 'title', None), year=getattr(movie, 'year', None))
		rating_key = getattr(found, 'ratingKey', None) if found else None
		return str(rating_key) if rating_key else None
	except Exception:
		return None


def _resolve_plex_episode_id(episode: Episode, season: Season, series: Series) -> str | None:
	try:
		from services.services_old.plex_client import find_show_by_id
	except Exception as e:
		logger.debug(f"Plex helper import failed (episode): {e}", extra={'emoji_type': 'debug'})
		return None

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
			# Uses existing updater for compatibility with current status format.
			if _should_send_status_updates():
				try:
					from services.services_old.plex_client import update_plex_title_status
					if movie is not None:
						update_plex_title_status(session, int(movie.id), Movie, action='status_update')
					elif episode is not None:
						update_plex_title_status(session, int(episode.id), Episode, action='status_update')
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
	return bool(getattr(placeholder, 'plex_placeholder_id', None))


def observe_placeholder_media_ids(
	session,
	placeholder: Placeholder,
	movie: Movie | None,
	episode: Episode | None,
	plex_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Attempt to observe IDs for all enabled media servers for one placeholder."""
	placeholder.media_lookup_last_attempt_at = func.now()
	result = {
		'observed_plex': 0,
		'observed_jellyfin': 0,
		'observed_emby': 0,
		'status_updates_plex': 0,
		'resolved_all': False,
	}

	season = None
	series = None
	if episode is not None:
		season = session.query(Season).filter(Season.id == int(episode.season_id)).first()
		series = session.query(Series).filter(Series.id == int(season.series_id)).first() if season else None

	if _is_enabled('plex') and not getattr(placeholder, 'plex_placeholder_id', None):
		plex_id = None
		plex_ready = False
		if movie is not None:
			if plex_snapshot is not None:
				plex_item = _resolve_plex_movie_item_from_snapshot(plex_snapshot, movie)
				if plex_item is not None:
					plex_id = str(getattr(plex_item, 'ratingKey', '') or '') or None
					plex_ready = _plex_item_metadata_ready(plex_item)
			else:
				plex_id = _resolve_plex_movie_id(movie)
				plex_ready = bool(plex_id)
		elif episode is not None and season and series:
			if plex_snapshot is not None:
				plex_item = _resolve_plex_episode_item_from_snapshot(plex_snapshot, episode, season, series)
				if plex_item is not None:
					plex_id = str(getattr(plex_item, 'ratingKey', '') or '') or None
					plex_ready = _plex_item_metadata_ready(plex_item)
			else:
				plex_id = _resolve_plex_episode_id(episode, season, series)
				plex_ready = bool(plex_id)
		if plex_id and plex_ready:
			_set_plex_observed(placeholder, plex_id)
			result['observed_plex'] = 1
			if _should_send_status_updates():
				try:
					from services.services_old.plex_client import update_plex_title_status
					if movie is not None:
						if update_plex_title_status(session, int(movie.id), Movie, action='status_update'):
							result['status_updates_plex'] = 1
					elif episode is not None:
						if update_plex_title_status(session, int(episode.id), Episode, action='status_update'):
							result['status_updates_plex'] = 1
				except Exception:
					pass
		elif plex_id and not plex_ready:
			placeholder.media_lookup_error = 'plex_metadata_pending'

	if _is_placeholder_fully_resolved(placeholder):
		placeholder.media_lookup_error = None
		result['resolved_all'] = True
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
	probe_delays_seconds = [2, 5]
	poll_interval_seconds = 5
	activity_probe_window_seconds = 60
	max_wait_seconds = 900
	waited_seconds = 0
	checks = 0

	if not expected_section_ids:
		return {
			'ok': True,
			'reason': 'no_expected_sections',
			'waited_seconds': waited_seconds,
			'checks': checks,
			'scan_detected': False,
		}

	# Probe phase: check for Plex activity (both expected and any other activity)
	expected_active_sections: set[int] = set()
	has_any_activity = False
	expected_activity_seen = False
	for delay_seconds in probe_delays_seconds:
		time.sleep(delay_seconds)
		waited_seconds += delay_seconds
		checks += 1
		expected_active_sections = get_active_expected_scan_sections(expected_section_ids)
		if expected_active_sections:
			expected_activity_seen = True
		has_any_activity = has_any_active_plex_scan()
		if expected_active_sections or has_any_activity:
			if expected_active_sections:
				logger.info(
					f"Plex activity check detected expected library activity during {phase}: sections={sorted(expected_active_sections)}.",
					extra={'emoji_type': 'info'},
				)
			else:
				logger.info(
					f"Plex activity check detected other library scan active during {phase}; waiting for expected section or global idle.",
					extra={'emoji_type': 'info'},
				)
			break

	# If no Plex activity detected anywhere, quick-exit
	if not has_any_activity:
		logger.info(
			f"Plex activity check detected no Plex activity during {phase}; assuming quick scan and continuing to content polling.",
			extra={'emoji_type': 'info'},
		)
		return {
			'ok': True,
			'reason': 'quick_scan_or_no_activity',
			'waited_seconds': waited_seconds,
			'checks': checks,
			'scan_detected': False,
		}

	# If Plex activity was detected, wait for expected scan to clear or timeout
	while waited_seconds < max_wait_seconds:
		time.sleep(poll_interval_seconds)
		waited_seconds += poll_interval_seconds
		checks += 1
		expected_active_sections = get_active_expected_scan_sections(expected_section_ids)
		if expected_active_sections:
			expected_activity_seen = True
		has_any_activity = has_any_active_plex_scan()
		
		# If we previously saw expected activity and it has now cleared
		if expected_activity_seen and not expected_active_sections:
			logger.info(
				f"Plex activity check cleared expected library activity during {phase}; starting content polling.",
				extra={'emoji_type': 'info'},
			)
			return {
				'ok': True,
				'reason': 'expected_scan_completed',
				'waited_seconds': waited_seconds,
				'checks': checks,
				'scan_detected': True,
			}
		
		# If all Plex activity completely idle
		if not has_any_activity:
			logger.info(
				f"Plex activity check detected global idle during {phase}; starting content polling.",
				extra={'emoji_type': 'info'},
			)
			return {
				'ok': True,
				'reason': 'global_idle',
				'waited_seconds': waited_seconds,
				'checks': checks,
				'scan_detected': True,
			}

		if has_any_activity and waited_seconds >= activity_probe_window_seconds:
			logger.info(
				f"Plex activity check still sees active scans during {phase} after {waited_seconds}s; starting sparse content polling while scans continue.",
				extra={'emoji_type': 'info'},
			)
			return {
				'ok': True,
				'reason': 'activity_probe_window_elapsed',
				'waited_seconds': waited_seconds,
				'checks': checks,
				'scan_detected': True,
			}

	return {
		'ok': False,
		'reason': 'scan_wait_timeout',
		'waited_seconds': waited_seconds,
		'checks': checks,
		'scan_detected': True,
	}


def _run_observation_pass(session, unresolved: list[Placeholder]) -> tuple[list[Placeholder], dict[str, int], int]:
	pass_stats = {
		'observed_plex': 0,
		'observed_jellyfin': 0,
		'observed_emby': 0,
		'status_updates_plex': 0,
	}
	before_count = len(unresolved)
	plex_snapshot = None
	needs_plex = any(not getattr(placeholder, 'plex_placeholder_id', None) for placeholder in unresolved)
	if needs_plex:
		plex_snapshot = _build_plex_snapshot()

	next_round: list[Placeholder] = []
	for placeholder in unresolved:
		movie = None
		episode = None
		if getattr(placeholder, 'movie_id', None):
			movie = session.query(Movie).filter(Movie.id == int(placeholder.movie_id)).first()
		elif getattr(placeholder, 'episode_id', None):
			episode = session.query(Episode).filter(Episode.id == int(placeholder.episode_id)).first()

		result = observe_placeholder_media_ids(
			session,
			placeholder,
			movie,
			episode,
			plex_snapshot=plex_snapshot,
		)
		pass_stats['observed_plex'] += int(result.get('observed_plex', 0) or 0)
		pass_stats['observed_jellyfin'] += int(result.get('observed_jellyfin', 0) or 0)
		pass_stats['observed_emby'] += int(result.get('observed_emby', 0) or 0)
		pass_stats['status_updates_plex'] += int(result.get('status_updates_plex', 0) or 0)
		if not result.get('resolved_all', False):
			next_round.append(placeholder)

	session.commit()
	resolved_delta = max(0, before_count - len(next_round))
	return next_round, pass_stats, resolved_delta


def _content_poll_interval_seconds() -> int:
	normal_poll_interval_seconds = 5
	active_scan_poll_interval_seconds = 60
	if has_any_active_plex_scan():
		return active_scan_poll_interval_seconds
	return normal_poll_interval_seconds


def observe_placeholders_with_polling(session, placeholders: list[Placeholder]) -> dict[str, Any]:
	"""Observe placeholder media ids after refresh using a scan gate plus fixed polling."""
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
	initial_scan_gate = _run_plex_scan_gate(expected_section_ids, phase='initial')
	if not initial_scan_gate.get('ok', False):
		stats['observe_failed'] = len(unresolved)
		stats['stop_reason'] = str(initial_scan_gate.get('reason') or 'scan_gate_failed')
		logger.warning(
			f"Observation stopped before polling: reason={stats['stop_reason']}.",
			extra={'emoji_type': 'warning'},
		)
		return stats

	consecutive_no_progress = 0
	recovery_scan_used = False
	after_recovery_scan = False

	while unresolved:
		stats['attempts'] += 1
		unresolved, pass_stats, resolved_delta = _run_observation_pass(session, unresolved)
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

		if resolved_delta > 0:
			consecutive_no_progress = 0
			after_recovery_scan = False
			poll_interval_seconds = _content_poll_interval_seconds()
			logger.info(
				f"Content polling progress: +{resolved_delta} ready this pass ({resolved_total}/{total_expected}). Polling again in {poll_interval_seconds}s.",
				extra={'emoji_type': 'info'},
			)
			time.sleep(poll_interval_seconds)
			continue

		logger.info(
			f"Content polling progress: no new ready items this pass ({resolved_total}/{total_expected}).",
			extra={'emoji_type': 'info'},
		)

		if after_recovery_scan:
			stats['stopped_early'] = True
			stats['stop_reason'] = 'no_progress_after_recovery_scan'
			logger.warning(
				"Observation made no progress after the recovery scan gate; finishing unresolved.",
				extra={'emoji_type': 'warning'},
			)
			break

		consecutive_no_progress += 1
		if consecutive_no_progress < 2:
			poll_interval_seconds = _content_poll_interval_seconds()
			logger.info(
				f"No progress yet; polling again in {poll_interval_seconds}s before re-running Plex activity check.",
				extra={'emoji_type': 'info'},
			)
			time.sleep(poll_interval_seconds)
			continue

		if recovery_scan_used:
			stats['stopped_early'] = True
			stats['stop_reason'] = 'no_progress_recovery_exhausted'
			logger.warning(
				"Observation exhausted its recovery scan gate and still has unresolved placeholders.",
				extra={'emoji_type': 'warning'},
			)
			break

		recovery_scan_used = True
		consecutive_no_progress = 0
		recovery_scan_gate = _run_plex_scan_gate(expected_section_ids, phase='recovery')
		if not recovery_scan_gate.get('ok', False):
			stats['stopped_early'] = True
			stats['stop_reason'] = str(recovery_scan_gate.get('reason') or 'recovery_scan_gate_failed')
			logger.warning(
				f"Observation recovery scan gate failed: reason={stats['stop_reason']}.",
				extra={'emoji_type': 'warning'},
			)
			break
		after_recovery_scan = str(recovery_scan_gate.get('reason') or '') not in {'activity_probe_window_elapsed'}

	stats['observe_failed'] = len(unresolved)
	if not stats.get('stop_reason') and unresolved:
		stats['stop_reason'] = 'unresolved_after_observer'

	logger.info(
		"Observation summary: "
		f"ready={stats['resolved_total']}/{total_expected}, "
		f"still_waiting={stats['observe_failed']}, "
		f"reason={stats['stop_reason'] or 'unknown'}.",
		extra={'emoji_type': 'info'},
	)
	return stats

