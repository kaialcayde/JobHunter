# Phase 2: Automation Kernel

## Goal
Build a centralized state machine (`ApplicationKernel`) that owns all workflow transitions. Handlers are stateless workers that return results; the kernel decides what happens next. This prevents orchestration logic from spreading across handlers as the system grows.

## Why This Phase
Without the kernel, every new feature (email verification, account creation, retry strategies) adds more branching to the shim in `flow.py`. The kernel provides a single place where all control flow lives, making the system predictable and extensible.

---

## New Files

### `src/automation/kernel.py`

The `ApplicationKernel` class:

```python
class ApplicationKernel:
    """Centralized execution controller for job applications.

    Handlers are stateless workers. They return StepResults.
    Only the kernel advances state.
    """

    # State enum
    class State(Enum):
        SETUP = "setup"
        NAVIGATE = "navigate"
        ROUTE = "route"
        DETECT_STRATEGY = "detect_strategy"
        FILL_SELECTOR = "fill_selector"
        FILL_VISION = "fill_vision"
        SOLVE_CAPTCHA = "solve_captcha"
        RECOVER_LOGIN = "recover_login"
        VERIFY = "verify"
        CLEANUP = "cleanup"
        COMPLETE = "complete"

    # Transition table: (current_state, handler_result) -> next_state
    TRANSITIONS = {
        (State.SETUP, HandlerResult.SUCCESS): State.NAVIGATE,
        (State.SETUP, HandlerResult.FAILED): State.CLEANUP,

        (State.NAVIGATE, HandlerResult.SUCCESS): State.ROUTE,
        (State.NAVIGATE, HandlerResult.CAPTCHA_DETECTED): State.SOLVE_CAPTCHA,
        (State.NAVIGATE, HandlerResult.REQUIRES_LOGIN): State.RECOVER_LOGIN,
        (State.NAVIGATE, HandlerResult.FAILED_DEAD_PAGE): State.CLEANUP,
        (State.NAVIGATE, HandlerResult.FAILED_ERROR): State.CLEANUP,

        (State.ROUTE, HandlerResult.SUCCESS): State.DETECT_STRATEGY,
        (State.ROUTE, HandlerResult.FAILED_SELECTOR): State.CLEANUP,
        (State.ROUTE, HandlerResult.CAPTCHA_DETECTED): State.SOLVE_CAPTCHA,

        (State.DETECT_STRATEGY, HandlerResult.SUCCESS): None,  # dynamic: FILL_SELECTOR or FILL_VISION from metadata

        (State.FILL_SELECTOR, HandlerResult.SUCCESS): State.VERIFY,
        (State.FILL_SELECTOR, HandlerResult.RETRY): State.FILL_SELECTOR,  # retry same handler
        (State.FILL_SELECTOR, HandlerResult.FAILED): State.CLEANUP,
        (State.FILL_SELECTOR, HandlerResult.CAPTCHA_DETECTED): State.SOLVE_CAPTCHA,

        (State.FILL_VISION, HandlerResult.SUCCESS): State.VERIFY,
        (State.FILL_VISION, HandlerResult.RETRY): State.FILL_VISION,
        (State.FILL_VISION, HandlerResult.FAILED): State.CLEANUP,

        (State.VERIFY, HandlerResult.SUCCESS): State.CLEANUP,
        (State.VERIFY, HandlerResult.FAILED): State.CLEANUP,

        (State.SOLVE_CAPTCHA, HandlerResult.SUCCESS): None,  # return to pre-captcha state
        (State.SOLVE_CAPTCHA, HandlerResult.FAILED): State.CLEANUP,

        (State.RECOVER_LOGIN, HandlerResult.SUCCESS): State.NAVIGATE,  # retry from navigate
        (State.RECOVER_LOGIN, HandlerResult.FAILED): State.CLEANUP,
    }

    def run(self, page, job, settings, conn) -> str:
        """Execute the full application workflow. Returns final job status string."""
        state = self.State.SETUP
        context = KernelContext(page, job, settings, conn)
        history = []

        while state != self.State.COMPLETE:
            handler = self._get_handler(state)
            result = handler(context)
            history.append((state, result))

            # Debug screenshot on any non-success
            if result.result != HandlerResult.SUCCESS and not result.debug_screenshot:
                self._take_debug_screenshot(context, state)

            # Log state transition
            self._log_transition(context, state, result)

            # Advance state
            next_state = self._resolve_transition(state, result)
            if next_state == self.State.CLEANUP:
                context.final_result = result
            state = next_state

        return context.final_status
```

### Kernel Context
```python
@dataclass
class KernelContext:
    """Mutable context passed through the kernel lifecycle."""
    page: Page
    job: dict
    settings: dict
    conn: sqlite3.Connection

    # Populated by handlers
    app_id: int | None = None
    app_dir: str | None = None
    resume_file: str | None = None
    cl_file: str | None = None
    strategy: str | None = None  # "selector" or "vision"
    is_easy_apply: bool = False
    new_page: Page | None = None  # if apply opened a new tab

    # Kernel state
    final_result: StepResult | None = None
    final_status: str = "failed"
    pre_captcha_state: str | None = None  # for CAPTCHA resume
    retry_count: int = 0
    max_retries: int = 2
```

---

## Modified Files

### `src/automation/applicant.py`
Replace `apply_single_job()` call with `ApplicationKernel().run()`:

```python
# Before (current):
from src.automation.flow import apply_single_job
status = apply_single_job(page, job, settings, conn)

# After:
from src.automation.kernel import ApplicationKernel
kernel = ApplicationKernel()
status = kernel.run(page, job, settings, conn)
```

