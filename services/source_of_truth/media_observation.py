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
from typing import Any, Callable
from urllib.parse import quote_plus

import requests

from sqlalchemy import func, text

from core.config import settings
from core.logger import logger
from services.media_servers.plex import get_plex_section_scan_state, refresh_plex_section_ids
from services.media_servers.plex_identity import persist_episode_hierarchy_plex_identity, persist_movie_plex_identity
from services.media_servers.plex_lookup import find_show_by_id, get_plex_server
from services.media_servers.plex_status_writer import batch_update_plex_statuses
from services.postgres.db import get_engine, get_session
from services.postgres.models import Episode, Movie, ObservationFlight, Placeholder, Season, Series
from services.source_of_truth.refresh_throttle import try_acquire_refresh_lease



def _safe_now() -> datetime:
	return datetime.now(timezone.utc)


OBSERVATION_SINGLE_FLIGHT_LOCK_KEY = 4815162342
OBSERVATION_SINGLE_FLIGHT_ROW_KEY = 'global_observation_single_flight'


def _single_flight_enabled() -> bool:
	return bool(getattr(settings, 'OBSERVATION_SINGLE_FLIGHT_ENABLED', True))


def _single_flight_wait_seconds() -> float:
	try:
		return max(0.0, float(getattr(settings, 'OBSERVATION_SINGLE_FLIGHT_WAIT_SECONDS', 15.0) or 0.0))
	except Exception:
		return 15.0


def _single_flight_retry_seconds() -> float:
	try:
		return max(0.05, float(getattr(settings, 'OBSERVATION_SINGLE_FLIGHT_RETRY_SECONDS', 0.25) or 0.25))
	except Exception:
		return 0.25


def _single_flight_stale_seconds() -> int:
	try:
		return max(60, int(getattr(settings, 'OBSERVATION_FLIGHT_STALE_SECONDS', 300) or 300))
	except Exception:
		return 300


def _single_flight_holder() -> str:
	import os
	return str(os.getenv('HOSTNAME') or 'unknown-host')


def _observation_snapshot_cache_passes() -> int:
	try:
		return max(0, int(getattr(settings, 'OBSERVATION_SNAPSHOT_CACHE_PASSES', 2) or 0))
	except Exception:
		return 2


def _cleanup_stale_flight_rows() -> None:
	if not _single_flight_enabled():
		return
	now = _safe_now()
	cutoff = now.timestamp() - float(_single_flight_stale_seconds())
	session = get_session()
	try:
		rows = (
			session.query(ObservationFlight)
			.filter(
				ObservationFlight.flight_key == OBSERVATION_SINGLE_FLIGHT_ROW_KEY,
				ObservationFlight.is_active == True,  # noqa: E712
			)
			.all()
		)
		changed = False
		for row in rows:
			hb = getattr(row, 'heartbeat_at', None) or getattr(row, 'acquired_at', None)
			if not hb:
				continue
			if hb.timestamp() < cutoff:
				row.is_active = False
				row.last_reason = 'stale_timeout_cleanup'
				row.released_at = now
				row.updated_at = func.now()
				session.add(row)
				changed = True
		if changed:
			session.commit()
			logger.info(
				'Cleaned stale persisted observation flight row(s).',
				extra={'emoji_type': 'info'},
			)
		else:
			session.rollback()
	except Exception:
		try:
			session.rollback()
		except Exception:
			pass
	finally:
		try:
			session.close()
		except Exception:
			pass


def _mark_flight_active(*, source: str, lock_attempts: int) -> None:
	session = get_session()
	try:
		row = (
			session.query(ObservationFlight)
			.filter(ObservationFlight.flight_key == OBSERVATION_SINGLE_FLIGHT_ROW_KEY)
			.first()
		)
		if not row:
			row = ObservationFlight(flight_key=OBSERVATION_SINGLE_FLIGHT_ROW_KEY)
			session.add(row)
			session.flush()
		now = _safe_now()
		row.holder = _single_flight_holder()
		row.source = str(source or 'unknown')
		row.is_active = True
		row.lock_attempts = max(1, int(lock_attempts or 1))
		row.acquired_at = now
		row.heartbeat_at = now
		row.released_at = None
		row.last_reason = 'active'
		row.updated_at = func.now()
		session.add(row)
		session.commit()
	except Exception:
		try:
			session.rollback()
		except Exception:
			pass
	finally:
		try:
			session.close()
		except Exception:
			pass


def _heartbeat_active_flight() -> None:
	session = get_session()
	try:
		row = (
			session.query(ObservationFlight)
			.filter(ObservationFlight.flight_key == OBSERVATION_SINGLE_FLIGHT_ROW_KEY)
			.first()
		)
		if not row:
			session.rollback()
			return
		row.heartbeat_at = _safe_now()
		row.updated_at = func.now()
		session.add(row)
		session.commit()
	except Exception:
		try:
			session.rollback()
		except Exception:
			pass
	finally:
		try:
			session.close()
		except Exception:
			pass


