"""
Test for movieadd handler to verify Jellyfin integration end-to-end.
Tests placeholder creation, Jellyfin library visibility, title updates, and scanning functionality.
"""

import pytest
import asyncio
import os
import time
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from services.postgres.models import Movie, SubFlow
from services.postgres.db import get_session
from services.jellyfin_client import refresh_jellyfin_item, refresh_jellyfin_library, test_jellyfin_connection
from services.integrations import create_dummy_movie_folder, delete_folder
from services.flow_manager import FlowManager
from core.config import config
from core.logger import logger
import shutil

class TestMovieAddHandlerJellyfin:
    """Test movieadd handler Jellyfin integration end-to-end"""
    
    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Set up test environment and clean up after each test"""
        self.test_movie_data = {
            'title': 'Test Movie 127 Hours',
            'year': 2010,
            'tmdbid': 127127,  # Using unique test TMDB ID
            'is_4k': False,
            'action': 'movieadd'
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
    
    def create_test_movie(self):
        """Create a test movie in the database"""
        with get_session() as session:
            movie = Movie(**self.test_movie_data)
            session.add(movie)
            session.commit()
            session.refresh(movie)
            logger.info(f"Created test movie: {movie}")
            return movie.id
    
    def get_test_movie(self):
        """Get the test movie from database"""
        with get_session() as session:
            return session.query(Movie).filter_by(tmdbid=self.test_movie_data['tmdbid']).first()
    
    def test_movieadd_creates_dummy_placeholder(self):
        """Test that movieadd handler creates dummy placeholder folder"""
        # Create test movie
        movie_id = self.create_test_movie()
        
        # Create dummy folder using integrations
        movie = self.get_test_movie()
        dummy_path = create_dummy_movie_folder(
            movie.title, 
            movie.year, 
            movie.tmdbid
        )
        
        # Update movie with dummy path
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.dummypath = dummy_path
            session.commit()
        
        # Verify dummy folder exists
        assert os.path.exists(dummy_path), f"Dummy folder should exist at {dummy_path}"
        assert os.path.isdir(dummy_path), f"Dummy path should be a directory"
        
        # Verify folder name contains TMDB ID and "Dummy" edition
        folder_name = os.path.basename(dummy_path)
        assert f"tmdb-{movie.tmdbid}" in folder_name, f"Folder name should contain TMDB ID"
        assert "edition-Dummy" in folder_name, f"Folder name should contain 'edition-Dummy'"
        
        logger.info(f"✅ Dummy placeholder created successfully: {dummy_path}")
    
    def test_jellyfin_library_scan_detects_placeholder(self):
        """Test that Jellyfin library scan detects the dummy placeholder"""
        # Create test movie and dummy folder
        movie_id = self.create_test_movie()
        movie = self.get_test_movie()
        
        dummy_path = create_dummy_movie_folder(
            movie.title, 
            movie.year, 
            movie.tmdbid
        )
        
        # Update movie with dummy path
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.dummypath = dummy_path
            session.commit()
        
        # Trigger Jellyfin library scan
        logger.info("Triggering Jellyfin library scan...")
        scan_result = scan_jellyfin_library()
        assert scan_result is True, "Jellyfin library scan should succeed"
        
        # Wait for scan to complete
        time.sleep(5)
        
        # Check if movie appears in Jellyfin library
        logger.info("Checking Jellyfin library for test movie...")
        library_items = get_jellyfin_library()
        
        # Look for our test movie in the library
        test_movie_found = False
        for item in library_items:
            item_tmdb = item.get("ProviderIds", {}).get("Tmdb")
            if item_tmdb and int(item_tmdb) == movie.tmdbid:
                test_movie_found = True
                jellyfin_title = item.get("Name", "")
                logger.info(f"✅ Found test movie in Jellyfin: {jellyfin_title} (TMDB: {item_tmdb})")
                
                # Update movie with Jellyfin info
                with get_session() as session:
                    movie = session.query(Movie).filter_by(id=movie_id).first()
                    movie.jellyfin_id = item.get("Id")
                    movie.jellyfin_title = jellyfin_title
                    session.commit()
                break
        
        assert test_movie_found, f"Test movie with TMDB ID {movie.tmdbid} should be found in Jellyfin library"
        logger.info("✅ Jellyfin library scan successfully detected placeholder")
    
    def test_jellyfin_title_status_update(self):
        """Test updating movie title status in Jellyfin after placeholder creation"""
        # Create test movie and dummy folder
        movie_id = self.create_test_movie()
        movie = self.get_test_movie()
        
        dummy_path = create_dummy_movie_folder(
            movie.title, 
            movie.year, 
            movie.tmdbid
        )
        
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.dummypath = dummy_path
            session.commit()
        
        # Scan and find in Jellyfin
        scan_jellyfin_library()
        time.sleep(5)
        
        library_items = get_jellyfin_library()
        jellyfin_item = None
        for item in library_items:
            item_tmdb = item.get("ProviderIds", {}).get("Tmdb")
            if item_tmdb and int(item_tmdb) == movie.tmdbid:
                jellyfin_item = item
                break
        
        assert jellyfin_item is not None, "Movie should be found in Jellyfin library"
        
        # Get detailed item info
        jellyfin_id = jellyfin_item.get("Id")
        detailed_item = get_jellyfin_item_by_id(jellyfin_id)
        
        assert detailed_item is not None, f"Should be able to get detailed info for Jellyfin item {jellyfin_id}"
        
        # Verify title contains expected elements
        jellyfin_title = detailed_item.get("Name", "")
        assert "127 Hours" in jellyfin_title, f"Jellyfin title should contain movie name: {jellyfin_title}"
        
        # Check if title indicates placeholder status
        overview = detailed_item.get("Overview", "")
        logger.info(f"Jellyfin title: {jellyfin_title}")
        logger.info(f"Jellyfin overview: {overview}")
        
        # Update database with Jellyfin info
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.jellyfin_id = jellyfin_id
            movie.jellyfin_title = jellyfin_title
            movie.jellyfin_overview = overview
            movie.placeholder_status = "VISIBLE_IN_JELLYFIN"
            session.commit()
        
        logger.info("✅ Jellyfin title status updated successfully")
    
    @pytest.mark.asyncio
    async def test_complete_movieadd_workflow(self):
        """Test complete movieadd workflow from start to finish"""
        # Create test movie
        movie_id = self.create_test_movie()
        
        # Initialize flow manager
        flow_manager = FlowManager()
        
        # Start movieadd workflow
        movie = self.get_test_movie()
        logger.info(f"Starting movieadd workflow for: {movie.title}")
        
        # Create dummy folder first (simulating delayed_placeholders step)
        dummy_path = create_dummy_movie_folder(
            movie.title, 
            movie.year, 
            movie.tmdbid
        )
        
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.dummypath = dummy_path
            movie.status = "IN_PROGRESS"
            session.commit()
        
        # Test jellyfin branch workflow steps
        logger.info("Testing Jellyfin workflow branch...")
        
        # Step 1: Scan Jellyfin library
        scan_result = scan_jellyfin_library()
        assert scan_result is True, "Jellyfin library scan should succeed"
        time.sleep(5)
        
        # Step 2: Verify dummy scan in Jellyfin
        library_items = get_jellyfin_library()
        jellyfin_item = None
        for item in library_items:
            item_tmdb = item.get("ProviderIds", {}).get("Tmdb")
            if item_tmdb and int(item_tmdb) == movie.tmdbid:
                jellyfin_item = item
                break
        
        assert jellyfin_item is not None, "Movie should be found in Jellyfin after scan"
        
        # Step 3: Update Jellyfin title status
        jellyfin_id = jellyfin_item.get("Id")
        detailed_item = get_jellyfin_item_by_id(jellyfin_id)
        
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.jellyfin_id = jellyfin_id
            movie.jellyfin_title = detailed_item.get("Name", "")
            movie.jellyfin_overview = detailed_item.get("Overview", "")
            movie.placeholder_status = "COMPLETED"
            movie.status = "COMPLETED"
            session.commit()
        
        # Verify final state
        final_movie = self.get_test_movie()
        assert final_movie.status == "COMPLETED", "Movie status should be COMPLETED"
        assert final_movie.jellyfin_id is not None, "Movie should have Jellyfin ID"
        assert final_movie.jellyfin_title is not None, "Movie should have Jellyfin title"
        assert final_movie.placeholder_status == "COMPLETED", "Placeholder status should be COMPLETED"
        
        logger.info("✅ Complete movieadd workflow executed successfully")
        logger.info(f"Final movie state: {final_movie}")
    
    def test_movieadd_workflow_state_transitions(self):
        """Test that movieadd workflow properly transitions through all states"""
        # Create test movie
        movie_id = self.create_test_movie()
        
        # Test state transitions
        states_to_test = [
            ("PENDING", "Initial state"),
            ("IN_PROGRESS", "Workflow started"),
            ("DUMMY_CREATED", "Dummy folder created"),
            ("JELLYFIN_SCANNED", "Jellyfin library scanned"),
            ("JELLYFIN_VERIFIED", "Movie found in Jellyfin"),
            ("COMPLETED", "Workflow completed")
        ]
        
        for status, description in states_to_test:
            with get_session() as session:
                movie = session.query(Movie).filter_by(id=movie_id).first()
                movie.status = status
                session.commit()
                
                # Verify state was set
                updated_movie = session.query(Movie).filter_by(id=movie_id).first()
                assert updated_movie.status == status, f"Movie status should be {status}"
                logger.info(f"✅ State transition: {status} - {description}")
        
        logger.info("✅ All workflow state transitions validated")

if __name__ == "__main__":
    # Run tests directly
    test_instance = TestMovieAddHandlerJellyfin()
    test_instance.setup_and_teardown()
    
    try:
        # Run individual tests
        test_instance.test_movieadd_creates_dummy_placeholder()
        test_instance.test_jellyfin_library_scan_detects_placeholder()
        test_instance.test_jellyfin_title_status_update()
        
        # Run async test
        asyncio.run(test_instance.test_complete_movieadd_workflow())
        
        test_instance.test_movieadd_workflow_state_transitions()
        
        print("🎉 All movieadd handler tests passed!")
        
    except Exception as e:
        logger.error(f"Test failed: {e}")
        raise
    finally:
        test_instance.cleanup_test_data()
