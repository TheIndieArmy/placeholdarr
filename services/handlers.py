import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import text

from core.config import settings
from core.logger import logger
from services.event_normalization import infer_raw_event_type, normalize_event_type
from services.postgres.db import get_session
from services.postgres.models import EventLog, Job

# Collapse Radarr/Sonarr "MovieAdded"/"SeriesAdd" with Collections synthetic
# ``{"movie":{"id":N}}`` ingest into one EventLog + job within this window.
_ARR_ADD_DEDUPE_WINDOW = timedelta(seconds=15)
_ARR_ADD_EVENT_TYPES = frozenset({
    "movie_added",
    "movieadd",
    "movieadded",
    "series_added",
    "seriesadd",
})
_ARR_ADD_MOVIE_TYPES = frozenset({"movie_added", "movieadd", "movieadded"})
_ARR_ADD_SERIES_TYPES = frozenset({"series_added", "seriesadd"})
_PLAYBACK_DEDUPE_WINDOW = timedelta(seconds=15)
_PLAYBACK_EVENT_TYPES = frozenset({"playback_start", "playback.start", "playbackstart"})


def _payload_preview(payload: Dict[str, Any], max_chars: int = 2000) -> str:
    """Return a compact, bounded payload preview for log lines."""
    try:
        text = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
    except Exception:
        text = str(payload)

    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...<truncated:{len(text) - max_chars} chars>"


def _arr_add_entity_from_payload(
    payload: Dict[str, Any] | Any,
    event_type: str,
) -> tuple[str, int] | None:
    """Return ``('movie'|'series', arr_id)`` for MovieAdded / SeriesAdd payloads."""
    if not isinstance(payload, dict):
        return None
    et = str(event_type or "").strip().lower()
    if et in _ARR_ADD_MOVIE_TYPES:
        movie = payload.get("movie") if isinstance(payload.get("movie"), dict) else {}
        raw = (
            movie.get("id")
            or payload.get("movieId")
            or payload.get("movie_id")
            or payload.get("id")
        )
        kind = "movie"
    elif et in _ARR_ADD_SERIES_TYPES:
        series = payload.get("series") if isinstance(payload.get("series"), dict) else {}
        raw = (
            series.get("id")
            or payload.get("seriesId")
            or payload.get("series_id")
            or payload.get("id")
        )
        kind = "series"
    else:
        return None
    try:
        arr_id = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return None
    if arr_id <= 0:
        return None
    return kind, arr_id


def _arr_add_payload_richness(payload: Dict[str, Any] | Any, kind: str) -> int:
    """Prefer full *arr webhook bodies over synthetic ``{id}``-only ingest payloads."""
    if not isinstance(payload, dict):
        return 0
    entity = payload.get("movie" if kind == "movie" else "series")
    if not isinstance(entity, dict):
        return 0
    score = 0
    if entity.get("title"):
        score += 3
    if entity.get("tmdbId") or entity.get("tmdbid") or entity.get("tvdbId") or entity.get("tvdbid"):
        score += 2
    if entity.get("path") or entity.get("folderName"):
        score += 1
    # More keys than bare ``id`` means a real webhook body.
    if len(entity.keys()) > 1:
        score += 1
    return score


def _event_payload_dict(event: EventLog) -> Dict[str, Any]:
    raw = event.payload
    return dict(raw) if isinstance(raw, dict) else {}


def _find_recent_arr_add_event(
    session,
    *,
    source: str,
    kind: str,
    arr_id: int,
    event_types: frozenset[str],
) -> EventLog | None:
    cutoff = datetime.now(timezone.utc) - _ARR_ADD_DEDUPE_WINDOW
    candidates = (
        session.query(EventLog)
        .filter(
            EventLog.source == source,
            EventLog.event_type.in_(tuple(event_types)),
            EventLog.created_at >= cutoff,
            EventLog.status.in_(("PENDING", "PROCESSING", "CLAIMED", "DONE")),
        )
        .order_by(EventLog.id.desc())
        .limit(40)
        .all()
    )
    for event in candidates:
        payload = _event_payload_dict(event)
        # Strip internal meta for id extraction.
        clean = {k: v for k, v in payload.items() if k != "_event_meta"}
        entity = _arr_add_entity_from_payload(clean, str(event.event_type or ""))
        if entity == (kind, arr_id):
            return event
    return None


def _advisory_lock_key(source: str, kind: str, arr_id: int) -> int:
    # Stable positive int32 for pg_advisory_xact_lock (must not use randomized hash()).
    import zlib

    raw = f"{str(source).lower()}|{str(kind)}|{int(arr_id)}".encode("utf-8")
    return int(zlib.crc32(raw) & 0x7FFFFFFF)


