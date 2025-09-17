"""
Simplified test for handler logic validation without requiring live Jellyfin connection.
This test focuses on database operations, workflow states, and integration logic.
"""

import pytest
import os
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from services.postgres.models import Movie, SubFlow
from services.postgres.db import get_session
from services.integrations import create_dummy_movie_folder
from core.logger import logger
import shutil

class TestHandlerLogicValidation:
    """Test handler logic without requiring live Jellyfin connection"""
    
    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Set up test environment and clean up after each test"""
        self.test_movie_data = {
            'title': 'Test Logic Movie Validation',
            'year': 2023,
            'tmdbid': 999001,  # Using unique test TMDB ID
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
        try:
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
        except Exception as e:
            logger.warning(f"Cleanup error: {e}")
    
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
    
    def test_database_connection(self):
        """Test that database connection is working"""
        try:
            with get_session() as session:
                # Simple query to test connection
                count = session.query(Movie).count()
                logger.info(f"✅ Database connection test passed - found {count} movies")
                assert True
        except Exception as e:
            logger.error(f"Database connection test failed: {e}")
            assert False, f"Database connection failed: {e}"
    
    def test_movie_creation_and_retrieval(self):
        """Test basic movie CRUD operations"""
        # Create movie
        movie_id = self.create_test_movie()
        assert movie_id is not None, "Movie creation should return an ID"
        
        # Retrieve movie
        movie = self.get_test_movie()
        assert movie is not None, "Movie should be retrievable after creation"
        assert movie.title == self.test_movie_data['title'], "Movie title should match"
        assert movie.tmdbid == self.test_movie_data['tmdbid'], "Movie TMDB ID should match"
        
        logger.info("✅ Movie creation and retrieval test passed")
    
    def test_dummy_folder_creation(self):
        """Test dummy folder creation logic"""
        # Create test movie
        movie_id = self.create_test_movie()
        movie = self.get_test_movie()
        
        # Create dummy folder
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
        
        # Verify folder name contains expected elements
        folder_name = os.path.basename(dummy_path)
        assert f"tmdb-{movie.tmdbid}" in folder_name, f"Folder name should contain TMDB ID"
        assert "edition-Dummy" in folder_name, f"Folder name should contain 'edition-Dummy'"
        
        logger.info(f"✅ Dummy folder creation test passed: {dummy_path}")
    
    def test_subflow_creation(self):
        """Test SubFlow creation and management"""
        # Create test movie
        movie_id = self.create_test_movie()
        
        # Create a test SubFlow
        with get_session() as session:
            subflow = SubFlow(
                movie_id=movie_id,
                action='movieadd',
                branch='main',
                steps='delayed_placeholders,refresh_jellyfin_dummy',
                step_index=0,
                status='PENDING'
            )
            session.add(subflow)
            session.commit()
            subflow_id = subflow.id
        
        # Verify SubFlow creation
        with get_session() as session:
            created_subflow = session.query(SubFlow).get(subflow_id)
            assert created_subflow is not None, "SubFlow should be created"
            assert created_subflow.movie_id == movie_id, "SubFlow should reference correct movie"
            assert created_subflow.action == 'movieadd', "SubFlow should have correct action"
            assert created_subflow.status == 'PENDING', "SubFlow should have PENDING status"
        
        logger.info("✅ SubFlow creation test passed")
    
    def test_workflow_state_transitions(self):
        """Test workflow state transitions"""
        # Create test movie
        movie_id = self.create_test_movie()
        
        # Test state transitions
        states_to_test = [
            ("PENDING", "Initial state"),
            ("IN_PROGRESS", "Workflow started"),
            ("DUMMY_CREATED", "Dummy folder created"),
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
    
    def test_movie_metadata_preservation(self):
        """Test that movie metadata is preserved during operations"""
        # Create test movie
        movie_id = self.create_test_movie()
        movie = self.get_test_movie()
        
        # Capture original metadata
        original_title = movie.title
        original_year = movie.year
        original_tmdbid = movie.tmdbid
        
        # Simulate operations that should preserve metadata
        with get_session() as session:
            movie = session.query(Movie).filter_by(id=movie_id).first()
            movie.status = "IN_PROGRESS"
            movie.placeholder_status = "CREATED"
            movie.dummypath = "/test/path"
            session.commit()
        
        # Verify metadata preservation
        updated_movie = self.get_test_movie()
        assert updated_movie.title == original_title, "Title should be preserved"
        assert updated_movie.year == original_year, "Year should be preserved"
        assert updated_movie.tmdbid == original_tmdbid, "TMDB ID should be preserved"
        
        logger.info("✅ Movie metadata preservation test passed")
    
    def test_import_configuration_loading(self):
        """Test that configuration and imports work correctly"""
        try:
            from services.postgres.models import Movie, Series, Episode, SubFlow
            from services.integrations import create_dummy_movie_folder
            from services.flow_manager import FlowManager
            from core.config import config
            
            logger.info("✅ All required modules imported successfully")
            assert True
        except ImportError as e:
            logger.error(f"Import test failed: {e}")
            assert False, f"Failed to import required modules: {e}"
    
    def test_database_models_functionality(self):
        """Test that database models work correctly"""
        # Test Movie model
        movie_id = self.create_test_movie()
        movie = self.get_test_movie()
        
        # Test model properties
        assert hasattr(movie, 'id'), "Movie should have id attribute"
        assert hasattr(movie, 'title'), "Movie should have title attribute"
        assert hasattr(movie, 'tmdbid'), "Movie should have tmdbid attribute"
        assert hasattr(movie, 'status'), "Movie should have status attribute"
        assert hasattr(movie, 'dummypath'), "Movie should have dummypath attribute"
        
        # Test model relationships
        with get_session() as session:
            movie_with_subflows = session.query(Movie).filter_by(id=movie_id).first()
            subflows = movie_with_subflows.subflows
            assert isinstance(subflows, list), "Movie subflows should be a list"
        
        logger.info("✅ Database models functionality test passed")

def run_basic_tests():
    """Run basic validation tests"""
    test_instance = TestHandlerLogicValidation()
    
    try:
        # Setup
        test_instance.setup_and_teardown()
        
        # Run tests
        logger.info("🧪 Starting basic handler logic validation tests...")
        
        test_instance.test_database_connection()
        test_instance.test_import_configuration_loading()
        test_instance.test_database_models_functionality()
        test_instance.test_movie_creation_and_retrieval()
        test_instance.test_dummy_folder_creation()
        test_instance.test_subflow_creation()
        test_instance.test_workflow_state_transitions()
        test_instance.test_movie_metadata_preservation()
        
        logger.info("🎉 All basic handler logic validation tests passed!")
        return True
        
    except Exception as e:
        logger.error(f"Test failed: {e}")
        return False
    finally:
        test_instance.cleanup_test_data()

if __name__ == "__main__":
    success = run_basic_tests()
    if success:
        print("✅ Handler logic validation completed successfully!")
    else:
        print("❌ Handler logic validation failed!")
        exit(1)
