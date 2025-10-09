import time
from core import logger
from services.plex_client import update_plex_title_status, refresh_plex_item
from services.jellyfin_client import update_jellyfin_title_status, refresh_jellyfin_dummy, verify_dummy_scan_jellyfin, retry_failed_jellyfin_title_updates
from services.queue_monitor import check_movie_has_file
from services.utils import get_movie_by_id
from services.integrations import place_dummy_file, update_placeholder_status, delayed_placeholders, flow_attach_dummypaths
from services.plex_client import refresh_plex_dummy, verify_dummy_scan_plex, retry_failed_plex_title_updates
from core.config import settings

def steps():
    return [
        flow_attach_dummypaths,
        delayed_placeholders,
        {
            'jellyfin': [refresh_jellyfin_dummy, verify_dummy_scan_jellyfin, update_placeholder_status, update_jellyfin_title_status, verify_dummy_scan_jellyfin, retry_failed_jellyfin_title_updates],
            'plex': [refresh_plex_dummy, verify_dummy_scan_plex, update_placeholder_status, update_plex_title_status, verify_dummy_scan_plex, retry_failed_plex_title_updates]
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
#     dummy_path = place_dummy_file("movie", movie.title, movie.year, movie.tmdbid, settings.MOVIE_LIBRARY_FOLDER)
#     if dummy_path:
#         movie.dummypath = dummy_path
#         movie.commit()
#         logger.info(f"Created placeholder file for movie '{movie.title}'", extra={'emoji_type': 'create'})
#         return True
#     logger.error("Failed to create dummy file; skipping refresh.", extra={'emoji_type': 'error'})
#     return False