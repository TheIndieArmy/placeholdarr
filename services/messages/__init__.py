"""Customizable status message templates for player projection.

Public API:
    - get_registry(): all MessageKey definitions (including internal keys)
    - get_registry_for_settings_ui(): subset shown under Settings → Status Messages
    - get_token_specs(): list of TokenSpec definitions for the UI / API
    - render(key, ctx): render a registered key against a context dict
    - render_template(key, template, ctx): render a free-form template (for preview)
    - get_overrides(): current override dict from AppConfig
    - get_separator(): currently configured separator
    - get_case(): currently configured case mode
    - save_template_config(payload): persist overrides + separator + case
    - validate_template_text(key, text): static validation helper
"""

from services.messages.registry import (
    DEFAULT_SEPARATOR,
    DEFAULT_WRAPPER_PRESET,
    SEPARATOR_PRESETS,
    CASE_OPTIONS,
    WRAPPER_PRESETS,
    MessageKey,
    TokenSpec,
    get_message_key,
    get_registry,
    get_registry_for_settings_ui,
    get_token_specs,
    get_wrapper_presets,
    get_wrapper_preset_pair,
)
from services.messages.template_engine import (
    InvalidTemplateError,
    UnknownTemplateKeyError,
    apply_wrapper,
    render,
    render_template,
    sample_render,
    validate_template_text,
)
from services.messages.store import (
    get_case,
    get_overrides,
    get_separator,
    get_template_config,
    get_wrapper,
    save_template_config,
)

__all__ = [
    "DEFAULT_SEPARATOR",
    "DEFAULT_WRAPPER_PRESET",
    "SEPARATOR_PRESETS",
    "CASE_OPTIONS",
    "WRAPPER_PRESETS",
    "MessageKey",
    "TokenSpec",
    "get_message_key",
    "get_registry",
    "get_registry_for_settings_ui",
    "get_token_specs",
    "get_wrapper_presets",
    "get_wrapper_preset_pair",
    "InvalidTemplateError",
    "UnknownTemplateKeyError",
    "apply_wrapper",
    "render",
    "render_template",
    "sample_render",
    "validate_template_text",
    "get_case",
    "get_overrides",
    "get_separator",
    "get_template_config",
    "get_wrapper",
    "save_template_config",
]
