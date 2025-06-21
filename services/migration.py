import os
import requests
import time
from pathlib import Path
from fastapi.responses import JSONResponse

from core.config import settings
from core.logger import logger
from services.integrations import place_dummy_file
from services.handlers import handle_seriesadd, handle_movieadd
from services.plex_client import refresh_plex_item
from services.jellyfin_client import refresh_jellyfin_item

# ----------------------------------------------------------------------------
# Migration Flow:
# 1) Fetch tag definitions to find numeric ID for 'dummy'
# 2) Fetch each Arr integration's items
# 3) For unmonitored items tagged with dummy_tag_id:
#    a) Remove existing dummy file/folder
#    b) Create placeholder via handler
#    c) Trigger item-level scan/refresh
#    d) Clear numeric dummy tag via API
# ----------------------------------------------------------------------------

def migrate_placeholders():
    arr_configs = [
        (settings.SONARR_URL, settings.SONARR_API_KEY, 'tv', handle_seriesadd),
        (settings.SONARR_4K_URL, settings.SONARR_4K_API_KEY, 'tv', handle_seriesadd),  # tv4k treated as tv
        (settings.RADARR_URL, settings.RADARR_API_KEY, 'movie', handle_movieadd),
        (settings.RADARR_4K_URL, settings.RADARR_4K_API_KEY, 'movie', handle_movieadd),  # movie4k treated as movie
    ]

    for url, api_key, media_type, handler in arr_configs:
        if not url or not api_key:
            logger.warning(f"Skipping {media_type}: missing URL or API key")
            continue
        hdr = {'X-Api-Key': api_key}
        # series endpoints use /series, movie endpoints use /movie
        list_ep = '/series' if media_type == 'tv' else '/movie'

        # 1) Fetch tags to find dummy_tag_id
        try:
            tag_resp = requests.get(f"{url}/tag", headers=hdr)
            tag_resp.raise_for_status()
            tags_list = tag_resp.json()
            dummy_tag_id = next((t['id'] for t in tags_list if t.get('label', '').lower() == 'dummy'), None)
            if dummy_tag_id is None:
                logger.info(f"No 'dummy' tag defined for {media_type} at {url}; skipping")
                continue
        except Exception as e:
            logger.error(f"Failed fetching tags from {url}: {e}")
            continue

        # 2) Fetch items
        try:
            resp = requests.get(f"{url}{list_ep}", headers=hdr)
            resp.raise_for_status()
            items = resp.json()
            logger.info(f"Fetched {len(items)} {media_type} items from {url}")
        except Exception as e:
            logger.error(f"Failed fetching items from {url}{list_ep}: {e}")
            continue

        # 3) Process each unmonitored item with dummy tag
        for item in items:
            if item.get('monitored'):
                continue
            tags = item.get('tags') or []
            if dummy_tag_id not in tags:
                continue

            # a) Remove stale dummy file/folder
            path = item.get('path') or item.get('folderPath')
            if path and '(dummy)' in os.path.basename(path):
                parent = Path(path).parent
                try:
                    os.remove(path)
                    if not any(parent.iterdir()):
                        parent.rmdir()
                    if settings.plex_enabled:
                        refresh_plex_item(str(parent))
                    if settings.jellyfin_enabled:
                        refresh_jellyfin_item(str(parent), "Deleted")
                except Exception as e:
                    logger.error(f"Error removing stale dummy at {path}: {e}")

            try:
                # b) Create placeholder
                if media_type == 'tv':
                    eps_resp = requests.get(
                        f"{url}/episode", params={'seriesId': item['id']}, headers=hdr
                    )
                    eps_resp.raise_for_status()
                    handler({'series': item, 'episodes': eps_resp.json()}, is_4k=(api_key == settings.SONARR_4K_API_KEY))
                else:
                    handler({'movie': item})

                # c) Trigger scan/refresh
                trigger_refresh_scan(url, api_key, media_type)

                # d) Clear dummy tag
                clear_item_dummy_tag(url, api_key, list_ep, item['id'], dummy_tag_id)

            except Exception as e:
                logger.error(f"Error processing {media_type} id {item.get('id')}: {e}")

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def trigger_refresh_scan(api_url: str, api_key: str, media_type: str, retries: int = 3, delay: int = 5):
    hdr = {'X-Api-Key': api_key}
    commands = (
        [{'name': 'RescanSeries'}, {'name': 'RefreshSeries'}]
        if media_type == 'tv'
        else [{'name': 'RescanMovie'}, {'name': 'RefreshMovie'}]
    )
    for cmd in commands:
        for attempt in range(1, retries + 1):
            try:
                requests.post(f"{api_url}/command", json=cmd, headers=hdr).raise_for_status()
                break
            except Exception:
                time.sleep(delay)


def clear_item_dummy_tag(api_url: str, api_key: str, list_ep: str, item_id: int, dummy_tag_id: int):
    hdr = {'X-Api-Key': api_key}
    try:
        r = requests.get(f"{api_url}{list_ep}/{item_id}", headers=hdr)
        r.raise_for_status()
        itm = r.json()
        tags = itm.get('tags') or []
        if dummy_tag_id in tags:
            itm['tags'] = [t for t in tags if t != dummy_tag_id]
            requests.put(f"{api_url}{list_ep}/{item_id}", json=itm, headers=hdr).raise_for_status()
    except Exception as e:
        logger.error(f"Failed clearing dummy tag for {item_id}: {e}")

# ----------------------------------------------------------------------------
# Main entrypoint
# ----------------------------------------------------------------------------

def run_migration():
    if not settings.MIGRATION:
        logger.info("Migration disabled, skipping.")
        return
    migrate_placeholders()

# No auto-run on import
