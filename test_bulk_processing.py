#!/usr/bin/env python3
"""
Test script to verify bulk processing works for both Jellyfin and Plex
"""

from services.postgres.db import get_session
from services.postgres.models import Movie, SubFlow
from services.scheduler import handle_movieadd_scheduler

def create_test_movies():
    """Create multiple test movies for bulk processing test"""
    session = get_session()
    try:
        # Create 3 test movies
        movies = []
        for i in range(2, 5):  # Movies 2, 3, 4
            movie = Movie(
                title=f"Test Movie {i}",
                year=2024,
                tmdbid=1000 + i,
                action='handle_movieadd',
                status='PENDING',
                dummypath=f'/dummy/test_movie_{i}.mp4',
                filepath=f'/movies/test_movie_{i}.mp4'
            )
            session.add(movie)
            movies.append(movie)
        
        session.commit()
        
        for movie in movies:
            print(f"Created Movie {movie.id}: {movie.title}")
            
            # Create SubFlows for each movie
            created_subflows = handle_movieadd_scheduler.create_subflows(movie.id)
            print(f"  Created {len(created_subflows)} SubFlows for Movie {movie.id}")
            
    except Exception as e:
        session.rollback()
        print(f"Error creating test movies: {e}")
    finally:
        session.close()

def check_subflow_status():
    """Check the status of all SubFlows"""
    session = get_session()
    try:
        # Check movies 2, 3, 4
        for movie_id in range(2, 5):
            movie = session.query(Movie).filter(Movie.id == movie_id).first()
            if movie:
                print(f"\nMovie {movie_id}: status={movie.status}")
                
                subflows = session.query(SubFlow).filter(
                    SubFlow.movie_id == movie_id, 
                    SubFlow.action == 'handle_movieadd'
                ).all()
                
                for sf in subflows:
                    steps = sf.steps.split(',') if sf.steps else []
                    current_step = steps[sf.step_index] if sf.step_index < len(steps) else "COMPLETED"
                    print(f"  SubFlow {sf.id}: status={sf.status}, step={sf.step_index}/{len(steps)} ({current_step})")
            else:
                print(f"\nMovie {movie_id}: NOT FOUND")
                
    finally:
        session.close()

if __name__ == "__main__":
    print("=== Bulk Processing Test ===")
    print("\n1. Creating test movies...")
    create_test_movies()
    
    print("\n2. Checking initial SubFlow status...")
    check_subflow_status()
    
    print("\n3. Test movies created! The scheduler will process these automatically.")
    print("   Use: docker compose logs placeholdarr -f")
    print("   to monitor bulk processing in action.")
    print("\n4. To check status again, run:")
    print("   docker compose exec placeholdarr python /app/test_bulk_processing.py")
