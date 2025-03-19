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

console_handler = logging.StreamHandler()
console_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

file_handler = logging.FileHandler('media_handler.log')
file_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logger.addHandler(console_handler)
logger.addHandler(file_handler)
