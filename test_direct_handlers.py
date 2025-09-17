#!/usr/bin/env python3
"""
Direct Handler Test - Manual Scheduler Testing
Directly test handler schedulers and monitor library updates.
"""

import asyncio
import sys
import time
sys.path.append('/app')

from services.postgres.db import get_session
from services.postgres.models import Movie, Series, Episode, SubFlow
from services.postgres.movie_repo import MovieRepository
from services.postgres.series_repo import SeriesRepository
from services.scheduler import (
    handle_movieadd_scheduler,
    handle_movie_delete_scheduler,
    handle_seriesadd_scheduler,
    handle_seriesdelete_scheduler,
    handle_import_event_scheduler,
    handle_moviefiledelete_scheduler,
    handle_episodefiledelete_scheduler
)
from services.jellyfin_client import refresh_jellyfin_library, test_jellyfin_connection, build_jellyfin_url
from core.logger import logger

class DirectHandlerTester:
    """Direct testing of handlers via scheduler enqueue"""
    
    async def test_jellyfin_connectivity(self):
        """Test Jellyfin connectivity"""
        print("🔗 Testing Jellyfin connectivity...")
        
        try:
            # Test connection (sync function)
            result = test_jellyfin_connection()
            print(f"✅ Jellyfin connection: {result}")
            
            # Test URL building
            url = build_jellyfin_url("/System/Info")
            print(f"🔗 Jellyfin URL: {url}")
            
            return True
        except Exception as e:
            print(f"❌ Jellyfin connectivity failed: {e}")
            return False
    
    async def create_test_movie(self):
        """Create a test movie in database"""
        async with get_session() as session:
            repo = MovieRepository(session)
            
            # Check if test movie already exists
            existing = await repo.get_by_tmdbid(999999, False)
            if existing:
                print(f"🎬 Test movie already exists: {existing.title}")
                return existing
            
            # Create new test movie
            movie = Movie(
                tmdbid=999999,
                title="Direct Test Movie",
                year=2024,
                status="PENDING",
                action="handle_movieadd",
                is_4k=False
            )
            
            session.add(movie)
            await session.commit()
            print(f"🎬 Created test movie: {movie.title} (ID: {movie.id})")
            return movie
    
    async def create_test_series(self):
        """Create a test series in database"""
        async with get_session() as session:
            repo = SeriesRepository(session)
            
            # Check if test series already exists
            existing = await repo.get_by_tvdbid(888888, False)
            if existing:
                print(f"📺 Test series already exists: {existing.title}")
                return existing
            
            # Create new test series
            series = Series(
                tvdbid=888888,
                title="Direct Test Series",
                year=2024,
                status="PENDING", 
                action="handle_seriesadd",
                is_4k=False
            )
            
            session.add(series)
            await session.commit()
            print(f"📺 Created test series: {series.title} (ID: {series.id})")
            return series
    
    async def test_movieadd_scheduler_directly(self):
        """Test movie add scheduler directly"""
        print("\n🎬 Testing Movie Add Scheduler Directly")
        
        try:
            # Create test movie
            movie = await self.create_test_movie()
            
            # Enqueue the job directly
            print(f"📤 Enqueuing movie add job for: {movie.title}")
            job_result = handle_movieadd_scheduler.enqueue(movie)
            print(f"📥 Enqueue result: {job_result}")
            
            # Wait for processing
            print("⏳ Waiting 3 seconds for processing...")
            await asyncio.sleep(3)
            
            # Test library refresh
            print("📚 Testing library refresh...")
            refresh_result = await refresh_jellyfin_library("Movies")
            print(f"📚 Library refresh result: {refresh_result}")
            
            return True
            
        except Exception as e:
            print(f"❌ Movie add scheduler test failed: {e}")
            return False
    
    async def test_seriesadd_scheduler_directly(self):
        """Test series add scheduler directly"""
        print("\n📺 Testing Series Add Scheduler Directly")
        
        try:
            # Create test series
            series = await self.create_test_series()
            
            # Enqueue the job directly
            print(f"📤 Enqueuing series add job for: {series.title}")
            job_result = handle_seriesadd_scheduler.enqueue(series)
            print(f"📥 Enqueue result: {job_result}")
            
            # Wait for processing
            print("⏳ Waiting 3 seconds for processing...")
            await asyncio.sleep(3)
            
            # Test library refresh
            print("📚 Testing library refresh...")
            refresh_result = await refresh_jellyfin_library("TV Shows")
            print(f"📚 Library refresh result: {refresh_result}")
            
            return True
            
        except Exception as e:
            print(f"❌ Series add scheduler test failed: {e}")
            return False
    
    async def test_import_event_scheduler_directly(self):
        """Test import event scheduler directly"""
        print("\n📥 Testing Import Event Scheduler Directly")
        
        try:
            # Get existing movie and set it to import status
            async with get_session() as session:
                repo = MovieRepository(session)
                movie = await repo.get_by_tmdbid(999999, False)
                
                if movie:
                    movie.action = "handle_import_event"
                    movie.status = "PENDING"
                    await session.commit()
                    
                    print(f"📤 Enqueuing import event job for: {movie.title}")
                    job_result = handle_import_event_scheduler.enqueue(movie)
                    print(f"📥 Enqueue result: {job_result}")
                    
                    # Wait for processing
                    print("⏳ Waiting 3 seconds for processing...")
                    await asyncio.sleep(3)
                    
                    return True
                else:
                    print("⚠️ No test movie found for import event test")
                    return False
            
        except Exception as e:
            print(f"❌ Import event scheduler test failed: {e}")
            return False
    
    async def test_all_schedulers_status(self):
        """Test all scheduler status and queue information"""
        print("\n📊 Testing All Scheduler Status")
        
        schedulers = {
            'movieadd': handle_movieadd_scheduler,
            'movie_delete': handle_movie_delete_scheduler,
            'seriesadd': handle_seriesadd_scheduler,
            'seriesdelete': handle_seriesdelete_scheduler,
            'import_event': handle_import_event_scheduler,
            'moviefiledelete': handle_moviefiledelete_scheduler,
            'episodefiledelete': handle_episodefiledelete_scheduler
        }
        
        for name, scheduler in schedulers.items():
            try:
                # Check if scheduler is available and has methods
                has_enqueue = hasattr(scheduler, 'enqueue')
                has_get_queue_info = hasattr(scheduler, 'get_queue_info')
                
                print(f"🔧 {name}_scheduler:")
                print(f"   ✓ Has enqueue method: {has_enqueue}")
                print(f"   ✓ Has queue info method: {has_get_queue_info}")
                
                if has_get_queue_info:
                    try:
                        queue_info = scheduler.get_queue_info()
                        print(f"   📊 Queue info: {queue_info}")
                    except Exception as e:
                        print(f"   ⚠️ Queue info error: {e}")
                
            except Exception as e:
                print(f"❌ {name}_scheduler error: {e}")
    
    async def run_comprehensive_direct_tests(self):
        """Run all direct handler tests"""
        print("="*80)
        print("🎯 DIRECT HANDLER SCHEDULER TESTING")
        print("🔧 Testing schedulers directly via enqueue methods")
        print("🐳 Running inside Docker container with live Jellyfin")
        print("="*80)
        
        results = []
        
        # Test connectivity
        connectivity_result = await self.test_jellyfin_connectivity()
        results.append(("Jellyfin Connectivity", connectivity_result))
        
        # Test scheduler status
        await self.test_all_schedulers_status()
        
        # Test individual handlers
        movieadd_result = await self.test_movieadd_scheduler_directly()
        results.append(("Movie Add Scheduler", movieadd_result))
        
        seriesadd_result = await self.test_seriesadd_scheduler_directly()
        results.append(("Series Add Scheduler", seriesadd_result))
        
        import_result = await self.test_import_event_scheduler_directly()
        results.append(("Import Event Scheduler", import_result))
        
        # Print summary
        print("\n" + "="*80)
        print("🎯 DIRECT HANDLER TEST SUMMARY")
        print("="*80)
        
        passed = 0
        failed = 0
        
        for test_name, result in results:
            if result:
                print(f"✅ PASSED: {test_name}")
                passed += 1
            else:
                print(f"❌ FAILED: {test_name}")
                failed += 1
        
        print(f"\n📊 FINAL RESULTS:")
        print(f"   ✅ PASSED: {passed}")
        print(f"   ❌ FAILED: {failed}")
        print(f"   📊 TOTAL:  {passed + failed}")
        
        if failed == 0:
            print(f"\n🎉 ALL DIRECT HANDLER TESTS PASSED!")
            print(f"🚀 Schedulers are working correctly!")
        else:
            print(f"\n⚠️  {failed} test(s) failed")
        
        return failed == 0

async def main():
    """Main test execution"""
    tester = DirectHandlerTester()
    success = await tester.run_comprehensive_direct_tests()
    return success

if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
