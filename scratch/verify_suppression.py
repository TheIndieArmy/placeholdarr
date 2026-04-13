import sys
import os
from unittest.mock import MagicMock

# Ensure project root is in sys.path
sys.path.insert(0, '/mnt/user/appdata/infiniteplexlibrarytest/scripts/placeholdarr-main')

from core.config import settings
from services.media_servers.refresh import refresh_selected_sections, refresh_all_paths

def test_suppression():
    print("Testing Refresh Suppression...")
    
    # Mock the underlying media server calls to avoid actual API calls
    import services.media_servers.refresh
    services.media_servers.refresh.refresh_plex_sections = MagicMock(return_value={"refreshed": 1, "failed": 0})
    services.media_servers.refresh.refresh_jellyfin_sections = MagicMock(return_value={"refreshed": 1, "failed": 0})
    services.media_servers.refresh.refresh_emby_sections = MagicMock(return_value={"refreshed": 1, "failed": 0})
    
    # 1. Normal state (not suppressed)
    settings.REFRESH_TRIGGER_SUPPRESSED = False
    result = refresh_selected_sections(True, True)
    print(f"Normal Refresh: {result}")
    assert result["refreshed"] > 0
    
    # 2. Suppressed state
    settings.REFRESH_TRIGGER_SUPPRESSED = True
    result = refresh_selected_sections(True, True)
    print(f"Suppressed Refresh: {result}")
    assert result["refreshed"] == 0
    
    # 3. Bypassed state
    result = refresh_selected_sections(True, True, bypass_suppression=True)
    print(f"Bypassed Refresh: {result}")
    assert result["refreshed"] > 0
    
    print("Verification Successful!")

if __name__ == "__main__":
    test_suppression()
