import os
import logging
import shutil
from datetime import datetime
from typing import Dict, Optional
from pathlib import Path

class HandlerLogManager:
    """
    Manages separate log files for each handler execution.
    Current handler logs to [handler_name]/log.txt
    When new handler starts, moves existing log.txt to log_[datetime].txt
    """
    
    def __init__(self, base_log_dir: str = "logs"):
        self.base_log_dir = Path(base_log_dir)
        self.active_sessions: Dict[str, Dict] = {}
        self.ensure_log_directories()
        
    def ensure_log_directories(self):
        """Create log directories for each handler type"""
        handlers = [
            'handle_seriesadd', 'handle_seriesdelete', 'handle_episodefiledelete',
            'handle_movieadd', 'handle_movie_delete', 'handle_moviefiledelete',
            'handle_import_event', 'playback'
        ]
        
        for handler in handlers:
            handler_dir = self.base_log_dir / handler
            handler_dir.mkdir(parents=True, exist_ok=True)
    
    def _archive_existing_log(self, handler_name: str):
        """Move existing log.txt to log_[datetime].txt if it exists"""
        handler_dir = self.base_log_dir / handler_name
        current_log = handler_dir / "log.txt"
        
        if current_log.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            archived_log = handler_dir / f"log_{timestamp}.txt"
            shutil.move(str(current_log), str(archived_log))
            
            # Log the archival
            logger = logging.getLogger(__name__)
            logger.info(f"📦 Archived previous log: {current_log} → {archived_log}")
            
            return str(archived_log)
        return None
    
    def start_handler_session(self, handler_name: str, entity_id: int, entity_type: str, 
                             additional_context: Optional[Dict] = None) -> str:
        """
        Start a new logging session for a handler execution.
        Archives any existing log.txt and creates new one.
        
        Args:
            handler_name: Name of the handler (e.g., 'handle_seriesadd')
            entity_id: ID of the entity being processed
            entity_type: Type of entity ('series', 'movie', 'episode')
            additional_context: Additional context info
            
        Returns:
            session_id: Unique identifier for this logging session
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_id = f"{handler_name}_{entity_type}_{entity_id}_{timestamp}"
        
        # Archive existing log.txt if it exists
        archived_log = self._archive_existing_log(handler_name)
        
        # Create new log file path (always log.txt)
        log_file = self.base_log_dir / handler_name / "log.txt"
        
        # Create file handler with all log levels including VERBOSE
        file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
        from core.logger import VERBOSE_LEVEL_NUM
        file_handler.setLevel(VERBOSE_LEVEL_NUM)  # Capture everything including VERBOSE (level 5)
        
        # Create a custom formatter that handles the source location and emojis
        # but doesn't duplicate the processing done by ContextAwareLogger
        class HandlerLogFormatter(logging.Formatter):
            def format(self, record):
                # Import here to avoid circular imports
                from core.logger import LOG_EMOJIS, get_subflow_context
                import os
                
                # Get source location - use _actual_ paths if available (from ContextAwareLogger)
                if hasattr(record, '_actual_pathname') and hasattr(record, '_actual_lineno'):
                    filename = os.path.basename(record._actual_pathname)
                    line_num = record._actual_lineno
                else:
                    filename = os.path.basename(record.pathname)
                    line_num = record.lineno
                
                record.source_location = f"{filename}:{line_num}"
                
                # Get SubFlow context if not already in the message
                context = get_subflow_context()
                subflow_context = ""
                if (context['subflow_id'] and context['entity_id'] and context['entity_type'] and 
                    not any(f"[SF:{context['subflow_id']}]" in str(getattr(record, attr, '')) 
                           for attr in ['msg', 'message'] if hasattr(record, attr))):
                    entity_type = str(context['entity_type']).upper()
                    subflow_context = f"[SF:{context['subflow_id']}][ID:{context['entity_id']}][{entity_type}] "
                
                # Get emoji - check if message already has emoji
                emoji = ""
                emoji_type = getattr(record, 'emoji_type', None)
                if emoji_type and emoji_type in LOG_EMOJIS:
                    message = str(record.getMessage())
                    # Only add emoji if not already present
                    if not any(LOG_EMOJIS[et] in message for et in LOG_EMOJIS):
                        emoji = LOG_EMOJIS[emoji_type] + " "
                
                # Build the final message
                original_msg = record.getMessage()
                record.msg = f"{emoji}{subflow_context}{original_msg}"
                
                return super().format(record)
        
        formatter = HandlerLogFormatter('%(asctime)s - %(source_location)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        
        # Store session info
        self.active_sessions[session_id] = {
            'handler_name': handler_name,
            'entity_id': entity_id,
            'entity_type': entity_type,
            'start_time': datetime.now(),
            'log_file': str(log_file),
            'file_handler': file_handler,
            'additional_context': additional_context or {}
        }
        
        # Add handler to the base logger (before our ContextAwareLogger processing)
        from core.logger import base_logger, VERBOSE_LEVEL_NUM
        
        # Temporarily lower base logger level to capture VERBOSE logs for handler files
        original_level = base_logger.level
        if base_logger.level > VERBOSE_LEVEL_NUM:
            base_logger.setLevel(VERBOSE_LEVEL_NUM)
        
        # Store original level for restoration
        self.active_sessions[session_id]['original_logger_level'] = original_level
        
        base_logger.addHandler(file_handler)
        
        # Log session start
        logger = logging.getLogger(__name__)
        if archived_log:
            logger.info(f"� Previous log archived to: {archived_log}")
        logger.info(f"�🚀 HANDLER SESSION STARTED: {session_id}")
        logger.info(f"   Handler: {handler_name}")
        logger.info(f"   Entity: {entity_type} ID {entity_id}")
        if additional_context:
            logger.info(f"   Context: {additional_context}")
        logger.info(f"   Active log: {log_file}")
        logger.info("=" * 80)
        
        return session_id
    
    def end_handler_session(self, session_id: str, success: bool = True, 
                           summary: Optional[str] = None):
        """
        End a logging session for a handler execution.
        
        Args:
            session_id: The session identifier
            success: Whether the handler completed successfully
            summary: Optional summary of what was accomplished
        """
        if session_id not in self.active_sessions:
            return
            
        session = self.active_sessions[session_id]
        logger = logging.getLogger(__name__)
        
        # Log session end
        logger.info("=" * 80)
        logger.info(f"🏁 HANDLER SESSION ENDED: {session_id}")
        logger.info(f"   Duration: {datetime.now() - session['start_time']}")
        logger.info(f"   Success: {'✅' if success else '❌'}")
        if summary:
            logger.info(f"   Summary: {summary}")
        logger.info(f"   Log saved to: {session['log_file']}")
        
        # Remove handler from base logger and restore original level
        from core.logger import base_logger
        base_logger.removeHandler(session['file_handler'])
        
        # Restore original logger level
        original_level = session.get('original_logger_level')
        if original_level is not None:
            base_logger.setLevel(original_level)
        
        session['file_handler'].close()
        
        # Clean up session
        del self.active_sessions[session_id]
    
    def get_active_sessions(self) -> Dict[str, Dict]:
        """Get all currently active logging sessions"""
        return self.active_sessions.copy()
    
    def find_session_for_entity(self, handler_name: str, entity_id: int) -> Optional[str]:
        """Find active session for a specific handler and entity"""
        for session_id, session in self.active_sessions.items():
            if (session['handler_name'] == handler_name and 
                session['entity_id'] == entity_id):
                return session_id
        return None

# Global instance
handler_log_manager = HandlerLogManager()

def start_handler_logging(handler_name: str, entity_id: int, entity_type: str, 
                         **context) -> str:
    """
    Convenience function to start handler logging.
    
    Usage:
        session_id = start_handler_logging('handle_seriesadd', series_id, 'series', 
                                          tvdb_id=384429, title="1899")
    """
    return handler_log_manager.start_handler_session(
        handler_name, entity_id, entity_type, context
    )

def end_handler_logging(session_id: str, success: bool = True, summary: str = None):
    """
    Convenience function to end handler logging.
    
    Usage:
        end_handler_logging(session_id, success=True, 
                           summary="Processed 9 episodes successfully")
    """
    handler_log_manager.end_handler_session(session_id, success, summary)

def get_handler_session_for_entity(handler_name: str, entity_id: int) -> Optional[str]:
    """Find the active logging session for a handler and entity"""
    return handler_log_manager.find_session_for_entity(handler_name, entity_id)
