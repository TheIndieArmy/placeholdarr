import logging
import os
import sys
import traceback
import glob
from datetime import datetime

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

# Normalize legacy or ad-hoc emoji types onto canonical mapped keys.
LOG_EMOJI_ALIASES = {
    'refresh': 'update',
    'create': 'placeholder',
    'gear': 'processing',
    'process': 'processing',
}

class EnhancedEmojiLogFormatter(logging.Formatter):
    @staticmethod
    def _resolve_emoji_type(record: logging.LogRecord) -> str:
        raw_type = str(record.__dict__.get('emoji_type', '') or '').strip().lower()
        emoji_type = LOG_EMOJI_ALIASES.get(raw_type, raw_type)
        if emoji_type in LOG_EMOJIS:
            return emoji_type

        level = record.levelno
        if level >= logging.ERROR:
            return 'error'
        if level >= logging.WARNING:
            return 'warning'
        if level <= logging.DEBUG:
            return 'debug'
        return 'info'

    @staticmethod
    def _strip_leading_known_emojis(text: str) -> str:
        """Remove manually-prefixed known emojis from the start of a message.

        Keeps formatting consistent when callers include emoji text directly and
        avoids duplicate emoji prefixes once formatter-based emoji is applied.
        """
        if not text:
            return text
        cleaned = text.lstrip()
        known = sorted(set(LOG_EMOJIS.values()), key=len, reverse=True)
        changed = True
        while changed and cleaned:
            changed = False
            for emo in known:
                prefix = f"{emo} "
                if cleaned.startswith(prefix):
                    cleaned = cleaned[len(prefix):].lstrip()
                    changed = True
                elif cleaned.startswith(emo):
                    cleaned = cleaned[len(emo):].lstrip()
                    changed = True
        return cleaned

    def format(self, record):
        filename = os.path.basename(record.pathname)
        line_num = record.lineno
        emoji = LOG_EMOJIS[self._resolve_emoji_type(record)]
        old_msg = record.msg
        old_args = record.args
        old_format = self._style._fmt
        try:
            # Apply %-interpolation before mutating msg/args. Callers use
            # logger.info("uid=%s", uid, extra=...); we clear args when prefixing emoji.
            interpolated = record.getMessage()
            base_msg = self._strip_leading_known_emojis(interpolated)
            record.msg = f"{emoji} {base_msg}"
            record.args = ()
            self._style._fmt = old_format.replace('%(name)s', f'{filename}:{line_num}')
            formatted = super().format(record)
        finally:
            record.msg = old_msg
            record.args = old_args
            self._style._fmt = old_format
        if not formatted.endswith("\n"):
            formatted += "\n"
        return formatted

logger = logging.getLogger(__name__)

# Emit VERBOSE/custom + DEBUG/INFO/… for file handlers; console filters below.
logger.setLevel(VERBOSE_LEVEL_NUM)

logger.propagate = False

if logger.handlers:
    logger.handlers.clear()


def _resolve_log_dir() -> str:
    """Resolve the directory where log files should be stored."""
    explicit_file = str(getattr(settings, 'LOG_FILE', '') or '').strip()
    if explicit_file:
        return os.path.dirname(explicit_file) or '.'

    explicit_dir = str(getattr(settings, 'LOG_DIR', '') or '').strip()
    if explicit_dir:
        return explicit_dir

    appdata_path = str(getattr(settings, 'APPDATA_PATH', '/config') or '/config').strip() or '/config'
    return os.path.join(appdata_path, 'logs')

def _get_timestamped_log_filename(log_dir: str) -> str:
    """Generate a timestamped log filename for this run."""
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return os.path.join(log_dir, f"placeholdarr-{timestamp}.log")


def _get_workspace_log_filename() -> str:
    """Return stable workspace log file path for VS Code access.

    This writes to <repo_root>/logs/placeholdarr.log regardless of container/appdata
    file logging configuration so developers can tail logs directly in the workspace.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "logs", "placeholdarr.log")

def _cleanup_old_log_files(log_dir: str, max_files: int) -> None:
    """Keep only the most recent max_files log files, deleting older ones."""
    if max_files <= 0:
        return
    
    pattern = os.path.join(log_dir, "placeholdarr-*.log")
    log_files = sorted(glob.glob(pattern))
    
    # If we have more files than the max, delete the oldest ones
    if len(log_files) > max_files:
        files_to_delete = log_files[:len(log_files) - max_files]
        for old_file in files_to_delete:
            try:
                os.remove(old_file)
                logger.debug(f"Deleted old log file: {old_file}", extra={'emoji_type': 'cleanup'})
            except Exception as e:
                # Use basic print since logger might not be ready
                print(f"Failed to delete old log file {old_file}: {e}", file=sys.stderr)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

log_dir = _resolve_log_dir()
os.makedirs(log_dir, exist_ok=True)

max_run_files = max(1, int(getattr(settings, 'LOG_MAX_RUN_FILES', 10) or 10))
# Clean to max-1 BEFORE creating the new file so the total never exceeds max_run_files.
_cleanup_old_log_files(log_dir, max_run_files - 1)

log_file_path = _get_timestamped_log_filename(log_dir)
file_handler = logging.FileHandler(
    log_file_path,
    encoding='utf-8',
)
file_handler.setLevel(logging.NOTSET)
file_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

workspace_log_path = _get_workspace_log_filename()
workspace_file_handler = None
if os.path.abspath(workspace_log_path) != os.path.abspath(log_file_path):
    os.makedirs(os.path.dirname(workspace_log_path), exist_ok=True)
    workspace_file_handler = logging.FileHandler(
        workspace_log_path,
        mode='a',  # keep a continuous workspace mirror; run-rotation is handled by timestamped files
        encoding='utf-8',
    )
    workspace_file_handler.setLevel(logging.NOTSET)
    workspace_file_handler.setFormatter(EnhancedEmojiLogFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logger.addHandler(console_handler)
logger.addHandler(file_handler)
if workspace_file_handler is not None:
    logger.addHandler(workspace_file_handler)
logger.debug(f"File logging initialized at {log_file_path} (keeping {max_run_files} run files)", extra={'emoji_type': 'debug'})
if workspace_file_handler is not None:
    logger.debug(f"Workspace logging mirrored at {workspace_log_path}", extra={'emoji_type': 'debug'})