import os
import requests
import re
from pathlib import Path

"""
Migration script: Rename TV and movie folders to match the current *arr (Sonarr/Radarr) folder names.
- Fetches Sonarr and Radarr connection info from ENV or prompts user.
- Pings both APIs to get the correct folder names for all series and movies.
- Scans both TV and movie library roots for folders.
- Guides user through each rename with Y/N prompt (or skip all, or apply all).
- DRY_RUN = True by default (shows what would be renamed, does not rename until set to False).
"""

# CONFIGURATION (can be overridden by ENV or user input)
SONARR_URL = os.getenv('SONARR_URL') or input('Sonarr URL (default http://localhost:8989): ') or 'http://localhost:8989'
SONARR_API_KEY = os.getenv('SONARR_API_KEY') or input('Sonarr API Key: ')
RADARR_URL = os.getenv('RADARR_URL') or input('Radarr URL (default http://localhost:7878): ') or 'http://localhost:7878'
RADARR_API_KEY = os.getenv('RADARR_API_KEY') or input('Radarr API Key: ')
TV_LIBRARY_ROOT = os.getenv('TV_LIBRARY_FOLDER') or input('TV library root folder: ')
MOVIE_LIBRARY_ROOT = os.getenv('MOVIE_LIBRARY_FOLDER') or input('Movie library root folder: ')
DRY_RUN = False  # Set to False to actually rename folders


def clean_base_url(url):
    # Remove trailing /api or /api/v3 if present
    url = url.rstrip('/')
    if url.endswith('/api/v3'):
        url = url[:-7]
    elif url.endswith('/api'):
        url = url[:-4]
    return url

def join_url(base, endpoint):
    base = clean_base_url(base)
    # Remove trailing slash from base and leading slash from endpoint
    return base.rstrip('/') + '/' + endpoint.lstrip('/')

def get_arr_folders(url, api_key, media_type):
    headers = {'X-Api-Key': api_key}
    if media_type == 'tv':
        endpoint = 'api/v3/series'
        key = 'path'
    else:
        endpoint = 'api/v3/movie'
        key = 'path'
    full_url = join_url(url, endpoint)
    resp = requests.get(full_url, headers=headers)
    resp.raise_for_status()
    items = resp.json()
    if media_type == 'tv':
        # TV: map tvdbId (as str) to correct folder name
        tv_map = {}
        tvdb_ids = set()
        for item in items:
            tvdbid = str(item.get('tvdbId') or item.get('tvdbid') or item.get('tvdb_id'))
            folder_name = Path(item[key]).name
            if tvdbid and folder_name:
                tv_map[tvdbid] = folder_name
                tvdb_ids.add(tvdbid)
        return tv_map, tvdb_ids
    else:
        # MOVIE: map TMDB and IMDB id (as str) to correct folder name
        movie_map = {}
        tmdb_ids = set()
        imdb_ids = set()
        for item in items:
            tmdbid = str(item.get('tmdbId') or item.get('tmdbid') or item.get('tmdb_id'))
            imdbid = str(item.get('imdbId') or item.get('imdbid') or item.get('imdb_id'))
            folder_name = Path(item[key]).name
            if tmdbid and folder_name:
                movie_map[tmdbid] = folder_name
                tmdb_ids.add(tmdbid)
            if imdbid and folder_name:
                movie_map[imdbid] = folder_name
                imdb_ids.add(imdbid)
        return movie_map, tmdb_ids, imdb_ids

def extract_id_from_folder(folder_name, media_type):
    ids = {}
    if media_type == 'tv':
        # Try to extract TVDB id from folder name, e.g. {tvdb-12345}
        m = re.search(r'\{tvdb-(\d+)\}', folder_name)
        if m:
            ids['tvdb'] = m.group(1)
        # Fallback: look for a number at the end
        m = re.search(r'(\d+)$', folder_name)
        if m:
            ids['tvdb'] = m.group(1)
    else:
        # Try to extract TMDB id from folder name, e.g. {tmdb-12345}
        m = re.search(r'\{tmdb-(\d+)\}', folder_name)
        if m:
            ids['tmdb'] = m.group(1)
        # Try to extract IMDB id from folder name, e.g. {imdb-tt1234567}
        m = re.search(r'\{imdb-tt(\d+)\}', folder_name)
        if m:
            ids['imdb'] = 'tt' + m.group(1)
        # Fallback: look for a number at the end
        m = re.search(r'(\d+)$', folder_name)
        if m and 'tmdb' not in ids:
            ids['tmdb'] = m.group(1)
    return ids