def _playback_dedupe_token(payload: Dict[str, Any] | Any) -> str | None:
    """Stable token for collapsing duplicate media-server playback.start posts."""
    if not isinstance(payload, dict):
        return None
    item = payload.get("Item") if isinstance(payload.get("Item"), dict) else None
    if item is None and isinstance(payload.get("item"), dict):
        item = payload.get("item")
    if isinstance(item, dict):
        for key in ("PresentationUniqueKey", "Path", "path", "Id", "id"):
            val = str(item.get(key) or "").strip()
            if val:
                return f"item:{key}:{val}"
    for key in ("file_path", "filePath", "path", "Path", "rating_key", "ratingKey"):
        val = str(payload.get(key) or "").strip()
        if val:
            return f"top:{key}:{val}"
    media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
    ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
    for key in ("tmdb", "tmdbId", "imdb", "imdbId"):
        val = str(ids.get(key) or "").strip()
        if val:
            return f"media:{key}:{val}"
    return None


def _find_recent_playback_event(
    session,
    *,
    source: str,
    token: str,
) -> EventLog | None:
    cutoff = datetime.now(timezone.utc) - _PLAYBACK_DEDUPE_WINDOW
    candidates = (
        session.query(EventLog)
        .filter(
            EventLog.source == source,
            EventLog.event_type.in_(tuple(_PLAYBACK_EVENT_TYPES)),
            EventLog.created_at >= cutoff,
            EventLog.status.in_(("PENDING", "PROCESSING", "CLAIMED", "DONE")),
        )
        .order_by(EventLog.id.desc())
        .limit(30)
        .all()
    )
    for event in candidates:
        payload = _event_payload_dict(event)
        clean = {k: v for k, v in payload.items() if k != "_event_meta"}
        existing = _playback_dedupe_token(clean)
        if existing and existing == token:
            return event
    return None


def _allowed_webhook_instances() -> tuple[str, ...]:
    return tuple(getattr(settings, 'allowed_webhook_instance_keys', ()) or ())


def get_configured_webhook_instances() -> dict[str, bool]:
    configured: dict[str, bool] = {}
    for item in (getattr(settings, 'configured_arr_instances', []) or []):
        key = str(item.get('instance_key') or '').strip().lower()
        ok = bool(str(item.get('url') or '').strip() and str(item.get('api_key') or '').strip())
        if key:
            configured[key] = ok
        for a in item.get('instance_key_aliases') or []:
            av = str(a or '').strip().lower()
            if av:
                configured[av] = ok

    # Native/Tautulli ingest follows the saved per-player notifier so leftover
    # agents still posting here are ignored when Tracearr is selected.
    tautulli_ok = (
        settings.tautulli_playback_configured()
        if hasattr(settings, "tautulli_playback_configured")
        else bool(getattr(settings, "ENABLE_PLEX", False))
    )
    jellyfin_ok = (
        settings.jellyfin_native_playback_configured()
        if hasattr(settings, "jellyfin_native_playback_configured")
        else bool(getattr(settings, "ENABLE_JELLYFIN", False))
    )
    emby_ok = (
        settings.emby_native_playback_configured()
        if hasattr(settings, "emby_native_playback_configured")
        else bool(getattr(settings, "ENABLE_EMBY", False))
    )
    configured.update(
        {
            settings.TAUTULLI_INSTANCE_KEY: tautulli_ok,
            settings.JELLYFIN_INSTANCE_KEY: jellyfin_ok,
            settings.EMBY_INSTANCE_KEY: emby_ok,
            getattr(settings, "TRACEARR_INSTANCE_KEY", "tracearr"): bool(
                settings.tracearr_playback_configured()
                if hasattr(settings, "tracearr_playback_configured")
                else False
            ),
        }
    )
    return configured


def _allowed_instance_list() -> str:
    return ', '.join(_allowed_webhook_instances())


