#!/usr/bin/env python3
"""
Test the handler logging system to make sure it creates proper log files
"""

import sys
import os
import time
sys.path.append('.')

from core.handler_logging import start_handler_logging, end_handler_logging, handler_log_manager
from core.logger import logger

def test_handler_logging():
    """Test the handler logging functionality"""
    
    print("🧪 Testing handler logging system...")
    
    try:
        # Test starting a session
        session_id = start_handler_logging(
            'handle_seriesadd',
            12345,
            'series',
            title='Test Series',
            year=2023,
            tvdb_id=12345,
            is_4k=False,
            episode_count=10
        )
        
        print(f"✅ Started logging session: {session_id}")
        
        # Log some test messages at different levels
        logger.debug("🐛 This is a debug message for testing")
        logger.info("ℹ️ This is an info message for testing")
        logger.warning("⚠️ This is a warning message for testing")
        logger.error("❌ This is an error message for testing")
        
        # Check if session is active
        active_sessions = handler_log_manager.get_active_sessions()
        print(f"📊 Active sessions: {len(active_sessions)}")
        
        for sid, session_info in active_sessions.items():
            print(f"   Session {sid}: {session_info['handler_name']} for {session_info['entity_type']} {session_info['entity_id']}")
        
        # Wait a bit to simulate processing time
        time.sleep(1)
        
        # End the session
        end_handler_logging(session_id, success=True, 
                           summary="Test completed - processed 10 episodes successfully")
        
        print(f"✅ Ended logging session: {session_id}")
        
        # Check if log file was created
        session_info = active_sessions.get(session_id)
        if session_info:
            log_file = session_info['log_file']
            if os.path.exists(log_file):
                print(f"✅ Log file created: {log_file}")
                
                # Show file contents
                with open(log_file, 'r') as f:
                    content = f.read()
                    lines = content.strip().split('\n')
                    print(f"📄 Log file contains {len(lines)} lines")
                    print("📝 Sample log content:")
                    for line in lines[:5]:  # Show first 5 lines
                        print(f"   {line}")
                    if len(lines) > 5:
                        print(f"   ... and {len(lines) - 5} more lines")
                
                return True
            else:
                print(f"❌ Log file not found: {log_file}")
                return False
        else:
            print("❌ Session info not found")
            return False
            
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_handler_logging()
    if success:
        print("\n🎉 Handler logging test PASSED!")
        sys.exit(0)
    else:
        print("\n💥 Handler logging test FAILED!")
        sys.exit(1)
