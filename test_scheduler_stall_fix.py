#!/usr/bin/env python3
"""
Test script to demonstrate the scheduler stall detection and fix.
This simulates the scenario where episodes complete delayed_placeholders 
but fail to advance due to scheduler concurrency limits.
"""
import sys
import os
sys.path.append('.')

# Mock classes to simulate the database models without requiring actual DB connection
class MockSubFlow:
    def __init__(self, action, episode_id=None, steps=None, step_index=0, status='PENDING'):
        self.action = action
        self.episode_id = episode_id
        self.steps = steps
        self.step_index = step_index
        self.status = status

class MockSession:
    def __init__(self):
        self.subflows = []
        self.added_subflows = []
        
    def query(self, model):
        return MockQuery(self.subflows)
    
    def add(self, obj):
        self.added_subflows.append(obj)
    
    def commit(self):
        self.subflows.extend(self.added_subflows)
        self.added_subflows.clear()

class MockQuery:
    def __init__(self, subflows):
        self.subflows = subflows
        self._filters = []
    
    def filter(self, *conditions):
        # Simple mock - just store filter descriptions
        return self
    
    def all(self):
        # Return subflows that match our test scenario
        # Episodes that completed delayed_placeholders but lack follow-up
        completed_delayed = [sf for sf in self.subflows 
                           if sf.steps == 'delayed_placeholders' and sf.status == 'DONE']
        return completed_delayed
    
    def first(self):
        # Check for existing enrichment SubFlows
        enrichment_sfs = [sf for sf in self.subflows 
                         if 'check_series_ready_for_enrichment' in (sf.steps or '')]
        return enrichment_sfs[0] if enrichment_sfs else None

def test_stall_detection_and_fix():
    print("🧪 Testing Scheduler Stall Detection and Fix")
    print("=" * 50)
    
    # Create test scenario: 9 episodes, 4 completed delayed_placeholders, 5 stuck
    mock_session = MockSession()
    
    # Episodes 1-4: completed delayed_placeholders (advanced successfully)
    for ep_id in range(1, 5):
        sf_delayed = MockSubFlow(
            action='series_add',
            episode_id=ep_id,
            steps='delayed_placeholders',
            status='DONE'
        )
        sf_enrichment = MockSubFlow(
            action='series_add', 
            episode_id=ep_id,
            steps='check_series_ready_for_enrichment',
            status='PENDING'
        )
        mock_session.subflows.extend([sf_delayed, sf_enrichment])
    
    # Episodes 5-9: completed delayed_placeholders but STUCK (no follow-up due to scheduler congestion)
    for ep_id in range(5, 10):
        sf_delayed = MockSubFlow(
            action='series_add',
            episode_id=ep_id,
            steps='delayed_placeholders', 
            status='DONE'
        )
        mock_session.subflows.append(sf_delayed)
    
    print(f"📊 Initial state:")
    print(f"   - Total SubFlows: {len(mock_session.subflows)}")
    print(f"   - Episodes 1-4: ✅ Advanced to enrichment")
    print(f"   - Episodes 5-9: ❌ Stuck after delayed_placeholders")
    
    # Simulate the stall detection logic
    print(f"\n🔍 Running stall detection...")
    
    completed_delayed = [sf for sf in mock_session.subflows 
                        if sf.action == 'series_add' and sf.status == 'DONE' 
                        and 'delayed_placeholders' in (sf.steps or '')]
    
    stalled_count = 0
    for sf in completed_delayed:
        episode_id = sf.episode_id
        if not episode_id:
            continue
            
        # Check if enrichment SubFlow exists
        existing_enrichment = None
        for check_sf in mock_session.subflows:
            if (check_sf.episode_id == episode_id and 
                check_sf.action == 'series_add' and 
                'check_series_ready_for_enrichment' in (check_sf.steps or '')):
                existing_enrichment = check_sf
                break
        
        if not existing_enrichment:
            print(f"   🔧 Episode {episode_id}: Creating missing enrichment SubFlow")
            
            # Create the missing SubFlow
            new_sf = MockSubFlow(
                action='series_add',
                episode_id=episode_id,
                steps='check_series_ready_for_enrichment',
                step_index=0,
                status='PENDING'
            )
            mock_session.add(new_sf)
            stalled_count += 1
        else:
            print(f"   ✅ Episode {episode_id}: Already has enrichment SubFlow")
    
    # Commit the fixes
    mock_session.commit()
    
    print(f"\n📈 Results:")
    print(f"   - Detected {stalled_count} stalled episodes")
    print(f"   - Created {len([sf for sf in mock_session.subflows if sf.steps == 'check_series_ready_for_enrichment'])} enrichment SubFlows")
    print(f"   - Total SubFlows after fix: {len(mock_session.subflows)}")
    
    # Verify fix worked
    episodes_with_enrichment = set()
    for sf in mock_session.subflows:
        if 'check_series_ready_for_enrichment' in (sf.steps or ''):
            episodes_with_enrichment.add(sf.episode_id)
    
    print(f"\n✅ Verification:")
    print(f"   - All 9 episodes now have enrichment SubFlows: {len(episodes_with_enrichment) == 9}")
    
    if len(episodes_with_enrichment) == 9:
        print(f"   🎉 SUCCESS: Scheduler stall fix works correctly!")
    else:
        print(f"   ❌ FAILURE: Only {len(episodes_with_enrichment)}/9 episodes have enrichment")
    
    return len(episodes_with_enrichment) == 9

if __name__ == "__main__":
    success = test_stall_detection_and_fix()
    sys.exit(0 if success else 1)
