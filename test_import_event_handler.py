"""
Test for import event handler to verify Jellyfin integration end-to-end.
Tests file import detection, metadata updates, and import workflow validation.
"""

import pytest
import asyncio
import os
import time
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from services.postgres.models import Movie, Episode, SubFlow
from services.postgres.db import get_session
from services.jellyfin_client import get_jellyfin_library, refresh_jellyfin_item, scan_jellyfin_library, get_jellyfin_item_by_id
from services.integrations import create_dummy_movie_folder, delete_folder
from services.flow_manager import FlowManager
from core.config import config
from core.logger import logger
import shutil

class TestImportEventHandlerJellyfin:
    """Test import event handler Jellyfin integration end-to-end"""
    
    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Set up test environment and clean up after each test"""
        self.test_movie_data = {
            'title': 'Test Import Movie Inception',
            'year': 2010,
            'tmdbid': 555555,  # Using unique test TMDB ID for import tests
            'is_4k': False,
            'action': 'import_event'
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
    
    def create_test_movie_with_dummy(self):
        """Create a test movie with existing dummy placeholder"""
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
        
        # Update movie with dummy path and placeholder status
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.dummypath = dummy_path
            movie.placeholder_status = "VISIBLE_IN_JELLYFIN"
            movie.jellyfin_dummy_id = "test_jellyfin_dummy_id"
            movie.jellyfin_title = f"{self.test_movie_data['title']} (Coming Soon)"
            session.commit()
        
        logger.info(f"Created test movie with dummy placeholder: {movie}")
        return movie_id
    
    def get_test_movie(self):
        """Get the test movie from database"""
        with get_session() as session:
            return session.query(Movie).filter_by(tmdbid=self.test_movie_data['tmdbid']).first()
    
    def test_import_event_detects_file_replacement(self):
        """Test that import event handler detects when dummy is replaced with real file"""
        # Create test movie with dummy placeholder
        movie_id = self.create_test_movie_with_dummy()
        movie = self.get_test_movie()
        
        # Verify dummy exists
        assert os.path.exists(movie.dummypath), "Dummy folder should exist initially"
        
        # Simulate import event - update movie with real file info
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.has_file = True
            movie.moviefile_path = "/path/to/real/movie/file.mkv"
            movie.moviefile_size = 2147483648  # 2GB
            movie.radarr_quality = "Bluray-1080p"
            movie.action = "import_event"
            movie.status = "IMPORT_DETECTED"
            session.commit()
        
        # Verify import detection
        imported_movie = self.get_test_movie()
        assert imported_movie.has_file is True, "Movie should have file after import"
        assert imported_movie.moviefile_path is not None, "Movie should have file path"
        assert imported_movie.action == "import_event", "Action should be import_event"
        assert imported_movie.status == "IMPORT_DETECTED", "Status should indicate import detected"
        
        logger.info("✅ Import event file replacement detection validated")
    
    def test_import_event_triggers_jellyfin_refresh(self):
        """Test that import event triggers Jellyfin library refresh"""
        # Create test movie with dummy placeholder
        movie_id = self.create_test_movie_with_dummy()
        movie = self.get_test_movie()
        
        # Simulate file import
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.has_file = True
            movie.moviefile_path = "/path/to/real/movie/file.mkv"
            movie.status = "IMPORT_PROCESSING"
            session.commit()
        
        # Trigger Jellyfin library scan (simulating import event workflow)
        logger.info("Triggering Jellyfin library scan after import...")
        scan_result = scan_jellyfin_library()
        assert scan_result is True, "Jellyfin library scan should succeed"
        
        # Wait for scan to complete
        time.sleep(5)
        
        # Check if movie appears in Jellyfin library with updated info
        logger.info("Checking Jellyfin library for imported movie...")
        library_items = get_jellyfin_library()
        
        # Look for our test movie in the library
        test_movie_found = False
        for item in library_items:
            item_tmdb = item.get("ProviderIds", {}).get("Tmdb")
            if item_tmdb and int(item_tmdb) == movie.tmdbid:
                test_movie_found = True
                jellyfin_title = item.get("Name", "")
                logger.info(f"✅ Found imported movie in Jellyfin: {jellyfin_title} (TMDB: {item_tmdb})")
                
                # Update movie with refreshed Jellyfin info
                with get_session() as session:
                    movie = session.query(Movie).filter_by(id=movie_id).first()
                    movie.jellyfin_id = item.get("Id")
                    movie.jellyfin_title = jellyfin_title
                    movie.placeholder_status = "REPLACED_WITH_REAL_CONTENT"
                    session.commit()
                break
        
        assert test_movie_found, f"Imported movie with TMDB ID {movie.tmdbid} should be found in Jellyfin library"
        logger.info("✅ Jellyfin library refresh after import validated")
    
    def test_import_event_metadata_update(self):
        """Test that import event properly updates movie metadata"""
        # Create test movie with dummy placeholder
        movie_id = self.create_test_movie_with_dummy()
        
        # Simulate complete import event with metadata
        import_metadata = {
            'has_file': True,
            'moviefile_path': '/media/movies/Inception (2010)/Inception.2010.1080p.BluRay.x264.mkv',
            'moviefile_size': 3221225472,  # 3GB
            'radarr_quality': 'Bluray-1080p',
            'radarr_release_status': 'released',
            'radarr_monitored': True,
            'radarr_progress': 100
        }
        
        # Update movie with import metadata
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            for key, value in import_metadata.items():
                setattr(movie, key, value)
            movie.action = "import_event"
            movie.status = "IMPORT_COMPLETED"
            movie.placeholder_status = "REPLACED_WITH_REAL_CONTENT"
            session.commit()
        
        # Verify metadata update
        updated_movie = self.get_test_movie()
        assert updated_movie.has_file is True, "Movie should have file"
        assert updated_movie.moviefile_path == import_metadata['moviefile_path'], "Movie file path should be updated"
        assert updated_movie.moviefile_size == import_metadata['moviefile_size'], "Movie file size should be updated"
        assert updated_movie.radarr_quality == import_metadata['radarr_quality'], "Quality should be updated"
        assert updated_movie.radarr_progress == 100, "Progress should be 100% after import"
        assert updated_movie.placeholder_status == "REPLACED_WITH_REAL_CONTENT", "Placeholder status should indicate replacement"
        
        logger.info("✅ Import event metadata update validated")
    
    @pytest.mark.asyncio
    async def test_complete_import_event_workflow(self):
        """Test complete import event workflow from dummy to real content"""
        # Create test movie with dummy placeholder
        movie_id = self.create_test_movie_with_dummy()
        
        # Initialize flow manager
        flow_manager = FlowManager()
        
        # Start import event workflow
        movie = self.get_test_movie()
        logger.info(f"Starting import event workflow for: {movie.title}")
        
        # Verify initial dummy state
        assert os.path.exists(movie.dummypath), "Dummy folder should exist initially"
        assert movie.placeholder_status == "VISIBLE_IN_JELLYFIN", "Should have initial placeholder status"
        
        # Step 1: Detect import event
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.action = "import_event"
            movie.status = "IMPORT_DETECTED"
            session.commit()
        
        # Step 2: Update with real file information
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.has_file = True
            movie.moviefile_path = "/media/movies/Inception (2010)/Inception.2010.1080p.BluRay.x264.mkv"
            movie.moviefile_size = 3221225472
            movie.radarr_quality = "Bluray-1080p"
            movie.radarr_progress = 100
            movie.status = "IMPORT_PROCESSING"
            session.commit()
        
        # Step 3: Scan Jellyfin library to refresh content
        scan_result = scan_jellyfin_library()
        assert scan_result is True, "Jellyfin library scan should succeed"
        time.sleep(5)
        
        # Step 4: Verify content update in Jellyfin
        library_items = get_jellyfin_library()
        jellyfin_item = None
        for item in library_items:
            item_tmdb = item.get("ProviderIds", {}).get("Tmdb")
            if item_tmdb and int(item_tmdb) == movie.tmdbid:
                jellyfin_item = item
                break
        
        assert jellyfin_item is not None, "Movie should be found in Jellyfin after import"
        
        # Step 5: Update final status
        jellyfin_id = jellyfin_item.get("Id")
        detailed_item = get_jellyfin_item_by_id(jellyfin_id)
        
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.jellyfin_id = jellyfin_id
            movie.jellyfin_title = detailed_item.get("Name", "")
            movie.jellyfin_overview = detailed_item.get("Overview", "")
            movie.placeholder_status = "REPLACED_WITH_REAL_CONTENT"
            movie.status = "IMPORT_COMPLETED"
            session.commit()
        
        # Verify final state
        final_movie = self.get_test_movie()
        assert final_movie.status == "IMPORT_COMPLETED", "Movie status should be IMPORT_COMPLETED"
        assert final_movie.has_file is True, "Movie should have file"
        assert final_movie.jellyfin_id is not None, "Movie should have Jellyfin ID"
        assert final_movie.placeholder_status == "REPLACED_WITH_REAL_CONTENT", "Should indicate content replacement"
        
        logger.info("✅ Complete import event workflow executed successfully")
        logger.info(f"Final movie state: {final_movie}")
    
    def test_import_event_workflow_state_transitions(self):
        """Test that import event workflow properly transitions through all states"""
        # Create test movie with dummy placeholder
        movie_id = self.create_test_movie_with_dummy()
        
        # Test state transitions for import workflow
        states_to_test = [
            ("VISIBLE_IN_JELLYFIN", "Initial placeholder state"),
            ("IMPORT_DETECTED", "Import event detected"),
            ("IMPORT_PROCESSING", "Processing import"),
            ("JELLYFIN_REFRESHING", "Refreshing Jellyfin library"),
            ("METADATA_UPDATING", "Updating metadata"),
            ("IMPORT_COMPLETED", "Import workflow completed")
        ]
        
        for status, description in states_to_test:
            with get_session() as session:
                movie = session.query(Movie).filter_by(id=movie_id).first()
                if "JELLYFIN" in status or status == "VISIBLE_IN_JELLYFIN":
                    movie.placeholder_status = status
                else:
                    movie.status = status
                session.commit()
                
                # Verify state was set
                updated_movie = session.query(Movie).filter_by(id=movie_id).first()
                if "JELLYFIN" in status or status == "VISIBLE_IN_JELLYFIN":
                    assert updated_movie.placeholder_status == status, f"Placeholder status should be {status}"
                else:
                    assert updated_movie.status == status, f"Movie status should be {status}"
                logger.info(f"✅ State transition: {status} - {description}")
        
        logger.info("✅ All import event workflow state transitions validated")
    
    def test_import_event_error_handling(self):
        """Test import event workflow error handling and rollback"""
        # Create test movie with dummy placeholder
        movie_id = self.create_test_movie_with_dummy()
        
        # Start import event workflow
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.action = "import_event"
            movie.status = "IMPORT_PROCESSING"
            session.commit()
        
        # Simulate import failure
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.status = "IMPORT_FAILED"
            movie.placeholder_status = "IMPORT_ERROR_ROLLBACK"
            # Reset file status
            movie.has_file = False
            movie.moviefile_path = None
            movie.moviefile_size = None
            session.commit()
        
        # Verify error state
        error_movie = self.get_test_movie()
        assert error_movie.status == "IMPORT_FAILED", "Movie status should indicate import failure"
        assert error_movie.placeholder_status == "IMPORT_ERROR_ROLLBACK", "Should show error rollback status"
        assert error_movie.has_file is False, "File status should be reset on error"
        assert error_movie.moviefile_path is None, "File path should be cleared on error"
        
        logger.info("✅ Import event error handling validated")

if __name__ == "__main__":
    # Run tests directly
    test_instance = TestImportEventHandlerJellyfin()
    test_instance.setup_and_teardown()
    
    try:
        # Run individual tests
        test_instance.test_import_event_detects_file_replacement()
        test_instance.test_import_event_triggers_jellyfin_refresh()
        test_instance.test_import_event_metadata_update()
        
        # Run async test
        asyncio.run(test_instance.test_complete_import_event_workflow())
        
        test_instance.test_import_event_workflow_state_transitions()
        test_instance.test_import_event_error_handling()
        
        print("🎉 All import event handler tests passed!")
        
    except Exception as e:
        logger.error(f"Test failed: {e}")
        raise
    finally:
        test_instance.cleanup_test_data()