def resolve_canonical_webhook_instance(
    instance: str | None,
    instance_id: str | None,
) -> tuple[str | None, str | None]:
    """Map webhook query params to the canonical instance token stored on EventLog.source.

    - ``?instance_id=`` resolves to the row's current ``instance_key`` (stable id in URL).
    - ``?instance=`` accepts playback keys, current ARR ``instance_key``, or any saved alias.
    Returns (canonical_token, error_reason).
    """
    inst = str(instance or '').strip().lower() or None
    iid = str(instance_id or '').strip().lower() or None
    if inst and iid:
        return None, 'Provide only one of instance or instance_id query parameter.'
    if iid:
        for item in (getattr(settings, 'configured_arr_instances', []) or []):
            row_id = str(item.get('instance_id') or '').strip().lower()
            if row_id != iid:
                continue
            key = str(item.get('instance_key') or '').strip().lower()
            if not key:
                return None, f'instance_id {iid} is missing instance_key in configuration.'
            ok = bool(str(item.get('url') or '').strip() and str(item.get('api_key') or '').strip())
            if not ok:
                return None, f'instance_id {iid} is recognized but not fully configured (url/api key).'
            return key, None
        return None, f'Unknown instance_id: {iid}. Allowed values include configured ARR instance ids.'
    if inst:
        playback = set(getattr(settings, 'playback_source_instance_keys', ()) or ())
        if inst in playback:
            configured = get_configured_webhook_instances()
            if configured.get(inst):
                return inst, None
            return None, f'Webhook instance parameter is recognized but not enabled: {inst}.'
        for item in (getattr(settings, 'configured_arr_instances', []) or []):
            key = str(item.get('instance_key') or '').strip().lower()
            aliases = item.get('instance_key_aliases') if isinstance(item.get('instance_key_aliases'), list) else []
            alias_set = {str(a or '').strip().lower() for a in aliases if str(a or '').strip()}
            if inst == key or inst in alias_set:
                ok = bool(str(item.get('url') or '').strip() and str(item.get('api_key') or '').strip())
                if not ok:
                    return None, f'Instance {inst} maps to an incomplete ARR configuration.'
                return key, None
        return (
            None,
            f'Invalid webhook instance query parameter: {inst}. '
            f'Check the webhook URL query parameters. '
            f'Allowed values: {_allowed_instance_list()}.',
        )
    return None, (
        'Missing required webhook routing query parameter. '
        'Provide either instance=<key> or instance_id=<stable-id> (see dashboard webhook setup).'
    )


def validate_webhook_instance(instance: str | None) -> str | None:
    normalized = str(instance or '').strip().lower()
    if not normalized:
        return (
            'Missing required webhook instance query parameter. '
            'Check the webhook URL query parameters. '
            f'Allowed values: {_allowed_instance_list()}.'
        )

    if normalized not in _allowed_webhook_instances():
        return (
            f'Invalid webhook instance query parameter: {normalized}. '
            'Check the webhook URL query parameters. '
            f'Allowed values: {_allowed_instance_list()}.'
        )

    configured = get_configured_webhook_instances()
    if not configured.get(normalized, False):
        return (
            f'Webhook instance parameter is recognized but not configured in Placeholdarr: {normalized}. '
            'Check the webhook URL query parameters and the corresponding service settings.'
        )

    return None


def _infer_event_type(payload: Dict[str, Any], instance: str | None = None) -> str:
    raw_event_type = infer_raw_event_type(payload)
    normalized = normalize_event_type(raw_event_type, instance=instance)
    return normalized.canonical_event_type


def _infer_event_meta(payload: Dict[str, Any], instance: str | None = None) -> dict[str, Any]:
    raw_event_type = infer_raw_event_type(payload)
    normalized = normalize_event_type(raw_event_type, instance=instance)
    return {
        'raw_event_type': normalized.raw_event_type,
        'canonical_event_type': normalized.canonical_event_type,
        'matched_alias': normalized.matched_alias,
        'is_known': normalized.is_known,
    }


def _enqueue_event_job(session, event_log_id: int, event_type: str):
    from services.source_of_truth.job_priority import default_priority_for

    session.add(
        Job(
            job_type='webhook_event',
            payload={'event_log_id': event_log_id, 'event_type': event_type},
            status='PENDING',
            max_attempts=10,
            priority=default_priority_for('webhook_event'),
        )
    )


def build_webhook_source(instance: str | None = None) -> str:
    """Format source identifier from instance parameter.
    
    Args:
        instance: instance identifier (e.g. 'radarr_std', 'radarr_4k', 'sonarr_std', 'tautulli')
    
    Returns:
        source string for EventLog storage (e.g. 'webhook:radarr_std')
    """
    instance_str = str(instance).strip().lower() if instance else 'unknown'
    return f'webhook:{instance_str}'