def _mark_flight_released(*, reason: str) -> None:
	session = get_session()
	try:
		row = (
			session.query(ObservationFlight)
			.filter(ObservationFlight.flight_key == OBSERVATION_SINGLE_FLIGHT_ROW_KEY)
			.first()
		)
		if not row:
			session.rollback()
			return
		row.is_active = False
		row.last_reason = str(reason or 'released')
		row.released_at = _safe_now()
		row.heartbeat_at = _safe_now()
		row.updated_at = func.now()
		session.add(row)
		session.commit()
	except Exception:
		try:
			session.rollback()
		except Exception:
			pass
	finally:
		try:
			session.close()
		except Exception:
			pass


def _try_acquire_single_flight_lock() -> tuple[Any | None, bool, int]:
	if not _single_flight_enabled():
		return None, True, 0

	_cleanup_stale_flight_rows()

	conn = None
	try:
		conn = get_engine().connect()
	except Exception as exc:
		logger.warning(
			f"Observation single-flight disabled for this run (could not open lock connection): {exc}",
			extra={'emoji_type': 'warning'},
		)
		return None, True, 0

	attempts = 0
	deadline = time.monotonic() + _single_flight_wait_seconds()
	while True:
		attempts += 1
		try:
			got = bool(
				conn.execute(
					text('SELECT pg_try_advisory_lock(:k)'),
					{'k': OBSERVATION_SINGLE_FLIGHT_LOCK_KEY},
				).scalar()
			)
		except Exception as exc:
			try:
				conn.close()
			except Exception:
				pass
			logger.warning(
				f"Observation single-flight lock attempt failed: {exc}",
				extra={'emoji_type': 'warning'},
			)
			return None, False, attempts

		if got:
			logger.info(
				f"Observation single-flight acquired after {attempts} attempt(s).",
				extra={'emoji_type': 'info'},
			)
			return conn, True, attempts

		if time.monotonic() >= deadline:
			logger.info(
				f"Observation single-flight busy after {attempts} attempt(s); deferring this run.",
				extra={'emoji_type': 'info'},
			)
			try:
				conn.close()
			except Exception:
				pass
			return None, False, attempts

		time.sleep(_single_flight_retry_seconds())


def _release_single_flight_lock(conn: Any | None) -> None:
	if conn is None:
		return
	try:
		conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': OBSERVATION_SINGLE_FLIGHT_LOCK_KEY})
	except Exception:
		pass
	finally:
		try:
			conn.close()
		except Exception:
			pass


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


def _placeholder_customer_labels(session, placeholders: list[Placeholder]) -> dict[int, str]:
	"""Build customer-facing labels for placeholders.

	Episode labels prefer: "Show Title s00e00".
	Movie labels prefer: "Movie Title (Year)".
	"""
	labels: dict[int, str] = {}
	if not placeholders:
		return labels

	episode_ids = [int(getattr(p, 'episode_id')) for p in placeholders if getattr(p, 'episode_id', None)]
	movie_ids = [int(getattr(p, 'movie_id')) for p in placeholders if getattr(p, 'movie_id', None)]

	if episode_ids:
		episode_rows = (
			session.query(
				Episode.id,
				Episode.episode_number,
				Season.season_number,
				Series.title,
			)
			.join(Season, Episode.season_id == Season.id)
			.join(Series, Season.series_id == Series.id)
			.filter(Episode.id.in_(episode_ids))
			.all()
		)
		episode_map = {int(row[0]): row for row in episode_rows}
		for p in placeholders:
			pid = int(getattr(p, 'id', 0) or 0)
			episode_id = getattr(p, 'episode_id', None)
			if not pid or not episode_id:
				continue
			row = episode_map.get(int(episode_id))
			if not row:
				continue
			season_num = int(row[2] or 0)
			episode_num = int(row[1] or 0)
			show_title = str(row[3] or 'Unknown Show').strip() or 'Unknown Show'
			labels[pid] = f"{show_title} s{season_num:02d}e{episode_num:02d}"

	if movie_ids:
		movie_rows = (
			session.query(Movie.id, Movie.title, Movie.year)
			.filter(Movie.id.in_(movie_ids))
			.all()
		)
		movie_map = {int(row[0]): row for row in movie_rows}
		for p in placeholders:
			pid = int(getattr(p, 'id', 0) or 0)
			movie_id = getattr(p, 'movie_id', None)
			if not pid or not movie_id or pid in labels:
				continue
			row = movie_map.get(int(movie_id))
			if not row:
				continue
			title = str(row[1] or 'Unknown Movie').strip() or 'Unknown Movie'
			year = row[2]
			labels[pid] = f"{title} ({year})" if year else title

	for p in placeholders:
		pid = int(getattr(p, 'id', 0) or 0)
		if pid and pid not in labels:
			labels[pid] = f"Placeholder[{pid}]"

	return labels


