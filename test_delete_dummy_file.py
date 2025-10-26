#!/usr/bin/env python3
"""
Test script to debug why delete_dummy_file isn't actually deleting files
"""

import sys
import os
sys.path.append('.')

from services.postgres.db import get_session
from services.postgres.models import Episode, Season, Series
from services.integrations import delete_dummy_file

def test_delete_dummy_file():
    """Test the delete_dummy_file function directly"""
    
    print("🧪 Testing delete_dummy_file function...")
    
    session = get_session()
    try:
        # Find an episode that should be deleted but still has files
        episode = session.query(Episode).filter(
            Episode.id == 95,  # Episode with The Key.mp4 that still exists
            Episode.is_deleted == True
        ).first()
        
        if not episode:
            print("❌ Test episode not found")
            return False
            
        print(f"📋 Testing Episode {episode.id}: {episode.dummypath}")
        
        # Check if file exists before
        file_exists_before = os.path.exists(episode.dummypath) if episode.dummypath else False
        print(f"📄 File exists before: {file_exists_before}")
        
        if file_exists_before:
            print(f"📂 File path: {episode.dummypath}")
            
            # Execute the delete_dummy_file function
            print(f"\n⚡ Executing delete_dummy_file...")
            result = delete_dummy_file(
                session=session,
                ent_id=episode.id,
                model=Episode,
                action='handle_seriesdelete'
            )
            
            print(f"✅ delete_dummy_file returned: {result}")
            
            # Check if file exists after
            file_exists_after = os.path.exists(episode.dummypath)
            print(f"📄 File exists after: {file_exists_after}")
            
            if file_exists_before and not file_exists_after:
                print("✅ File was properly deleted!")
                return True
            elif file_exists_before and file_exists_after:
                print("❌ File should have been deleted but still exists")
                return False
            else:
                print("ℹ️  File was not present to delete")
                return True
        else:
            print("ℹ️  File doesn't exist, nothing to delete")
            return True
            
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        session.close()

if __name__ == "__main__":
    success = test_delete_dummy_file()
    if success:
        print("\n🎉 delete_dummy_file test PASSED!")
        sys.exit(0)
    else:
        print("\n💥 delete_dummy_file test FAILED!")
        sys.exit(1)
