# Comprehensive Handler Test Suite for Jellyfin Integration

## Overview
This test suite provides end-to-end validation of all workflow handlers for Jellyfin integration in the IndieFork media management system. The tests verify that each handler properly manages placeholder creation, file operations, Jellyfin library scanning, and state transitions.

## Test Files Created

### 1. `test_movieadd_handler.py` - Movie Addition Workflow
**Tests:**
- ✅ Dummy placeholder creation with correct naming convention
- ✅ Jellyfin library scan detection of placeholders
- ✅ Title status updates in Jellyfin
- ✅ Complete movieadd workflow execution
- ✅ Workflow state transitions (PENDING → IN_PROGRESS → COMPLETED)

**Key Scenarios:**
- Creates test movie with TMDB ID 127127 ("Test Movie 127 Hours")
- Verifies folder naming includes `tmdb-{id}` and `edition-Dummy`
- Validates Jellyfin library integration and visibility
- Tests complete workflow from creation to completion

### 2. `test_movie_delete_handler.py` - Movie Deletion Workflow
**Tests:**
- ✅ Dummy placeholder removal from filesystem
- ✅ Jellyfin library scan after deletion (placeholder removal)
- ✅ Database cleanup with audit trail preservation
- ✅ Complete movie delete workflow execution
- ✅ Delete workflow state transitions and rollback scenarios

**Key Scenarios:**
- Creates test movie with TMDB ID 999999 ("Test Delete Movie")
- Tests placeholder removal and Jellyfin library cleanup
- Validates database soft-delete patterns
- Tests error handling and rollback functionality

### 3. `test_seriesadd_handler.py` - Series Addition Workflow
**Tests:**
- ✅ Series dummy placeholder creation
- ✅ Jellyfin library scan detection for TV series
- ✅ Season/Episode structure validation
- ✅ Complete seriesadd workflow execution
- ✅ Series metadata updates from Jellyfin

**Key Scenarios:**
- Creates test series with TVDB ID 888888 ("Test Series Breaking Bad")
- Tests series/season/episode hierarchical structure
- Validates TV series integration with Jellyfin
- Tests series-specific placeholder management

### 4. `test_seriesdelete_handler.py` - Series Deletion Workflow
**Tests:**
- ✅ Series placeholder removal from filesystem
- ✅ Cascade deletion to seasons and episodes
- ✅ Jellyfin library scan after series deletion
- ✅ Complete series delete workflow execution
- ✅ Audit trail preservation for deleted series

**Key Scenarios:**
- Creates test series with TVDB ID 777777 ("Test Delete Series Lost")
- Tests cascading deletion through series → seasons → episodes
- Validates Jellyfin library cleanup for TV content
- Tests audit information preservation

### 5. `test_import_event_handler.py` - File Import Workflow
**Tests:**
- ✅ File replacement detection (dummy → real content)
- ✅ Jellyfin library refresh after import
- ✅ Metadata updates during import process
- ✅ Complete import event workflow execution
- ✅ Import error handling and rollback

**Key Scenarios:**
- Creates test movie with TMDB ID 555555 ("Test Import Movie Inception")
- Tests transition from placeholder to real content
- Validates file metadata updates (size, quality, path)
- Tests import failure recovery mechanisms

### 6. `test_moviefiledelete_handler.py` - Movie File Deletion Workflow
**Tests:**
- ✅ File removal detection (real content → placeholder)
- ✅ Placeholder recreation after file deletion
- ✅ Jellyfin library refresh for content changes
- ✅ Complete moviefiledelete workflow execution
- ✅ Movie metadata preservation during file operations

**Key Scenarios:**
- Creates test movie with TMDB ID 444444 ("Test File Delete Movie Matrix")
- Tests transition from real content back to placeholder
- Validates file metadata clearing while preserving movie info
- Tests placeholder restoration functionality

### 7. `test_episodefiledelete_handler.py` - Episode File Deletion Workflow
**Tests:**
- ✅ Episode file removal detection
- ✅ Series-level placeholder recreation
- ✅ Jellyfin library refresh for episode changes
- ✅ Complete episodefiledelete workflow execution
- ✅ Series-level placeholder management for partial content

**Key Scenarios:**
- Creates test series with TVDB ID 333333 ("Test Episode Delete Series Westworld")
- Tests episode file deletion with series-level impact
- Validates partial content management (some episodes remain)
- Tests episode metadata preservation

### 8. `test_all_handlers.py` - Comprehensive Test Runner
**Features:**
- 🚀 Runs all handler tests in sequence
- 📊 Provides detailed test results and statistics
- ✅ Individual test status tracking
- 📈 Overall success rate calculation
- 🎉 Comprehensive summary reporting

## Test Database Design

Each test uses unique identifiers to avoid conflicts:
- **Movie Tests:** TMDB IDs 127127, 999999, 555555, 444444
- **Series Tests:** TVDB IDs 888888, 777777, 333333

## Key Testing Patterns

### 1. **Placeholder Management**
- Creation of dummy folders with correct naming conventions
- Filesystem validation (folder existence, structure)
- Database state management (paths, status fields)

### 2. **Jellyfin Integration**
- Library scanning and refresh operations
- Item detection and metadata retrieval
- Content visibility validation

### 3. **Workflow State Transitions**
- Status field progression through workflow stages
- Error state handling and recovery
- Audit trail preservation

### 4. **Data Integrity**
- Metadata preservation during operations
- File information management
- Database consistency validation

## Running the Tests

### Individual Test Execution:
```bash
# Run specific handler test
python test_movieadd_handler.py
python test_movie_delete_handler.py
python test_seriesadd_handler.py
# ... etc
```

### Comprehensive Test Suite:
```bash
# Run all handler tests with summary report
python test_all_handlers.py
```

## Expected Test Results

When all tests pass, you should see:
```
🎉 ALL HANDLER TESTS PASSED! 🎉
Jellyfin integration is working correctly across all workflows.

📈 OVERALL SUMMARY:
   Total Tests Passed: 42/42
   Success Rate: 100.0%
   Successful Handlers: 7/7
   Failed Handlers: 0/7
```

## Test Environment Requirements

- ✅ Jellyfin server accessible and configured
- ✅ Database connection (PostgreSQL) available
- ✅ File system permissions for dummy folder creation
- ✅ Python dependencies installed (pytest, sqlalchemy, etc.)

## Integration Points Validated

1. **Database Operations**: SQLAlchemy ORM interactions
2. **Jellyfin API**: Library scanning, item retrieval, metadata updates
3. **File System**: Dummy folder creation/deletion, path management
4. **Workflow Engine**: State transitions, error handling, progress tracking
5. **Configuration**: Environment settings and service integration

This comprehensive test suite ensures that all critical workflows function correctly with Jellyfin integration and provides confidence in the system's reliability and data integrity.
