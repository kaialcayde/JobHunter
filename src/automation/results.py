"""Structured result types for automation handlers.

All handlers return StepResult instead of mixed strings/bools/tuples.
The HandlerResult enum replaces scattered status strings.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Any


class HandlerResult(Enum):
    SUCCESS = "success"
    RETRY = "retry"
    FAILED = "failed"
    CAPTCHA_DETECTED = "captcha_detected"
    REQUIRES_LOGIN = "requires_login"
    FAILED_SELECTOR = "failed_selector"
    FAILED_DEAD_PAGE = "failed_dead_page"
    FAILED_ERROR = "failed_error"
    ALREADY_APPLIED = "already_applied"
    NEEDS_MANUAL = "needs_manual"
    REQUIRES_REGISTRATION = "requires_registration"
    REQUIRES_VERIFICATION = "requires_verification"
    REQUIRES_EXISTING_LOGIN = "requires_existing_login"


@dataclass
class StepResult:
    result: HandlerResult
    next_state: str | None = None       # hint for kernel (Phase 2)
    metadata: dict[str, Any] = field(default_factory=dict)
    debug_screenshot: str | None = None  # path to screenshot on failure
    message: str = ""                    # human-readable status
