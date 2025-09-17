"""
Comprehensive test suite for all handlers to verify Jellyfin integration end-to-end.
This test runner executes all handler tests and provides a summary of results.
"""

import pytest
import asyncio
import sys
import time
from pathlib import Path
import importlib.util
from core.logger import logger

class TestHandlersRunner:
    """Comprehensive test runner for all workflow handlers"""
    
    def __init__(self):
        self.test_modules = [
            'test_movieadd_handler',
            'test_movie_delete_handler', 
            'test_seriesadd_handler',
            'test_seriesdelete_handler',
            'test_import_event_handler',
            'test_moviefiledelete_handler',
            'test_episodefiledelete_handler'
        ]
        self.results = {}
        
    def load_test_module(self, module_name):
        """Dynamically load a test module"""
        module_path = Path(f"/home/priky/indiefork/{module_name}.py")
        if not module_path.exists():
            logger.error(f"Test module not found: {module_path}")
            return None
            
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    
    def run_movieadd_tests(self):
        """Run movieadd handler tests"""
        logger.info("🎬 Running movieadd handler tests...")
        try:
            from test_movieadd_handler import TestMovieAddHandlerJellyfin
            test_instance = TestMovieAddHandlerJellyfin()
            test_instance.setup_and_teardown()
            
            tests_passed = 0
            total_tests = 0
            
            try:
                # Individual tests
                test_instance.test_movieadd_creates_dummy_placeholder()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ movieadd_creates_dummy_placeholder")
                
                test_instance.test_jellyfin_library_scan_detects_placeholder()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ jellyfin_library_scan_detects_placeholder")
                
                test_instance.test_jellyfin_title_status_update()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ jellyfin_title_status_update")
                
                # Async test
                asyncio.run(test_instance.test_complete_movieadd_workflow())
                tests_passed += 1
                total_tests += 1
                logger.info("✅ complete_movieadd_workflow")
                
                test_instance.test_movieadd_workflow_state_transitions()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ movieadd_workflow_state_transitions")
                
                self.results['movieadd'] = {'passed': tests_passed, 'total': total_tests, 'status': 'SUCCESS'}
                
            except Exception as e:
                total_tests += 1
                self.results['movieadd'] = {'passed': tests_passed, 'total': total_tests, 'status': 'FAILED', 'error': str(e)}
                logger.error(f"Movieadd test failed: {e}")
            finally:
                test_instance.cleanup_test_data()
                
        except Exception as e:
            self.results['movieadd'] = {'passed': 0, 'total': 0, 'status': 'MODULE_ERROR', 'error': str(e)}
            logger.error(f"Failed to load movieadd test module: {e}")
    
    def run_movie_delete_tests(self):
        """Run movie delete handler tests"""
        logger.info("🗑️ Running movie delete handler tests...")
        try:
            from test_movie_delete_handler import TestMovieDeleteHandlerJellyfin
            test_instance = TestMovieDeleteHandlerJellyfin()
            test_instance.setup_and_teardown()
            
            tests_passed = 0
            total_tests = 0
            
            try:
                # Individual tests
                test_instance.test_movie_delete_removes_dummy_placeholder()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ movie_delete_removes_dummy_placeholder")
                
                test_instance.test_jellyfin_library_scan_after_delete()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ jellyfin_library_scan_after_delete")
                
                test_instance.test_movie_delete_database_cleanup()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ movie_delete_database_cleanup")
                
                # Async test
                asyncio.run(test_instance.test_complete_movie_delete_workflow())
                tests_passed += 1
                total_tests += 1
                logger.info("✅ complete_movie_delete_workflow")
                
                test_instance.test_movie_delete_workflow_state_transitions()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ movie_delete_workflow_state_transitions")
                
                test_instance.test_delete_workflow_rollback_scenario()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ delete_workflow_rollback_scenario")
                
                self.results['movie_delete'] = {'passed': tests_passed, 'total': total_tests, 'status': 'SUCCESS'}
                
            except Exception as e:
                total_tests += 1
                self.results['movie_delete'] = {'passed': tests_passed, 'total': total_tests, 'status': 'FAILED', 'error': str(e)}
                logger.error(f"Movie delete test failed: {e}")
            finally:
                test_instance.cleanup_test_data()
                
        except Exception as e:
            self.results['movie_delete'] = {'passed': 0, 'total': 0, 'status': 'MODULE_ERROR', 'error': str(e)}
            logger.error(f"Failed to load movie delete test module: {e}")
    
    def run_seriesadd_tests(self):
        """Run seriesadd handler tests"""
        logger.info("📺 Running seriesadd handler tests...")
        try:
            from test_seriesadd_handler import TestSeriesAddHandlerJellyfin
            test_instance = TestSeriesAddHandlerJellyfin()
            test_instance.setup_and_teardown()
            
            tests_passed = 0
            total_tests = 0
            
            try:
                # Individual tests
                test_instance.test_seriesadd_creates_dummy_placeholder()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ seriesadd_creates_dummy_placeholder")
                
                test_instance.test_jellyfin_library_scan_detects_series_placeholder()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ jellyfin_library_scan_detects_series_placeholder")
                
                test_instance.test_series_season_episode_structure()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ series_season_episode_structure")
                
                # Async test
                asyncio.run(test_instance.test_complete_seriesadd_workflow())
                tests_passed += 1
                total_tests += 1
                logger.info("✅ complete_seriesadd_workflow")
                
                test_instance.test_seriesadd_workflow_state_transitions()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ seriesadd_workflow_state_transitions")
                
                test_instance.test_series_jellyfin_metadata_update()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ series_jellyfin_metadata_update")
                
                self.results['seriesadd'] = {'passed': tests_passed, 'total': total_tests, 'status': 'SUCCESS'}
                
            except Exception as e:
                total_tests += 1
                self.results['seriesadd'] = {'passed': tests_passed, 'total': total_tests, 'status': 'FAILED', 'error': str(e)}
                logger.error(f"Seriesadd test failed: {e}")
            finally:
                test_instance.cleanup_test_data()
                
        except Exception as e:
            self.results['seriesadd'] = {'passed': 0, 'total': 0, 'status': 'MODULE_ERROR', 'error': str(e)}
            logger.error(f"Failed to load seriesadd test module: {e}")
    
    def run_seriesdelete_tests(self):
        """Run seriesdelete handler tests"""
        logger.info("🗑️📺 Running seriesdelete handler tests...")
        try:
            from test_seriesdelete_handler import TestSeriesDeleteHandlerJellyfin
            test_instance = TestSeriesDeleteHandlerJellyfin()
            test_instance.setup_and_teardown()
            
            tests_passed = 0
            total_tests = 0
            
            try:
                # Individual tests
                test_instance.test_series_delete_removes_dummy_placeholder()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ series_delete_removes_dummy_placeholder")
                
                test_instance.test_series_delete_cascades_to_seasons_episodes()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ series_delete_cascades_to_seasons_episodes")
                
                test_instance.test_jellyfin_library_scan_after_series_delete()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ jellyfin_library_scan_after_series_delete")
                
                # Async test
                asyncio.run(test_instance.test_complete_series_delete_workflow())
                tests_passed += 1
                total_tests += 1
                logger.info("✅ complete_series_delete_workflow")
                
                test_instance.test_series_delete_workflow_state_transitions()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ series_delete_workflow_state_transitions")
                
                test_instance.test_series_delete_preserves_audit_trail()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ series_delete_preserves_audit_trail")
                
                self.results['seriesdelete'] = {'passed': tests_passed, 'total': total_tests, 'status': 'SUCCESS'}
                
            except Exception as e:
                total_tests += 1
                self.results['seriesdelete'] = {'passed': tests_passed, 'total': total_tests, 'status': 'FAILED', 'error': str(e)}
                logger.error(f"Seriesdelete test failed: {e}")
            finally:
                test_instance.cleanup_test_data()
                
        except Exception as e:
            self.results['seriesdelete'] = {'passed': 0, 'total': 0, 'status': 'MODULE_ERROR', 'error': str(e)}
            logger.error(f"Failed to load seriesdelete test module: {e}")
    
    def run_import_event_tests(self):
        """Run import event handler tests"""
        logger.info("📥 Running import event handler tests...")
        try:
            from test_import_event_handler import TestImportEventHandlerJellyfin
            test_instance = TestImportEventHandlerJellyfin()
            test_instance.setup_and_teardown()
            
            tests_passed = 0
            total_tests = 0
            
            try:
                # Individual tests
                test_instance.test_import_event_detects_file_replacement()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ import_event_detects_file_replacement")
                
                test_instance.test_import_event_triggers_jellyfin_refresh()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ import_event_triggers_jellyfin_refresh")
                
                test_instance.test_import_event_metadata_update()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ import_event_metadata_update")
                
                # Async test
                asyncio.run(test_instance.test_complete_import_event_workflow())
                tests_passed += 1
                total_tests += 1
                logger.info("✅ complete_import_event_workflow")
                
                test_instance.test_import_event_workflow_state_transitions()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ import_event_workflow_state_transitions")
                
                test_instance.test_import_event_error_handling()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ import_event_error_handling")
                
                self.results['import_event'] = {'passed': tests_passed, 'total': total_tests, 'status': 'SUCCESS'}
                
            except Exception as e:
                total_tests += 1
                self.results['import_event'] = {'passed': tests_passed, 'total': total_tests, 'status': 'FAILED', 'error': str(e)}
                logger.error(f"Import event test failed: {e}")
            finally:
                test_instance.cleanup_test_data()
                
        except Exception as e:
            self.results['import_event'] = {'passed': 0, 'total': 0, 'status': 'MODULE_ERROR', 'error': str(e)}
            logger.error(f"Failed to load import event test module: {e}")
    
    def run_moviefiledelete_tests(self):
        """Run moviefiledelete handler tests"""
        logger.info("🗑️🎬 Running moviefiledelete handler tests...")
        try:
            from test_moviefiledelete_handler import TestMovieFileDeleteHandlerJellyfin
            test_instance = TestMovieFileDeleteHandlerJellyfin()
            test_instance.setup_and_teardown()
            
            tests_passed = 0
            total_tests = 0
            
            try:
                # Individual tests
                test_instance.test_moviefiledelete_detects_file_removal()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ moviefiledelete_detects_file_removal")
                
                test_instance.test_moviefiledelete_creates_placeholder_replacement()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ moviefiledelete_creates_placeholder_replacement")
                
                test_instance.test_moviefiledelete_triggers_jellyfin_refresh()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ moviefiledelete_triggers_jellyfin_refresh")
                
                # Async test
                asyncio.run(test_instance.test_complete_moviefiledelete_workflow())
                tests_passed += 1
                total_tests += 1
                logger.info("✅ complete_moviefiledelete_workflow")
                
                test_instance.test_moviefiledelete_workflow_state_transitions()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ moviefiledelete_workflow_state_transitions")
                
                test_instance.test_moviefiledelete_preserves_movie_metadata()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ moviefiledelete_preserves_movie_metadata")
                
                self.results['moviefiledelete'] = {'passed': tests_passed, 'total': total_tests, 'status': 'SUCCESS'}
                
            except Exception as e:
                total_tests += 1
                self.results['moviefiledelete'] = {'passed': tests_passed, 'total': total_tests, 'status': 'FAILED', 'error': str(e)}
                logger.error(f"Movie file delete test failed: {e}")
            finally:
                test_instance.cleanup_test_data()
                
        except Exception as e:
            self.results['moviefiledelete'] = {'passed': 0, 'total': 0, 'status': 'MODULE_ERROR', 'error': str(e)}
            logger.error(f"Failed to load moviefiledelete test module: {e}")
    
    def run_episodefiledelete_tests(self):
        """Run episodefiledelete handler tests"""
        logger.info("🗑️📺 Running episodefiledelete handler tests...")
        try:
            from test_episodefiledelete_handler import TestEpisodeFileDeleteHandlerJellyfin
            test_instance = TestEpisodeFileDeleteHandlerJellyfin()
            test_instance.setup_and_teardown()
            
            tests_passed = 0
            total_tests = 0
            
            try:
                # Individual tests
                test_instance.test_episodefiledelete_detects_file_removal()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ episodefiledelete_detects_file_removal")
                
                test_instance.test_episodefiledelete_creates_placeholder_replacement()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ episodefiledelete_creates_placeholder_replacement")
                
                test_instance.test_episodefiledelete_triggers_jellyfin_refresh()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ episodefiledelete_triggers_jellyfin_refresh")
                
                # Async test
                asyncio.run(test_instance.test_complete_episodefiledelete_workflow())
                tests_passed += 1
                total_tests += 1
                logger.info("✅ complete_episodefiledelete_workflow")
                
                test_instance.test_episodefiledelete_workflow_state_transitions()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ episodefiledelete_workflow_state_transitions")
                
                test_instance.test_episodefiledelete_preserves_episode_metadata()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ episodefiledelete_preserves_episode_metadata")
                
                test_instance.test_episodefiledelete_series_level_placeholder_management()
                tests_passed += 1
                total_tests += 1
                logger.info("✅ episodefiledelete_series_level_placeholder_management")
                
                self.results['episodefiledelete'] = {'passed': tests_passed, 'total': total_tests, 'status': 'SUCCESS'}
                
            except Exception as e:
                total_tests += 1
                self.results['episodefiledelete'] = {'passed': tests_passed, 'total': total_tests, 'status': 'FAILED', 'error': str(e)}
                logger.error(f"Episode file delete test failed: {e}")
            finally:
                test_instance.cleanup_test_data()
                
        except Exception as e:
            self.results['episodefiledelete'] = {'passed': 0, 'total': 0, 'status': 'MODULE_ERROR', 'error': str(e)}
            logger.error(f"Failed to load episodefiledelete test module: {e}")
    
    def run_all_tests(self):
        """Run all handler tests"""
        logger.info("🚀 Starting comprehensive handler test suite...")
        start_time = time.time()
        
        # Run all test suites
        self.run_movieadd_tests()
        self.run_movie_delete_tests()
        self.run_seriesadd_tests()
        self.run_seriesdelete_tests()
        self.run_import_event_tests()
        self.run_moviefiledelete_tests()
        self.run_episodefiledelete_tests()
        
        end_time = time.time()
        duration = end_time - start_time
        
        # Generate summary report
        self.generate_summary_report(duration)
    
    def generate_summary_report(self, duration):
        """Run all handler tests"""
        logger.info("🚀 Starting comprehensive handler test suite...")
        start_time = time.time()
        
        # Run all test suites
        self.run_movieadd_tests()
        self.run_movie_delete_tests()
        self.run_seriesadd_tests()
        self.run_seriesdelete_tests()
        
        end_time = time.time()
        duration = end_time - start_time
        
        # Generate summary report
        self.generate_summary_report(duration)
    
    def generate_summary_report(self, duration):
        """Generate comprehensive test summary report"""
        logger.info("📊 Generating test summary report...")
        
        total_passed = 0
        total_tests = 0
        successful_handlers = 0
        failed_handlers = 0
        
        print("\n" + "="*60)
        print("🧪 COMPREHENSIVE HANDLER TEST RESULTS")
        print("="*60)
        
        for handler, result in self.results.items():
            status_emoji = "✅" if result['status'] == 'SUCCESS' else "❌"
            print(f"\n{status_emoji} {handler.upper()} HANDLER:")
            print(f"   Passed: {result['passed']}/{result['total']} tests")
            print(f"   Status: {result['status']}")
            
            if result['status'] == 'SUCCESS':
                successful_handlers += 1
            else:
                failed_handlers += 1
                if 'error' in result:
                    print(f"   Error: {result['error']}")
            
            total_passed += result['passed']
            total_tests += result['total']
        
        print("\n" + "-"*60)
        print("📈 OVERALL SUMMARY:")
        print(f"   Total Tests Passed: {total_passed}/{total_tests}")
        print(f"   Success Rate: {(total_passed/total_tests)*100:.1f}%" if total_tests > 0 else "   Success Rate: 0%")
        print(f"   Successful Handlers: {successful_handlers}/{len(self.results)}")
        print(f"   Failed Handlers: {failed_handlers}/{len(self.results)}")
        print(f"   Execution Time: {duration:.2f} seconds")
        
        # Overall status
        if successful_handlers == len(self.results):
            print("\n🎉 ALL HANDLER TESTS PASSED! 🎉")
            print("Jellyfin integration is working correctly across all workflows.")
        else:
            print(f"\n⚠️  {failed_handlers} HANDLER(S) FAILED")
            print("Review the errors above and fix the failing handlers.")
        
        print("="*60)
        
        # Return overall success status
        return successful_handlers == len(self.results)

def main():
    """Main test runner function"""
    runner = TestHandlersRunner()
    success = runner.run_all_tests()
    
    # Exit with appropriate code
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
