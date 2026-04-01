"""Compatibility facade for ATS login and registration handlers."""

from .auth_flow import (
    _click_verify_button,
    _is_application_form,
    handle_detect_auth_type,
    handle_login_registry,
    handle_register,
    handle_verify_registration,
)

__all__ = [
    "handle_detect_auth_type",
    "_is_application_form",
    "handle_login_registry",
    "handle_register",
    "handle_verify_registration",
    "_click_verify_button",
]