def get_expected_folder_names(url, api_key, media_type):
    headers = {'X-Api-Key': api_key}
    if media_type == 'tv':
        endpoint = 'api/v3/series'
    else:
        endpoint = 'api/v3/movie'
    full_url = join_url(url, endpoint)
    resp = requests.get(full_url, headers=headers)
    resp.raise_for_status()
    items = resp.json()
    expected_names = set()
    if media_type == 'tv':
        # Add all season folders for each series
        for item in items:
            series_folder = Path(item['path']).name
            seasons = item.get('seasons', [])
            for season in seasons:
                season_folder = season.get('folder') or season.get('seasonFolder') or season.get('seasonName')
                if season_folder:
                    expected_names.add(os.path.join(series_folder, season_folder))
            # Also add the series folder itself
            expected_names.add(series_folder)
    else:
        expected_names = set(Path(item['path']).name for item in items if 'path' in item)
    return expected_names

def scan_and_rename_by_name(library_root, expected_names, media_type):
    global DRY_RUN
    rename_candidates = []
    unmatched_folders = []
    print(f"\nScanning {media_type.upper()} library: {library_root}")
    for folder in os.listdir(library_root):
        folder_path = os.path.join(library_root, folder)
        if not os.path.isdir(folder_path):
            continue
        # Check for season folders inside series folders
        if media_type == 'tv':
            for subfolder in os.listdir(folder_path):
                subfolder_path = os.path.join(folder_path, subfolder)
                if not os.path.isdir(subfolder_path):
                    continue
                full_season_path = os.path.join(folder, subfolder)
                if full_season_path in expected_names:
                    print(f"[OK] {full_season_path} matches TV season folder name.")
                else:
                    match = None
                    for expected in expected_names:
                        if full_season_path.lower() == expected.lower():
                            match = expected
                            break
                    if match:
                        target_path = os.path.join(library_root, match)
                        if os.path.exists(target_path):
                            print(f"[SKIPPED] Target season folder already exists: {target_path}. Will delete old folder.")
                            rename_candidates.append((full_season_path, match, subfolder_path, target_path, True))
                        else:
                            print(f"[RENAME] {full_season_path} -> {match}")
                            rename_candidates.append((full_season_path, match, subfolder_path, target_path, False))
                    else:
                        print(f"[WARN] No TV season match for {full_season_path}")
                        unmatched_folders.append((full_season_path, subfolder_path))
        # Check series/movie folder itself
        if folder in expected_names:
            print(f"[OK] {folder} matches {media_type} folder name.")
        else:
            match = None
            for expected in expected_names:
                if folder.lower() == expected.lower():
                    match = expected
                    break
            if not match:
                for expected in expected_names:
                    if folder.split(' (')[0].lower() == expected.split(' (')[0].lower():
                        match = expected
                        break
            if match:
                target_path = os.path.join(library_root, match)
                if os.path.exists(target_path):
                    print(f"[SKIPPED] Target folder already exists: {target_path}. Will delete old folder.")
                    rename_candidates.append((folder, match, folder_path, target_path, True))
                else:
                    print(f"[RENAME] {folder} -> {match}")
                    rename_candidates.append((folder, match, folder_path, target_path, False))
            else:
                print(f"[WARN] No {media_type} match for {folder}")
                unmatched_folders.append((folder, folder_path))
    print(f"Done scanning {media_type} library.")
    return rename_candidates, unmatched_folders

def confirm_delete(folder_path, reason):
    print(f"\n[CONFIRM] You are about to delete: {folder_path}")
    print(f"Reason: {reason}")
    confirm = input("Are you sure you want to permanently delete this folder and all its contents? [y/N]: ").strip().lower()
    return confirm == 'y'

def confirm_delete_batch(folders, reason):
    print("\n[CONFIRM] You are about to delete the following folders:")
    for folder_path in folders:
        print(f"- {folder_path}")
    print(f"\nReason: {reason}")
    confirm1 = input("Do you want to delete ALL of these folders? [Y/N]: ").strip().upper()
    if confirm1 != 'Y':
        print("Aborting deletion.")
        return False
    confirm2 = input("Are you sure? This action is IRREVERSIBLE and will permanently delete all listed folders. [Y/N]: ").strip().upper()
    if confirm2 != 'Y':
        print("Aborting deletion.")
        return False
    return True

