from services.plex_client import update_plex_title_status, refresh_plex_item
from services.jellyfin_client import (
    refresh_jellyfin_dummy, update_jellyfin_nfo_status, update_jellyfin_title_status, 
    verify_dummy_scan_jellyfin, delete_jellyfin_nfo, 
    refresh_jellyfin_arr_path, create_jellyfin_nfo
)
from services.integrations import update_placeholder_status, delayed_placeholders
from services.plex_client import refresh_plex_dummy, refresh_plex_arr_path, verify_dummy_scan_plex, retry_failed_plex_title_updates

def steps():
    return [
        delayed_placeholders,
        {
            'jellyfin': [
                create_jellyfin_nfo,  # Create NFO file first
                refresh_jellyfin_dummy, 
                refresh_jellyfin_arr_path, 
                verify_dummy_scan_jellyfin, 
                update_placeholder_status, 
                delete_jellyfin_nfo,  # Update NFO instead of API call
                verify_dummy_scan_jellyfin
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