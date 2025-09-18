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
        print('\nLikely causes:\n  * The folder locations have not been created\n  * A typo in your .env for MOVIE_LIBRARY_FOLDER / TV_LIBRARY_FOLDER\n ', file=sys.stderr)
        print('\nSuggested actions:', file=sys.stderr)
        print('  1) Ensure folder locations exist and create them if not.', file=sys.stderr)
        print('  2) Verify the paths in your .env or environment variables (MOVIE_LIBRARY_FOLDER, TV_LIBRARY_FOLDER, MOVIE_LIBRARY_4K_FOLDER, TV_LIBRARY_4K_FOLDER).', file=sys.stderr)
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