import os
import sys
import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Import actual application code
from core.config import settings
from services.placeholdarr.placehold_handlers import (
    place_dummy_file, 
    get_folder_name, 
    get_file_name,
    handle_movieadd,
    handle_seriesadd,
    schedule_movie_request_update,
    schedule_episode_request_update
)

# Test fixtures
@pytest.fixture
def temp_folder():
    """Create a temporary folder for testing file operations"""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)

@pytest.fixture
def dummy_file(temp_folder):
    """Create a dummy file to use as template"""
    dummy_path = os.path.join(temp_folder, "dummy.mkv")
    with open(dummy_path, "wb") as f:
        f.write(b"DUMMY CONTENT")
    return dummy_path

# Regular unit tests - these should always run
def test_get_folder_name():
    """Test folder naming convention"""
    # Movie folder
    movie_folder = get_folder_name("movie", "Test Movie", 2023, 12345)
    assert movie_folder == "Test Movie (2023) {tmdb-12345}{edition-Dummy}"
    
    # TV show folder
    tv_folder = get_folder_name("episode", "Test Series", 2023, 67890)
    assert tv_folder == "Test Series (2023) {tvdb-67890}"
    
    # Handle special characters
    special_folder = get_folder_name("movie", "Test: Movie?", 2023, 12345)
    assert ":" not in special_folder
    assert "?" not in special_folder

def test_get_file_name():
    """Test file naming convention"""
    # Movie file
    movie_file = get_file_name("movie", "Test Movie", 2023)
    assert movie_file == "Test Movie (2023).mkv"
    
    # TV episode file
    episode_file = get_file_name("episode", "Test Series", season=1, episode=5)
    assert episode_file == "Test Series - s01e05.mkv"
    
    # Handle special characters
    special_file = get_file_name("movie", "Test: Movie?", 2023)
    assert ":" not in special_file
    assert "?" not in special_file

def test_place_dummy_file(temp_folder, dummy_file):
    """Test creating placeholder files"""
    # Store the original settings
    original_dummy_file = settings.DUMMY_FILE_PATH
    original_strategy = settings.PLACEHOLDER_STRATEGY
    
    try:
        # Use the temp dummy file and folder for testing
        settings.DUMMY_FILE_PATH = dummy_file
        settings.PLACEHOLDER_STRATEGY = "copy"
        
        # Test movie placeholder
        movie_folder = os.path.join(temp_folder, "movies")
        os.makedirs(movie_folder, exist_ok=True)
        
        movie_result = place_dummy_file(
            "movie", 
            movie_folder, 
            "Illumination", 
            1972, 
            184978
        )
        
        # Verify folder structure
        expected_movie_folder = os.path.join(movie_folder, "Illumination (1972) {tmdb-184978}{edition-Dummy}")
        expected_movie_file = os.path.join(expected_movie_folder, "Illumination (1972).mkv")
        
        assert os.path.exists(expected_movie_folder), f"Movie folder not created: {expected_movie_folder}"
        assert os.path.exists(expected_movie_file), f"Movie file not created: {expected_movie_file}"
        
        # Test TV show placeholder
        tv_folder = os.path.join(temp_folder, "tv")
        os.makedirs(tv_folder, exist_ok=True)
        
        tv_result = place_dummy_file(
            "episode",
            tv_folder,
            "Gangsters",
            1976,
            78953,
            1,
            1
        )
        
        # Verify folder structure
        expected_series_folder = os.path.join(tv_folder, "Gangsters (1976) {tvdb-78953}")
        expected_season_folder = os.path.join(expected_series_folder, "Season 01")
        expected_episode_file = os.path.join(expected_season_folder, "Gangsters - s01e01.mkv")
        
        assert os.path.exists(expected_series_folder), f"Series folder not created: {expected_series_folder}"
        assert os.path.exists(expected_season_folder), f"Season folder not created: {expected_season_folder}"
        assert os.path.exists(expected_episode_file), f"Episode file not created: {expected_episode_file}"
        
    finally:
        # Restore original settings
        settings.DUMMY_FILE_PATH = original_dummy_file
        settings.PLACEHOLDER_STRATEGY = original_strategy

def test_extract_ids():
    """Test extracting IDs from file paths"""
    path = "/media/tv/Show Title (2023) {tvdb-123456}/Season 01/Show Title - s01e01.mkv"
    import re
    tvdb_match = re.search(r'tvdb-(\d+)', path)
    assert tvdb_match
    assert tvdb_match.group(1) == "123456"
    
    path = "/media/movies/Movie Title (2023) {tmdb-789012}{edition-Dummy}/Movie Title (2023).mkv"
    tmdb_match = re.search(r'tmdb-(\d+)', path)
    assert tmdb_match
    assert tmdb_match.group(1) == "789012"

