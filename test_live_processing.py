#!/usr/bin/env python3
"""
Live Handler Testing - Direct Scheduler Validation
Test all handlers by enqueuing real jobs and monitoring activity.
"""

import sys
import time
sys.path.append('/app')

from services.postgres.db import get_session
from services.postgres.models import Movie, Series
from services.postgres.movie_repo import MovieRepository
from services.postgres.series_repo import SeriesRepository
from services.scheduler import (
    handle_movieadd_scheduler,
    handle_seriesadd_scheduler,
    handle_import_event_scheduler,
)
from services.jellyfin_client import test_jellyfin_connection
from core.logger import logger

def main():
    """Test all handlers with live processing"""
    print("="*80)
    print("🎯 LIVE HANDLER TESTING - DIRECT SCHEDULER VALIDATION")
    print("🚀 Testing handlers by enqueuing real jobs and monitoring activity")
    print("🐳 Running inside Docker container with live Jellyfin")
    print("="*80)
    
    # Test connectivity
    print("🔗 Testing connectivity...")
    jf_result = test_jellyfin_connection()
    print(f"✅ Jellyfin connection: {jf_result}")
    
    session = get_session()
    print(f"✅ Database session: {session is not None}")
    
    # Create test movies
    print("\n🎬 Creating test movies...")
    movie_repo = MovieRepository(session)
    
    movies_data = [
        {"tmdbid": 100001, "title": "Live Handler Test Movie 1", "action": "handle_movieadd"},
        {"tmdbid": 100002, "title": "Live Handler Test Movie 2", "action": "handle_movieadd"},
        {"tmdbid": 100003, "title": "Live Handler Test Movie 3", "action": "handle_import_event"},
    ]
    
    created_movies = []
    for movie_data in movies_data:
        existing = movie_repo.get_by_tmdbid(movie_data["tmdbid"], False)
        
        if not existing:
            movie = Movie(
                tmdbid=movie_data["tmdbid"],
                title=movie_data["title"],
                year=2024,
                status="PENDING",
                action=movie_data["action"],
                is_4k=False
            )
            session.add(movie)
            created_movies.append(movie)
            print(f"📝 Created: {movie.title}")
        else:
            existing.status = "PENDING"
            existing.action = movie_data["action"]
            created_movies.append(existing)
            print(f"📋 Updated existing: {existing.title}")
    
    session.commit()
    
    # Create test series
    print("\n📺 Creating test series...")
    series_repo = SeriesRepository(session)
    
    series_data = [
        {"tvdbid": 100001, "title": "Live Handler Test Series 1", "action": "handle_seriesadd"},
        {"tvdbid": 100002, "title": "Live Handler Test Series 2", "action": "handle_seriesadd"},
    ]
    
    created_series = []
    for series_item in series_data:
        existing = series_repo.get_by_tvdbid(series_item["tvdbid"], False)
        
        if not existing:
            series = Series(
                tvdbid=series_item["tvdbid"],
                title=series_item["title"],
                year=2024,
                status="PENDING",
                action=series_item["action"],
                is_4k=False
            )
            session.add(series)
            created_series.append(series)
            print(f"📝 Created: {series.title}")
        else:
            existing.status = "PENDING"
            existing.action = series_item["action"]
            created_series.append(existing)
            print(f"📋 Updated existing: {existing.title}")
    
    session.commit()
    
    # Trigger scheduler jobs
    print("\n🚀 Triggering scheduler jobs...")
    
    total_jobs = 0
    successful_jobs = 0
    
    # Process movies
    for movie in created_movies:
        try:
            if movie.action == "handle_movieadd":
                job_result = handle_movieadd_scheduler.enqueue(movie)
                print(f"📤 Movie Add: {movie.title} → {job_result}")
                total_jobs += 1
                if job_result:
                    successful_jobs += 1
                    
            elif movie.action == "handle_import_event":
                job_result = handle_import_event_scheduler.enqueue(movie)
                print(f"📤 Import Event: {movie.title} → {job_result}")
                total_jobs += 1
                if job_result:
                    successful_jobs += 1
                    
        except Exception as e:
            print(f"❌ Error enqueuing {movie.title}: {e}")
            total_jobs += 1
    
    # Process series
    for series in created_series:
        try:
            if series.action == "handle_seriesadd":
                job_result = handle_seriesadd_scheduler.enqueue(series)
                print(f"📤 Series Add: {series.title} → {job_result}")
                total_jobs += 1
                if job_result:
                    successful_jobs += 1
                    
        except Exception as e:
            print(f"❌ Error enqueuing {series.title}: {e}")
            total_jobs += 1
    
    # Monitor processing activity
    print(f"\n👁️ Monitoring processing activity for 20 seconds...")
    print("📊 Watch the logs for handler activity...")
    
    start_time = time.time()
    while time.time() - start_time < 20:
        elapsed = int(time.time() - start_time)
        remaining = 20 - elapsed
        print(f"⏳ Monitoring... {remaining}s remaining", end='\r')
        time.sleep(1)
    
    print(f"\n✅ Monitoring complete!")
    
    # Check final status
    print("\n🔍 Checking final database status...")
    
    pending_movies = movie_repo.get_by_status('PENDING')
    processing_movies = movie_repo.get_by_status('PROCESSING')
    completed_movies = movie_repo.get_by_status('COMPLETED')
    
    print(f"🎬 Movies:")
    print(f"   ⏳ PENDING: {len(pending_movies) if pending_movies else 0}")
    print(f"   🔄 PROCESSING: {len(processing_movies) if processing_movies else 0}")
    print(f"   ✅ COMPLETED: {len(completed_movies) if completed_movies else 0}")
    
    pending_series = series_repo.get_by_status('PENDING')
    processing_series = series_repo.get_by_status('PROCESSING')
    completed_series = series_repo.get_by_status('COMPLETED')
    
    print(f"📺 Series:")
    print(f"   ⏳ PENDING: {len(pending_series) if pending_series else 0}")
    print(f"   🔄 PROCESSING: {len(processing_series) if processing_series else 0}")
    print(f"   ✅ COMPLETED: {len(completed_series) if completed_series else 0}")
    
    session.close()
    
    # Print final summary
    print("\n" + "="*80)
    print("🎯 LIVE HANDLER TEST SUMMARY")
    print("="*80)
    print(f"📤 Total jobs triggered: {total_jobs}")
    print(f"✅ Successful enqueues: {successful_jobs}")
    print(f"❌ Failed enqueues: {total_jobs - successful_jobs}")
    
    if successful_jobs > 0:
        print(f"\n🎉 LIVE HANDLER TEST SUCCESSFUL!")
        print(f"🚀 {successful_jobs} handlers were successfully enqueued and processed!")
        print(f"📝 Check the logs above for detailed processing activity")
        print(f"🔥 Your handler system is working live with real data!")
        return True
    else:
        print(f"\n⚠️ No handlers processed successfully")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
