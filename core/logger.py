import logging
import os
import sys
import traceback

# Try to import settings; if pydantic validation fails (missing paths), print a friendly message and exit
try:
    from core.config import settings
except Exception as ex:
    # Attempt to extract helpful messages about missing paths
    msg = str(ex)
    missing_lines = []
    for line in msg.splitlines():
        if 'Path does not exist:' in line or 'Path does not exist' in line:
            missing_lines.append(line.strip())
    if missing_lines:
        print('\n❌ Placeholdarr startup failed: missing configured library paths.\n', file=sys.stderr)
        print('The following path validations failed:', file=sys.stderr)
        for l in missing_lines:
            print('  -', l, file=sys.stderr)
        print('\nLikely causes:\n  * The folder locations have not been created\n  * A typo in your .env for DUMMY_MOVIE_LIBRARY_FOLDER / DUMMY_TV_LIBRARY_FOLDER\n ', file=sys.stderr)
        print('\nSuggested actions:', file=sys.stderr)
        print('  1) Ensure folder locations exist and create them if not.', file=sys.stderr)
        print('  2) Verify the paths in your .env or environment variables (DUMMY_MOVIE_LIBRARY_FOLDER, DUMMY_TV_LIBRARY_FOLDER, DUMMY_MOVIE_LIBRARY_4K_FOLDER, DUMMY_TV_LIBRARY_4K_FOLDER).', file=sys.stderr)
        print('\nOnce fixed, restart Placeholdarr.', file=sys.stderr)
        sys.exit(1)
    else:
        # Unknown import error - print traceback and exit
        print('\n❌ Placeholdarr failed to start due to an error importing settings:', file=sys.stderr)
        traceback.print_exception(type(ex), ex, ex.__traceback__, file=sys.stderr)
        sys.exit(1)

# Add VERBOSE log level
VERBOSE_LEVEL_NUM = 5
logging.addLevelName(VERBOSE_LEVEL_NUM, "VERBOSE")

def verbose(self, message, *args, **kws):
    if self.isEnabledFor(VERBOSE_LEVEL_NUM):
        self._log(VERBOSE_LEVEL_NUM, message, args, **kws)
logging.Logger.verbose = verbose

LOG_EMOJIS = {
    'success': '✅', 'error': '❌', 'info': 'ℹ️', 'debug': '🐛',
    'webhook': '🌐', 'playback': '🎬', 'dummy': '📁', 'search': '🔍',
    'delete': '🗑️', 'update': '🔄', 'warning': '⚠️', 'verbose': '🔍',
    'processing': '⏳', 'monitored': '👀', 'progress': '🔄',
    'tracking': '⏳', 'tv': '📺', 'timeout': '⏱️', 'status': '🔄',
    'cleanup': '🧹', 'placeholder': '➡️'
}

class EnhancedEmojiLogFormatter(logging.Formatter):
    def format(self, record):
        # Use actual source info captured by ContextAwareLogger or fallback to record info
        if hasattr(record, '_actual_pathname') and hasattr(record, '_actual_lineno'):
            filename = os.path.basename(record._actual_pathname)
            line_num = record._actual_lineno
        else:
            # Fallback to record info
            filename = os.path.basename(record.pathname)
            line_num = record.lineno
        
        # Add source location to the record for formatting
        record.source_location = f"{filename}:{line_num}"
        
        # Get emoji and build message
        emoji = LOG_EMOJIS.get(record.__dict__.get('emoji_type', ''), '➡️')
        
        # Add SubFlow context if available - but check if already added to avoid duplication
        subflow_context = ""
        original_msg = record.getMessage()
        
        # Only add context if not already in the message
        if hasattr(record, 'subflow_id') and f"[SF:{record.subflow_id}]" not in original_msg:
            subflow_context += f"[SF:{record.subflow_id}]"
        if hasattr(record, 'entity_id') and f"[ID:{record.entity_id}]" not in original_msg:
            subflow_context += f"[ID:{record.entity_id}]"
        if hasattr(record, 'entity_type') and f"[{record.entity_type.upper()}]" not in original_msg:
            subflow_context += f"[{record.entity_type.upper()}]"
        
        # Check if emoji already in message
        has_emoji = any(emoji_char in original_msg for emoji_char in LOG_EMOJIS.values())
        
        # Build final message WITHOUT mutating record.msg (to prevent double-formatting by multiple handlers)
        if subflow_context:
            formatted_msg = f"{emoji if not has_emoji else ''} {subflow_context} {original_msg}".strip()
        else:
            formatted_msg = f"{emoji if not has_emoji else ''} {original_msg}".strip()
        
        # Temporarily replace msg for formatting, then restore
        original_record_msg = record.msg
        record.msg = formatted_msg
        record.message = formatted_msg
        
        # Replace the name field with actual file:line
        old_format = self._style._fmt
        self._style._fmt = old_format.replace('%(name)s', f'{filename}:{line_num}')
        formatted = super().format(record)
        self._style._fmt = old_format
        
        # Restore original msg to prevent mutation issues
        record.msg = original_record_msg
        
        if not formatted.endswith("\n"):
            formatted += "\n"
        return formatted

# Thread-local storage for SubFlow context
import threading
_context = threading.local()

