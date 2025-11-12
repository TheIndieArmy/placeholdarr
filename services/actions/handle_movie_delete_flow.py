import os
from services.jellyfin_client import refresh_jellyfin_dummy, delete_jellyfin_nfo, reset_jellyfin_id
from services.plex_client import refresh_plex_dummy
from services.integrations import delete_dummy_file

def steps():
    return [
        delete_dummy_file, 
        {
            "jellyfin": [
                delete_jellyfin_nfo,  # Delete NFO file first
                refresh_jellyfin_dummy,  # Then refresh to clean up from Jellyfin
                reset_jellyfin_id,
            ],
            "plex": [refresh_plex_dummy]
        }
    ]