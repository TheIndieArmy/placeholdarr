import logging
import os
from core.config import settings

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
    'delete': '🗑️', 'update': '🔄', 'warning': '⚠️',
    'processing': '⏳', 'monitored': '👀', 'progress': '🔄',
    'tracking': '⏳', 'tv': '📺', 'timeout': '⏱️', 'status': '🔄',
    'cleanup': '🧹', 'placeholder': '➡️'
}

class EnhancedEmojiLogFormatter(logging.Formatter):
    def format(self, record):
        filename = os.path.basename(record.pathname)
        line_num = record.lineno
        emoji = LOG_EMOJIS.get(record.__dict__.get('emoji_type', ''), '➡️')
        record.msg = f"{emoji} {record.msg}"
        old_format = self._style._fmt
        self._style._fmt = old_format.replace('%(name)s', f'{filename}:{line_num}')
        formatted = super().format(record)
        self._style._fmt = old_format
        if not formatted.endswith("\n"):
            formatted += "\n"
        return formatted

logger = logging.getLogger(__name__)
log_level = getattr(settings, 'LOG_LEVEL', 'INFO').upper()
if log_level == 'VERBOSE':
    logger.setLevel(VERBOSE_LEVEL_NUM)
else:
    logger.setLevel(getattr(logging, log_level, logging.INFO))

console_handler = logging.StreamHandler()
console_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

file_handler = logging.FileHandler('media_handler.log')
file_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logger.addHandler(console_handler)
logger.addHandler(file_handler)