def set_subflow_context(subflow_id=None, entity_id=None, entity_type=None, action=None):
    """Set SubFlow context for current thread's log messages"""
    _context.subflow_id = subflow_id
    _context.entity_id = entity_id
    _context.entity_type = entity_type
    _context.action = action
    
    # Automatically set up handler-specific file logging if action is provided
    if action:
        _ensure_handler_file_logging(action)

def clear_subflow_context():
    """Clear SubFlow context for current thread"""
    # Remove handler-specific file logging if it was added
    if hasattr(_context, 'action') and _context.action:
        _remove_handler_file_logging(_context.action)
    
    _context.subflow_id = None
    _context.entity_id = None
    _context.entity_type = None
    _context.action = None

def get_subflow_context():
    """Get current SubFlow context"""
    return {
        'subflow_id': getattr(_context, 'subflow_id', None),
        'entity_id': getattr(_context, 'entity_id', None),
        'entity_type': getattr(_context, 'entity_type', None),
        'action': getattr(_context, 'action', None)
    }

# Thread-local storage for handler file handlers
_handler_file_handlers = {}

def _ensure_handler_file_logging(action: str):
    """Ensure that logs for this action go to the handler-specific log.txt file"""
    from pathlib import Path
    
    # Determine handler folder name
    handler_name = action if action.startswith('handle_') else f'handle_{action}'
    
    # Check if we already have a file handler for this action in this thread
    thread_id = threading.current_thread().ident
    handler_key = f"{thread_id}_{handler_name}"
    
    if handler_key in _handler_file_handlers:
        return  # Already set up
    
    # Create log directory and file path
    log_dir = Path("logs") / handler_name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "log.txt"
    
    # Create file handler (append mode to preserve logs from current session)
    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setLevel(VERBOSE_LEVEL_NUM)  # Capture everything including VERBOSE
    
    # Use the same EnhancedEmojiLogFormatter as console (not double-formatting)
    file_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(source_location)s - %(levelname)s - %(message)s'))
    
    # Add to base logger
    base_logger.addHandler(file_handler)
    
    # Store reference so we can remove it later
    _handler_file_handlers[handler_key] = file_handler

def _remove_handler_file_logging(action: str):
    """Remove the handler-specific file logging for this thread"""
    handler_name = action if action.startswith('handle_') else f'handle_{action}'
    thread_id = threading.current_thread().ident
    handler_key = f"{thread_id}_{handler_name}"
    
    if handler_key in _handler_file_handlers:
        file_handler = _handler_file_handlers[handler_key]
        base_logger.removeHandler(file_handler)
        file_handler.close()
        del _handler_file_handlers[handler_key]

class ContextAwareLogger:
    """Logger wrapper that automatically adds SubFlow context and fixes source location"""
    def __init__(self, logger):
        self._logger = logger
    
    def _add_context_and_source(self, extra=None):
        """Add SubFlow context and fix source location"""
        if extra is None:
            extra = {}
        
        # Add SubFlow context
        context = get_subflow_context()
        for key, value in context.items():
            if value is not None:
                extra[key] = value
        
        # Find the actual calling frame (skip our wrapper methods)
        import inspect
        try:
            stack = inspect.stack()
            # Walk up the stack to find the first non-logger frame
            for i in range(len(stack)):
                frame_info = stack[i]
                frame_filename = frame_info.filename
                function_name = frame_info.function
                
                # Skip frames that are part of the logging infrastructure
                if (frame_filename.endswith('core/logger.py') or 
                    'logging' in frame_filename or
                    function_name in ['_add_context_and_source', 'debug', 'info', 'warning', 'error', 'verbose']):
                    continue
                
                # Found the actual caller
                extra['_actual_pathname'] = frame_filename
                extra['_actual_lineno'] = frame_info.lineno
                break
        except:
            pass
        
        return extra
    
    def debug(self, msg, *args, **kwargs):
        kwargs['extra'] = self._add_context_and_source(kwargs.get('extra'))
        return self._logger.debug(msg, *args, **kwargs)
    
    def info(self, msg, *args, **kwargs):
        kwargs['extra'] = self._add_context_and_source(kwargs.get('extra'))
        return self._logger.info(msg, *args, **kwargs)
    
    def warning(self, msg, *args, **kwargs):
        kwargs['extra'] = self._add_context_and_source(kwargs.get('extra'))
        return self._logger.warning(msg, *args, **kwargs)
    
    def error(self, msg, *args, **kwargs):
        kwargs['extra'] = self._add_context_and_source(kwargs.get('extra'))
        return self._logger.error(msg, *args, **kwargs)
    
    def verbose(self, msg, *args, **kwargs):
        kwargs['extra'] = self._add_context_and_source(kwargs.get('extra'))
        return self._logger.verbose(msg, *args, **kwargs)

# Create the base logger
base_logger = logging.getLogger(__name__)
log_level = getattr(settings, 'LOG_LEVEL', 'INFO').upper()
if log_level == 'VERBOSE':
    base_logger.setLevel(VERBOSE_LEVEL_NUM)
else:
    base_logger.setLevel(getattr(logging, log_level, logging.INFO))

console_handler = logging.StreamHandler()
console_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(source_location)s - %(levelname)s - %(message)s'))

file_handler = logging.FileHandler('media_handler.log')
file_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(source_location)s - %(levelname)s - %(message)s'))

base_logger.addHandler(console_handler)

# Export the context-aware logger
logger = ContextAwareLogger(base_logger)
