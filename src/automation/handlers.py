"""Compatibility facade for kernel state handlers."""

from .handlers_steps import (
    handle_cleanup,
    handle_fill_selector,
    handle_fill_vision,
    handle_navigate,
    handle_route,
    handle_setup,
    handle_verification,
    handle_verify,
)

__all__ = [
    "handle_setup",
    "handle_navigate",
    "handle_route",
    "handle_fill_vision",
    "handle_fill_selector",
    "handle_verify",
    "handle_cleanup",
    "handle_verification",
]
