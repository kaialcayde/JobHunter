"""Avature prefill orchestration."""

from rich.console import Console

from .common import logger
from .profile_sections import fill_profile_sections
from .work_history import fill_work_history


def prefill(page, profile: dict, settings: dict) -> dict:
    """Fill Avature-specific custom widgets that generic form extraction misses."""
    console = Console(force_terminal=True)
    filled = {}

    fill_profile_sections(page, profile, filled, console)
    fill_work_history(page, profile, filled, console)

    if filled:
        console.print(f"  [dim]Avature prefill: filled {len(filled)} custom widgets[/]")
        logger.info(f"Avature prefill filled: {list(filled.keys())}")
    else:
        console.print("  [dim]Avature prefill: no custom widgets matched (may need debug inspection)[/]")

    return filled
