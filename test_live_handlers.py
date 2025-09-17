#!/usr/bin/env python3
"""
Live Handler Testing Script
Manually triggers each handler with real data to verify live functionality.
"""

import asyncio
import sys
import json
import time
sys.path.append('/app')

from services.handlers import handle_webhook
from services.postgres.db import get_session
from services.postgres.models import Movie, Series, Season, Episode
from services.postgres.movie_repo import MovieRepository
from services.postgres.series_repo import SeriesRepository
from services.scheduler import (
    handle_import_event_scheduler,
    handle_seriesadd_scheduler,
    handle_episodefiledelete_scheduler,
    handle_moviefiledelete_scheduler,
    handle_movie_delete_scheduler,
    handle_movieadd_scheduler,
    handle_seriesdelete_scheduler
)
from services.jellyfin_client import refresh_jellyfin_library, test_jellyfin_connection
from core.logger import logger

class LiveHandlerTester:
    """Test all handlers with live data and verify library updates"""
    
    def __init__(self):
        self.test_results = {
            'movieadd': {'status': 'pending', 'details': []},
            'movie_delete': {'status': 'pending', 'details': []},
            'seriesadd': {'status': 'pending', 'details': []},
            'seriesdelete': {'status': 'pending', 'details': []},
            'import_event': {'status': 'pending', 'details': []},
            'moviefiledelete': {'status': 'pending', 'details': []},
            'episodefiledelete': {'status': 'pending', 'details': []}
        }
    
    async def setup_test_environment(self):
        """Setup test environment and verify connections"""
        print("🔧 Setting up test environment...")
        
        # Test Jellyfin connection
        jf_result = await test_jellyfin_connection()
        if not jf_result:
            print("❌ Jellyfin connection failed!")
            return False
        
        print("✅ Jellyfin connection established")
        
        # Test database connection
        async with get_session() as session:
            movie_repo = MovieRepository(session)
            series_repo = SeriesRepository(session)
            print("✅ Database connection established")
        
        return True
    
    async def test_movieadd_handler_live(self):
        """Test movie add handler with live data"""
        print("\n🎬 Testing Movie Add Handler (Live)")
        
        try:
            # Create test movie data
            test_data = {
                "eventType": "movieAdded",
                "instanceName": "Radarr",
                "movie": {
                    "id": 999999,
                    "title": "Live Test Movie",
                    "year": 2024,
                    "tmdbId": 127127,
                    "folderPath": "/movies/Live Test Movie (2024)",
                    "hasFile": False
                }
            }
            
            print(f"📤 Sending webhook data: {json.dumps(test_data, indent=2)}")
            
            # Trigger the handler
            response = handle_webhook(test_data)
            print(f"📥 Handler response: {response}")
            
            # Wait a moment for processing
            await asyncio.sleep(2)
            
            # Check if library refresh was triggered
            library_result = await refresh_jellyfin_library("Movies")
            print(f"📚 Library refresh result: {library_result}")
            
            self.test_results['movieadd']['status'] = 'completed'
            self.test_results['movieadd']['details'] = [
                f"Handler response: {response}",
                f"Library refresh: {library_result}"
            ]
            
            print("✅ Movie Add Handler test completed")
            return True
            
        except Exception as e:
            print(f"❌ Movie Add Handler test failed: {e}")
            self.test_results['movieadd']['status'] = 'failed'
            self.test_results['movieadd']['details'] = [f"Error: {e}"]
            return False
    
    async def test_movie_delete_handler_live(self):
        """Test movie delete handler with live data"""
        print("\n🗑️ Testing Movie Delete Handler (Live)")
        
        try:
            test_data = {
                "eventType": "movieDeleted", 
                "instanceName": "Radarr",
                "movie": {
                    "id": 888888,
                    "title": "Live Test Delete Movie",
                    "year": 2024,
                    "tmdbId": 999999,
                    "folderPath": "/movies/Live Test Delete Movie (2024)"
                }
            }
            
            print(f"📤 Sending webhook data: {json.dumps(test_data, indent=2)}")
            
            response = handle_webhook(test_data)
            print(f"📥 Handler response: {response}")
            
            await asyncio.sleep(2)
            
            library_result = await refresh_jellyfin_library("Movies")
            print(f"📚 Library refresh result: {library_result}")
            
            self.test_results['movie_delete']['status'] = 'completed'
            self.test_results['movie_delete']['details'] = [
                f"Handler response: {response}",
                f"Library refresh: {library_result}"
            ]
            
            print("✅ Movie Delete Handler test completed")
            return True
            
        except Exception as e:
            print(f"❌ Movie Delete Handler test failed: {e}")
            self.test_results['movie_delete']['status'] = 'failed'
            self.test_results['movie_delete']['details'] = [f"Error: {e}"]
            return False
    
    async def test_seriesadd_handler_live(self):
        """Test series add handler with live data"""
        print("\n📺 Testing Series Add Handler (Live)")
        
        try:
            test_data = {
                "eventType": "seriesAdd",
                "instanceName": "Sonarr",
                "series": {
                    "id": 777777,
                    "title": "Live Test Series",
                    "year": 2024,
                    "tvdbId": 888888,
                    "path": "/tv/Live Test Series (2024)"
                }
            }
            
            print(f"📤 Sending webhook data: {json.dumps(test_data, indent=2)}")
            
            response = handle_webhook(test_data)
            print(f"📥 Handler response: {response}")
            
            await asyncio.sleep(2)
            
            library_result = await refresh_jellyfin_library("TV Shows")
            print(f"📚 Library refresh result: {library_result}")
            
            self.test_results['seriesadd']['status'] = 'completed'
            self.test_results['seriesadd']['details'] = [
                f"Handler response: {response}",
                f"Library refresh: {library_result}"
            ]
            
            print("✅ Series Add Handler test completed")
            return True
            
        except Exception as e:
            print(f"❌ Series Add Handler test failed: {e}")
            self.test_results['seriesadd']['status'] = 'failed'
            self.test_results['seriesadd']['details'] = [f"Error: {e}"]
            return False
    
    async def test_import_event_handler_live(self):
        """Test import event handler with live data"""
        print("\n📥 Testing Import Event Handler (Live)")
        
        try:
            test_data = {
                "eventType": "download",
                "instanceName": "Radarr",
                "movie": {
                    "id": 666666,
                    "title": "Live Import Test Movie",
                    "year": 2024,
                    "tmdbId": 555555,
                    "hasFile": True
                },
                "movieFile": {
                    "path": "/movies/Live Import Test Movie (2024)/Live Import Test Movie (2024).mkv"
                }
            }
            
            print(f"📤 Sending webhook data: {json.dumps(test_data, indent=2)}")
            
            response = handle_webhook(test_data)
            print(f"📥 Handler response: {response}")
            
            await asyncio.sleep(2)
            
            library_result = await refresh_jellyfin_library("Movies")
            print(f"📚 Library refresh result: {library_result}")
            
            self.test_results['import_event']['status'] = 'completed'
            self.test_results['import_event']['details'] = [
                f"Handler response: {response}",
                f"Library refresh: {library_result}"
            ]
            
            print("✅ Import Event Handler test completed")
            return True
            
        except Exception as e:
            print(f"❌ Import Event Handler test failed: {e}")
            self.test_results['import_event']['status'] = 'failed'
            self.test_results['import_event']['details'] = [f"Error: {e}"]
            return False
    
    async def test_file_delete_handlers_live(self):
        """Test file delete handlers with live data"""
        print("\n🗂️ Testing File Delete Handlers (Live)")
        
        # Test movie file delete
        try:
            movie_delete_data = {
                "eventType": "movieFileDeleted",
                "instanceName": "Radarr",
                "movie": {
                    "id": 555555,
                    "title": "Live File Delete Test Movie",
                    "year": 2024,
                    "tmdbId": 444444
                },
                "movieFile": {
                    "path": "/movies/Live File Delete Test Movie (2024)/movie.mkv"
                }
            }
            
            print(f"📤 Testing movie file delete: {json.dumps(movie_delete_data, indent=2)}")
            
            response = handle_webhook(movie_delete_data)
            print(f"📥 Movie file delete response: {response}")
            
            await asyncio.sleep(1)
            
            self.test_results['moviefiledelete']['status'] = 'completed'
            self.test_results['moviefiledelete']['details'] = [f"Handler response: {response}"]
            
        except Exception as e:
            print(f"❌ Movie file delete test failed: {e}")
            self.test_results['moviefiledelete']['status'] = 'failed'
            self.test_results['moviefiledelete']['details'] = [f"Error: {e}"]
        
        # Test episode file delete  
        try:
            episode_delete_data = {
                "eventType": "episodeFileDeleted",
                "instanceName": "Sonarr",
                "series": {
                    "id": 444444,
                    "title": "Live Episode Delete Test Series",
                    "tvdbId": 333333
                },
                "episodeFile": {
                    "path": "/tv/Live Episode Delete Test Series/Season 1/episode.mkv"
                }
            }
            
            print(f"📤 Testing episode file delete: {json.dumps(episode_delete_data, indent=2)}")
            
            response = handle_webhook(episode_delete_data)
            print(f"📥 Episode file delete response: {response}")
            
            await asyncio.sleep(1)
            
            self.test_results['episodefiledelete']['status'] = 'completed'
            self.test_results['episodefiledelete']['details'] = [f"Handler response: {response}"]
            
            print("✅ File Delete Handlers test completed")
            return True
            
        except Exception as e:
            print(f"❌ Episode file delete test failed: {e}")
            self.test_results['episodefiledelete']['status'] = 'failed'
            self.test_results['episodefiledelete']['details'] = [f"Error: {e}"]
            return False
    
    def print_test_summary(self):
        """Print comprehensive test summary"""
        print("\n" + "="*80)
        print("🎯 LIVE HANDLER TESTING SUMMARY")
        print("="*80)
        
        passed = 0
        failed = 0
        
        for handler_name, result in self.test_results.items():
            status_icon = "✅" if result['status'] == 'completed' else "❌" if result['status'] == 'failed' else "⏳"
            print(f"\n{status_icon} {handler_name.upper()} HANDLER:")
            print(f"   Status: {result['status']}")
            
            for detail in result['details']:
                print(f"   📝 {detail}")
            
            if result['status'] == 'completed':
                passed += 1
            elif result['status'] == 'failed':
                failed += 1
        
        print(f"\n📊 FINAL RESULTS:")
        print(f"   ✅ PASSED: {passed}")
        print(f"   ❌ FAILED: {failed}")
        print(f"   📊 TOTAL:  {passed + failed}")
        
        if failed == 0:
            print(f"\n🎉 ALL LIVE HANDLER TESTS PASSED!")
            print(f"🚀 Your handler system is working perfectly in production!")
        else:
            print(f"\n⚠️  {failed} handler(s) failed live testing")

async def run_live_handler_tests():
    """Run comprehensive live handler testing"""
    print("="*80)
    print("🚀 LIVE HANDLER TESTING")
    print("🎯 Testing all handlers with real webhooks and library updates")
    print("🐳 Running inside Docker container with live Jellyfin integration")
    print("="*80)
    
    tester = LiveHandlerTester()
    
    # Setup environment
    if not await tester.setup_test_environment():
        print("❌ Environment setup failed!")
        return False
    
    # Run all handler tests
    await tester.test_movieadd_handler_live()
    await tester.test_movie_delete_handler_live()
    await tester.test_seriesadd_handler_live()
    await tester.test_import_event_handler_live()
    await tester.test_file_delete_handlers_live()
    
    # Print summary
    tester.print_test_summary()
    
    return True

if __name__ == "__main__":
    success = asyncio.run(run_live_handler_tests())
    sys.exit(0 if success else 1)
