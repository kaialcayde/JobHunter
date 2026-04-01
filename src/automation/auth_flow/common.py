"""Shared auth-flow logging utilities."""

import logging

from rich.console import Console

logger = logging.getLogger(__name__)
console = Console(force_terminal=True)
