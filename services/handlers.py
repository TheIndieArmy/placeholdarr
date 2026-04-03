import json
from datetime import datetime, timezone
from typing import Any, Dict

from core.config import settings
from core.logger import logger
from services.event_normalization import infer_raw_event_type, normalize_event_type
from services.postgres.db import get_session
from services.postgres.models import EventLog, Job


def _payload_preview(payload: Dict[str, Any], max_chars: int = 2000) -> str:
    """Return a compact, bounded payload preview for log lines."""
    try:
        text = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
    except Exception:
        text = str(payload)

    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...<truncated:{len(text) - max_chars} chars>"


def _allowed_webhook_instances() -> tuple[str, ...]:
    return tuple(getattr(settings, 'allowed_webhook_instance_keys', ()) or ())


def get_configured_webhook_instances() -> dict[str, bool]:
    return {
        settings.RADARR_STD_INSTANCE_KEY: bool(settings.RADARR_URL and settings.RADARR_API_KEY),
        settings.RADARR_4K_INSTANCE_KEY: bool(settings.RADARR_4K_URL and settings.RADARR_4K_API_KEY),
        settings.SONARR_STD_INSTANCE_KEY: bool(settings.SONARR_URL and settings.SONARR_API_KEY),
        settings.SONARR_4K_INSTANCE_KEY: bool(settings.SONARR_4K_URL and settings.SONARR_4K_API_KEY),
        settings.TAUTULLI_INSTANCE_KEY: bool(getattr(settings, 'ENABLE_PLEX', False)),
        settings.JELLYFIN_INSTANCE_KEY: bool(getattr(settings, 'ENABLE_JELLYFIN', False)),
        settings.EMBY_INSTANCE_KEY: bool(getattr(settings, 'ENABLE_EMBY', False)),
    }


def _allowed_instance_list() -> str:
    return ', '.join(_allowed_webhook_instances())


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
    session.add(
        Job(
            job_type='webhook_event',
            payload={'event_log_id': event_log_id, 'event_type': event_type},
            status='PENDING',
            max_attempts=10,
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


def handle_webhook(
    payload: Dict[str, Any],
    instance: str | None = None,
) -> Dict[str, Any]:
    """Persist webhook payload durably and enqueue a worker job.
    
    Args:
        payload: webhook payload from ARR/Tautulli
        instance: instance identifier (e.g. 'radarr_std', 'radarr_4k', 'sonarr_std', 'tautulli')
    
    Returns:
        dict with status ('accepted' or 'rejected'), event_log_id (if accepted), and reason (if rejected)
    """
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
        session.commit()
        logger.info(
            f'Accepted webhook event {event_type} as event_log_id={event.id} instance={instance} payload={_payload_preview(payload_to_store)}',
            extra={'emoji_type': 'info'},
        )
        return {'status': 'accepted', 'event_log_id': event.id, 'event_type': event_type}
    except Exception as e:
        session.rollback()
        logger.error(f'Failed to persist webhook event: {e}', extra={'emoji_type': 'error'})
        raise
    finally:
        session.close()


def handle_event(event: Dict[str, Any]) -> bool:
    """Compatibility entrypoint used by some scripts/tests."""
    instance = None
    if isinstance(event, dict):
        instance = event.get('instance') or event.get('_instance')
    handle_webhook(event, instance=instance)
    return True
