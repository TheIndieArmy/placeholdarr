from services.integrations import update_placeholder_status, delayed_placeholders, flow_enrich_series
from services.plex_client import update_plex_title_status, refresh_plex_dummy, verify_dummy_scan_plex, retry_failed_plex_title_updates
from services.jellyfin_client import update_jellyfin_title_status, refresh_jellyfin_dummy, verify_dummy_scan_jellyfin, retry_failed_jellyfin_title_updates

def steps():
    return [
        flow_enrich_series,
        delayed_placeholders,
        {
            'jellyfin': [refresh_jellyfin_dummy, verify_dummy_scan_jellyfin, update_placeholder_status, update_jellyfin_title_status, verify_dummy_scan_jellyfin, retry_failed_jellyfin_title_updates],
            'plex': [refresh_plex_dummy, verify_dummy_scan_plex, update_placeholder_status, update_plex_title_status, verify_dummy_scan_plex, retry_failed_plex_title_updates]
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

