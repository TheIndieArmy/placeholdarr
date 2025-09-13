from services.jellyfin_client import (
    verify_dummy_scan_jellyfin,
    refresh_jellyfin_arr_path,
    refresh_jellyfin_dummy,
    verify_arr_scan_jellyfin
)
from services.plex_client import (
    refresh_plex_arr_path,
    refresh_plex_dummy,
    refresh_plex_arr_path, 
    verify_arr_scan_plex,
    verify_dummy_scan_plex
)
from services.integrations import delete_dummy_file, update_placeholder_status

def steps():
    """Return the import steps or a function to handle a single episode/movie"""
    return [
        update_placeholder_status,
        {
            'jellyfin': [refresh_jellyfin_arr_path, verify_arr_scan_jellyfin],
            'plex': [refresh_plex_arr_path, verify_arr_scan_plex]
        },
        delete_dummy_file,
        {
            'jellyfin': [refresh_jellyfin_dummy, verify_dummy_scan_jellyfin],
            'plex': [refresh_plex_dummy, verify_dummy_scan_plex]
        }
    ]
    ## change scheduler to step - 1 when failed on verify