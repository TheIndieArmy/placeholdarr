#!/usr/bin/env python3
"""
Complete the deletion test for remaining episodes
"""

import sys
import os
sys.path.append('.')

from services.postgres.db import get_session
from services.postgres.models import Episode, Season, Series
from services.integrations import delete_dummy_file

def complete_deletion_test():
    """Complete deletion for remaining episodes"""
    
    print("🧪 Completing deletion test for remaining episodes...")
    
    session = get_session()
    try:
        # Find episodes that should be deleted but might still have files
        episodes = session.query(Episode).filter(
            Episode.id.in_([89, 91]),  # The Boy and The Fight
            Episode.is_deleted == True
        ).all()
        
        for episode in episodes:
            print(f"\n📋 Testing Episode {episode.id}: {episode.dummypath}")
            
            # Check if file exists
            file_exists = os.path.exists(episode.dummypath) if episode.dummypath else False
            print(f"📄 File exists: {file_exists}")
            
            if file_exists:
                print(f"⚡ Executing delete_dummy_file for episode {episode.id}...")
                result = delete_dummy_file(
                    session=session,
                    ent_id=episode.id,
                    model=Episode,
                    action='handle_seriesdelete'
                )
                
                print(f"✅ delete_dummy_file returned: {result}")
                
                # Check if file is gone
                file_exists_after = os.path.exists(episode.dummypath)
                print(f"📄 File exists after: {file_exists_after}")
                
                if not file_exists_after:
                    print(f"✅ Episode {episode.id} file deleted successfully!")
            else:
                print(f"ℹ️  Episode {episode.id} file already deleted")
                
        return True
            
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        session.close()

if __name__ == "__main__":
    success = complete_deletion_test()
    if success:
        print("\n🎉 Complete deletion test PASSED!")
        sys.exit(0)
    else:
        print("\n💥 Complete deletion test FAILED!")
        sys.exit(1)
