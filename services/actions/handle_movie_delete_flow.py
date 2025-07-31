import os
from services.jellyfin_client import refresh_jellyfin_dummy
from services.plex_client import refresh_plex_dummy
from services.integrations import delete_dummy_file

def steps():
    return [
        delete_dummy_file, 
        {
            "jellyfin": [refresh_jellyfin_dummy],
            "plex": [refresh_plex_dummy]
        }
    ]