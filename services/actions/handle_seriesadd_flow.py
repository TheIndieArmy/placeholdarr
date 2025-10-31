from services.integrations import update_placeholder_status, delayed_placeholders, enrich_series_from_sonarr, enrich_comprehensive_metadata, check_series_ready_for_enrichment
from services.plex_client import update_plex_title_status, refresh_plex_dummy, verify_dummy_scan_plex, retry_failed_plex_title_updates
from services.jellyfin_client import (
    update_jellyfin_nfo_status, update_jellyfin_title_status, 
    refresh_jellyfin_dummy, verify_dummy_scan_jellyfin, 
    retry_failed_jellyfin_title_updates, create_jellyfin_nfo
)
from core import logger
from services.postgres.models import Series

def steps():
    return [
        delayed_placeholders,
        check_series_ready_for_enrichment,
        enrich_comprehensive_metadata,
        {
            'jellyfin': [
                update_placeholder_status,
                create_jellyfin_nfo,
                refresh_jellyfin_dummy,
                verify_dummy_scan_jellyfin, 
                # verify_dummy_scan_jellyfin
            ],
            'plex': [
                update_placeholder_status, 
                refresh_plex_dummy, 
                verify_dummy_scan_plex, 
                update_plex_title_status, 
                verify_dummy_scan_plex, 
                retry_failed_plex_title_updates
            ]
        }
    ]


# def refresh_jellyfin_dummy(session, ent_id, model):
#     obj = session.query(model).get(ent_id)
#     if not obj:
#         return False

#     action = getattr(obj, 'action', '') or ''
#     update_type = "Created" if any(k in action for k in ('add','import')) else "Updated"
#     path = getattr(obj, 'dummypath', None)
#     if path:
#         refresh_jellyfin_item(path, update_type)
#         return True
#     return False

