#!/usr/bin/env python3
"""
Test script to trigger a movie add flow
"""
import sys
sys.path.append('/app')

from services.postgres.db import get_session
from services.postgres.models import Movie
from services.scheduler import ActionScheduler

# Get the movie scheduler
session = get_session()
movie = session.query(Movie).get(1)
if movie:
    print(f"Found movie: {movie.title}")
    
    # Trigger the handle_movieadd flow
    from services import handlers
    handlers.enqueue_movieadd_action(movie.tmdbid, movie.title)
    print("Triggered movieadd action")
else:
    print("Movie 1 not found")

session.close()
