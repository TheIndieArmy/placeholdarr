from services.plex_client import update_plex_title_status
from services.jellyfin_client import (
    refresh_jellyfin_dummy, 
    verify_dummy_scan_jellyfin, 
    refresh_jellyfin_arr_path, create_jellyfin_nfo, verify_arr_scan_jellyfin, reset_jellyfin_id
)
from services.integrations import update_placeholder_status, delayed_placeholders
from services.plex_client import refresh_plex_dummy, refresh_plex_arr_path, verify_dummy_scan_plex, retry_failed_plex_title_updates

def steps():
    return [
        delayed_placeholders,
        {
            'jellyfin': [
                update_placeholder_status, 
                create_jellyfin_nfo, 
                refresh_jellyfin_dummy, 
                refresh_jellyfin_arr_path, 
                verify_arr_scan_jellyfin,
                verify_dummy_scan_jellyfin,
                reset_jellyfin_id
            ],
            'plex': [
                refresh_plex_dummy, 
                refresh_plex_arr_path, 
                verify_dummy_scan_plex, 
                update_placeholder_status, 
                update_plex_title_status, 
                verify_dummy_scan_plex, 
                retry_failed_plex_title_updates
            ]
        }
    ]