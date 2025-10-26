#!/usr/bin/env python3
"""
Complete cleanup - delete everything remaining and clean up empty directories
"""

import sys
import os
import shutil
sys.path.append('.')

from services.postgres.db import get_session
from services.postgres.models import Episode, Season, Series
from services.integrations import delete_dummy_file
from services.jellyfin_client import delete_jellyfin_nfo

def complete_cleanup():
    """Delete everything remaining and clean up directories"""
    
    print("🧹 Complete cleanup - removing all remaining files and directories...")
    
    # The paths we need to clean
    series_folder = "/mnt/plex/TV_dummy/1899 (2022) {tvdb-384429}"
    season_folder = "/mnt/plex/TV_dummy/1899 (2022) {tvdb-384429}/Season 01"
    
    print(f"📂 Series folder: {series_folder}")
    print(f"📂 Season folder: {season_folder}")
    
    # Check what's in the season folder
    if os.path.exists(season_folder):
        files_in_season = os.listdir(season_folder)
        print(f"📄 Files in season folder: {files_in_season}")
        
        # Delete remaining NFO files
        for file in files_in_season:
            if file.endswith('.nfo'):
                nfo_path = os.path.join(season_folder, file)
                print(f"🗑️ Deleting NFO file: {nfo_path}")
                try:
                    os.remove(nfo_path)
                    print(f"✅ Deleted: {file}")
                except Exception as e:
                    print(f"❌ Failed to delete {file}: {e}")
        
        # Check if season folder is now empty
        remaining_files = os.listdir(season_folder)
        print(f"📄 Remaining files in season folder: {remaining_files}")
        
        if not remaining_files:
            print(f"🗑️ Deleting empty season folder: {season_folder}")
            try:
                os.rmdir(season_folder)
                print(f"✅ Deleted empty season folder")
            except Exception as e:
                print(f"❌ Failed to delete season folder: {e}")
    else:
        print(f"ℹ️  Season folder doesn't exist")
    
    # Check what's left in the series folder
    if os.path.exists(series_folder):
        files_in_series = os.listdir(series_folder)
        print(f"📄 Files in series folder: {files_in_series}")
        
        if not files_in_series:
            print(f"🗑️ Deleting empty series folder: {series_folder}")
            try:
                os.rmdir(series_folder)
                print(f"✅ Deleted empty series folder")
            except Exception as e:
                print(f"❌ Failed to delete series folder: {e}")
        else:
            # Delete any remaining files in series folder
            for item in files_in_series:
                item_path = os.path.join(series_folder, item)
                if os.path.isfile(item_path):
                    print(f"🗑️ Deleting remaining file: {item_path}")
                    try:
                        os.remove(item_path)
                        print(f"✅ Deleted: {item}")
                    except Exception as e:
                        print(f"❌ Failed to delete {item}: {e}")
                elif os.path.isdir(item_path):
                    print(f"🗑️ Deleting remaining directory: {item_path}")
                    try:
                        shutil.rmtree(item_path)
                        print(f"✅ Deleted directory: {item}")
                    except Exception as e:
                        print(f"❌ Failed to delete directory {item}: {e}")
            
            # Try to delete series folder again
            try:
                remaining = os.listdir(series_folder)
                if not remaining:
                    os.rmdir(series_folder)
                    print(f"✅ Deleted empty series folder")
                else:
                    print(f"⚠️ Series folder still contains: {remaining}")
            except Exception as e:
                print(f"❌ Failed to delete series folder: {e}")
    else:
        print(f"ℹ️  Series folder doesn't exist")
    
    print("\n🎯 Final verification:")
    
    # Final check
    if os.path.exists(series_folder):
        remaining = os.listdir(series_folder)
        if remaining:
            print(f"⚠️  Series folder still exists with: {remaining}")
            return False
        else:
            print(f"⚠️  Series folder exists but is empty")
            return False
    else:
        print(f"✅ Series folder completely removed!")
        return True

if __name__ == "__main__":
    success = complete_cleanup()
    if success:
        print("\n🎉 Complete cleanup SUCCESSFUL!")
        sys.exit(0)
    else:
        print("\n🔄 Cleanup completed but some items may remain")
        sys.exit(0)