def validate_webhook_payload(
    payload: Dict[str, Any],
    instance: str | None = None,
) -> tuple[bool, str | None, str, dict[str, Any]]:
    """Validate webhook payload and return (ok, reason, canonical_event_type, event_meta)."""
    event_meta = _infer_event_meta(payload, instance=instance)
    event_type = str(event_meta.get('canonical_event_type') or 'unknown')
    reason = validate_webhook_instance(instance)
    if reason:
        return False, reason, event_type, event_meta

    normalized_instance = str(instance or '').strip().lower()
    playback_sources = set(getattr(settings, 'playback_source_instance_keys', ()) or ())
    if event_type == 'playback_start' and normalized_instance not in playback_sources:
        return (
            False,
            'Playback events must use a configured media-server webhook instance key.',
            event_type,
            event_meta,
        )

    return True, None, event_type, event_meta


def _apply_gated_playback_search_queued(session, payload: Dict[str, Any]) -> int:
    """When a playback_start arrives while the startup-sync gate is closed, set a
    visible "Search queued" status on matching placeholders so users get immediate
    feedback. Returns intents applied (0 if no matches or anything fails).

    Best-effort: any exception is swallowed (logged at debug); the webhook itself
    is still accepted and queued for processing once the gate opens.
    """
    try:
        from services.startup_gate import startup_sync_complete
        if startup_sync_complete.is_set():
            return 0
        from services.source_of_truth.event_playback import apply_search_queued_for_playback
        return int(apply_search_queued_for_playback(session, payload))
    except Exception as exc:
        logger.debug(
            f"Failed to apply gated SEARCH_QUEUED status: {exc}",
            extra={'emoji_type': 'debug'},
        )
        return 0


def handle_webhook(
    payload: Dict[str, Any],
    instance: str | None = None,
) -> Dict[str, Any]:
    """Persist webhook payload durably and enqueue a worker job.
    
    Args:
        payload: webhook payload from ARR/Tautulli/Tracearr
        instance: instance identifier (e.g. 'radarr_std', 'tautulli', 'tracearr')
    
    Returns:
        dict with status ('accepted' or 'rejected'), event_log_id (if accepted), and reason (if rejected)
    """
    if isinstance(payload, dict):
        from services.tracearr_playback import adapt_tracearr_webhook_payload, is_tracearr_instance

        if is_tracearr_instance(instance, getattr(settings, "TRACEARR_INSTANCE_KEY", "tracearr")):
            payload = adapt_tracearr_webhook_payload(payload)

    session = get_session()
    try:
        ok, reason, event_type, event_meta = validate_webhook_payload(payload, instance)
        if not ok:
            logger.warning(
                f'Rejected webhook event {event_type}: {reason}',
                extra={'emoji_type': 'warning'},
            )
            return {'status': 'rejected', 'reason': reason, 'event_type': event_type}

        source = build_webhook_source(instance)
        payload_to_store: Dict[str, Any]
        if isinstance(payload, dict):
            payload_to_store = dict(payload)
            payload_to_store['_event_meta'] = event_meta
        else:
            payload_to_store = {'raw': payload, '_event_meta': event_meta}

        # Deduplicate MovieAdded / SeriesAdd: Collections synthetic ingest and the
        # real *arr webhook often arrive within milliseconds for the same title.
        et_l = str(event_type or "").strip().lower()
        if et_l in _ARR_ADD_EVENT_TYPES and isinstance(payload, dict):
            entity = _arr_add_entity_from_payload(payload, et_l)
            if entity is not None:
                kind, arr_id = entity
                type_set = _ARR_ADD_MOVIE_TYPES if kind == "movie" else _ARR_ADD_SERIES_TYPES
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:k)"),
                    {"k": _advisory_lock_key(source, kind, arr_id)},
                )
                existing = _find_recent_arr_add_event(
                    session,
                    source=source,
                    kind=kind,
                    arr_id=arr_id,
                    event_types=type_set,
                )
                if existing is not None:
                    existing_payload = _event_payload_dict(existing)
                    existing_clean = {
                        k: v for k, v in existing_payload.items() if k != "_event_meta"
                    }
                    new_score = _arr_add_payload_richness(payload, kind)
                    old_score = _arr_add_payload_richness(existing_clean, kind)
                    if new_score > old_score and str(existing.status or "").upper() == "PENDING":
                        merged = dict(payload_to_store)
                        existing.payload = merged
                        existing.updated_at = datetime.now(timezone.utc)
                        session.add(existing)
                        session.commit()
                        logger.info(
                            f"Deduped webhook event {event_type} into pending "
                            f"event_log_id={existing.id} instance={instance} "
                            f"{kind}_id={arr_id} (enriched payload)",
                            extra={"emoji_type": "info"},
                        )
                    else:
                        session.commit()
                        logger.info(
                            f"Deduped webhook event {event_type} as duplicate of "
                            f"event_log_id={existing.id} instance={instance} "
                            f"{kind}_id={arr_id} existing_status={existing.status}",
                            extra={"emoji_type": "info"},
                        )
                    return {
                        "status": "accepted",
                        "event_log_id": int(existing.id),
                        "event_type": event_type,
                        "deduped": True,
                        "deduped_of": int(existing.id),
                        "gated_search_queued_intents": 0,
                    }

        if et_l in _PLAYBACK_EVENT_TYPES and isinstance(payload, dict):
            token = _playback_dedupe_token(payload)
            if token:
                import zlib

                lock = int(zlib.crc32(f"{source}|playback|{token}".encode("utf-8")) & 0x7FFFFFFF)
                session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock})
                existing = _find_recent_playback_event(session, source=source, token=token)
                if existing is not None:
                    session.commit()
                    logger.info(
                        f"Deduped webhook event {event_type} as duplicate of "
                        f"event_log_id={existing.id} instance={instance} token={token}",
                        extra={"emoji_type": "info"},
                    )
                    return {
                        "status": "accepted",
                        "event_log_id": int(existing.id),
                        "event_type": event_type,
                        "deduped": True,
                        "deduped_of": int(existing.id),
                        "gated_search_queued_intents": 0,
                    }

        event = EventLog(
            event_type=event_type,
            source=source,
            payload=payload_to_store,
            status='PENDING',
            attempts=0,
            max_attempts=10,
            updated_at=datetime.now(timezone.utc),
        )
        session.add(event)
        session.flush()
        _enqueue_event_job(session, event.id, event_type)

        # If this is a playback_start event arriving while the startup-sync gate
        # is closed, set an immediate "Search queued" status on matching
        # placeholders so the user gets visible feedback right away. The worker
        # will eventually process the queued Job and transition through the
        # normal SEARCHING -> DOWNLOADING -> ... lifecycle once the gate opens.
        gated_intents_applied = 0
        if event_type == 'playback_start' and isinstance(payload, dict):
            gated_intents_applied = _apply_gated_playback_search_queued(session, payload)

        session.commit()
        logger.info(
            f'Accepted webhook event {event_type} as event_log_id={event.id} instance={instance} '
            f'gated_search_queued={gated_intents_applied} payload={_payload_preview(payload_to_store)}',
            extra={'emoji_type': 'info'},
        )
        return {
            'status': 'accepted',
            'event_log_id': event.id,
            'event_type': event_type,
            'gated_search_queued_intents': int(gated_intents_applied),
        }
    except Exception as e:
        session.rollback()
        logger.error(f'Failed to persist webhook event: {e}', extra={'emoji_type': 'error'})
        raise
    finally:
        session.close()


