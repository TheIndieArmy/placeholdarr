#!/usr/bin/env python3

"""
Test script to verify the enrichment optimization.
This tests that enrich_comprehensive_metadata is called only once per series
instead of once per episode.
"""

import asyncio
import sys
sys.path.append('/home/priky/indiefork')

from core.config import settings
from services.postgres.models import Series, Season, Episode, SubFlow, Base
from services.postgres.db import get_session
from services.scheduler import ActionScheduler

async def test_enrichment_optimization():
    """Test the optimized enrichment workflow"""
    
        # Get database session using existing connection
    session = get_session()
    
    try:
        print("🧪 Testing enrichment optimization...")
        
        # Find a series with multiple episodes
        series = session.query(Series).join(Season).join(Episode).first()
        if not series:
            print("❌ No series with episodes found for testing")
            return
            
        print(f"📺 Using series: {series.title} (ID: {series.id})")
        
        # Count episodes
        episodes = session.query(Episode).join(Season).filter(Season.series_id == series.id).all()
        print(f"📋 Episodes in series: {len(episodes)}")
        
        # Clear any existing SubFlows for this series
        session.query(SubFlow).filter(SubFlow.series_id == series.id).delete()
        session.commit()
        print("🧹 Cleared existing SubFlows")
        
        # Reset episode states
        for episode in episodes[:3]:  # Test with first 3 episodes only
            episode.current_step_name = "delayed_placeholders"
            episode.status = 'PENDING'
            session.add(episode)
        session.commit()
        print(f"🔄 Reset {min(3, len(episodes))} episodes to initial state")
        
        # Create initial SubFlows for delayed_placeholders (simulating normal flow start)
        for episode in episodes[:3]:
            subflow = SubFlow(
                series_id=series.id,
                episode_id=episode.id,
                action="handle_seriesadd",
                steps="delayed_placeholders",
                branch=str(episode.id),
                status="QUEUED"
            )
            session.add(subflow)
        session.commit()
        print(f"✅ Created {min(3, len(episodes))} initial SubFlows")
        
        # Test the scheduler processing
        scheduler = ActionScheduler(action="handle_seriesadd", model=Episode, args=episodes[0].id)
        
        print("\n🚀 Starting scheduler processing...")
        print("This should:")
        print("1. Process delayed_placeholders for each episode")
        print("2. Move to check_series_ready_for_enrichment") 
        print("3. Call enrich_comprehensive_metadata only ONCE when all episodes are ready")
        print("4. Create episode SubFlows for platform processing")
        
        # Process the scheduler (this will run the optimized flow)
        await scheduler.process()
        
        print("\n📊 Results:")
        
        # Check SubFlows created
        subflows = session.query(SubFlow).filter(SubFlow.series_id == series.id).all()
        enrichment_subflows = [sf for sf in subflows if sf.steps == "enrich_comprehensive_metadata"]
        platform_subflows = [sf for sf in subflows if sf.steps == "jellyfin,plex"]
        
        print(f"📋 Total SubFlows created: {len(subflows)}")
        print(f"🎯 Enrichment SubFlows: {len(enrichment_subflows)}")
        print(f"📱 Platform SubFlows: {len(platform_subflows)}")
        
        if len(enrichment_subflows) == 1:
            print("✅ SUCCESS: Only 1 enrichment SubFlow created (optimal!)")
        else:
            print(f"❌ ISSUE: {len(enrichment_subflows)} enrichment SubFlows created (should be 1)")
            
        # Check that enrichment SubFlow is series-level (no episode_id)
        series_level_enrichment = [sf for sf in enrichment_subflows if sf.episode_id is None]
        if len(series_level_enrichment) == 1:
            print("✅ SUCCESS: Enrichment is series-level (no episode_id)")
        else:
            print(f"❌ ISSUE: Series-level enrichment count: {len(series_level_enrichment)}")
        
        # Show SubFlow details
        print("\n📋 SubFlow Details:")
        for sf in subflows:
            episode_info = f"Episode {sf.episode_id}" if sf.episode_id else "Series-level"
            print(f"  - {sf.steps} | {episode_info} | Status: {sf.status}")
            
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        session.close()

if __name__ == "__main__":
    asyncio.run(test_enrichment_optimization())