def main():
    print("\n--- Placeholdarr Folder Migration Script ---")
    print("This script will help you rename your TV and movie folders to match your current Sonarr/Radarr setup.")
    print("It will prompt you before making any changes. Use with caution!")
    if DRY_RUN:
        print("\nDRY_RUN is ON (no changes will be made; actions will be shown as [DRY RUN]).")
    else:
        print("\nDRY_RUN is OFF (changes will be applied if you answer 'Y').")
    expected_tv_names = get_expected_folder_names(SONARR_URL, SONARR_API_KEY, 'tv')
    expected_movie_names = get_expected_folder_names(RADARR_URL, RADARR_API_KEY, 'movie')
    tv_renames, tv_unmatched = scan_and_rename_by_name(TV_LIBRARY_ROOT, expected_tv_names, 'tv')
    movie_renames, movie_unmatched = scan_and_rename_by_name(MOVIE_LIBRARY_ROOT, expected_movie_names, 'movie')

    # Summary and prompt to proceed
    print(f"\nSummary:")
    print(f"TV folders to rename: {len(tv_renames)}")
    print(f"TV unmatched folders: {len(tv_unmatched)}")
    print(f"Movie folders to rename: {len(movie_renames)}")
    print(f"Movie unmatched folders: {len(movie_unmatched)}")
    proceed = input("\nWould you like to proceed with renaming ALL folders listed above? [Y/N]: ").strip().upper()
    if proceed != 'Y':
        print("Aborting renaming.")
        return
    # Renaming and cleanup step
    for folder, correct_name, folder_path, target_path, target_exists in tv_renames + movie_renames:
        print(f"[RENAME] {folder} -> {correct_name}")
        if DRY_RUN:
            if target_exists:
                print(f"[DRY RUN] Would delete old folder: {folder_path}")
            else:
                print(f"[DRY RUN] Would rename: {folder_path} -> {target_path}")
        else:
            try:
                if target_exists:
                    print(f"[INFO] Target folder exists. Deleting old folder: {folder_path}")
                    # Batch deletion handled below
                else:
                    os.rename(folder_path, target_path)
                    print(f"[RENAMED] {folder} -> {correct_name}")
            except Exception as e:
                print(f"[ERROR] Could not process {folder}: {e}")
    # Batch delete old-format folders after renaming
    old_folders_to_delete = [folder_path for folder, _, folder_path, _, target_exists in tv_renames + movie_renames if target_exists]
    if old_folders_to_delete:
        reason = "These are old-format folders. The new-format folder already exists for these titles. Keeping both is unnecessary and may cause confusion."
        if DRY_RUN:
            print("[DRY RUN] Would delete the following old-format folders:")
            for folder_path in old_folders_to_delete:
                print(f"- {folder_path}")
        else:
            if confirm_delete_batch(old_folders_to_delete, reason):
                for folder_path in old_folders_to_delete:
                    try:
                        for root, dirs, files in os.walk(folder_path, topdown=False):
                            for name in files:
                                os.remove(os.path.join(root, name))
                            for name in dirs:
                                os.rmdir(os.path.join(root, name))
                        os.rmdir(folder_path)
                        print(f"[DELETED] {folder_path}")
                    except Exception as e:
                        print(f"[ERROR] Could not delete {folder_path}: {e}")
            else:
                print("[SKIPPED] Did not delete old-format folders.")
    # List unmatched folders and explain
    print("\nUnmatched folders (not found in Sonarr/Radarr):")
    unmatched_to_delete = [folder_path for _, folder_path in tv_unmatched + movie_unmatched]
    for folder_path in unmatched_to_delete:
        print(f"- {folder_path}")
    print("\nExplanation: These folders do not match any current Sonarr/Radarr entries. They are likely orphaned placeholders left behind if a title was removed from *arrs while Placeholdarr was not running. It is recommended to delete these folders to keep your library clean.")
    cleanup = input("\nWould you like to delete these unmatched folders? [Y/N]: ").strip().upper()
    if cleanup == 'Y':
        reason = "These folders do not match any current Sonarr/Radarr entries. They are likely orphaned placeholders and safe to delete."
        if DRY_RUN:
            print("[DRY RUN] Would delete the following unmatched folders:")
            for folder_path in unmatched_to_delete:
                print(f"- {folder_path}")
        else:
            if confirm_delete_batch(unmatched_to_delete, reason):
                for folder_path in unmatched_to_delete:
                    try:
                        for root, dirs, files in os.walk(folder_path, topdown=False):
                            for name in files:
                                os.remove(os.path.join(root, name))
                            for name in dirs:
                                os.rmdir(os.path.join(root, name))
                        os.rmdir(folder_path)
                        print(f"[DELETED] {folder_path}")
                    except Exception as e:
                        print(f"[ERROR] Could not delete {folder_path}: {e}")
            else:
                print("[SKIPPED] Did not delete unmatched folders.")
    print("\nMigration complete.")

if __name__ == '__main__':
    main()