### `src/automation/handlers.py`
- Handlers now accept `KernelContext` instead of individual params
- DB writes move out of handlers into kernel cleanup hooks
- Handlers become pure page-state readers that return `StepResult`

```python
# Before:
def handle_navigate(page, url, settings, conn, app_id, job_id):
    # ... navigation logic + DB logging mixed in ...

# After:
def handle_navigate(ctx: KernelContext) -> StepResult:
    """Navigate to job URL and check for blockers. No side effects."""
    # ... navigation logic only, returns StepResult ...
```

---

## State Machine Diagram

```
SETUP ──SUCCESS──> NAVIGATE ──SUCCESS──> ROUTE ──SUCCESS──> DETECT_STRATEGY
  |                   |                    |                    |
  FAILED              |                    |              ┌─────┴──────┐
  |                   |                    |              |            |
  v                   |                    |         FILL_SELECTOR  FILL_VISION
CLEANUP <─────────────┘                    |              |            |
  ^          CAPTCHA/LOGIN/DEAD/ERROR      |          SUCCESS      SUCCESS
  |                                        |              |            |
  |                                     FAILED_SELECTOR   v            v
  |                                        |           VERIFY ──SUCCESS──> CLEANUP ──> COMPLETE
  |                                        v              |
  └────────────────────────────────────────┘           FAILED
                                                          |
                                                          v
                                                       CLEANUP

Cross-cutting:
  Any state ──CAPTCHA_DETECTED──> SOLVE_CAPTCHA ──SUCCESS──> (resume pre-captcha state)
  Any state ──REQUIRES_LOGIN──> RECOVER_LOGIN ──SUCCESS──> NAVIGATE (retry)
  SOLVE_CAPTCHA/RECOVER_LOGIN ──FAILED──> CLEANUP
```

---

## LLM Injection Point: DETECT_STRATEGY

The `DETECT_STRATEGY` state decides selector-based vs vision-based filling. Currently a simple config check:

```python
# Current logic (flow.py line ~192):
if settings["automation"].get("vision_agent") and "linkedin.com" not in page.url:
    use_vision = True
```

In the kernel, this becomes a pluggable strategy:

```python
def _detect_strategy(self, ctx: KernelContext) -> StepResult:
    """Decide fill strategy. This is an LLM injection point.

    Default: config-driven. Future: cheap LLM classifies page type
    (standard form vs complex ATS vs LinkedIn) and picks optimal strategy.
    """
    if ctx.is_easy_apply:
        strategy = "selector"
    elif ctx.settings.get("automation", {}).get("vision_agent"):
        strategy = "vision"
    else:
        strategy = "selector"

    return StepResult(
        result=HandlerResult.SUCCESS,
        metadata={"strategy": strategy}
    )
```

**Future LLM enhancement:** Send a DOM summary or small screenshot to a cheap model (haiku-class) to classify the page. Is it a multi-step modal (selector strategy)? A long single-page form (vision strategy)? A registration wall (redirect to account creation)? This replaces the boolean config check with intelligent routing.

---

## Kernel Rules (Enforced)

1. **Handlers must not advance workflow state.** Only kernel calls `_resolve_transition()`.
2. **Handlers must not call other handlers.** If `handle_fill_selector` needs to extract fields, it calls `extract_fields()` (a utility), not `handle_route()` (a handler).
3. **All DB writes happen in cleanup or post-transition hooks.** Handlers read page state and return results. The kernel's cleanup phase writes to the database.
4. **Debug screenshot on every non-SUCCESS result.** The kernel checks and takes one if the handler didn't.
5. **State history is logged.** Every `(state, result)` pair is appended to `application_log` table for diagnostics.
6. **Retry limits are kernel-enforced.** `max_retries` in `KernelContext` prevents infinite loops. CAPTCHA retry, login retry, and fill retry all decrement the same counter.

---

## CAPTCHA Pause/Resume

When any handler returns `CAPTCHA_DETECTED`:
1. Kernel saves `pre_captcha_state` (the state that detected the CAPTCHA)
2. Transitions to `SOLVE_CAPTCHA`
3. `handle_solve_captcha()` calls existing `try_solve_captcha()` from `detection.py`
4. On SUCCESS: kernel transitions back to `pre_captcha_state` (retry the interrupted handler)
5. On FAILED: kernel transitions to CLEANUP with status `failed_captcha`

This is cleaner than the current inline CAPTCHA handling scattered across `flow.py`.

---

## Testing Strategy

1. **Mock kernel test:** Inject mock handlers that return canned `StepResult` values. Verify transition table produces correct state sequences for:
   - Happy path: SETUP → NAVIGATE → ROUTE → DETECT → FILL → VERIFY → CLEANUP
   - CAPTCHA path: SETUP → NAVIGATE → CAPTCHA → SOLVE → NAVIGATE (retry) → ... → CLEANUP
   - Login path: SETUP → NAVIGATE → LOGIN → RECOVER → NAVIGATE (retry) → ... → CLEANUP
   - Failure path: SETUP → NAVIGATE → FAILED_DEAD_PAGE → CLEANUP
2. **Integration test:** Run `python -m src apply` on same test jobs as Phase 1. Results must match.
3. **Delete flow.py test:** Remove `flow.py`, verify no imports break, run pipeline.

---

## Dependencies
- **Phase 1** (handlers + results must exist)

## Estimated Scope
- ~300 lines new code (kernel.py + KernelContext)
- ~100 lines modified (applicant.py, handlers.py signature changes)
- Net behavior: identical, but state transitions are now explicit and logged
