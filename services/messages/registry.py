"""Registry of customizable player-projection messages.

Each MessageKey describes one user-visible string we render in player title/summary
brackets, NFO output, or related projection surfaces. Keys are grouped for the UI
and declare which tokens are valid in their template.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ----- Tokens ---------------------------------------------------------------


@dataclass(frozen=True)
class TokenSpec:
    """Describes a single {Token} that templates may interpolate."""

    name: str  # The bare token name without braces, e.g. "Title"
    label: str  # Friendly label for the picker UI
    group: str  # UI group: "Media", "Episode", "Status", "Calendar", "Queue", "Import Grace", "General"
    description: str  # Tooltip text shown in the picker
    sample: str  # Sample value shown in the picker chip and used for previews

    @property
    def placeholder(self) -> str:
        return "{" + self.name + "}"


_TOKEN_SPECS: tuple[TokenSpec, ...] = (
    # General
    TokenSpec(
        name="Sep",
        label="Separator",
        group="General",
        description="The configured separator character for this section. Defaults to a middle dot.",
        sample="·",
    ),
    # Media
    TokenSpec(
        name="Title",
        label="Title",
        group="Media",
        description="Movie or series title.",
        sample="Inception",
    ),
    TokenSpec(
        name="Year",
        label="Year",
        group="Media",
        description="Release year of the movie or series.",
        sample="2010",
    ),
    TokenSpec(
        name="ReleaseYear",
        label="Release Year",
        group="Media",
        description="Alias for the release year.",
        sample="2010",
    ),
    TokenSpec(
        name="Genres",
        label="Genres",
        group="Media",
        description="Comma-separated list of genres reported by the ARR.",
        sample="Action, Sci-Fi",
    ),
    TokenSpec(
        name="Certification",
        label="Certification",
        group="Media",
        description="Content rating reported by the ARR (e.g. PG-13, R, TV-MA).",
        sample="PG-13",
    ),
    TokenSpec(
        name="Studio",
        label="Studio",
        group="Media",
        description="Studio or network reported by the ARR.",
        sample="Warner Bros.",
    ),
    TokenSpec(
        name="Runtime",
        label="Runtime",
        group="Media",
        description="Formatted runtime, e.g. 1h 15m. Honors your runtime templates.",
        sample="2h 28m",
    ),
    TokenSpec(
        name="RuntimeMinutes",
        label="Runtime (minutes)",
        group="Media",
        description="Raw runtime expressed in whole minutes.",
        sample="148",
    ),
    TokenSpec(
        name="Hours",
        label="Hours",
        group="Media",
        description="Hour component of the runtime.",
        sample="2",
    ),
    TokenSpec(
        name="Minutes",
        label="Minutes",
        group="Media",
        description="Minute component of the runtime (0-59).",
        sample="28",
    ),
    # Episode
    TokenSpec(
        name="SeriesTitle",
        label="Series Title",
        group="Episode",
        description="Title of the TV series this episode belongs to.",
        sample="Breaking Bad",
    ),
    TokenSpec(
        name="SeasonNumber",
        label="Season Number",
        group="Episode",
        description="Two-digit season number, e.g. 02.",
        sample="02",
    ),
    TokenSpec(
        name="EpisodeNumber",
        label="Episode Number",
        group="Episode",
        description="Two-digit episode number, e.g. 05.",
        sample="05",
    ),
    TokenSpec(
        name="EpisodeTitle",
        label="Episode Title",
        group="Episode",
        description="Title of the episode.",
        sample="Pilot",
    ),
    TokenSpec(
        name="SXXEYY",
        label="Season/Episode (SxxEyy)",
        group="Episode",
        description="Combined season and episode code.",
        sample="S02E05",
    ),
    # Status
    TokenSpec(
        name="Status",
        label="Status",
        group="Status",
        description=(
            "Inside bracket templates: the short label Placeholdarr shows for that item in brackets "
            "(same wording as in Plex for bracket-style lines)."
        ),
        sample="REQUEST",
    ),
    TokenSpec(
        name="Bracket",
        label="Bracket",
        group="Status",
        description="The full bracketed status text Placeholdarr would otherwise project (uses bracket templates).",
        sample="[REQUEST]",
    ),
    # Calendar
    TokenSpec(
        name="DaysUntil",
        label="Days Until",
        group="Calendar",
        description="Number of days until the upcoming release or air date.",
        sample="5",
    ),
    TokenSpec(
        name="ReleaseLabel",
        label="Release Label",
        group="Calendar",
        description=(
            "Resolves to the configured movie release type label: Theatrical, Digital, or Physical. "
            "When no release type is configured it resolves to 'Coming Soon' to keep the message readable."
        ),
        sample="Theatrical",
    ),
    TokenSpec(
        name="ReleaseDate",
        label="Release Date",
        group="Calendar",
        description="Release or air date in YYYY-MM-DD form.",
        sample="2026-12-25",
    ),
    # Queue
    TokenSpec(
        name="Progress",
        label="Progress %",
        group="Queue",
        description="Whole-number download progress percentage, 0-100.",
        sample="42",
    ),
    # Import grace
    TokenSpec(
        name="MinutesRemaining",
        label="Minutes Remaining",
        group="Import Grace",
        description="Minutes remaining in the import-grace countdown (5, 4, 3, 2, or 1).",
        sample="3",
    ),
)


_TOKEN_BY_NAME: dict[str, TokenSpec] = {t.name: t for t in _TOKEN_SPECS}


def get_token_specs() -> tuple[TokenSpec, ...]:
    """Return all known token specs (read-only tuple)."""
    return _TOKEN_SPECS


def get_token_spec(name: str) -> TokenSpec | None:
    return _TOKEN_BY_NAME.get(str(name or "").strip())


# ----- Message keys ---------------------------------------------------------


@dataclass(frozen=True)
class MessageKey:
    """One customizable message string.

    Settings → Status Messages only exposes rows where ``settings_ui`` is True.
    Hidden rows still resolve through the engine so legacy overrides keep working
    (read-compat) until the user saves a new value through the new UI.
    """

    key: str
    label: str
    default: str
    group: str  # Top-level UI group
    subgroup: str | None  # Optional sub-group (e.g. Movie / TV inside Calendar)
    tooltip: str
    allowed_tokens: tuple[str, ...]
    sample_context: dict[str, Any] = field(default_factory=dict)
    # Internal-only defaults used when the engine needs an alternate phrasing.
    # The user only edits ``default`` from the UI.
    alt_defaults: dict[str, str] = field(default_factory=dict)
    # If False, hidden from Settings → Status Messages (internal / plumbing keys).
    settings_ui: bool = True


# Token bundles reused across keys.
_MEDIA_TOKENS = (
    "Sep",
    "Title",
    "Year",
    "ReleaseYear",
    "Genres",
    "Certification",
    "Studio",
    "Runtime",
    "RuntimeMinutes",
    "Hours",
    "Minutes",
)
_EPISODE_TOKENS = (
    "SeriesTitle",
    "SeasonNumber",
    "EpisodeNumber",
    "EpisodeTitle",
    "SXXEYY",
)


def _bracket_tokens() -> tuple[str, ...]:
    return ("Sep", "Status", "Runtime", "Hours", "Minutes")


def _request_line_tokens() -> tuple[str, ...]:
    """Tokens allowed inside the customizable Request line template (line.request).

    Excludes ``Status``/``Bracket`` because the Request line itself IS the bracket inner;
    those nested tokens would create a confusing recursive context.
    """
    return (
        "Sep",
        "Runtime",
        "RuntimeMinutes",
        "Hours",
        "Minutes",
        "Title",
        "Year",
        "ReleaseYear",
        "Genres",
        "Certification",
        "Studio",
    )


# Must match ``DisplayStatus`` in ``services/source_of_truth/status_intent.py`` (order + values).
_DISPLAY_STATUS_ENUM_VALUES: tuple[str, ...] = (
    "REQUEST",
    "COMING_SOON",
    "COMING_SOON_30",
    "COMING_SOON_14",
    "COMING_SOON_7",
    "COMING_SOON_1",
    "COMING_SOON_TODAY",
    "SEARCHING",
    "DOWNLOADING",
    "IMPORT_IN_PROGRESS",
    "NOT_FOUND",
    "AVAILABLE",
    "CLEANUP",
    "DELETED",
    "ARCHIVED",
)


# Tooltips: these strings are the *visible* word inside [ … ] for each internal stage (DisplayStatus).
# Users edit wording here; bracket *shape* (e.g. runtime + separator) is under Bracket & Duration.
_STATUS_LABEL_TOOLTIPS: dict[str, str] = {
    "REQUEST": (
        "Word inside [ … ] for the REQUEST stage. With runtime, combine with “Bracket format (with runtime)” "
        "— e.g. default templates give [1h 5m · REQUEST]. Change only the word here unless you also edit bracket shape."
    ),
    "SEARCHING": "Word inside [ … ] when the item is searching and no queue line replaces the bracket.",
    "DOWNLOADING": "Word inside [ … ] when downloading without a detailed queue progress line.",
    "IMPORT_IN_PROGRESS": "Word inside [ … ] when import is in progress and no finer queue import line is shown.",
    "NOT_FOUND": "Word inside [ … ] when no qualifying release was found.",
    "AVAILABLE": "Word inside [ … ] when the file exists (rarely projected).",
    "DELETED": "Word inside [ … ] briefly after removal from ARR.",
    "ARCHIVED": "Reserved stage — rarely emitted; wording if it ever appears in brackets.",
    "CLEANUP": "Reserved stage — rarely emitted; wording if it ever appears in brackets.",
}


# Plex/NFO usually show calendar countdown lines for these stages — not the raw COMING_SOON_* bracket word.
# Hide those rows from Settings so users only edit templates that actually drive on-screen copy.
_STAGES_WITHOUT_PLEX_BRACKET_WORD_SETTINGS: frozenset[str] = frozenset(
    {
        "COMING_SOON",
        "COMING_SOON_30",
        "COMING_SOON_14",
        "COMING_SOON_7",
        "COMING_SOON_1",
        "COMING_SOON_TODAY",
        "ARCHIVED",
        "CLEANUP",
    }
)


def _settings_ui_for_status_label(enum_value: str) -> bool:
    return enum_value not in _STAGES_WITHOUT_PLEX_BRACKET_WORD_SETTINGS


def _status_label_ui_name(enum_value: str) -> str:
    special = {
        "REQUEST": "Request label",
        "SEARCHING": "Searching label",
        "DOWNLOADING": "Downloading label",
        "IMPORT_IN_PROGRESS": "Import in progress label",
        "NOT_FOUND": "Not found label",
        "AVAILABLE": "Available label",
        "DELETED": "Deleted label",
        "ARCHIVED": "Archived label",
        "CLEANUP": "Cleanup label",
        "COMING_SOON": "Coming soon label",
        "COMING_SOON_30": "Coming soon (30+ days) label",
        "COMING_SOON_14": "Coming soon (14–29 days) label",
        "COMING_SOON_7": "Coming soon (7–13 days) label",
        "COMING_SOON_1": "Coming soon (1–6 days) label",
        "COMING_SOON_TODAY": "Release day label",
        "RETRYING": "Retrying label",
    }
    return special.get(enum_value, f"{enum_value.replace('_', ' ').title()} label")


def _iter_status_label_keys() -> tuple[MessageKey, ...]:
    """Per-stage word fallbacks used by the engine when ``{Status}`` is rendered
    inside a legacy bracket template. Hidden from Settings UI in the situation-first
    model — users edit the visible Plex line directly via ``line.request`` and the
    queue/calendar/import-grace situation rows.
    """
    rows: list[MessageKey] = []
    for ev in _DISPLAY_STATUS_ENUM_VALUES:
        rows.append(
            MessageKey(
                key=f"status.label.{ev}",
                label=_status_label_ui_name(ev),
                default=ev,
                group="Internal: Stage labels",
                subgroup=None,
                tooltip=_STATUS_LABEL_TOOLTIPS.get(
                    ev,
                    f"Internal fallback word for stage {ev} (used by legacy bracket templates).",
                ),
                allowed_tokens=("Sep",),
                sample_context={},
                settings_ui=False,
            )
        )
    rows.append(
        MessageKey(
            key="status.label.RETRYING",
            label=_status_label_ui_name("RETRYING"),
            default="RETRYING",
            group="Internal: Stage labels",
            subgroup=None,
            tooltip=(
                "Internal fallback word while the queue monitor is retrying after a transient queue failure "
                "or after an item left the queue during the retry grace window."
            ),
            allowed_tokens=("Sep",),
            sample_context={},
            settings_ui=False,
        )
    )
    return tuple(rows)


def _build_registry() -> tuple[MessageKey, ...]:
    keys: list[MessageKey] = []

    # --- Bracket labels (word inside [ … ] per internal stage) ---
    keys.extend(_iter_status_label_keys())

    # --- Request line (situation-first; the inner text wrapped by the global preset) ---
    keys.append(
        MessageKey(
            key="line.request",
            label="Request line",
            default="{Runtime} {Sep} REQUEST",
            group="Request",
            subgroup=None,
            tooltip=(
                "The bracketed line shown in Plex/NFO for newly requested items. The global "
                "wrapper preset sandwiches it (e.g. [ ]). Empty tokens auto-collapse, so "
                "without a runtime this renders as just \u201cREQUEST\u201d."
            ),
            allowed_tokens=_request_line_tokens(),
            sample_context={"Runtime": "1h 5m"},
        )
    )

    # --- Title suffix (kept; user-facing for title/both projection mode) ---
    keys.append(
        MessageKey(
            key="title.suffix.format",
            label="Title suffix format",
            default="{Title} - {Bracket}",
            group="Title Suffix",
            subgroup=None,
            tooltip=(
                "How the projected status is appended to the player title when projection mode is set to "
                "Title or Both. Use {Title} for the cleaned title and {Bracket} for the rendered bracket."
            ),
            allowed_tokens=("Title", "Bracket", "Sep"),
            sample_context={"Title": "Inception", "Bracket": "[REQUEST]"},
        )
    )

    # --- Internal: legacy bracket / runtime templates kept for read-compat fallback ---
    # These are hidden from Settings UI. Users who customized them on a previous
    # version keep their behavior until they save in the new UI; new users only see
    # ``line.request`` + the global wrapper preset.
    keys.append(
        MessageKey(
            key="bracket.format",
            label="Bracket format",
            default="[{Status}]",
            group="Internal: Legacy bracket",
            subgroup=None,
            tooltip=(
                "Legacy bracket shape used as a fallback when projecting non-REQUEST stages "
                "without a situation-specific row. Hidden in the new UI."
            ),
            allowed_tokens=_bracket_tokens(),
            sample_context={"Status": "REQUEST"},
            settings_ui=False,
        )
    )
    keys.append(
        MessageKey(
            key="bracket.with_runtime",
            label="Bracket format (with runtime)",
            default="[{Runtime} {Sep} {Status}]",
            group="Internal: Legacy bracket",
            subgroup=None,
            tooltip=(
                "Legacy bracket-with-runtime template used during read-compat for users who "
                "customized it before the situation-first redesign."
            ),
            allowed_tokens=_bracket_tokens(),
            sample_context={"Status": "REQUEST", "Runtime": "1h 5m"},
            settings_ui=False,
        )
    )
    keys.append(
        MessageKey(
            key="runtime.format.hm",
            label="Runtime format (hours and minutes)",
            default="{Hours}h {Minutes}m",
            group="Internal: Runtime",
            subgroup=None,
            tooltip="Internal: how {Runtime} renders when both hours and minutes are non-zero.",
            allowed_tokens=("Hours", "Minutes"),
            sample_context={"Hours": "1", "Minutes": "5"},
            settings_ui=False,
        )
    )
    keys.append(
        MessageKey(
            key="runtime.format.h",
            label="Runtime format (hours only)",
            default="{Hours}h",
            group="Internal: Runtime",
            subgroup=None,
            tooltip="Internal: how {Runtime} renders when minutes are zero.",
            allowed_tokens=("Hours",),
            sample_context={"Hours": "2"},
            settings_ui=False,
        )
    )
    keys.append(
        MessageKey(
            key="runtime.format.m",
            label="Runtime format (minutes only)",
            default="{Minutes}m",
            group="Internal: Runtime",
            subgroup=None,
            tooltip="Internal: how {Runtime} renders when the runtime is under one hour.",
            allowed_tokens=("Minutes",),
            sample_context={"Minutes": "45"},
            settings_ui=False,
        )
    )

    # --- Calendar ---
    cal_movie_tokens = ("Sep", "DaysUntil", "ReleaseLabel", "ReleaseDate") + _MEDIA_TOKENS
    cal_tv_tokens = ("Sep", "DaysUntil", "ReleaseDate") + _MEDIA_TOKENS + _EPISODE_TOKENS

    keys.append(
        MessageKey(
            key="calendar.movie.countdown.singular",
            label="Movie countdown (1 day)",
            default="{ReleaseLabel} release in 1 day",
            group="Calendar Coming Soon",
            subgroup="Movie",
            tooltip=(
                "Shown for movies the day before release. {ReleaseLabel} resolves to Theatrical / Digital / Physical "
                "based on your preferred movie date type, or 'Coming Soon' when no release type is configured."
            ),
            allowed_tokens=cal_movie_tokens,
            sample_context={"ReleaseLabel": "Theatrical", "DaysUntil": "1"},
            alt_defaults={"no_release_type": "Coming Soon (1 day)"},
        )
    )
    keys.append(
        MessageKey(
            key="calendar.movie.countdown.plural",
            label="Movie countdown (multiple days)",
            default="{ReleaseLabel} release in {DaysUntil} days",
            group="Calendar Coming Soon",
            subgroup="Movie",
            tooltip=(
                "Shown for upcoming movies when more than one day remains. Use {DaysUntil} for the day count. "
                "When no release type is configured Placeholdarr falls back to a parenthetical phrasing."
            ),
            allowed_tokens=cal_movie_tokens,
            sample_context={"ReleaseLabel": "Theatrical", "DaysUntil": "5"},
            alt_defaults={"no_release_type": "Coming Soon ({DaysUntil} days)"},
        )
    )
    keys.append(
        MessageKey(
            key="calendar.movie.today",
            label="Movie release today",
            default="{ReleaseLabel} release today",
            group="Calendar Coming Soon",
            subgroup="Movie",
            tooltip=(
                "Shown for movies on their release day. {ReleaseLabel} resolves to Theatrical / Digital / Physical "
                "or 'Coming Soon' when no release type is configured."
            ),
            allowed_tokens=cal_movie_tokens,
            sample_context={"ReleaseLabel": "Theatrical", "DaysUntil": "0"},
            alt_defaults={"no_release_type": "Coming Soon (Today)"},
        )
    )
    keys.append(
        MessageKey(
            key="calendar.movie.generic",
            label="Movie coming soon (countdown disabled)",
            default="{ReleaseLabel} release coming soon",
            group="Calendar Coming Soon",
            subgroup="Movie",
            tooltip=(
                "Shown for upcoming movies when 'Enable Coming Soon countdown text' is off. "
                "Falls back to 'Coming Soon' when no release type is configured."
            ),
            allowed_tokens=cal_movie_tokens,
            sample_context={"ReleaseLabel": "Theatrical"},
            alt_defaults={"no_release_type": "Coming Soon"},
        )
    )

    keys.append(
        MessageKey(
            key="calendar.tv.countdown.singular",
            label="TV countdown (1 day)",
            default="Airing in 1 day",
            group="Calendar Coming Soon",
            subgroup="TV",
            tooltip="Shown for TV episodes airing the next day.",
            allowed_tokens=cal_tv_tokens,
            sample_context={"DaysUntil": "1"},
        )
    )
    keys.append(
        MessageKey(
            key="calendar.tv.countdown.plural",
            label="TV countdown (multiple days)",
            default="Airing in {DaysUntil} days",
            group="Calendar Coming Soon",
            subgroup="TV",
            tooltip="Shown for upcoming TV episodes when more than one day remains. Use {DaysUntil} for the day count.",
            allowed_tokens=cal_tv_tokens,
            sample_context={"DaysUntil": "5"},
        )
    )
    keys.append(
        MessageKey(
            key="calendar.tv.today",
            label="TV airing today",
            default="Airing today",
            group="Calendar Coming Soon",
            subgroup="TV",
            tooltip="Shown for TV episodes airing today.",
            allowed_tokens=cal_tv_tokens,
            sample_context={"DaysUntil": "0"},
        )
    )
    keys.append(
        MessageKey(
            key="calendar.tv.generic",
            label="TV coming soon (countdown disabled)",
            default="Airing soon",
            group="Calendar Coming Soon",
            subgroup="TV",
            tooltip="Shown for upcoming TV episodes when 'Enable Coming Soon countdown text' is off.",
            allowed_tokens=cal_tv_tokens,
            sample_context={},
        )
    )

    # --- Queue monitor ---
    queue_tokens = ("Sep", "Progress") + _MEDIA_TOKENS + _EPISODE_TOKENS

    keys.append(
        MessageKey(
            key="queue.searching",
            label="Searching for release",
            default="Searching for release",
            group="Queue Monitor",
            subgroup=None,
            tooltip=(
                "Shown when the queue monitor is waiting for a download to enter the ARR queue. "
                "If the search timeout elapses with nothing found, 'No qualifying release found' takes over."
            ),
            allowed_tokens=queue_tokens,
            sample_context={},
        )
    )
    keys.append(
        MessageKey(
            key="queue.queued",
            label="Queued",
            default="Queued",
            group="Queue Monitor",
            subgroup=None,
            tooltip="Shown when the ARR has queued the download but progress has not started yet.",
            allowed_tokens=queue_tokens,
            sample_context={},
        )
    )
    keys.append(
        MessageKey(
            key="queue.downloading",
            label="Downloading (with progress)",
            default="Downloading {Progress}%",
            group="Queue Monitor",
            subgroup=None,
            tooltip="Shown while a download is in progress. Use {Progress} for the percent complete.",
            allowed_tokens=queue_tokens,
            sample_context={"Progress": "42"},
        )
    )
    keys.append(
        MessageKey(
            key="queue.import.pending",
            label="Import: waiting to import",
            default="Waiting to import",
            group="Queue Monitor",
            subgroup="Import",
            tooltip="Shown after a download completes while the ARR is preparing to import it.",
            allowed_tokens=queue_tokens,
            sample_context={},
        )
    )
    keys.append(
        MessageKey(
            key="queue.import.importing",
            label="Import: importing",
            default="Importing",
            group="Queue Monitor",
            subgroup="Import",
            tooltip="Shown while the ARR is actively importing the completed download into your library.",
            allowed_tokens=queue_tokens,
            sample_context={},
        )
    )
    keys.append(
        MessageKey(
            key="queue.import.blocked",
            label="Import: blocked",
            default="Import blocked",
            group="Queue Monitor",
            subgroup="Import",
            tooltip="Shown when the ARR has blocked the import (e.g. permissions, missing files, mismatched media).",
            allowed_tokens=queue_tokens,
            sample_context={},
        )
    )
    keys.append(
        MessageKey(
            key="queue.import.failed_pending",
            label="Import: waiting for retry",
            default="Waiting for import retry",
            group="Queue Monitor",
            subgroup="Import",
            tooltip="Shown when the ARR's import attempt failed and another retry is pending.",
            allowed_tokens=queue_tokens,
            sample_context={},
        )
    )
    keys.append(
        MessageKey(
            key="queue.import.processing",
            label="Import: processing",
            default="Processing import",
            group="Queue Monitor",
            subgroup="Import",
            tooltip="Generic fallback shown for the 'completed' queue state when no specific tracked state applies.",
            allowed_tokens=queue_tokens,
            sample_context={},
        )
    )
    keys.append(
        MessageKey(
            key="queue.retry.queue_failure",
            label="Retry after queue failure",
            default="Retrying after queue failure",
            group="Queue Monitor",
            subgroup="Retry",
            tooltip="Shown when the ARR queue reports the download as warning/error/failed and Placeholdarr is waiting for the next attempt.",
            allowed_tokens=queue_tokens,
            sample_context={},
        )
    )
    keys.append(
        MessageKey(
            key="queue.retry.left_queue",
            label="Retry after queue exit",
            default="Retrying; waiting for another qualifying release",
            group="Queue Monitor",
            subgroup="Retry",
            tooltip=(
                "Shown when a download exited the ARR queue (canceled, removed, or failed) and Placeholdarr "
                "is waiting for another release. After the retry grace period ends, 'No qualifying release found' takes over."
            ),
            allowed_tokens=queue_tokens,
            sample_context={},
        )
    )
    keys.append(
        MessageKey(
            key="queue.not_found",
            label="No qualifying release found",
            default="NO QUALIFYING RELEASE FOUND",
            group="Queue Monitor",
            subgroup=None,
            tooltip=(
                "Shown when the search timeout or retry grace expires and no qualifying release was found. "
                "This is the terminal state for queue monitoring."
            ),
            allowed_tokens=queue_tokens,
            sample_context={},
        )
    )

    # --- Import grace ---
    keys.append(
        MessageKey(
            key="import_grace.countdown",
            label="Import grace countdown",
            default="NOW IN LIBRARY - RETIRING PLACEHOLDER IN {MinutesRemaining} MIN",
            group="Import Grace",
            subgroup=None,
            tooltip=(
                "Shown each minute during the import-grace countdown after the file is imported into your "
                "library. {MinutesRemaining} resolves to 5, 4, 3, 2, or 1."
            ),
            allowed_tokens=("Sep", "MinutesRemaining"),
            sample_context={"MinutesRemaining": "3"},
        )
    )
    keys.append(
        MessageKey(
            key="import_grace.countdown_lt_1_min",
            label="Import grace final tick",
            default="NOW IN LIBRARY - RETIRING PLACEHOLDER IN LESS THAN A MINUTE",
            group="Import Grace",
            subgroup=None,
            tooltip="Final import-grace tick shown just before the placeholder is retired.",
            allowed_tokens=("Sep",),
            sample_context={},
        )
    )

    return tuple(keys)


_REGISTRY: tuple[MessageKey, ...] = _build_registry()
_REGISTRY_BY_KEY: dict[str, MessageKey] = {k.key: k for k in _REGISTRY}


def get_registry() -> tuple[MessageKey, ...]:
    return _REGISTRY


def get_registry_for_settings_ui() -> tuple[MessageKey, ...]:
    """Templates exposed under Settings → Status Messages (user-facing copy only)."""
    return tuple(m for m in _REGISTRY if m.settings_ui)


def get_message_key(key: str) -> MessageKey | None:
    return _REGISTRY_BY_KEY.get(str(key or "").strip())


# ----- Separator / case presets --------------------------------------------


DEFAULT_SEPARATOR = "·"

SEPARATOR_PRESETS: tuple[dict[str, str], ...] = (
    {"value": "·", "label": "Middle dot (·)"},
    {"value": "•", "label": "Bullet (•)"},
    {"value": "-", "label": "Dash (-)"},
    {"value": "–", "label": "En dash (–)"},
    {"value": "—", "label": "Em dash (—)"},
    {"value": "_", "label": "Underscore (_)"},
    {"value": " ", "label": "Space"},
    {"value": "/", "label": "Slash (/)"},
    {"value": "|", "label": "Pipe (|)"},
    {"value": ":", "label": "Colon (:)"},
)

CASE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "default", "label": "Default Case"},
    {"value": "upper", "label": "UPPERCASE"},
    {"value": "lower", "label": "lowercase"},
    {"value": "title", "label": "Title Case"},
)


# ----- Wrapper presets (global presentation chrome around inner lines) -----

DEFAULT_WRAPPER_PRESET = "brackets"

WRAPPER_PRESETS: tuple[dict[str, str], ...] = (
    {"value": "brackets", "label": "Brackets [ ]", "open": "[", "close": "]"},
    {"value": "parens", "label": "Parentheses ( )", "open": "(", "close": ")"},
    {"value": "curly", "label": "Curly braces { }", "open": "{", "close": "}"},
    {"value": "angle", "label": "Angle brackets < >", "open": "<", "close": ">"},
    {"value": "none", "label": "No wrapper", "open": "", "close": ""},
    {"value": "custom", "label": "Custom\u2026", "open": "", "close": ""},
)


def get_wrapper_presets() -> tuple[dict[str, str], ...]:
    return WRAPPER_PRESETS


def get_wrapper_preset_pair(preset: str | None) -> tuple[str, str] | None:
    """Return the ``(open, close)`` pair for a non-custom preset, or ``None`` if unknown / custom."""
    name = str(preset or "").strip().lower()
    if not name or name == "custom":
        return None
    for entry in WRAPPER_PRESETS:
        if entry["value"] == name:
            return (entry["open"], entry["close"])
    return None
