import pytest
import json
from unittest.mock import patch, MagicMock
from services.handlers import handle_webhook
from services.placeholdarr.placehold_handlers import handle_movieadd, handle_seriesadd

# Sample webhook data
SAMPLE_MOVIE_ADD = {
    "eventType": "MovieAdd",
    "movie": {
        "id": 1,
        "title": "Test Movie",
        "folderPath": "/movies/Test Movie",
        "year": 2023
    }
}

SAMPLE_SERIES_ADD = {
    "eventType": "SeriesAdd",
    "series": {
        "id": 1,
        "title": "Test Series",
        "path": "/tv/Test Series",
        "year": 2023
    }
}

# We need to patch where the function is used, not where it's defined
@patch('services.handlers.handle_movieadd')
def test_movie_add_webhook_calls_handler(mock_handle_movieadd):
    """Test that movie add webhook properly calls the handler"""
    # Setup mock
    mock_handle_movieadd.return_value = {"status": "success"}
    
    # Call the webhook handler directly
    result = handle_webhook(SAMPLE_MOVIE_ADD)
    
    # Verify the handler was called with correct data
    mock_handle_movieadd.assert_called_once()
    args, kwargs = mock_handle_movieadd.call_args
    assert args[0] == SAMPLE_MOVIE_ADD
    
    # Verify result
    assert isinstance(result, dict)

@patch('services.handlers.handle_seriesadd')
def test_series_add_webhook_calls_handler(mock_handle_seriesadd):
    """Test that series add webhook properly calls the handler"""
    # Setup mock
    mock_handle_seriesadd.return_value = {"status": "success"}
    
    # Call the webhook handler directly
    result = handle_webhook(SAMPLE_SERIES_ADD)
    
    # Verify the handler was called with correct data
    mock_handle_seriesadd.assert_called_once()
    args, kwargs = mock_handle_seriesadd.call_args
    assert args[0] == SAMPLE_SERIES_ADD
    
    # Verify result
    assert isinstance(result, dict)