def enqueue_synthetic_arr_adds(
    *,
    instance_key: str,
    movie: bool,
    lookups: list[Dict[str, Any]],
) -> int:
    """Enqueue MovieAdded/SeriesAdd-equivalent jobs for titles already in *arr.

    Used after Collections add when lookup reports the title is already present
    (``skipped``). Those cases do not emit a new *arr add webhook, so Placeholdarr
    still needs an ingest kick to create/update placeholders. Fresh successful
    adds rely on the real *arr webhook instead.
    """
    queued = 0
    key = str(instance_key or "").strip().lower()
    if not key or not lookups:
        return 0
    for lookup in lookups:
        if not isinstance(lookup, dict):
            continue
        try:
            arr_id = int(lookup.get("id") or 0)
        except (TypeError, ValueError):
            arr_id = 0
        if arr_id <= 0:
            continue
        payload: Dict[str, Any] = (
            {"eventType": "MovieAdded", "movie": {"id": arr_id}}
            if movie
            else {"eventType": "SeriesAdd", "series": {"id": arr_id}}
        )
        try:
            handle_webhook(payload, instance=key)
            queued += 1
        except Exception as exc:
            logger.warning(
                f"Failed to enqueue synthetic ARR add ingest instance={key} arr_id={arr_id}: {exc}",
                extra={"emoji_type": "warning"},
            )
    if queued:
        logger.info(
            f"Enqueued {queued} synthetic {'movie' if movie else 'series'} add ingest job(s) instance={key}",
            extra={"emoji_type": "info"},
        )
    return queued


def handle_event(event: Dict[str, Any]) -> bool:
    """Compatibility entrypoint used by some scripts/tests."""
    instance = None
    if isinstance(event, dict):
        instance = event.get('instance') or event.get('_instance')
    handle_webhook(event, instance=instance)
    return True