def _format_label_list(labels: list[str], *, max_items: int = 12) -> str:
	if not labels:
		return "none"
	if len(labels) <= max_items:
		return ', '.join(labels)
	visible = labels[:max_items]
	return f"{', '.join(visible)} (+{len(labels) - max_items} more)"


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


def _observation_min_chunks_per_pass() -> int:
	"""Guarantee each pass performs some real checks before capping by time."""
	try:
		return max(1, int(getattr(settings, 'OBSERVATION_MIN_CHUNKS_PER_PASS', 1) or 1))
	except Exception:
		return 1


def _observation_bulk_strict_keys_only() -> bool:
	try:
		return bool(getattr(settings, 'OBSERVATION_BULK_STRICT_KEYS_ONLY', True))
	except Exception:
		return True


def _observation_strict_keys_min_placeholders() -> int:
	try:
		return max(1, int(getattr(settings, 'OBSERVATION_STRICT_KEYS_MIN_PLACEHOLDERS', 100) or 100))
	except Exception:
		return 100


def _timing_ms(started: float) -> int:
	return int(round((time.monotonic() - started) * 1000.0))


def _select_snapshot_key_candidates(
	placeholders: list[Placeholder],
	reverse_matches: dict[str, Any],
	strict_mode: bool,
) -> tuple[list[Placeholder], int, int]:
	"""Return (candidates, candidate_count, skipped_count) for snapshot-key filtered matching."""
	if not strict_mode:
		return placeholders, len(placeholders), 0

	movie_map = reverse_matches.get('movie_plex_item_by_movie_id', {}) or {}
	episode_map = reverse_matches.get('episode_plex_item_by_episode_id', {}) or {}

	candidates: list[Placeholder] = []
	skipped = 0
	for placeholder in placeholders:
		movie_id = getattr(placeholder, 'movie_id', None)
		episode_id = getattr(placeholder, 'episode_id', None)
		if movie_id is not None and int(movie_id) in movie_map:
			candidates.append(placeholder)
			continue
		if episode_id is not None and int(episode_id) in episode_map:
			candidates.append(placeholder)
			continue
		skipped += 1

	return candidates, len(candidates), skipped


def _select_snapshot_present_candidates(
	placeholders: list[Placeholder],
	reverse_matches: dict[str, Any],
) -> tuple[list[Placeholder], list[Placeholder]]:
	"""Return placeholders present in the Plex snapshot and those still awaiting scan."""
	movie_map = reverse_matches.get('movie_plex_item_by_movie_id', {}) or {}
	episode_map = reverse_matches.get('episode_plex_item_by_episode_id', {}) or {}

	present: list[Placeholder] = []
	awaiting_scan: list[Placeholder] = []
	for placeholder in placeholders:
		movie_id = getattr(placeholder, 'movie_id', None)
		episode_id = getattr(placeholder, 'episode_id', None)
		if movie_id is not None and int(movie_id) in movie_map:
			present.append(placeholder)
			continue
		if episode_id is not None and int(episode_id) in episode_map:
			present.append(placeholder)
			continue
		awaiting_scan.append(placeholder)

	return present, awaiting_scan


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
	strict_keys_only: bool = False,
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
		if not strict_keys_only:
			if norm_title and year:
				movies_by_title_year.setdefault((norm_title, year), []).append(movie_id)
			if norm_title:
				movies_by_title.setdefault(norm_title, []).append(movie_id)

	# Walk unique movie items discovered in snapshot indexes.
	seen_movie_keys: set[str] = set()
	movie_candidates: list[Any] = []
	movie_buckets = [
		plex_snapshot.get('movies_by_tmdb', {}),
		plex_snapshot.get('movies_by_path_tmdb', {}),
		plex_snapshot.get('movies_by_imdb', {}),
	]
	if not strict_keys_only:
		movie_buckets.append(plex_snapshot.get('movies_by_title_year', {}))
	for bucket in movie_buckets:
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

		if not strict_keys_only and matched_movie_id is None:
			norm_title = _normalize_title(getattr(item, 'title', None))
			year = int(getattr(item, 'year', 0) or 0)
			if norm_title and year:
				ids = movies_by_title_year.get((norm_title, year), [])
				if len(ids) == 1:
					matched_movie_id = ids[0]

		if not strict_keys_only and matched_movie_id is None:
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
		if not strict_keys_only:
			series_title = _normalize_title(getattr(series, 'title', None))
			if series_title:
				episodes_by_title_sxe.setdefault((series_title, season_num, ep_num), []).append(episode_id)

	for key, item in (plex_snapshot.get('episodes_by_tvdb_sxe', {}) or {}).items():
		ids = episodes_by_tvdb_sxe.get(key, [])
		if len(ids) == 1 and ids[0] not in result['episode_plex_item_by_episode_id']:
			result['episode_plex_item_by_episode_id'][ids[0]] = item

	if not strict_keys_only:
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
				movie_id = int(getattr(movie, 'id', getattr(placeholder, 'movie_id', 0) or 0) or 0)
				if movie_id and movie_id in movie_plex_item_by_movie_id:
					plex_item = movie_plex_item_by_movie_id.get(movie_id)
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
				episode_id = int(getattr(episode, 'id', getattr(placeholder, 'episode_id', 0) or 0) or 0)
				if episode_id and episode_id in episode_plex_item_by_episode_id:
					plex_item = episode_plex_item_by_episode_id.get(episode_id)
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


