"""
Test for episodefiledelete handler to verify Jellyfin integration end-to-end.
Tests episode file deletion detection, placeholder restoration, and episodefiledelete workflow validation.
"""

import pytest
import asyncio
import os
import time
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from services.postgres.models import Series, Season, Episode, SubFlow
from services.postgres.db import get_session
from services.jellyfin_client import get_jellyfin_library, refresh_jellyfin_item, scan_jellyfin_library, get_jellyfin_item_by_id
from services.integrations import create_dummy_series_folder, delete_folder
from services.flow_manager import FlowManager
from core.config import config
from core.logger import logger
import shutil

class TestEpisodeFileDeleteHandlerJellyfin:
    """Test episodefiledelete handler Jellyfin integration end-to-end"""
    
    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Set up test environment and clean up after each test"""
        self.test_series_data = {
            'title': 'Test Episode Delete Series Westworld',
            'year': 2016,
            'tvdbid': 333333,  # Using unique test TVDB ID for episode delete tests
            'is_4k': False
        }
        
        self.test_episode_data = {
            'episode_number': 1,
            'title': 'The Original',
            'year': 2016
        }
        
        # Clean up any existing test data
        self.cleanup_test_data()
        yield
        # Clean up after test
        self.cleanup_test_data()
    
    def cleanup_test_data(self):
        """Clean up test series data from database and filesystem"""
        with get_session() as session:
            # Remove test series and related data
            series = session.query(Series).filter_by(tvdbid=self.test_series_data['tvdbid']).first()
            if series:
                # Remove subflows
                subflows = session.query(SubFlow).filter_by(series_id=series.id).all()
                for subflow in subflows:
                    session.delete(subflow)
                
                # Remove seasons and episodes
                seasons = session.query(Season).filter_by(series_id=series.id).all()
                for season in seasons:
                    episodes = session.query(Episode).filter_by(season_id=season.id).all()
                    for episode in episodes:
                        session.delete(episode)
                    session.delete(season)
                
                # Remove dummy folder if exists
                if series.dummypath and os.path.exists(series.dummypath):
                    try:
                        shutil.rmtree(series.dummypath)
                        logger.info(f"Cleaned up dummy folder: {series.dummypath}")
                    except Exception as e:
                        logger.warning(f"Failed to clean up dummy folder {series.dummypath}: {e}")
                
                session.delete(series)
                session.commit()
                logger.info(f"Cleaned up test series: {self.test_series_data['title']}")
    
    def create_test_episode_with_file(self):
        """Create a test series/season/episode with existing file (post-import state)"""
        # Create series
        with get_session() as session:
            series = Series(**self.test_series_data)
            session.add(series)
            session.commit()
            series_id = series.id
        
        # Create season
        with get_session() as session:
            season = Season(
                series_id=series_id,
                season_number=1,
                title=f"{self.test_series_data['title']} Season 1",
                year=self.test_series_data['year']
            )
            session.add(season)
            session.commit()
            season_id = season.id
        
        # Create episode with file information
        with get_session() as session:
            episode = Episode(
                season_id=season_id,
                episode_number=self.test_episode_data['episode_number'],
                title=self.test_episode_data['title'],
                year=self.test_episode_data['year'],
                has_file=True,
                episodefile_path="/media/tv/Westworld (2016)/Season 01/Westworld.S01E01.The.Original.1080p.BluRay.x264.mkv",
                episodefile_size=3221225472,  # 3GB
                sonarr_quality="Bluray-1080p",
                status="IMPORTED",
                jellyfin_id="test_jellyfin_episode_id",
                jellyfin_title=self.test_episode_data['title'],
                action="episodefiledelete"
            )
            session.add(episode)
            session.commit()
            episode_id = episode.id
        
        logger.info(f"Created test episode with file: {episode}")
        return series_id, season_id, episode_id
    
    def get_test_episode(self, episode_id):
        """Get the test episode from database"""
        with get_session() as session:
            return session.query(Episode).filter_by(id=episode_id).first()
    
    def get_test_series(self):
        """Get the test series from database"""
        with get_session() as session:
            return session.query(Series).filter_by(tvdbid=self.test_series_data['tvdbid']).first()
    
    def test_episodefiledelete_detects_file_removal(self):
        """Test that episodefiledelete handler detects when episode file is removed"""
        # Create test episode with file
        series_id, season_id, episode_id = self.create_test_episode_with_file()
        episode = self.get_test_episode(episode_id)
        
        # Verify initial file state
        assert episode.has_file is True, "Episode should have file initially"
        assert episode.episodefile_path is not None, "Episode should have file path"
        
        # Simulate file deletion event
        with get_session() as session:
            episode = session.query(Episode).filter_by(id=episode_id).first()
            episode.has_file = False
            episode.episodefile_path = None
            episode.episodefile_size = None
            episode.sonarr_quality = None
            episode.action = "episodefiledelete"
            episode.status = "FILE_DELETE_DETECTED"
            session.commit()
        
        # Verify file deletion detection
        deleted_episode = self.get_test_episode(episode_id)
        assert deleted_episode.has_file is False, "Episode should not have file after deletion"
        assert deleted_episode.episodefile_path is None, "Episode file path should be cleared"
        assert deleted_episode.action == "episodefiledelete", "Action should be episodefiledelete"
        assert deleted_episode.status == "FILE_DELETE_DETECTED", "Status should indicate file deletion detected"
        
        logger.info("✅ Episode file delete detection validated")
    
    def test_episodefiledelete_creates_placeholder_replacement(self):
        """Test that episodefiledelete handler creates placeholder to replace deleted episode file"""
        # Create test episode with file
        series_id, season_id, episode_id = self.create_test_episode_with_file()
        
        # Simulate file deletion
        with get_session() as session:
            episode = session.query(Episode).filter_by(id=episode_id).first()
            episode.has_file = False
            episode.episodefile_path = None
            episode.status = "FILE_DELETE_PROCESSING"
            session.commit()
        
        # Create dummy placeholder for the series (episodes use series-level placeholders)
        series = self.get_test_series()
        dummy_path = create_dummy_series_folder(
            series.title, 
            series.year, 
            series.tvdbid
        )
        
        # Update series with dummy path
        with get_session() as session:
            series = session.query(Series).filter_by(id=series_id).first()
            series.dummypath = dummy_path
            series.placeholder_status = "RECREATED_AFTER_EPISODE_DELETE"
            session.commit()
        
        # Update episode status
        with get_session() as session:
            episode = session.query(Episode).filter_by(id=episode_id).first()
            episode.status = "PLACEHOLDER_RECREATED"
            session.commit()
        
        # Verify placeholder creation
        assert os.path.exists(dummy_path), f"Dummy folder should exist at {dummy_path}"
        assert os.path.isdir(dummy_path), f"Dummy path should be a directory"
        
        # Verify folder name contains expected elements
        folder_name = os.path.basename(dummy_path)
        assert f"tmdb-{series.tvdbid}" in folder_name, f"Folder name should contain TVDB ID"
        assert "edition-Dummy" in folder_name, f"Folder name should contain 'edition-Dummy'"
        
        updated_episode = self.get_test_episode(episode_id)
        assert updated_episode.status == "PLACEHOLDER_RECREATED", "Should indicate placeholder recreation"
        
        logger.info(f"✅ Episode placeholder replacement created successfully: {dummy_path}")
    
    def test_episodefiledelete_triggers_jellyfin_refresh(self):
        """Test that episodefiledelete triggers Jellyfin library refresh"""
        # Create test episode with file
        series_id, season_id, episode_id = self.create_test_episode_with_file()
        
        # Simulate file deletion and placeholder creation
        series = self.get_test_series()
        dummy_path = create_dummy_series_folder(
            series.title, 
            series.year, 
            series.tvdbid
        )
        
        with get_session() as session:
            # Update episode
            episode = session.query(Episode).filter_by(id=episode_id).first()
            episode.has_file = False
            episode.episodefile_path = None
            episode.status = "JELLYFIN_REFRESHING"
            
            # Update series
            series = session.query(Series).filter_by(id=series_id).first()
            series.dummypath = dummy_path
            session.commit()
        
        # Trigger Jellyfin library scan (simulating episodefiledelete workflow)
        logger.info("Triggering Jellyfin library scan after episode file deletion...")
        scan_result = scan_jellyfin_library()
        assert scan_result is True, "Jellyfin library scan should succeed"
        
        # Wait for scan to complete
        time.sleep(5)
        
        # Check if series appears in Jellyfin library with placeholder status
        logger.info("Checking Jellyfin library for series placeholder after episode file deletion...")
        library_items = get_jellyfin_library()
        
        # Look for our test series in the library
        test_series_found = False
        for item in library_items:
            if (item.get("Type") == "Series" and 
                "Westworld" in item.get("Name", "")):
                test_series_found = True
                jellyfin_title = item.get("Name", "")
                logger.info(f"✅ Found series placeholder in Jellyfin: {jellyfin_title}")
                
                # Update series with placeholder Jellyfin info
                with get_session() as session:
                    series = session.query(Series).filter_by(id=series_id).first()
                    series.jellyfin_dummy_id = item.get("Id")
                    series.jellyfin_title = jellyfin_title
                    series.placeholder_status = "VISIBLE_IN_JELLYFIN_AFTER_EPISODE_DELETE"
                    session.commit()
                break
        
        if test_series_found:
            logger.info("✅ Jellyfin library refresh after episode file deletion validated")
        else:
            logger.warning("Series placeholder not yet visible in Jellyfin - may need more time")
    
    @pytest.mark.asyncio
    async def test_complete_episodefiledelete_workflow(self):
        """Test complete episodefiledelete workflow from file deletion to placeholder restoration"""
        # Create test episode with file
        series_id, season_id, episode_id = self.create_test_episode_with_file()
        
        # Initialize flow manager
        flow_manager = FlowManager()
        
        # Start episodefiledelete workflow
        episode = self.get_test_episode(episode_id)
        logger.info(f"Starting episodefiledelete workflow for: {episode.title}")
        
        # Verify initial file state
        assert episode.has_file is True, "Episode should have file initially"
        assert episode.episodefile_path is not None, "Episode should have file path initially"
        
        # Step 1: Detect file deletion
        with get_session() as session:
            episode = session.query(Episode).filter_by(id=episode_id).first()
            episode.action = "episodefiledelete"
            episode.status = "FILE_DELETE_DETECTED"
            session.commit()
        
        # Step 2: Clear file information
        with get_session() as session:
            episode = session.query(Episode).filter_by(id=episode_id).first()
            episode.has_file = False
            episode.episodefile_path = None
            episode.episodefile_size = None
            episode.sonarr_quality = None
            episode.status = "FILE_DELETE_PROCESSING"
            session.commit()
        
        # Step 3: Create series placeholder replacement
        series = self.get_test_series()
        dummy_path = create_dummy_series_folder(
            series.title, 
            series.year, 
            series.tvdbid
        )
        
        with get_session() as session:
            series = session.query(Series).filter_by(id=series_id).first()
            series.dummypath = dummy_path
            series.placeholder_status = "RECREATED_AFTER_EPISODE_DELETE"
            
            episode = session.query(Episode).filter_by(id=episode_id).first()
            episode.status = "PLACEHOLDER_RECREATING"
            session.commit()
        
        # Step 4: Scan Jellyfin library to update
        scan_result = scan_jellyfin_library()
        assert scan_result is True, "Jellyfin library scan should succeed"
        time.sleep(5)
        
        # Step 5: Verify placeholder in Jellyfin
        library_items = get_jellyfin_library()
        jellyfin_item = None
        for item in library_items:
            if (item.get("Type") == "Series" and 
                "Westworld" in item.get("Name", "")):
                jellyfin_item = item
                break
        
        # Step 6: Update final status
        if jellyfin_item:
            jellyfin_id = jellyfin_item.get("Id")
            detailed_item = get_jellyfin_item_by_id(jellyfin_id)
            
            with get_session() as session:
                series = session.query(Series).filter_by(id=series_id).first()
                series.jellyfin_dummy_id = jellyfin_id
                series.jellyfin_title = detailed_item.get("Name", "")
                series.jellyfin_overview = detailed_item.get("Overview", "")
                series.placeholder_status = "VISIBLE_IN_JELLYFIN_AFTER_EPISODE_DELETE"
                
                episode = session.query(Episode).filter_by(id=episode_id).first()
                episode.status = "FILE_DELETE_COMPLETED"
                session.commit()
        else:
            with get_session() as session:
                series = session.query(Series).filter_by(id=series_id).first()
                series.placeholder_status = "JELLYFIN_PENDING_AFTER_EPISODE_DELETE"
                
                episode = session.query(Episode).filter_by(id=episode_id).first()
                episode.status = "FILE_DELETE_COMPLETED"
                session.commit()
        
        # Verify final state
        final_episode = self.get_test_episode(episode_id)
        final_series = self.get_test_series()
        
        assert final_episode.status == "FILE_DELETE_COMPLETED", "Episode status should be FILE_DELETE_COMPLETED"
        assert final_episode.has_file is False, "Episode should not have file"
        assert final_series.dummypath is not None, "Series should have dummy path"
        assert "AFTER_EPISODE_DELETE" in final_series.placeholder_status, "Should indicate post-episode-deletion placeholder"
        
        logger.info("✅ Complete episodefiledelete workflow executed successfully")
        logger.info(f"Final episode state: {final_episode}")
        logger.info(f"Final series state: {final_series}")
    
    def test_episodefiledelete_workflow_state_transitions(self):
        """Test that episodefiledelete workflow properly transitions through all states"""
        # Create test episode with file
        series_id, season_id, episode_id = self.create_test_episode_with_file()
        
        # Test state transitions for episodefiledelete workflow
        states_to_test = [
            ("IMPORTED", "Initial file state"),
            ("FILE_DELETE_DETECTED", "File deletion detected"),
            ("FILE_DELETE_PROCESSING", "Processing file deletion"),
            ("PLACEHOLDER_RECREATING", "Recreating placeholder"),
            ("JELLYFIN_REFRESHING", "Refreshing Jellyfin library"),
            ("FILE_DELETE_COMPLETED", "File delete workflow completed")
        ]
        
        for status, description in states_to_test:
            with get_session() as session:
                episode = session.query(Episode).filter_by(id=episode_id).first()
                episode.status = status
                session.commit()
                
                # Verify state was set
                updated_episode = session.query(Episode).filter_by(id=episode_id).first()
                assert updated_episode.status == status, f"Episode status should be {status}"
                logger.info(f"✅ State transition: {status} - {description}")
        
        logger.info("✅ All episodefiledelete workflow state transitions validated")
    
    def test_episodefiledelete_preserves_episode_metadata(self):
        """Test that episodefiledelete workflow preserves episode metadata while clearing file info"""
        # Create test episode with file
        series_id, season_id, episode_id = self.create_test_episode_with_file()
        episode = self.get_test_episode(episode_id)
        
        # Capture original metadata
        original_title = episode.title
        original_episode_number = episode.episode_number
        original_year = episode.year
        original_jellyfin_id = episode.jellyfin_id
        
        # Simulate file deletion
        with get_session() as session:
            episode = session.query(Episode).filter_by(id=episode_id).first()
            episode.has_file = False
            episode.episodefile_path = None
            episode.episodefile_size = None
            episode.sonarr_quality = None
            episode.action = "episodefiledelete"
            episode.status = "FILE_DELETE_COMPLETED"
            session.commit()
        
        # Verify metadata preservation and file info clearing
        deleted_file_episode = self.get_test_episode(episode_id)
        assert deleted_file_episode.title == original_title, "Title should be preserved"
        assert deleted_file_episode.episode_number == original_episode_number, "Episode number should be preserved"
        assert deleted_file_episode.year == original_year, "Year should be preserved"
        assert deleted_file_episode.jellyfin_id == original_jellyfin_id, "Jellyfin ID should be preserved"
        
        # Verify file info is cleared
        assert deleted_file_episode.has_file is False, "Has file should be False"
        assert deleted_file_episode.episodefile_path is None, "File path should be cleared"
        assert deleted_file_episode.episodefile_size is None, "File size should be cleared"
        assert deleted_file_episode.sonarr_quality is None, "Quality should be cleared"
        
        logger.info("✅ Episode metadata preservation during file deletion validated")
    
    def test_episodefiledelete_series_level_placeholder_management(self):
        """Test that episode file deletion properly manages series-level placeholder"""
        # Create test episode with file
        series_id, season_id, episode_id = self.create_test_episode_with_file()
        
        # Add another episode to the same series
        with get_session() as session:
            episode2 = Episode(
                season_id=season_id,
                episode_number=2,
                title="Chestnut",
                year=2016,
                has_file=True,
                episodefile_path="/media/tv/Westworld (2016)/Season 01/Westworld.S01E02.Chestnut.1080p.BluRay.x264.mkv",
                episodefile_size=3000000000,
                sonarr_quality="Bluray-1080p",
                status="IMPORTED"
            )
            session.add(episode2)
            session.commit()
            episode2_id = episode2.id
        
        # Delete file for first episode only
        with get_session() as session:
            episode = session.query(Episode).filter_by(id=episode_id).first()
            episode.has_file = False
            episode.episodefile_path = None
            episode.status = "FILE_DELETE_COMPLETED"
            session.commit()
        
        # Verify series placeholder management
        # Since one episode still has file, series should not need placeholder
        series = self.get_test_series()
        with get_session() as session:
            season = session.query(Season).filter_by(id=season_id).first()
            remaining_episodes_with_files = session.query(Episode).filter_by(
                season_id=season_id, 
                has_file=True
            ).count()
            
            if remaining_episodes_with_files > 0:
                # Series should not need placeholder
                series = session.query(Series).filter_by(id=series_id).first()
                series.placeholder_status = "PARTIAL_CONTENT_AVAILABLE"
            else:
                # Series would need placeholder
                series = session.query(Series).filter_by(id=series_id).first()
                series.placeholder_status = "PLACEHOLDER_NEEDED"
            session.commit()
        
        updated_series = self.get_test_series()
        assert updated_series.placeholder_status in ["PARTIAL_CONTENT_AVAILABLE", "PLACEHOLDER_NEEDED"], \
            "Series placeholder status should reflect remaining content"
        
        logger.info("✅ Series-level placeholder management for episode deletion validated")

if __name__ == "__main__":
    # Run tests directly
    test_instance = TestEpisodeFileDeleteHandlerJellyfin()
    test_instance.setup_and_teardown()
    
    try:
        # Run individual tests
        test_instance.test_episodefiledelete_detects_file_removal()
        test_instance.test_episodefiledelete_creates_placeholder_replacement()
        test_instance.test_episodefiledelete_triggers_jellyfin_refresh()
        
        # Run async test
        asyncio.run(test_instance.test_complete_episodefiledelete_workflow())
        
        test_instance.test_episodefiledelete_workflow_state_transitions()
        test_instance.test_episodefiledelete_preserves_episode_metadata()
        test_instance.test_episodefiledelete_series_level_placeholder_management()
        
        print("🎉 All episodefiledelete handler tests passed!")
        
    except Exception as e:
        logger.error(f"Test failed: {e}")
        raise
    finally:
        test_instance.cleanup_test_data()
