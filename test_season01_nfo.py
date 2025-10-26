#!/usr/bin/env python3
"""
Test NFO cleanup for Season 01 specifically
"""

import sys
import os
sys.path.append('.')

from services.postgres.db import get_session
from services.postgres.models import Episode, Season, Series
from services.jellyfin_client import delete_jellyfin_nfo

def test_season01_nfo_cleanup():
    """Test NFO cleanup for Season 01"""
    
    print("🧪 Testing Season 01 NFO cleanup...")
    
    session = get_session()
    try:
        # Find an episode from Season 01 (episodes 89, 90, 91, 92, 93, 94, 95)
        episode = session.query(Episode).filter(
            Episode.id == 89,  # Season 01 episode
            Episode.is_deleted == True
        ).first()
        
        if not episode:
            print("❌ Test episode from Season 01 not found")
            return False
            
        print(f"📋 Testing Episode {episode.id}: {episode.dummypath}")
        
        # Check season folder
        if episode.dummypath:
            season_folder = os.path.dirname(episode.dummypath)
            season_nfo_path = os.path.join(season_folder, "season.nfo")
            
            print(f"📂 Season folder: {season_folder}")
            print(f"📄 season.nfo path: {season_nfo_path}")
            
            season_nfo_exists_before = os.path.exists(season_nfo_path)
            print(f"📄 season.nfo exists before: {season_nfo_exists_before}")
            
            # Execute NFO deletion
            print(f"\n⚡ Executing delete_jellyfin_nfo for Season 01 episode...")
            result = delete_jellyfin_nfo(
                dbsession=session,
                ent_id=episode.id,
                model=Episode,
                action='handle_seriesdelete'
            )
            
            print(f"✅ delete_jellyfin_nfo returned: {result}")
            
            # Check if season.nfo was deleted
            season_nfo_exists_after = os.path.exists(season_nfo_path)
            print(f"📄 season.nfo exists after: {season_nfo_exists_after}")
            
            if season_nfo_exists_before and not season_nfo_exists_after:
                print("✅ season.nfo was properly deleted!")
                return True
            elif season_nfo_exists_before and season_nfo_exists_after:
                print("❌ season.nfo should have been deleted but still exists")
                return False
            else:
                print("ℹ️  season.nfo was already deleted")
                return True
                
        return True
            
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        session.close()

if __name__ == "__main__":
    success = test_season01_nfo_cleanup()
    if success:
        print("\n🎉 Season 01 NFO cleanup test PASSED!")
        sys.exit(0)
    else:
        print("\n💥 Season 01 NFO cleanup test FAILED!")
        sys.exit(1)
