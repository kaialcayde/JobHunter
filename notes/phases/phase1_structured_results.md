# Phase 1: Structured Results + Handler Extraction

## Goal
Extract the implicit state machine from `flow.py` into explicit, testable handler functions. Zero behavioral changes — same external API, same output. This is a pure refactor.

## Why This Phase First
`flow.py` is a 425-line monolithic function with implicit state transitions, mixed DB writes, and deeply nested try/except. Every future feature (kernel, selector cache, email polling) needs clean handler boundaries. This phase creates those boundaries without changing behavior.

---

## New Files

### `src/automation/results.py`
Structured result types that all handlers return.

```python
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

@dataclass
class StepResult:
    result: HandlerResult
    next_state: str | None = None       # hint for kernel (Phase 2)
    metadata: dict[str, Any] = field(default_factory=dict)
    debug_screenshot: str | None = None  # path to screenshot on failure
    message: str = ""                    # human-readable status
```

### `src/automation/handlers.py`
Seven stateless handler functions extracted from `flow.py`. Each takes explicit inputs and returns `StepResult`.

| Handler | Extracted From (flow.py) | Inputs | Returns |
|---------|-------------------------|--------|---------|
| `handle_setup(job, settings, conn)` | Lines 38-94 | Job dict, settings, DB connection | SUCCESS + metadata (file paths, app_id, app_dir) or FAILED |
| `handle_navigate(page, url, settings, conn, app_id, job_id)` | Lines 107-120 | Page, URL, settings, DB conn | SUCCESS, CAPTCHA_DETECTED, REQUIRES_LOGIN, FAILED_DEAD_PAGE, FAILED_ERROR |
| `handle_route(page, url, listing_url, settings)` | Lines 123-189 | Page, URLs, settings | SUCCESS + metadata (strategy, is_easy_apply, new_page) or FAILED_SELECTOR |
| `handle_fill_selector(page, job, settings, files, conn)` | Lines 288-365 | Page, job, settings, file paths | SUCCESS, RETRY, FAILED |
| `handle_fill_vision(page, job, settings, files)` | Lines 197-285 | Page, job, settings, file paths | SUCCESS, RETRY, FAILED |
| `handle_verify(page, settings, app_dir)` | Lines 368-386 | Page, settings, app dir | SUCCESS, FAILED |
| `handle_cleanup(result, conn, job, app_data)` | Lines 388-424 | HandlerResult, DB conn, job, app data | SUCCESS (always) |

---

## Modified Files

### `src/automation/flow.py`
Rewritten to call handlers sequentially. Same external API (`apply_single_job`), but internals delegate to handlers. This is a **compatibility shim** that will be deleted in Phase 5.

```python
# Simplified structure after refactor:
def apply_single_job(page, job, settings, conn):
    # Setup
    setup_result = handle_setup(job, settings, conn)
    if setup_result.result != HandlerResult.SUCCESS:
        return setup_result.metadata.get("status", "failed")

    # Navigate
    nav_result = handle_navigate(page, url, settings, conn, app_id, job_id)
    if nav_result.result == HandlerResult.CAPTCHA_DETECTED:
        # existing CAPTCHA handling...
    if nav_result.result != HandlerResult.SUCCESS:
        return handle_cleanup(nav_result.result, conn, job, app_data)

    # Route
    route_result = handle_route(page, url, listing_url, settings)
    # ... etc
```

### `src/automation/page_checks.py`
- `check_page_blockers()` returns `StepResult` instead of `(bool, str)` tuple
- `try_recover_login()` returns `StepResult` instead of mixed bool/string
- `detect_login_page()` unchanged (still returns bool — internal helper)
- `detect_dead_page()` unchanged (still returns bool — internal helper)

---

## Extraction Rules

1. **No behavioral changes.** The refactored code must produce identical output, identical DB writes, identical screenshots. This is a pure structural refactor.
2. **Handlers don't call each other.** `handle_navigate` does not call `handle_route`. The shim in `flow.py` sequences them.
3. **Handlers return results, not status strings.** The `HandlerResult` enum replaces the scattered `"failed"`, `"applied"`, `"needs_login"` strings.
4. **Metadata carries context.** File paths, app IDs, strategy choices, error messages — all in `StepResult.metadata` instead of local variables.
5. **Debug screenshots on every failure.** If a handler returns a non-SUCCESS result, it should include a `debug_screenshot` path. Enforced by convention now, by kernel in Phase 2.

---

## Testing Strategy

1. **Before refactor:** Run `python -m src apply` on 3-5 test jobs (mix of LinkedIn Easy Apply + external ATS). Record: final statuses, screenshots taken, DB records created, console output.
2. **After refactor:** Run same jobs. Diff: statuses, screenshots, DB records, console output. Must be identical.
3. **Unit tests (optional but recommended):** Each handler can be tested with a mock `page` object that returns canned DOM state. Assert `StepResult` values.

---

## Key Decisions

- **Why not merge handlers.py into kernel.py directly?** Because the kernel (Phase 2) needs clean handler interfaces to dispatch. Building handlers first lets us validate the extraction before adding state machine complexity.
- **Why keep flow.py as a shim?** So `applicant.py` doesn't need to change yet. One file changes at a time = easier debugging.
- **Why 7 handlers and not more/fewer?** Maps 1:1 to the natural phases of a job application. Each handler has a clear single responsibility. Could split `handle_route` further (apply button + tab switching) but not worth the complexity yet.

---

## Dependencies
- None. This is the foundation phase.

## Estimated Scope
- ~400 lines of new code (results.py + handlers.py)
- ~200 lines modified (flow.py shim + page_checks.py returns)
- Zero net new behavior
