#!/usr/bin/env python3
"""
Test script to validate NFO cleanup fix for episodefiledelete concurrency issue.
This will simulate the delete_jellyfin_nfo function execution to test if it properly
deletes season.nfo and tvshow.nfo files when all episodes are marked as deleted.
"""

import sys
import os
sys.path.append('.')

from services.postgres.db import get_session
from services.postgres.models import Episode, Season, Series
from services.jellyfin_client import delete_jellyfin_nfo
from services.nfo_manager import delete_nfo_file

def test_nfo_cleanup():
    """Test the NFO cleanup logic with the fixed scheduler changes"""
    
    print("🧪 Testing NFO cleanup fix...")
    
    session = get_session()
    try:
        # Find episodes from series 2 (the test case)
        episodes = session.query(Episode).join(Season).filter(
            Season.series_id == 2,
            Episode.action == 'handle_seriesdelete'
        ).all()
        
        if not episodes:
            print("❌ No test episodes found for series 2")
            return False
            
        print(f"📋 Found {len(episodes)} episodes for series 2")
        
        # Check current state
        for ep in episodes[:3]:  # Show first 3
            print(f"   Episode {ep.id}: is_deleted={ep.is_deleted}, status={ep.status}")
            
        # Test the NFO deletion logic for one episode
        test_episode = episodes[0]
        print(f"\n🗑️ Testing delete_jellyfin_nfo for Episode {test_episode.id}")
        
        # Check what NFO files exist before
        if test_episode.dummypath:
            season_folder = os.path.dirname(test_episode.dummypath)
            series_folder = os.path.dirname(season_folder)
            season_nfo_path = os.path.join(season_folder, "season.nfo")
            tvshow_nfo_path = os.path.join(series_folder, "tvshow.nfo")
            
            print(f"📂 Season folder: {season_folder}")
            print(f"📂 Series folder: {series_folder}")
            
            season_nfo_exists_before = os.path.exists(season_nfo_path)
            tvshow_nfo_exists_before = os.path.exists(tvshow_nfo_path)
            
            print(f"📄 season.nfo exists before: {season_nfo_exists_before}")
            print(f"📄 tvshow.nfo exists before: {tvshow_nfo_exists_before}")
            
            # Execute the NFO deletion function
            print(f"\n⚡ Executing delete_jellyfin_nfo...")
            result = delete_jellyfin_nfo(
                dbsession=session,
                ent_id=test_episode.id,
                model=Episode,
                action='handle_seriesdelete'
            )
            
            print(f"✅ delete_jellyfin_nfo returned: {result}")
            
            # Check what NFO files exist after
            season_nfo_exists_after = os.path.exists(season_nfo_path)
            tvshow_nfo_exists_after = os.path.exists(tvshow_nfo_path)
            
            print(f"📄 season.nfo exists after: {season_nfo_exists_after}")
            print(f"📄 tvshow.nfo exists after: {tvshow_nfo_exists_after}")
            
            # Analyze results
            if season_nfo_exists_before and not season_nfo_exists_after:
                print("✅ season.nfo was properly deleted!")
            elif season_nfo_exists_before and season_nfo_exists_after:
                print("❌ season.nfo should have been deleted but still exists")
            else:
                print("ℹ️  season.nfo was not present to delete")
                
            if tvshow_nfo_exists_before and not tvshow_nfo_exists_after:
                print("✅ tvshow.nfo was properly deleted!")
            elif tvshow_nfo_exists_before and tvshow_nfo_exists_after:
                print("❌ tvshow.nfo should have been deleted but still exists")
            else:
                print("ℹ️  tvshow.nfo was not present to delete")
                
            # Test success conditions
            success = True
            if season_nfo_exists_before and season_nfo_exists_after:
                success = False
            if tvshow_nfo_exists_before and tvshow_nfo_exists_after:
                success = False
                
            return success
        else:
            print("❌ No dummy path found for test episode")
            return False
            
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        session.close()

if __name__ == "__main__":
    success = test_nfo_cleanup()
    if success:
        print("\n🎉 NFO cleanup fix test PASSED!")
        sys.exit(0)
    else:
        print("\n💥 NFO cleanup fix test FAILED!")
        sys.exit(1)
