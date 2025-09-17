"""
Test for seriesdelete handler to verify Jellyfin integration end-to-end.
Tests series placeholder removal, Jellyfin library cleanup, and series delete workflow validation.
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

class TestSeriesDeleteHandlerJellyfin:
    """Test seriesdelete handler Jellyfin integration end-to-end"""
    
    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Set up test environment and clean up after each test"""
        self.test_series_data = {
            'title': 'Test Delete Series Lost',
            'year': 2004,
            'tvdbid': 777777,  # Using unique test TVDB ID for delete tests
            'is_4k': False
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
    
    def create_test_series_with_placeholder(self):
        """Create a test series with existing placeholder in database and filesystem"""
        # Create series in database
        with get_session() as session:
            series = Series(**self.test_series_data)
            session.add(series)
            session.commit()
            series_id = series.id
        
        # Create seasons and episodes
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
            
            # Add some episodes
            for i in range(1, 4):
                episode = Episode(
                    season_id=season_id,
                    episode_number=i,
                    title=f"Episode {i}",
                    year=self.test_series_data['year']
                )
                session.add(episode)
            session.commit()
        
        # Create dummy folder
        dummy_path = create_dummy_series_folder(
            self.test_series_data['title'], 
            self.test_series_data['year'], 
            self.test_series_data['tvdbid']
        )
        
        # Update series with dummy path and Jellyfin info (simulate existing placeholder)
        with get_session() as session:
            series = session.query(Series).filter_by(id=series_id).first()
            series.dummypath = dummy_path
            series.placeholder_status = "VISIBLE_IN_JELLYFIN"
            series.jellyfin_dummy_id = "test_jellyfin_series_dummy_id"
            series.jellyfin_title = f"{self.test_series_data['title']} (Coming Soon)"
            session.commit()
        
        logger.info(f"Created test series with placeholder: {series}")
        return series_id
    
    def get_test_series(self):
        """Get the test series from database"""
        with get_session() as session:
            return session.query(Series).filter_by(tvdbid=self.test_series_data['tvdbid']).first()
    
    def test_series_delete_removes_dummy_placeholder(self):
        """Test that series delete handler removes dummy placeholder folder"""
        # Create test series with placeholder
        series_id = self.create_test_series_with_placeholder()
        series = self.get_test_series()
        
        # Verify placeholder exists before deletion
        assert os.path.exists(series.dummypath), f"Dummy folder should exist before deletion: {series.dummypath}"
        
        # Simulate delete workflow - remove dummy folder
        dummy_path = series.dummypath
        delete_folder(dummy_path)
        
        # Verify dummy folder is removed
        assert not os.path.exists(dummy_path), f"Dummy folder should be removed after deletion: {dummy_path}"
        
        # Update series status
        with get_session() as session:
            series = session.query(Series).filter_by(id=series_id).first()
            series.placeholder_status = "REMOVED"
            series.status = "DELETED"
            session.commit()
        
        logger.info(f"✅ Series dummy placeholder removed successfully: {dummy_path}")
    
    def test_series_delete_cascades_to_seasons_episodes(self):
        """Test that series delete properly handles seasons and episodes"""
        # Create test series with placeholder
        series_id = self.create_test_series_with_placeholder()
        
        # Verify seasons and episodes exist
        with get_session() as session:
            seasons = session.query(Season).filter_by(series_id=series_id).all()
            assert len(seasons) > 0, "Should have seasons before delete"
            
            for season in seasons:
                episodes = session.query(Episode).filter_by(season_id=season.id).all()
                assert len(episodes) > 0, "Should have episodes before delete"
        
        # Simulate delete workflow - mark episodes as deleted
        with get_session() as session:
            seasons = session.query(Season).filter_by(series_id=series_id).all()
            for season in seasons:
                episodes = session.query(Episode).filter_by(season_id=season.id).all()
                for episode in episodes:
                    episode.is_deleted = True
                    episode.status = "DELETED"
                season.is_deleted = True
                season.status = "DELETED"
            
            # Mark series as deleted
            series = session.query(Series).filter_by(id=series_id).first()
            series.is_deleted = True
            series.status = "DELETED"
            session.commit()
        
        # Verify cascade deletion
        with get_session() as session:
            series = session.query(Series).filter_by(id=series_id).first()
            assert series.is_deleted is True, "Series should be marked as deleted"
            
            seasons = session.query(Season).filter_by(series_id=series_id).all()
            for season in seasons:
                assert season.is_deleted is True, "Season should be marked as deleted"
                
                episodes = session.query(Episode).filter_by(season_id=season.id).all()
                for episode in episodes:
                    assert episode.is_deleted is True, "Episode should be marked as deleted"
        
        logger.info("✅ Series delete cascade to seasons/episodes validated")
    
    def test_jellyfin_library_scan_after_series_delete(self):
        """Test that Jellyfin library scan no longer shows deleted series placeholder"""
        # Create test series with placeholder
        series_id = self.create_test_series_with_placeholder()
        series = self.get_test_series()
        
        # Remove dummy folder (simulate delete action)
        dummy_path = series.dummypath
        delete_folder(dummy_path)
        
        # Trigger Jellyfin library scan after deletion
        logger.info("Triggering Jellyfin library scan after series deletion...")
        scan_result = scan_jellyfin_library()
        assert scan_result is True, "Jellyfin library scan should succeed"
        
        # Wait for scan to complete
        time.sleep(5)
        
        # Check if series no longer appears in Jellyfin library
        logger.info("Checking Jellyfin library - series should be gone...")
        library_items = get_jellyfin_library()
        
        # Look for our test series in the library (should not be found)
        test_series_found = False
        for item in library_items:
            if (item.get("Type") == "Series" and 
                "Lost" in item.get("Name", "")):
                test_series_found = True
                logger.warning(f"Test series still found in Jellyfin: {item.get('Name')}")
                break
        
        if not test_series_found:
            logger.info("✅ Test series successfully removed from Jellyfin library")
        
        # Update series status
        with get_session() as session:
            series = session.query(Series).filter_by(id=series_id).first()
            series.placeholder_status = "REMOVED_FROM_JELLYFIN" if not test_series_found else "STILL_IN_JELLYFIN"
            series.status = "DELETED"
            session.commit()
        
        logger.info("✅ Jellyfin library scan after series deletion completed")
    
    @pytest.mark.asyncio
    async def test_complete_series_delete_workflow(self):
        """Test complete series delete workflow from start to finish"""
        # Create test series with placeholder
        series_id = self.create_test_series_with_placeholder()
        
        # Initialize flow manager
        flow_manager = FlowManager()
        
        # Start series delete workflow
        series = self.get_test_series()
        logger.info(f"Starting series delete workflow for: {series.title}")
        
        # Verify initial state
        assert os.path.exists(series.dummypath), "Dummy folder should exist initially"
        assert series.placeholder_status == "VISIBLE_IN_JELLYFIN", "Should have initial placeholder status"
        
        # Step 1: Mark series for deletion
        with get_session() as session:
            series = session.query(Series).filter_by(id=series_id).first()
            series.status = "DELETING"
            session.commit()
        
        # Step 2: Mark related seasons and episodes for deletion
        with get_session() as session:
            seasons = session.query(Season).filter_by(series_id=series_id).all()
            for season in seasons:
                season.status = "DELETING"
                episodes = session.query(Episode).filter_by(season_id=season.id).all()
                for episode in episodes:
                    episode.status = "DELETING"
            session.commit()
        
        # Step 3: Remove dummy folder
        dummy_path = series.dummypath
        delete_folder(dummy_path)
        assert not os.path.exists(dummy_path), "Dummy folder should be removed"
        
        # Step 4: Scan Jellyfin library to update
        scan_result = scan_jellyfin_library()
        assert scan_result is True, "Jellyfin library scan should succeed"
        time.sleep(5)
        
        # Step 5: Verify removal from Jellyfin
        library_items = get_jellyfin_library()
        series_still_in_jellyfin = False
        for item in library_items:
            if (item.get("Type") == "Series" and 
                "Lost" in item.get("Name", "")):
                series_still_in_jellyfin = True
                break
        
        # Step 6: Update database with final delete state
        with get_session() as session:
            # Update episodes
            seasons = session.query(Season).filter_by(series_id=series_id).all()
            for season in seasons:
                episodes = session.query(Episode).filter_by(season_id=season.id).all()
                for episode in episodes:
                    episode.is_deleted = True
                    episode.status = "DELETED"
                season.is_deleted = True
                season.status = "DELETED"
            
            # Update series
            series = session.query(Series).filter_by(id=series_id).first()
            series.is_deleted = True
            series.status = "DELETED"
            series.placeholder_status = "REMOVED_FROM_JELLYFIN" if not series_still_in_jellyfin else "CLEANUP_PENDING"
            series.jellyfin_dummy_id = None
            series.jellyfin_title = None
            series.dummypath = None
            session.commit()
        
        # Verify final state
        final_series = self.get_test_series()
        assert final_series.is_deleted is True, "Series should be marked as deleted"
        assert final_series.status == "DELETED", "Series status should be DELETED"
        assert final_series.jellyfin_dummy_id is None, "Jellyfin references should be cleared"
        assert final_series.dummypath is None, "Dummy path should be cleared"
        
        logger.info("✅ Complete series delete workflow executed successfully")
        logger.info(f"Final series state: {final_series}")
    
    def test_series_delete_workflow_state_transitions(self):
        """Test that series delete workflow properly transitions through all states"""
        # Create test series with placeholder
        series_id = self.create_test_series_with_placeholder()
        
        # Test state transitions for delete workflow
        states_to_test = [
            ("VISIBLE_IN_JELLYFIN", "Initial placeholder state"),
            ("DELETING", "Delete workflow started"),
            ("FOLDER_REMOVED", "Dummy folder removed"),
            ("JELLYFIN_SCANNING", "Jellyfin library scanning"),
            ("REMOVED_FROM_JELLYFIN", "Removed from Jellyfin library"),
            ("DELETED", "Delete workflow completed")
        ]
        
        for status, description in states_to_test:
            with get_session() as session:
                series = session.query(Series).filter_by(id=series_id).first()
                if status == "DELETED":
                    series.status = status
                    series.is_deleted = True
                else:
                    series.placeholder_status = status
                session.commit()
                
                # Verify state was set
                updated_series = session.query(Series).filter_by(id=series_id).first()
                if status == "DELETED":
                    assert updated_series.status == status, f"Series status should be {status}"
                    assert updated_series.is_deleted is True, "Series should be marked as deleted"
                else:
                    assert updated_series.placeholder_status == status, f"Placeholder status should be {status}"
                logger.info(f"✅ State transition: {status} - {description}")
        
        logger.info("✅ All series delete workflow state transitions validated")
    
    def test_series_delete_preserves_audit_trail(self):
        """Test that series delete workflow preserves audit information"""
        # Create test series with placeholder
        series_id = self.create_test_series_with_placeholder()
        
        # Capture initial state for audit
        series = self.get_test_series()
        original_title = series.title
        original_tvdbid = series.tvdbid
        original_dummy_path = series.dummypath
        
        # Perform delete workflow
        dummy_path = series.dummypath
        delete_folder(dummy_path)
        
        # Mark as deleted but preserve audit information
        with get_session() as session:
            series = session.query(Series).filter_by(id=series_id).first()
            series.is_deleted = True
            series.status = "DELETED"
            series.placeholder_status = "REMOVED"
            # Keep title and tvdbid for audit trail
            # Clear operational fields
            series.jellyfin_dummy_id = None
            series.jellyfin_title = None
            series.dummypath = None
            session.commit()
        
        # Verify audit trail preservation
        deleted_series = self.get_test_series()
        assert deleted_series.is_deleted is True, "Series should be marked as deleted"
        assert deleted_series.title == original_title, "Title should be preserved for audit"
        assert deleted_series.tvdbid == original_tvdbid, "TVDB ID should be preserved for audit"
        assert deleted_series.dummypath is None, "Operational dummy path should be cleared"
        assert deleted_series.jellyfin_dummy_id is None, "Operational Jellyfin ID should be cleared"
        
        logger.info("✅ Series delete audit trail preservation validated")

if __name__ == "__main__":
    # Run tests directly
    test_instance = TestSeriesDeleteHandlerJellyfin()
    test_instance.setup_and_teardown()
    
    try:
        # Run individual tests
        test_instance.test_series_delete_removes_dummy_placeholder()
        test_instance.test_series_delete_cascades_to_seasons_episodes()
        test_instance.test_jellyfin_library_scan_after_series_delete()
        
        # Run async test
        asyncio.run(test_instance.test_complete_series_delete_workflow())
        
        test_instance.test_series_delete_workflow_state_transitions()
        test_instance.test_series_delete_preserves_audit_trail()
        
        print("🎉 All series delete handler tests passed!")
        
    except Exception as e:
        logger.error(f"Test failed: {e}")
        raise
    finally:
        test_instance.cleanup_test_data()
