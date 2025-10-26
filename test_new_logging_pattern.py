#!/usr/bin/env python3
"""
Test the new logging pattern where:
1. Current handler logs to [handler_name]/log.txt
2. When new handler starts, moves existing log.txt to log_[datetime].txt
"""

import os
import sys
import time
import logging
from pathlib import Path

# Add the project root to the Python path
sys.path.insert(0, '/home/priky/indiefork')

from core.handler_logging import start_handler_logging, end_handler_logging

def main():
    """Test the new logging pattern"""
    
    # Configure root logger for testing
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s:%(lineno)d - %(levelname)s - %(message)s'
    )
    
    print("🧪 Testing new logging pattern...")
    print("=" * 60)
    
    # Test 1: Start first handler session
    print("📝 Test 1: Starting first seriesadd handler...")
    session_id_1 = start_handler_logging('handle_seriesadd', 12345, 'series', tvdb_id=384429)
    
    logger = logging.getLogger("test")
    logger.debug("First handler debug message")
    logger.info("First handler info message")
    logger.warning("First handler warning message")
    
    # Check if log.txt was created
    log_file = Path("logs/handle_seriesadd/log.txt")
    print(f"✅ Current log file exists: {log_file.exists()}")
    
    # End first session
    end_handler_logging(session_id_1, success=True, summary="First handler completed")
    
    # Wait a moment to ensure different timestamps
    time.sleep(2)
    
    # Test 2: Start second handler session (should archive the first)
    print("\n📝 Test 2: Starting second seriesadd handler (should archive first)...")
    session_id_2 = start_handler_logging('handle_seriesadd', 67890, 'series', tvdb_id=999999)
    
    logger.debug("Second handler debug message")
    logger.info("Second handler info message")
    logger.error("Second handler error message")
    
    # Check files
    logs_dir = Path("logs/handle_seriesadd")
    files = list(logs_dir.glob("*"))
    print(f"📂 Files in logs/handle_seriesadd/:")
    for file in sorted(files):
        print(f"   - {file.name}")
    
    # Verify archived file exists
    archived_files = list(logs_dir.glob("log_*.txt"))
    print(f"✅ Archived log files found: {len(archived_files)}")
    
    # Verify current log.txt exists
    print(f"✅ Current log.txt exists: {log_file.exists()}")
    
    # End second session
    end_handler_logging(session_id_2, success=True, summary="Second handler completed")
    
    # Test 3: Test different handler type
    print("\n📝 Test 3: Starting movieadd handler...")
    session_id_3 = start_handler_logging('handle_movieadd', 555, 'movie', tmdb_id=12345)
    
    logger.info("Movie handler message")
    
    # Check movieadd log
    movie_log_file = Path("logs/handle_movieadd/log.txt")
    print(f"✅ Movie log.txt exists: {movie_log_file.exists()}")
    
    # Verify seriesadd log.txt still exists (different handler)
    print(f"✅ Series log.txt still exists: {log_file.exists()}")
    
    end_handler_logging(session_id_3, success=True, summary="Movie handler completed")
    
    # Test 4: Start another seriesadd (should archive second one)
    print("\n📝 Test 4: Starting third seriesadd handler...")
    session_id_4 = start_handler_logging('handle_seriesadd', 11111, 'series')
    
    logger.info("Third seriesadd handler message")
    
    # Count archived files
    archived_files = list(logs_dir.glob("log_*.txt"))
    print(f"✅ Archived log files now: {len(archived_files)}")
    
    end_handler_logging(session_id_4, success=True, summary="Third handler completed")
    
    print("\n" + "=" * 60)
    print("📊 FINAL RESULTS:")
    
    # Show final state
    for handler in ['handle_seriesadd', 'handle_movieadd']:
        handler_dir = Path(f"logs/{handler}")
        if handler_dir.exists():
            files = list(handler_dir.glob("*"))
            print(f"\n📂 {handler}/:")
            for file in sorted(files):
                size = file.stat().st_size if file.is_file() else 0
                print(f"   - {file.name} ({size} bytes)")
    
    print("\n✅ Test completed successfully!")

if __name__ == "__main__":
    main()
