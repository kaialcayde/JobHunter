"""Public ATS login and registration handler package."""

from .detect import _is_application_form, handle_detect_auth_type
from .login import handle_login_registry
from .register import handle_register
from .verification import _click_verify_button, handle_verify_registration

__all__ = [
    "handle_detect_auth_type",
    "_is_application_form",
    "handle_login_registry",
    "handle_register",
    "handle_verify_registration",
    "_click_verify_button",
]