def test_movieadd_handler():
    """Test the movie add handler"""
    from fastapi.responses import JSONResponse
    
    # Create a sample movie add payload
    payload = {
        "movie": {
            "title": "The Falls",
            "year": 1980,
            "tmdbId": 36540,
            "folderPath": "/movies/The Falls (1980)"
        },
        "eventType": "movieadd"
    }
    
    # Store original paths
    original_movie_folder = settings.MOVIE_LIBRARY_FOLDER
    original_dummy_file = settings.DUMMY_FILE_PATH
    original_4k_folder = settings.MOVIE_LIBRARY_4K_FOLDER
    
    try:
        # Create test folders
        with tempfile.TemporaryDirectory() as temp_dir:
            dummy_file = os.path.join(temp_dir, "dummy.mkv")
            with open(dummy_file, "wb") as f:
                f.write(b"DUMMY")
                
            movie_folder = os.path.join(temp_dir, "movies")
            os.makedirs(movie_folder, exist_ok=True)
            
            # Temporarily override settings
            settings.MOVIE_LIBRARY_FOLDER = movie_folder
            settings.DUMMY_FILE_PATH = dummy_file
            settings.MOVIE_LIBRARY_4K_FOLDER = ""  # Explicitly set to empty string
            
            # Mock the schedule_movie_request_update function since it requires Plex
            with patch("services.placeholdarr.placehold_handlers.schedule_movie_request_update") as mock_update:
                # Call the handler
                response = handle_movieadd(payload)
                
                # Check response
                assert isinstance(response, JSONResponse)
                assert response.status_code == 200
                
                # Check if placeholder was created - ID should be in the folder name
                expected_folder = os.path.join(movie_folder, "The Falls (1980) {tmdb-36540}{edition-Dummy}")
                expected_file = os.path.join(expected_folder, "The Falls (1980).mkv")
                
                print(f"Checking for movie folder: {expected_folder}")
                assert os.path.exists(expected_folder), "Movie folder was not created"
                
                print(f"Checking for movie file: {expected_file}")
                assert os.path.exists(expected_file), "Movie file was not created"
                
    finally:
        # Restore original settings
        settings.MOVIE_LIBRARY_FOLDER = original_movie_folder
        settings.DUMMY_FILE_PATH = original_dummy_file
        settings.MOVIE_LIBRARY_4K_FOLDER = original_4k_folder

def test_seriesadd_handler():
    """Test the series add handler"""
    from fastapi.responses import JSONResponse
    
    # Create a sample series add payload
    payload = {
        "series": {
            "title": "Children of the Stones",
            "year": 1977,
            "tvdbId": 74834,
            "path": "/tv/Children of the Stones (1977)"
        },
        "seasons": [
            {"seasonNumber": 1, "monitored": True}
        ],
        "eventType": "seriesadd"
    }
    
    # Store original paths
    original_tv_folder = settings.TV_LIBRARY_FOLDER
    original_dummy_file = settings.DUMMY_FILE_PATH
    original_4k_folder = settings.TV_LIBRARY_4K_FOLDER
    
    try:
        # Create test folders
        with tempfile.TemporaryDirectory() as temp_dir:
            dummy_file = os.path.join(temp_dir, "dummy.mkv")
            with open(dummy_file, "wb") as f:
                f.write(b"DUMMY")
                
            tv_folder = os.path.join(temp_dir, "tv")
            os.makedirs(tv_folder, exist_ok=True)
            
            # Temporarily override settings
            settings.TV_LIBRARY_FOLDER = tv_folder
            settings.DUMMY_FILE_PATH = dummy_file
            settings.TV_LIBRARY_4K_FOLDER = ""  # Explicitly set to empty string
            
            # Mock the schedule_episode_request_update function since it requires Plex
            with patch("services.placeholdarr.placehold_handlers.schedule_episode_request_update") as mock_update:
                # Call the handler
                response = handle_seriesadd(payload, False)
                
                # Check response
                assert isinstance(response, JSONResponse)
                assert response.status_code == 200
                
                # Check if placeholder was created
                expected_folder = os.path.join(tv_folder, "Children of the Stones (1977) {tvdb-74834}")
                expected_season_folder = os.path.join(expected_folder, "Season 01")
                expected_file = os.path.join(expected_season_folder, "Children of the Stones - s01e01.mkv")
                
                print(f"Checking for series folder: {expected_folder}")
                assert os.path.exists(expected_folder), "Series folder was not created"
                
                print(f"Checking for season folder: {expected_season_folder}")
                assert os.path.exists(expected_season_folder), "Season folder was not created"
                
                print(f"Checking for episode file: {expected_file}")
                assert os.path.exists(expected_file), "Episode file was not created"
                
    finally:
        # Restore original settings
        settings.TV_LIBRARY_FOLDER = original_tv_folder
        settings.DUMMY_FILE_PATH = original_dummy_file
        settings.TV_LIBRARY_4K_FOLDER = original_4k_folder

def test_schedule_movie_request_update():
    """Test the movie request update scheduling"""
    with patch("services.placeholdarr.placehold_handlers.find_movie_by_tmdb_id") as mock_find:
        with patch("services.placeholdarr.placehold_handlers.update_title") as mock_update:
            # Mock the Plex functions
            mock_find.return_value = "12345"  # Simulate finding a movie
            
            # Call the function
            schedule_movie_request_update("Test Movie (2021)", 123456, False)
            
            # Check that threading.Timer was called (it's harder to test directly)
            # But we can verify the mocks were set up correctly
            assert mock_find.call_count == 0  # Not called immediately (will be called by timer)
            assert mock_update.call_count == 0  # Not called immediately

def test_schedule_episode_request_update():
    """Test the episode request update scheduling"""
    with patch("services.placeholdarr.placehold_handlers.find_episode_by_tvdb_id") as mock_find:
        with patch("services.placeholdarr.placehold_handlers.update_title") as mock_update:
            # Mock the Plex functions
            mock_find.return_value = "12345"  # Simulate finding an episode
            
            # Call the function
            schedule_episode_request_update(67890, 1, 1, "Episode 1", False)
            
            # Check that threading.Timer was called (it's harder to test directly)
            # But we can verify the mocks were set up correctly
            assert mock_find.call_count == 0  # Not called immediately (will be called by timer)
            assert mock_update.call_count == 0  # Not called immediately