def _overlap_refresh_min_interval_seconds() -> int:
	try:
		return max(0, int(getattr(settings, 'MATERIALIZATION_OVERLAP_REFRESH_MIN_INTERVAL_SECONDS', 90) or 90))
	except Exception:
		return 90


def _overlap_refresh_lease_seconds() -> int:
	try:
		return max(0, int(getattr(settings, 'MATERIALIZATION_OVERLAP_REFRESH_LEASE_SECONDS', 180) or 180))
	except Exception:
		return 180


def _can_issue_post_observation_refresh(*, now_mono: float, last_refresh_mono: float | None, refresh_lease_until: float) -> bool:
	if now_mono < float(refresh_lease_until):
		return False
	min_interval = float(_overlap_refresh_min_interval_seconds())
	if last_refresh_mono is not None and (now_mono - float(last_refresh_mono)) < min_interval:
		return False
	return True


def _post_observation_target_scan_state(expected_section_ids: set[int], *, phase: str) -> dict[str, Any]:
	state = get_plex_section_scan_state(expected_section_ids)
	state['phase'] = phase
	return state


def _trigger_post_observation_refresh(expected_section_ids: set[int]) -> dict[str, Any]:
	if not expected_section_ids:
		return {'requested': False, 'reason': 'no_expected_sections', 'refreshed': 0, 'failed': 0}

	section_ids = sorted(int(x) for x in expected_section_ids)
	lease = try_acquire_refresh_lease(
		section_ids=section_ids,
		source='post_observation_idle_refresh',
		min_interval_seconds=_overlap_refresh_min_interval_seconds(),
		lease_seconds=_overlap_refresh_lease_seconds(),
	)
	if not bool(lease.get('allowed', False)):
		return {
			'requested': False,
			'reason': f"throttled:{lease.get('reason')}",
			'refreshed': 0,
			'failed': 0,
		}

	refresh_stats = refresh_plex_section_ids(section_ids)
	return {
		'requested': True,
		'reason': 'refresh_triggered',
		'refreshed': int(refresh_stats.get('refreshed', 0) or 0),
		'failed': int(refresh_stats.get('failed', 0) or 0),
	}


# Sleep durations (seconds) per consecutive no-progress poll.
# Schedule: 5 → 10 → 20 → 20, then stop on the 5th consecutive no-progress.
# Total worst-case sleep: 5 + 10 + 20 + 20 = 55 s.
_NO_PROGRESS_SLEEP_SCHEDULE = [5, 10, 20, 20, 20]


def _placeholder_ids(rows: list[Placeholder]) -> list[int]:
	ids: list[int] = []
	for row in rows:
		row_id = getattr(row, 'id', None)
		if row_id is None:
			continue
		ids.append(int(row_id))
	return ids


def _merge_observation_context(target: dict[str, Any], extra: dict[str, Any]) -> None:
	for key, value in (extra or {}).items():
		if isinstance(value, dict):
			target.setdefault(key, {})
			target[key].update(value)
		else:
			target[key] = value


