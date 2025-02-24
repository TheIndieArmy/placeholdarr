import logging
from core.config import settings

LOG_EMOJIS = {
    'success': '✅', 'error': '❌', 'info': 'ℹ️', 'debug': '🐛',
    'webhook': '🌐', 'playback': '🎬', 'dummy': '📁', 'search': '🔍',
    'delete': '🗑️', 'update': '🔄', 'warning': '⚠️',
    'processing': '⏳', 'monitored': '👀', 'progress': '🔄',
    'tracking': '⏳', 'tv': '📺'
}

class EmojiLogFormatter(logging.Formatter):
    def format(self, record):
        emoji = LOG_EMOJIS.get(record.__dict__.get('emoji_type', ''), '➡️')
        record.msg = f"{emoji} {record.msg}"
        formatted = super().format(record)
        if not formatted.endswith("\n"):
            formatted += "\n"
        return formatted

logger = logging.getLogger(__name__)
logger.setLevel(settings.LOG_LEVEL)
console_handler = logging.StreamHandler()
console_handler.setFormatter(EmojiLogFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
file_handler = logging.FileHandler('media_handler.log')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(console_handler)
logger.addHandler(file_handler)
