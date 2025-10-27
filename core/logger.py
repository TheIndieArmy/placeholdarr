import logging
import os
from core.config import settings

LOG_EMOJIS = {
    'success': '✅', 'error': '❌', 'info': 'ℹ️', 'debug': '🐛',
    'webhook': '🌐', 'playback': '🎬', 'dummy': '📁', 'search': '🔍',
    'delete': '🗑️', 'update': '🔄', 'warning': '⚠️',
    'processing': '⏳', 'monitored': '👀', 'progress': '🔄',
    'tracking': '⏳', 'tv': '📺', 'timeout': '⏱️', 'status': '🔄',
    'cleanup': '🧹', 'placeholder': '➡️'
}

class EnhancedEmojiLogFormatter(logging.Formatter):
    def format(self, record):
        # Add source file information
        filename = os.path.basename(record.pathname)
        line_num = record.lineno
        
        # Add emoji
        emoji = LOG_EMOJIS.get(record.__dict__.get('emoji_type', ''), '➡️')
        
        # Modify the message format to include file:line and emoji
        record.msg = f"{emoji} {record.msg}"
        
        # Store original format
        old_format = self._style._fmt
        
        # Temporarily update format to include source information
        self._style._fmt = old_format.replace('%(name)s', f'{filename}:{line_num}')
        
        # Format the record
        formatted = super().format(record)
        
        # Restore original format
        self._style._fmt = old_format
        
        # Add newline if needed
        if not formatted.endswith("\n"):
            formatted += "\n"
            
        return formatted

logger = logging.getLogger(__name__)
logger.setLevel(settings.LOG_LEVEL)
# Always log to stdout (Docker-friendly)
console_handler = logging.StreamHandler()
console_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(console_handler)

# Try to also log to a file if possible. Use LOG_FILE env var to override; default
# is the historic 'media_handler.log'. If opening the file fails (permissions, read-only
# filesystem), we fall back to stdout only and emit a warning instead of crashing.
log_file = os.getenv('LOG_FILE', '/var/log/placeholdarr/media_handler.log')
file_handler = None
try:
    # Ensure the log directory exists
    log_dir = os.path.dirname(log_file)
    if not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)
except Exception as e:
    # Use the console logger to report this so startup doesn't crash for users who run
    # the container as a non-root user or haven't mounted a writable path.
    logger.warning(f"Log file '{log_file}' not writable, continuing with stdout only: {e}", extra={'emoji_type': 'warning'})