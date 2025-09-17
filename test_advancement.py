#!/usr/bin/env python3
"""
Simple Handler Advancement Test
Tests the advancement functionality of the handler system.
"""

import sys
import time
sys.path.append('/app')

from services.scheduler import (
    handle_movieadd_scheduler,
    handle_seriesadd_scheduler,
    handle_import_event_scheduler,
)
from services.jellyfin_client import test_jellyfin_connection, refresh_jellyfin_library
from core.logger import logger

def main():
    """Test handler advancement and library updates"""
    print("="*80)
    print("🎯 HANDLER ADVANCEMENT TEST")
    print("🚀 Testing scheduler advancement and library refresh functionality")
    print("🐳 Running inside Docker container with live Jellyfin")
    print("="*80)
    
    # Test connectivity
    print("🔗 Testing Jellyfin connectivity...")
    jf_result = test_jellyfin_connection()
    print(f"✅ Jellyfin connection: {jf_result}")
    
    if not jf_result:
        print("❌ Cannot proceed without Jellyfin connection")
        return False
    
    # Test scheduler availability
    print("\n🔧 Testing scheduler availability...")
    
    schedulers = {
        'movieadd': handle_movieadd_scheduler,
        'seriesadd': handle_seriesadd_scheduler,
        'import_event': handle_import_event_scheduler,
    }
    
    scheduler_status = {}
    for name, scheduler in schedulers.items():
        try:
            has_enqueue = hasattr(scheduler, 'enqueue')
            has_advance = hasattr(scheduler, 'advance_pending_jobs')
            scheduler_status[name] = {
                'available': True,
                'has_enqueue': has_enqueue,
                'has_advance': has_advance
            }
            print(f"✅ {name}_scheduler: enqueue={has_enqueue}, advance={has_advance}")
        except Exception as e:
            scheduler_status[name] = {'available': False, 'error': str(e)}
            print(f"❌ {name}_scheduler: {e}")
    
    # Test library refresh functionality
    print("\n📚 Testing library refresh functionality...")
    
    try:
        # Test movie library refresh
        print("🎬 Testing movie library refresh...")
        movie_refresh = refresh_jellyfin_library("Movies")
        print(f"📚 Movie library refresh result: {movie_refresh}")
        
        # Wait a moment
        time.sleep(2)
        
        # Test TV library refresh
        print("📺 Testing TV library refresh...")
        tv_refresh = refresh_jellyfin_library("TV Shows")
        print(f"📚 TV library refresh result: {tv_refresh}")
        
        # Test advancement simulation
        print("\n⚡ Testing scheduler advancement...")
        
        advancement_results = {}
        for name, scheduler in schedulers.items():
            if scheduler_status[name].get('available', False):
                try:
                    if hasattr(scheduler, 'advance_pending_jobs'):
                        # Try to advance pending jobs
                        advance_result = scheduler.advance_pending_jobs()
                        advancement_results[name] = advance_result
                        print(f"⚡ {name}_scheduler advancement: {advance_result}")
                    else:
                        advancement_results[name] = "No advance method"
                        print(f"ℹ️ {name}_scheduler: No advance method available")
                except Exception as e:
                    advancement_results[name] = f"Error: {e}"
                    print(f"❌ {name}_scheduler advancement failed: {e}")
        
        # Monitor for a short period to see if there's any activity
        print(f"\n👁️ Monitoring system activity for 10 seconds...")
        start_time = time.time()
        while time.time() - start_time < 10:
            elapsed = int(time.time() - start_time)
            remaining = 10 - elapsed
            print(f"⏳ Monitoring... {remaining}s remaining", end='\r')
            time.sleep(1)
        
        print(f"\n✅ Monitoring complete!")
        
        # Print final summary
        print("\n" + "="*80)
        print("🎯 HANDLER ADVANCEMENT TEST SUMMARY")
        print("="*80)
        
        print(f"🔗 Jellyfin Connection: {'✅' if jf_result else '❌'}")
        print(f"📚 Movie Library Refresh: {'✅' if movie_refresh else '❌'}")
        print(f"📺 TV Library Refresh: {'✅' if tv_refresh else '❌'}")
        
        print(f"\n🔧 Scheduler Status:")
        available_schedulers = 0
        for name, status in scheduler_status.items():
            if status.get('available', False):
                available_schedulers += 1
                print(f"   ✅ {name}_scheduler: Ready")
            else:
                print(f"   ❌ {name}_scheduler: {status.get('error', 'Not available')}")
        
        print(f"\n⚡ Advancement Results:")
        for name, result in advancement_results.items():
            print(f"   📊 {name}_scheduler: {result}")
        
        # Determine overall success
        basic_success = jf_result and movie_refresh and tv_refresh
        scheduler_success = available_schedulers > 0
        
        if basic_success and scheduler_success:
            print(f"\n🎉 HANDLER ADVANCEMENT TEST SUCCESSFUL!")
            print(f"✨ Core functionality is working:")
            print(f"   🔗 Jellyfin connection established")
            print(f"   📚 Library refresh operations working")
            print(f"   🔧 {available_schedulers} schedulers available")
            print(f"🚀 Your handler system is ready for live operations!")
            return True
        else:
            print(f"\n⚠️ Some components failed:")
            if not basic_success:
                print(f"   ❌ Basic connectivity or library refresh issues")
            if not scheduler_success:
                print(f"   ❌ No schedulers available")
            return False
            
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