def _apply_mid_pass_refill(
	session,
	*,
	process_placeholders: list[Placeholder],
	still_unresolved: list[Placeholder],
	next_chunk_start: int,
	observation_context: dict[str, Any],
	plex_snapshot: dict[str, Any] | None,
	pass_stats: dict[str, Any],
	strict_keys_only: bool,
	snapshot_authority_mode: bool,
	mid_pass_refill_callback: Callable[..., list[int]] | None,
) -> tuple[list[Placeholder], list[Placeholder], dict[str, Any], bool]:
	if mid_pass_refill_callback is None:
		return process_placeholders, still_unresolved, observation_context, False

	remaining_placeholders = process_placeholders[next_chunk_start:]
	active_ids = _placeholder_ids(still_unresolved) + _placeholder_ids(remaining_placeholders)
	unresolved_ids = _placeholder_ids(still_unresolved)
	active_id_set = set(active_ids)
	extra_ids = [
		int(x)
		for x in (
			mid_pass_refill_callback(
				active_placeholder_ids=active_ids,
				unresolved_placeholder_ids=unresolved_ids,
				pass_stats=dict(pass_stats),
			)
			or []
		)
		if x is not None and int(x) not in active_id_set
	]
	if not extra_ids:
		return process_placeholders, still_unresolved, observation_context, False

	extra_rows = session.query(Placeholder).filter(Placeholder.id.in_(extra_ids)).all()
	if not extra_rows:
		return process_placeholders, still_unresolved, observation_context, False

	row_by_id = {int(getattr(row, 'id')): row for row in extra_rows if getattr(row, 'id', None) is not None}
	ordered_extra_rows = [
		row_by_id[row_id]
		for row_id in extra_ids
		if row_id in row_by_id
		and bool(getattr(row_by_id[row_id], 'has_placeholder', False))
		and not _is_placeholder_fully_resolved(row_by_id[row_id])
	]
	if not ordered_extra_rows:
		return process_placeholders, still_unresolved, observation_context, False

	extra_prefetch = _build_observation_prefetch(session, ordered_extra_rows)
	extra_reverse_matches = _build_reverse_snapshot_match_maps(
		extra_prefetch,
		plex_snapshot,
		strict_keys_only=True if snapshot_authority_mode else strict_keys_only,
	)
	modified_waiting = False
	added_count = 0

	if snapshot_authority_mode:
		extra_process, extra_waiting = _select_snapshot_present_candidates(
			ordered_extra_rows,
			extra_reverse_matches,
		)
		pass_stats['awaiting_plex_scan_count'] = int(pass_stats.get('awaiting_plex_scan_count', 0) or 0) + len(extra_waiting)
		pass_stats['candidate_count'] = int(pass_stats.get('candidate_count', 0) or 0) + len(extra_process)
		for placeholder in extra_waiting:
			still_unresolved.append(placeholder)
			if bool(getattr(placeholder, 'plex_placeholder_id', None)):
				continue
			if getattr(placeholder, 'media_lookup_error', None) == 'plex_metadata_pending':
				continue
			placeholder.media_lookup_error = 'awaiting_plex_scan'
			placeholder.media_lookup_last_attempt_at = func.now()
			placeholder.updated_at = func.now()
			session.add(placeholder)
			modified_waiting = True
	else:
		extra_process, extra_candidate_count, extra_skipped = _select_snapshot_key_candidates(
			ordered_extra_rows,
			extra_reverse_matches,
			strict_mode=strict_keys_only,
		)
		pass_stats['candidate_count'] = int(pass_stats.get('candidate_count', 0) or 0) + int(extra_candidate_count or 0)
		pass_stats['skipped_not_in_snapshot_keys'] = int(pass_stats.get('skipped_not_in_snapshot_keys', 0) or 0) + int(extra_skipped or 0)
		if strict_keys_only:
			extra_candidate_ids = {int(getattr(p, 'id', 0) or 0) for p in extra_process}
			for placeholder in ordered_extra_rows:
				if int(getattr(placeholder, 'id', 0) or 0) not in extra_candidate_ids:
					still_unresolved.append(placeholder)

	if extra_process:
		process_placeholders.extend(extra_process)
		added_count = len(extra_process)

	if modified_waiting:
		commit_started = time.monotonic()
		try:
			session.commit()
		except Exception:
			session.rollback()
			raise
		pass_stats['db_commit_ms'] += _timing_ms(commit_started)

	if added_count <= 0:
		return process_placeholders, still_unresolved, observation_context, modified_waiting

	_merge_observation_context(observation_context, extra_prefetch)
	_merge_observation_context(observation_context, extra_reverse_matches)
	pass_stats['mid_pass_refill_events'] = int(pass_stats.get('mid_pass_refill_events', 0) or 0) + 1
	pass_stats['mid_pass_refill_added'] = int(pass_stats.get('mid_pass_refill_added', 0) or 0) + added_count
	return process_placeholders, still_unresolved, observation_context, True


