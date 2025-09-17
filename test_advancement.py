#!/usr/bin/env python3
"""
Test script to manually trigger entity advancement check
"""
import sys
sys.path.append('/app')

from services.scheduler import ActionScheduler
from services.postgres.models import Movie

# Test Movie 1 with handle_movie_delete action
scheduler = ActionScheduler('handle_movie_delete')
scheduler.model = Movie

print("Testing manual advancement for Movie 1...")
scheduler.check_entity_advancement(1)
print("Done!")
