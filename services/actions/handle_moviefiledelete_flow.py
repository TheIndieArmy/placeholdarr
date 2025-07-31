from services.plex_client import update_plex_title_status, refresh_plex_dummy, refresh_plex_arr_path, verify_dummy_scan_plex, retry_failed_plex_title_updates
from services.jellyfin_client import refresh_jellyfin_dummy, update_jellyfin_title_status, verify_dummy_scan_jellyfin, retry_failed_jellyfin_title_updates, refresh_jellyfin_arr_path
from services.integrations import update_placeholder_status, delayed_placeholders

def steps():
    return [
        delayed_placeholders,
        {
            'jellyfin': [refresh_jellyfin_dummy, refresh_jellyfin_arr_path, verify_dummy_scan_jellyfin, update_placeholder_status, update_jellyfin_title_status, verify_dummy_scan_jellyfin, retry_failed_jellyfin_title_updates],
            'plex': [refresh_plex_dummy, refresh_plex_arr_path, verify_dummy_scan_plex, update_placeholder_status, update_plex_title_status, verify_dummy_scan_plex, retry_failed_plex_title_updates]
        }
    ]
