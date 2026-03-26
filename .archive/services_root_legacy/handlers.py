import logging
from typing import Any, Dict
from core.logger import logger

log = logging.getLogger('services.handlers')


def handle_webhook(payload: Dict[str, Any], source_port: int = None) -> Dict[str, Any]:
    """Minimal webhook handler stub.

    This function intentionally performs minimal work: it logs the payload and
    returns an acknowledgement. The real handler logic will be implemented
    progressively; keeping a stub here prevents imports from failing.
    """
    try:
        etype = payload.get('eventType') if isinstance(payload, dict) else None
    except Exception:
        etype = None
    log.info(f"Received webhook (eventType={etype}) from port={source_port}")
    # Return a simple accepted dict so callers/tests can inspect a response
    return {'status': 'accepted', 'eventType': etype}


def handle_event(event: Dict[str, Any]) -> bool:
    """Generic event handler stub used in tests.

    Real implementations will be added later. For now, just log and return True.
    """
    try:
        log.info(f"handle_event invoked: {event}")
    except Exception:
        pass
    return True


def enqueue_import_list_job(session, series_tvdb: list):
    """Placeholder for enqueueing import-list jobs if needed later.

    Currently a no-op to keep existing callers working.
    """
    log.debug(f"enqueue_import_list_job stub called for {len(series_tvdb) if series_tvdb else 0} series")
    return None
