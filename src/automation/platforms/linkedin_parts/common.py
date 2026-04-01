"""Shared LinkedIn logging and constants."""

import logging

from rich.console import Console

from ...browser_scripts import evaluate_script
from ...selectors import (
    LINKEDIN_APPLY_SELECTORS,
    LINKEDIN_APPLY_WAIT_SELECTORS,
    LINKEDIN_EASY_APPLY_SELECTORS,
    LINKEDIN_MODAL_SELECTORS,
    LINKEDIN_OVERLAY_SELECTORS,
    LINKEDIN_SHADOW_HOST_SELECTORS,
    SHARE_PROFILE_CONTINUE_SELECTORS,
)

logger = logging.getLogger(__name__)
console = Console(force_terminal=True)
