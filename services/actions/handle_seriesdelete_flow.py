from services.jellyfin_client import refresh_jellyfin_dummy, verify_dummy_scan_jellyfin, delete_jellyfin_nfo, reset_jellyfin_id
from services.plex_client import refresh_plex_dummy, verify_dummy_scan_plex
from services.integrations import delete_dummy_file, update_placeholder_status

def steps():
    return [
        delete_dummy_file,
        {
            "jellyfin": [
                delete_jellyfin_nfo,
                refresh_jellyfin_dummy,
                update_placeholder_status,
                reset_jellyfin_id,
            ],
            "plex": [refresh_plex_dummy]
        }
    ]