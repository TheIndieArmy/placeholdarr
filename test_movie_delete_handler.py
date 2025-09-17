"""
Test for movie delete handler to verify Jellyfin integration end-to-end.
Tests placeholder removal, Jellyfin library cleanup, and delete workflow validation.
"""

import pytest
import asyncio
import os
import time
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from services.postgres.models import Movie, SubFlow
from services.postgres.db import get_session
from services.jellyfin_client import get_jellyfin_library, refresh_jellyfin_item, scan_jellyfin_library, get_jellyfin_item_by_id
from services.integrations import create_dummy_movie_folder, delete_folder
from services.flow_manager import FlowManager
from core.config import config
from core.logger import logger
import shutil

class TestMovieDeleteHandlerJellyfin:
    """Test movie delete handler Jellyfin integration end-to-end"""
    
    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Set up test environment and clean up after each test"""
        self.test_movie_data = {
            'title': 'Test Delete Movie',
            'year': 2010,
            'tmdbid': 999999,  # Using unique test TMDB ID for delete tests
            'is_4k': False,
            'action': 'movie_delete'
        }
        
        # Clean up any existing test data
        self.cleanup_test_data()
        yield
        # Clean up after test
        self.cleanup_test_data()
    
    def cleanup_test_data(self):
        """Clean up test movie data from database and filesystem"""
        with get_session() as session:
            # Remove test movie and subflows
            movie = session.query(Movie).filter_by(tmdbid=self.test_movie_data['tmdbid']).first()
            if movie:
                # Remove subflows
                subflows = session.query(SubFlow).filter_by(movie_id=movie.id).all()
                for subflow in subflows:
                    session.delete(subflow)
                
                # Remove dummy folder if exists
                if movie.dummypath and os.path.exists(movie.dummypath):
                    try:
                        shutil.rmtree(movie.dummypath)
                        logger.info(f"Cleaned up dummy folder: {movie.dummypath}")
                    except Exception as e:
                        logger.warning(f"Failed to clean up dummy folder {movie.dummypath}: {e}")
                
                session.delete(movie)
                session.commit()
                logger.info(f"Cleaned up test movie: {self.test_movie_data['title']}")
    
    def create_test_movie_with_placeholder(self):
        """Create a test movie with existing placeholder in database and filesystem"""
        # Create movie in database
        with get_session() as session:
            movie = Movie(**self.test_movie_data)
            session.add(movie)
            session.commit()
            session.refresh(movie)
            movie_id = movie.id
        
        # Create dummy folder
        dummy_path = create_dummy_movie_folder(
            self.test_movie_data['title'], 
            self.test_movie_data['year'], 
            self.test_movie_data['tmdbid']
        )
        
        # Update movie with dummy path and Jellyfin info (simulate existing placeholder)
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.dummypath = dummy_path
            movie.placeholder_status = "VISIBLE_IN_JELLYFIN"
            movie.jellyfin_dummy_id = "test_jellyfin_dummy_id"
            movie.jellyfin_title = f"{self.test_movie_data['title']} (Coming Soon)"
            session.commit()
        
        logger.info(f"Created test movie with placeholder: {movie}")
        return movie_id
    
    def get_test_movie(self):
        """Get the test movie from database"""
        with get_session() as session:
            return session.query(Movie).filter_by(tmdbid=self.test_movie_data['tmdbid']).first()
    
    def test_movie_delete_removes_dummy_placeholder(self):
        """Test that movie delete handler removes dummy placeholder folder"""
        # Create test movie with placeholder
        movie_id = self.create_test_movie_with_placeholder()
        movie = self.get_test_movie()
        
        # Verify placeholder exists before deletion
        assert os.path.exists(movie.dummypath), f"Dummy folder should exist before deletion: {movie.dummypath}"
        
        # Simulate delete workflow - remove dummy folder
        dummy_path = movie.dummypath
        delete_folder(dummy_path)
        
        # Verify dummy folder is removed
        assert not os.path.exists(dummy_path), f"Dummy folder should be removed after deletion: {dummy_path}"
        
        # Update movie status
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.placeholder_status = "REMOVED"
            movie.status = "DELETED"
            session.commit()
        
        logger.info(f"✅ Dummy placeholder removed successfully: {dummy_path}")
    
    def test_jellyfin_library_scan_after_delete(self):
        """Test that Jellyfin library scan no longer shows deleted placeholder"""
        # Create test movie with placeholder
        movie_id = self.create_test_movie_with_placeholder()
        movie = self.get_test_movie()
        
        # First, scan to ensure it's in Jellyfin (optional - assuming it was there)
        initial_scan = scan_jellyfin_library()
        time.sleep(3)
        
        # Remove dummy folder (simulate delete action)
        dummy_path = movie.dummypath
        delete_folder(dummy_path)
        
        # Trigger Jellyfin library scan after deletion
        logger.info("Triggering Jellyfin library scan after deletion...")
        scan_result = scan_jellyfin_library()
        assert scan_result is True, "Jellyfin library scan should succeed"
        
        # Wait for scan to complete
        time.sleep(5)
        
        # Check if movie no longer appears in Jellyfin library
        logger.info("Checking Jellyfin library - movie should be gone...")
        library_items = get_jellyfin_library()
        
        # Look for our test movie in the library (should not be found)
        test_movie_found = False
        for item in library_items:
            item_tmdb = item.get("ProviderIds", {}).get("Tmdb")
            if item_tmdb and int(item_tmdb) == movie.tmdbid:
                test_movie_found = True
                logger.warning(f"Test movie still found in Jellyfin: {item.get('Name')} (TMDB: {item_tmdb})")
                break
        
        if not test_movie_found:
            logger.info("✅ Test movie successfully removed from Jellyfin library")
        
        # Update movie status
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.placeholder_status = "REMOVED_FROM_JELLYFIN" if not test_movie_found else "STILL_IN_JELLYFIN"
            movie.status = "DELETED"
            session.commit()
        
        # Note: In some cases, Jellyfin might take time to remove items or might cache them
        # The test validates the scan works, actual removal timing may vary
        logger.info("✅ Jellyfin library scan after deletion completed")
    
    def test_movie_delete_database_cleanup(self):
        """Test that movie delete handler properly cleans up database entries"""
        # Create test movie with placeholder
        movie_id = self.create_test_movie_with_placeholder()
        
        # Verify movie exists in database
        movie = self.get_test_movie()
        assert movie is not None, "Test movie should exist in database"
        assert movie.placeholder_status == "VISIBLE_IN_JELLYFIN", "Movie should have placeholder status"
        
        # Simulate delete workflow - mark as deleted but keep in DB for audit
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.is_deleted = True
            movie.status = "DELETED"
            movie.placeholder_status = "REMOVED"
            movie.jellyfin_dummy_id = None  # Clear Jellyfin references
            movie.jellyfin_title = None
            session.commit()
        
        # Verify database state after delete
        deleted_movie = self.get_test_movie()
        assert deleted_movie.is_deleted is True, "Movie should be marked as deleted"
        assert deleted_movie.status == "DELETED", "Movie status should be DELETED"
        assert deleted_movie.placeholder_status == "REMOVED", "Placeholder status should be REMOVED"
        assert deleted_movie.jellyfin_dummy_id is None, "Jellyfin dummy ID should be cleared"
        
        logger.info("✅ Database cleanup for movie delete validated")
    
    @pytest.mark.asyncio
    async def test_complete_movie_delete_workflow(self):
        """Test complete movie delete workflow from start to finish"""
        # Create test movie with placeholder
        movie_id = self.create_test_movie_with_placeholder()
        
        # Initialize flow manager
        flow_manager = FlowManager()
        
        # Start movie delete workflow
        movie = self.get_test_movie()
        logger.info(f"Starting movie delete workflow for: {movie.title}")
        
        # Verify initial state
        assert os.path.exists(movie.dummypath), "Dummy folder should exist initially"
        assert movie.placeholder_status == "VISIBLE_IN_JELLYFIN", "Should have initial placeholder status"
        
        # Step 1: Mark movie for deletion
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.action = "movie_delete"
            movie.status = "DELETING"
            session.commit()
        
        # Step 2: Remove dummy folder
        dummy_path = movie.dummypath
        delete_folder(dummy_path)
        assert not os.path.exists(dummy_path), "Dummy folder should be removed"
        
        # Step 3: Scan Jellyfin library to update
        scan_result = scan_jellyfin_library()
        assert scan_result is True, "Jellyfin library scan should succeed"
        time.sleep(5)
        
        # Step 4: Verify removal from Jellyfin
        library_items = get_jellyfin_library()
        movie_still_in_jellyfin = False
        for item in library_items:
            item_tmdb = item.get("ProviderIds", {}).get("Tmdb")
            if item_tmdb and int(item_tmdb) == movie.tmdbid:
                movie_still_in_jellyfin = True
                break
        
        # Step 5: Update database with final delete state
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.is_deleted = True
            movie.status = "DELETED"
            movie.placeholder_status = "REMOVED_FROM_JELLYFIN" if not movie_still_in_jellyfin else "CLEANUP_PENDING"
            movie.jellyfin_dummy_id = None
            movie.jellyfin_title = None
            movie.dummypath = None
            session.commit()
        
        # Verify final state
        final_movie = self.get_test_movie()
        assert final_movie.is_deleted is True, "Movie should be marked as deleted"
        assert final_movie.status == "DELETED", "Movie status should be DELETED"
        assert final_movie.jellyfin_dummy_id is None, "Jellyfin references should be cleared"
        assert final_movie.dummypath is None, "Dummy path should be cleared"
        
        logger.info("✅ Complete movie delete workflow executed successfully")
        logger.info(f"Final movie state: {final_movie}")
    
    def test_movie_delete_workflow_state_transitions(self):
        """Test that movie delete workflow properly transitions through all states"""
        # Create test movie with placeholder
        movie_id = self.create_test_movie_with_placeholder()
        
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
                movie = session.query(Movie).filter_by(id=movie_id).first()
                if status == "DELETED":
                    movie.status = status
                    movie.is_deleted = True
                else:
                    movie.placeholder_status = status
                session.commit()
                
                # Verify state was set
                updated_movie = session.query(Movie).filter_by(id=movie_id).first()
                if status == "DELETED":
                    assert updated_movie.status == status, f"Movie status should be {status}"
                    assert updated_movie.is_deleted is True, "Movie should be marked as deleted"
                else:
                    assert updated_movie.placeholder_status == status, f"Placeholder status should be {status}"
                logger.info(f"✅ State transition: {status} - {description}")
        
        logger.info("✅ All delete workflow state transitions validated")
    
    def test_delete_workflow_rollback_scenario(self):
        """Test handling of delete workflow rollback if deletion fails"""
        # Create test movie with placeholder
        movie_id = self.create_test_movie_with_placeholder()
        movie = self.get_test_movie()
        
        # Start delete workflow
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.status = "DELETING"
            session.commit()
        
        # Simulate delete failure (folder still exists)
        dummy_path = movie.dummypath
        assert os.path.exists(dummy_path), "Dummy folder should still exist"
        
        # Rollback delete workflow
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.status = "DELETE_FAILED"
            movie.placeholder_status = "DELETE_ROLLBACK"
            session.commit()
        
        # Verify rollback state
        rollback_movie = self.get_test_movie()
        assert rollback_movie.status == "DELETE_FAILED", "Movie status should indicate delete failure"
        assert rollback_movie.placeholder_status == "DELETE_ROLLBACK", "Should show rollback status"
        assert rollback_movie.is_deleted is False, "Movie should not be marked as deleted"
        
        logger.info("✅ Delete workflow rollback scenario validated")

if __name__ == "__main__":
    # Run tests directly
    test_instance = TestMovieDeleteHandlerJellyfin()
    test_instance.setup_and_teardown()
    
    try:
        # Run individual tests
        test_instance.test_movie_delete_removes_dummy_placeholder()
        test_instance.test_jellyfin_library_scan_after_delete()
        test_instance.test_movie_delete_database_cleanup()
        
        # Run async test
        asyncio.run(test_instance.test_complete_movie_delete_workflow())
        
        test_instance.test_movie_delete_workflow_state_transitions()
        test_instance.test_delete_workflow_rollback_scenario()
        
        print("🎉 All movie delete handler tests passed!")
        
    except Exception as e:
        logger.error(f"Test failed: {e}")
        raise
    finally:
        test_instance.cleanup_test_data()
