from services.jellyfin_client import refresh_jellyfin_dummy, verify_dummy_scan_jellyfin
from services.plex_client import refresh_plex_dummy, verify_dummy_scan_plex
from services.integrations import delete_dummy_file

def steps():
    return [
        delete_dummy_file,
        {
            "jellyfin": [refresh_jellyfin_dummy],
            "plex": [refresh_plex_dummy]
        }
    ]