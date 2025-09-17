#!/usr/bin/env python3
"""
Test script to manually create SubFlows and test bulk processing
"""
import sys
sys.path.append('/app')

from services.postgres.db import get_session
from services.postgres.models import Movie, SubFlow
from services.scheduler import ActionScheduler

# Create SubFlows manually to test bulk processing
session = get_session()

try:
    # Create a delayed_placeholders SubFlow for Movie 1
    sf1 = SubFlow(
        movie_id=1,
        action='handle_movieadd',
        steps='delayed_placeholders',
        branch='main',
        step_index=0,
        status='PENDING'
    )
    session.add(sf1)
    
    # Create a jellyfin branch SubFlow for Movie 1  
    sf2 = SubFlow(
        movie_id=1,
        action='handle_movieadd',
        steps='refresh_jellyfin_dummy,verify_dummy_scan_jellyfin,update_placeholder_status,update_jellyfin_title_status,verify_dummy_scan_jellyfin,retry_failed_jellyfin_title_updates',
        branch='jellyfin',
        step_index=0,
        status='PENDING'
    )
    session.add(sf2)
    
    session.commit()
    print(f"Created SubFlows: {sf1.id}, {sf2.id}")
    print("SubFlows will be picked up by scheduler automatically")
    
except Exception as e:
    print(f"Error: {e}")
    session.rollback()
finally:
    session.close()