def _run_observation_pass(
	session,
	placeholders: list[Placeholder],
	allow_title_fallback: bool = True,
	plex_snapshot: dict[str, Any] | None = None,
	mid_pass_refill_callback: Callable[..., list[int]] | None = None,
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
		'candidate_count': 0,
		'skipped_not_in_snapshot_keys': 0,
		'strict_keys_only': False,
		'snapshot_authority_mode': False,
		'awaiting_plex_scan_count': 0,
		'snapshot_build_ms': 0,
		'match_lookup_ms': 0,
		'status_projection_ms': 0,
		'db_commit_ms': 0,
		'mid_pass_refill_events': 0,
		'mid_pass_refill_added': 0,
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
	snapshot_authority_mode = bool(_is_enabled('plex') and plex_snapshot is not None)
	strict_keys_only = bool(
		snapshot_authority_mode
		and _observation_bulk_strict_keys_only()
		and len(placeholders) >= _observation_strict_keys_min_placeholders()
	)
	pass_stats['strict_keys_only'] = strict_keys_only
	pass_stats['snapshot_authority_mode'] = snapshot_authority_mode
	reverse_matches = _build_reverse_snapshot_match_maps(
		prefetch,
		plex_snapshot,
		strict_keys_only=True if snapshot_authority_mode else strict_keys_only,
	)
	observation_context: dict[str, Any] = {
		**prefetch,
		**reverse_matches,
	}

	if snapshot_authority_mode:
		process_placeholders, awaiting_scan_placeholders = _select_snapshot_present_candidates(
			placeholders,
			reverse_matches,
		)
		candidate_count = len(process_placeholders)
		skipped_count = len(awaiting_scan_placeholders)
		pass_stats['awaiting_plex_scan_count'] = int(skipped_count)
		still_unresolved.extend(awaiting_scan_placeholders)
		for placeholder in awaiting_scan_placeholders:
			if bool(getattr(placeholder, 'plex_placeholder_id', None)):
				continue
			if getattr(placeholder, 'media_lookup_error', None) == 'plex_metadata_pending':
				continue
			placeholder.media_lookup_error = 'awaiting_plex_scan'
			placeholder.media_lookup_last_attempt_at = func.now()
			placeholder.updated_at = func.now()
			session.add(placeholder)
	else:
		process_placeholders, candidate_count, skipped_count = _select_snapshot_key_candidates(
			placeholders,
			reverse_matches,
			strict_mode=strict_keys_only,
		)
	pass_stats['candidate_count'] = int(candidate_count)
	pass_stats['skipped_not_in_snapshot_keys'] = int(skipped_count)

	if strict_keys_only and not snapshot_authority_mode:
		candidate_ids = {int(getattr(p, 'id', 0) or 0) for p in process_placeholders}
		still_unresolved.extend(
			[
				placeholder
				for placeholder in placeholders
				if int(getattr(placeholder, 'id', 0) or 0) not in candidate_ids
			]
		)

	if snapshot_authority_mode and not process_placeholders and still_unresolved:
		commit_started = time.monotonic()
		try:
			session.commit()
		except Exception:
			session.rollback()
			raise
		pass_stats['db_commit_ms'] += _timing_ms(commit_started)

	for chunk_start in range(0, len(process_placeholders), chunk_size):
		if (
			max_pass_seconds > 0
			and (time.monotonic() - pass_started) >= max_pass_seconds
			and pass_stats['chunks_processed'] >= _observation_min_chunks_per_pass()
		):
			still_unresolved.extend(process_placeholders[chunk_start:])
			pass_stats['pass_capped'] = True
			break

		chunk = process_placeholders[chunk_start:chunk_start + chunk_size]
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

		next_chunk_start = chunk_start + chunk_size
		process_placeholders, still_unresolved, observation_context, _ = _apply_mid_pass_refill(
			session,
			process_placeholders=process_placeholders,
			still_unresolved=still_unresolved,
			next_chunk_start=next_chunk_start,
			observation_context=observation_context,
			plex_snapshot=plex_snapshot,
			pass_stats=pass_stats,
			strict_keys_only=strict_keys_only,
			snapshot_authority_mode=snapshot_authority_mode,
			mid_pass_refill_callback=mid_pass_refill_callback,
		)

	progress_delta = pass_stats['plex_progress']
	return still_unresolved, pass_stats, resolved_delta, progress_delta, plex_snapshot


def observe_placeholders_with_polling(
	session,
	placeholders: list[Placeholder],
	allow_title_fallback: bool = False,
	mid_pass_refill_callback: Callable[..., list[int]] | None = None,
	auto_enqueue_trail_on_defer: bool = True,
) -> dict[str, Any]:
	"""Observe placeholder media ids using polling with post-observation scan-state decisions."""
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
		'single_flight_lock_attempts': 0,
		'single_flight_busy': False,
		'passes_capped': 0,
		'timing': {
			'snapshot_build_ms': 0,
			'match_lookup_ms': 0,
			'status_projection_ms': 0,
			'db_commit_ms': 0,
		},
		'mid_pass_refill_events': 0,
		'mid_pass_refill_added': 0,
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
			mid_pass_refill_callback=mid_pass_refill_callback,
		)
		stats['observed_plex'] += pass_stats.get('observed_plex', 0)
		stats['observed_jellyfin'] += pass_stats.get('observed_jellyfin', 0)
		stats['observed_emby'] += pass_stats.get('observed_emby', 0)
		stats['status_updates_plex'] += pass_stats.get('status_updates_plex', 0)
		stats['timing']['snapshot_build_ms'] += int(pass_stats.get('snapshot_build_ms', 0) or 0)
		stats['timing']['match_lookup_ms'] += int(pass_stats.get('match_lookup_ms', 0) or 0)
		stats['timing']['status_projection_ms'] += int(pass_stats.get('status_projection_ms', 0) or 0)
		stats['timing']['db_commit_ms'] += int(pass_stats.get('db_commit_ms', 0) or 0)
		stats['mid_pass_refill_events'] += int(pass_stats.get('mid_pass_refill_events', 0) or 0)
		stats['mid_pass_refill_added'] += int(pass_stats.get('mid_pass_refill_added', 0) or 0)
		stats['resolved_total'] = resolved_delta
		stats['observe_failed'] = max(0, len(unresolved))
		stats['stop_reason'] = 'single_pass_non_plex'
		return stats

	lock_conn = None
	flight_release_reason = 'released'
	try:
		lock_conn, has_single_flight, lock_attempts = _try_acquire_single_flight_lock()
		stats['single_flight_lock_attempts'] = int(lock_attempts)
		if not has_single_flight:
			stats['single_flight_busy'] = True
			stats['observe_failed'] = len(unresolved)
			stats['stop_reason'] = 'single_flight_busy'
			flight_release_reason = 'single_flight_busy'
			logger.info(
				f"Observation single-flight busy; deferring {len(unresolved)} unresolved placeholder(s).",
				extra={'emoji_type': 'info'},
			)
			return stats

		_mark_flight_active(source='observe_placeholders_with_polling', lock_attempts=lock_attempts)

		total_expected = len(unresolved)
		resolved_total = 0
		expected_section_ids = _expected_plex_section_ids(unresolved)

		# Polling loop: adaptive interval 5→10→20→20, stop on 5th consecutive no-progress.
		# Any progress resets the interval back to 5 s.
		no_progress_count = 0
		idle_no_progress_streak = 0
		plex_snapshot_cache: dict[str, Any] | None = None
		snapshot_reuse_streak = 0
		snapshot_cache_passes = _observation_snapshot_cache_passes()

		while unresolved:
			_heartbeat_active_flight()
			stats['attempts'] += 1
			pass_started = time.monotonic()
			start_unresolved = len(unresolved)
			unresolved, pass_stats, resolved_delta, progress_delta, plex_snapshot_cache = _run_observation_pass(
				session,
				unresolved,
				allow_title_fallback=allow_title_fallback,
				plex_snapshot=plex_snapshot_cache,
				mid_pass_refill_callback=mid_pass_refill_callback,
			)
			pass_elapsed = time.monotonic() - pass_started
			stats['observed_plex'] += pass_stats['observed_plex']
			stats['observed_jellyfin'] += pass_stats['observed_jellyfin']
			stats['observed_emby'] += pass_stats['observed_emby']
			stats['status_updates_plex'] += pass_stats['status_updates_plex']
			stats['timing']['snapshot_build_ms'] += int(pass_stats.get('snapshot_build_ms', 0) or 0)
			stats['timing']['match_lookup_ms'] += int(pass_stats.get('match_lookup_ms', 0) or 0)
			stats['timing']['status_projection_ms'] += int(pass_stats.get('status_projection_ms', 0) or 0)
			stats['timing']['db_commit_ms'] += int(pass_stats.get('db_commit_ms', 0) or 0)
			stats['mid_pass_refill_events'] += int(pass_stats.get('mid_pass_refill_events', 0) or 0)
			stats['mid_pass_refill_added'] += int(pass_stats.get('mid_pass_refill_added', 0) or 0)
			resolved_total += resolved_delta
			stats['resolved_total'] = resolved_total
			if pass_stats.get('pass_capped'):
				stats['passes_capped'] += 1
			# Format skip metric based on active mode
			skip_metric_name = 'awaiting_plex_scan' if pass_stats.get('snapshot_authority_mode') else 'skipped_not_in_snapshot_keys'
			skip_metric_value = pass_stats.get('awaiting_plex_scan_count', 0) if pass_stats.get('snapshot_authority_mode') else pass_stats.get('skipped_not_in_snapshot_keys', 0)
			
			logger.info(
				f"Observation poll pass #{stats['attempts']}: "
				f"start_unresolved={start_unresolved} remaining_unresolved={len(unresolved)} "
				f"resolved_delta={resolved_delta} progress_delta={progress_delta} "
				f"candidates_from_snapshot={int(pass_stats.get('candidate_count', 0) or 0)} "
				f"{skip_metric_name}={int(skip_metric_value or 0)} "
				f"strict_keys_only={bool(pass_stats.get('strict_keys_only', False))} "
				f"status_updates_plex={pass_stats['status_updates_plex']} "
				f"elapsed={pass_elapsed:.1f}s "
				f"timing_ms[snapshot_build={int(pass_stats.get('snapshot_build_ms', 0) or 0)} "
				f"match={int(pass_stats.get('match_lookup_ms', 0) or 0)} "
				f"project={int(pass_stats.get('status_projection_ms', 0) or 0)} "
				f"commit={int(pass_stats.get('db_commit_ms', 0) or 0)}] "
				f"chunks={int(pass_stats.get('chunks_processed', 0) or 0)} "
				f"pass_capped={bool(pass_stats.get('pass_capped', False))}.",
				extra={'emoji_type': 'info'},
			)

			if not unresolved:
				stats['stop_reason'] = 'all_resolved'
				flight_release_reason = 'all_resolved'
				logger.info(
					f"Observation complete: all expected items are ready ({resolved_total}/{total_expected}).",
					extra={'emoji_type': 'success'},
				)
				break

			# Reuse scoped snapshots briefly for speed, but force periodic refresh so
			# newly scanned Plex content can be discovered on subsequent passes.
			if _is_enabled('plex'):
				if progress_delta <= 0:
					# No progress can indicate a stale snapshot while Plex is still ingesting.
					# Invalidate immediately so the next pass rebuilds from fresh scan state.
					plex_snapshot_cache = None
					snapshot_reuse_streak = 0
				elif plex_snapshot_cache is not None and snapshot_cache_passes > 0:
					snapshot_reuse_streak += 1
					if snapshot_reuse_streak >= snapshot_cache_passes:
						plex_snapshot_cache = None
						snapshot_reuse_streak = 0
				else:
					plex_snapshot_cache = None
					snapshot_reuse_streak = 0

			if progress_delta > 0:
				# Progress: identity found or metadata confirmed — reset escalation state.
				no_progress_count = 0
				idle_no_progress_streak = 0
				time.sleep(_NO_PROGRESS_SLEEP_SCHEDULE[0])
				continue

			# No progress this pass.
			scan_state = _post_observation_target_scan_state(expected_section_ids, phase='post_observation_no_progress')
			refresh_requested = False
			if bool(scan_state.get('all_target_idle', False)):
				idle_no_progress_streak += 1

				refresh_result = _trigger_post_observation_refresh(expected_section_ids)
				refresh_requested = bool(refresh_result.get('requested', False))
				logger.info(
					"Post-observation idle refresh decision: "
					f"requested={refresh_requested} "
					f"refreshed={int(refresh_result.get('refreshed', 0) or 0)} "
					f"failed={int(refresh_result.get('failed', 0) or 0)} "
					f"reason={refresh_result.get('reason')}.",
					extra={'emoji_type': 'info'},
				)

				if idle_no_progress_streak >= 2 and not refresh_requested:
					stats['stopped_early'] = True
					stats['stop_reason'] = 'idle_no_progress_deferred'
					flight_release_reason = 'idle_no_progress_deferred'
					logger.info(
						"Observation reached two consecutive idle/no-progress passes with no refresh lease; deferring follow-up.",
						extra={'emoji_type': 'info'},
					)
					break
			else:
				# Busy/unknown scan state never refreshes and does not count toward idle defer streak.
				idle_no_progress_streak = 0

			if no_progress_count >= len(_NO_PROGRESS_SLEEP_SCHEDULE):
				if bool(scan_state.get('any_target_scanning', False)):
					sleep_seconds = max(_NO_PROGRESS_SLEEP_SCHEDULE[-1], 20)
					no_progress_count = max(0, len(_NO_PROGRESS_SLEEP_SCHEDULE) - 1)
					logger.info(
						"No-progress schedule exhausted, but target library is still scanning; continuing same-slice observation.",
						extra={'emoji_type': 'info'},
					)
					time.sleep(sleep_seconds)
					continue

				# Exhausted all retries — stop without sleeping.
				stats['stopped_early'] = True
				stats['stop_reason'] = 'no_progress_max_polls'
				flight_release_reason = 'no_progress_max_polls'
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
	finally:
		_mark_flight_released(reason=flight_release_reason)
		_release_single_flight_lock(lock_conn)

	stats['observe_failed'] = len(unresolved)
	if not stats.get('stop_reason') and unresolved:
		stats['stop_reason'] = 'unresolved_after_observer'

	if auto_enqueue_trail_on_defer and unresolved and stats.get('stop_reason') in {'idle_no_progress_deferred', 'no_progress_max_polls'}:
		unresolved_ids = [int(p.id) for p in unresolved if getattr(p, 'id', None)]
		if unresolved_ids:
			try:
				# Local import to avoid circular dependency (observation_trail imports us).
				from services.source_of_truth.observation_trail import enqueue_observation_trail  # noqa: PLC0415
				delay_seconds = 900 if stats.get('stop_reason') == 'idle_no_progress_deferred' else 300
				enqueue_result = enqueue_observation_trail(
					session,
					placeholder_ids=unresolved_ids,
					source=str(stats.get('stop_reason') or 'poller_deferred'),
					delay_seconds=delay_seconds,
				)
				logger.info(
					f"Enqueued follow-up observation trail after stop_reason={stats.get('stop_reason')} "
					f"for {len(unresolved_ids)} unresolved item(s): "
					f"job_id={enqueue_result.get('job_id')}, enqueued={enqueue_result.get('enqueued')}.",
					extra={'emoji_type': 'info'},
				)
			except Exception as exc:
				logger.warning(
					f"Could not enqueue follow-up observation trail after idle/no-progress defer: {exc}",
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

