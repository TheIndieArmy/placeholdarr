import time
from services.plex_client import update_plex_title_status
from services.jellyfin_client import (
    refresh_jellyfin_dummy, verify_dummy_scan_jellyfin, 
     create_jellyfin_nfo
)
from services.integrations import  update_placeholder_status, delayed_placeholders, enrich_movie_metadata
from services.plex_client import refresh_plex_dummy, verify_dummy_scan_plex, retry_failed_plex_title_updates
from services.postgres.models import Movie


def steps():
    return [
        delayed_placeholders,
        {
            'jellyfin': [
                enrich_movie_metadata,  # Enrich with Radarr metadata before NFO creation
                create_jellyfin_nfo,  # Create NFO file with enriched metadata
                refresh_jellyfin_dummy,  # Now refresh with both dummy and NFO
                verify_dummy_scan_jellyfin, 
                update_placeholder_status, 
                verify_dummy_scan_jellyfin
            ],
            'plex': [
                refresh_plex_dummy, 
                verify_dummy_scan_plex, 
                update_placeholder_status, 
                update_plex_title_status, 
                verify_dummy_scan_plex, 
                retry_failed_plex_title_updates
            ]
        }
    ]

# def delayed_placeholder(session, ent_id, model):
#     movie = session.query(model).get(ent_id)
#     # movie = get_movie_by_id(obj.id, session)
#     delay_seconds = 3  # Adjust as needed
#     logger.debug(f"Delaying {delay_seconds}s before checking hasFile for movie '{movie.title}'", extra={'emoji_type': 'debug'})
#     time.sleep(delay_seconds)
#     has_file = False
#     if movie.radarrid and check_movie_has_file(movie.radarrid):
#         has_file = True
#     if has_file:
#         logger.info(f"Skipping placeholder for movie '{movie.title}' (real file exists)", extra={'emoji_type': 'skip'})
#         return True
#     dummy_path = place_dummy_file("movie", movie.title, movie.year, movie.tmdbid, settings.DUMMY_MOVIE_LIBRARY_FOLDER)
#     if dummy_path:
#         movie.dummypath = dummy_path
#         movie.commit()
#         logger.info(f"Created placeholder file for movie '{movie.title}'", extra={'emoji_type': 'create'})
#         return True
#     logger.error("Failed to create dummy file; skipping refresh.", extra={'emoji_type': 'error'})
#     return False