"""astrbot_plugin_gemini_image 核心模块。"""

from __future__ import annotations

from .client import GeminiImageClient
from .constants import (
    COMMAND_ALIASES,
    COMMAND_HELP,
    COMMAND_MAIN,
    COMMAND_RESET,
    COMMAND_SHORT,
    COMMAND_STATUS,
    DEFAULT_EDIT_PROMPT_PREFIX,
    DEFAULT_MAX_TEXT_LENGTH,
    DEFAULT_PROMPT_PREFIX,
    IMAGES_DIRNAME,
    LIMITER_FILENAME,
    PLUGIN_AUTHOR,
    PLUGIN_DESC,
    PLUGIN_NAME,
    PLUGIN_REPO,
    PLUGIN_VERSION,
)
from .cookies import load_cached_1psidts, load_cached_cookies
from .errors import COOKIE_HINT, GeminiImageError, describe_exception
from .generator import GenerationResult, ImageGenerator
from .limiter import UsageLimiter

__all__ = [
    "COMMAND_ALIASES",
    "COMMAND_HELP",
    "COMMAND_MAIN",
    "COMMAND_RESET",
    "COMMAND_SHORT",
    "COMMAND_STATUS",
    "COOKIE_HINT",
    "DEFAULT_EDIT_PROMPT_PREFIX",
    "DEFAULT_MAX_TEXT_LENGTH",
    "DEFAULT_PROMPT_PREFIX",
    "GeminiImageClient",
    "GeminiImageError",
    "GenerationResult",
    "IMAGES_DIRNAME",
    "ImageGenerator",
    "LIMITER_FILENAME",
    "PLUGIN_AUTHOR",
    "PLUGIN_DESC",
    "PLUGIN_NAME",
    "PLUGIN_REPO",
    "PLUGIN_VERSION",
    "UsageLimiter",
    "describe_exception",
    "load_cached_1psidts",
    "load_cached_cookies",
]
