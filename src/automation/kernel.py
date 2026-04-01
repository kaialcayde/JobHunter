"""Centralized execution controller for job applications.

The ApplicationKernel owns all workflow transitions. Handlers are
stateless workers that return StepResults; only the kernel advances state.
Handlers are stateless workers; only the kernel advances state.
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from rich.console import Console

from ..db import (
    get_connection, update_job_status, log_action,
    increment_retry_count, update_application,
)
from ..utils import move_application_dir

from .results import HandlerResult, StepResult
from .handlers import (
    handle_setup,
    handle_navigate,
    handle_route,
    handle_fill_vision,
    handle_fill_selector,
    handle_verify,
    handle_cleanup,
    handle_verification,
)
from .handlers_account import (
    handle_detect_auth_type,
    handle_login_registry,
    handle_register,
    handle_verify_registration,
)
from .detection import try_solve_captcha
from .element_finder import ElementFinder
from .page_checks import try_recover_login
from .selector_cache import SelectorCache

logger = logging.getLogger(__name__)
console = Console(force_terminal=True)


class State(Enum):
    """Kernel workflow states."""
    SETUP = "setup"
    NAVIGATE = "navigate"
    ROUTE = "route"
    DETECT_STRATEGY = "detect_strategy"
    FILL_SELECTOR = "fill_selector"
    FILL_VISION = "fill_vision"
    SOLVE_CAPTCHA = "solve_captcha"
    RECOVER_LOGIN = "recover_login"
    DETECT_AUTH_TYPE = "detect_auth_type"
    LOGIN_REGISTRY = "login_registry"
    REGISTER = "register"
    VERIFY_REGISTRATION = "verify_registration"
    VERIFY = "verify"
    VERIFY_EMAIL = "verify_email"
    CLEANUP = "cleanup"
    COMPLETE = "complete"


@dataclass
class KernelContext:
    """Mutable context passed through the kernel lifecycle."""
    page: Any  # playwright Page
    job: dict
    settings: dict
    conn: sqlite3.Connection

    # Populated by setup handler
    app_id: int | None = None
    app_dir: Path | None = None
    resume_file: Path | None = None
    cl_file: Path | None = None
    url: str = ""
    listing_url: str = ""
    company: str = ""
    position: str = ""
    job_id: int = 0

    # Populated by route/strategy handlers
    strategy: str | None = None  # "selector" or "vision"
    is_easy_apply: bool = False

    # Populated by fill handlers
    submitted: bool = False
    form_answers_all: dict = field(default_factory=dict)

    # Element finding
    finder: ElementFinder | None = None

    # Account registry (Phase 6 — lazy-initialized on first use)
    account_registry: Any = None

    # Kernel state
    final_result: StepResult | None = None
    final_status: str = "failed"
    pre_captcha_state: Any = None  # State enum value
    retry_count: int = 0
    max_retries: int = 2
    verbose: bool = True
    take_screenshot: bool = True


class ApplicationKernel:
    """Centralized execution controller for job applications.

    Handlers are stateless workers. They return StepResults.
    Only the kernel advances state.
    """

    # Transition table: (current_state, handler_result) -> next_state
    # None = dynamic resolution in _resolve_transition
    TRANSITIONS: dict[tuple, State | None] = {
        (State.SETUP, HandlerResult.SUCCESS): State.NAVIGATE,
        (State.SETUP, HandlerResult.FAILED): State.CLEANUP,

        (State.NAVIGATE, HandlerResult.SUCCESS): State.ROUTE,
        (State.NAVIGATE, HandlerResult.CAPTCHA_DETECTED): State.CLEANUP,
        (State.NAVIGATE, HandlerResult.REQUIRES_LOGIN): State.DETECT_AUTH_TYPE,
        (State.NAVIGATE, HandlerResult.REQUIRES_VERIFICATION): State.VERIFY_EMAIL,
        (State.NAVIGATE, HandlerResult.FAILED_DEAD_PAGE): State.CLEANUP,
        (State.NAVIGATE, HandlerResult.FAILED_ERROR): State.CLEANUP,

        # Auth-type detection: registration wall vs login wall vs existing account
        (State.DETECT_AUTH_TYPE, HandlerResult.SUCCESS): State.DETECT_STRATEGY,  # navigated to application form directly
        (State.DETECT_AUTH_TYPE, HandlerResult.REQUIRES_REGISTRATION): State.REGISTER,
        (State.DETECT_AUTH_TYPE, HandlerResult.REQUIRES_EXISTING_LOGIN): State.LOGIN_REGISTRY,
        (State.DETECT_AUTH_TYPE, HandlerResult.REQUIRES_LOGIN): State.CLEANUP,

        # Registry login: success retries job URL, failure falls through to fresh registration
        (State.LOGIN_REGISTRY, HandlerResult.SUCCESS): State.NAVIGATE,
        (State.LOGIN_REGISTRY, HandlerResult.FAILED): State.REGISTER,

        # Registration flow
        (State.REGISTER, HandlerResult.SUCCESS): State.VERIFY_REGISTRATION,
        (State.REGISTER, HandlerResult.FAILED): State.CLEANUP,

        # Post-registration verification -- on success retry the job URL
        (State.VERIFY_REGISTRATION, HandlerResult.SUCCESS): State.NAVIGATE,
        (State.VERIFY_REGISTRATION, HandlerResult.FAILED): State.CLEANUP,

        (State.ROUTE, HandlerResult.SUCCESS): State.DETECT_STRATEGY,
        (State.ROUTE, HandlerResult.FAILED): State.CLEANUP,
        (State.ROUTE, HandlerResult.FAILED_DEAD_PAGE): State.CLEANUP,
        (State.ROUTE, HandlerResult.CAPTCHA_DETECTED): State.CLEANUP,
        (State.ROUTE, HandlerResult.REQUIRES_LOGIN): State.DETECT_AUTH_TYPE,  # ATS login wall after Apply click
        (State.ROUTE, HandlerResult.FAILED_ERROR): State.CLEANUP,

        (State.DETECT_STRATEGY, HandlerResult.SUCCESS): None,

        (State.FILL_SELECTOR, HandlerResult.SUCCESS): None,
        (State.FILL_SELECTOR, HandlerResult.RETRY): State.FILL_SELECTOR,
        (State.FILL_SELECTOR, HandlerResult.FAILED): State.CLEANUP,
        (State.FILL_SELECTOR, HandlerResult.CAPTCHA_DETECTED): State.CLEANUP,
        (State.FILL_SELECTOR, HandlerResult.REQUIRES_VERIFICATION): State.VERIFY_EMAIL,

        (State.FILL_VISION, HandlerResult.SUCCESS): None,
        (State.FILL_VISION, HandlerResult.RETRY): State.FILL_VISION,
        (State.FILL_VISION, HandlerResult.FAILED): State.CLEANUP,
        (State.FILL_VISION, HandlerResult.REQUIRES_LOGIN): State.CLEANUP,
        (State.FILL_VISION, HandlerResult.ALREADY_APPLIED): State.CLEANUP,
        (State.FILL_VISION, HandlerResult.REQUIRES_VERIFICATION): State.VERIFY_EMAIL,

        (State.VERIFY, HandlerResult.SUCCESS): State.CLEANUP,
        (State.VERIFY, HandlerResult.FAILED): State.CLEANUP,

        (State.VERIFY_EMAIL, HandlerResult.SUCCESS): State.NAVIGATE,  # retry navigation after verification
        (State.VERIFY_EMAIL, HandlerResult.REQUIRES_LOGIN): State.CLEANUP,
        (State.VERIFY_EMAIL, HandlerResult.FAILED): State.CLEANUP,

        (State.SOLVE_CAPTCHA, HandlerResult.SUCCESS): None,
        (State.SOLVE_CAPTCHA, HandlerResult.FAILED): State.CLEANUP,

        (State.RECOVER_LOGIN, HandlerResult.SUCCESS): State.NAVIGATE,
        (State.RECOVER_LOGIN, HandlerResult.FAILED): State.CLEANUP,
    }

    def run(self, browser_context, job: dict, settings: dict, take_screenshot: bool) -> str:
        """Execute the full application workflow. Returns final job status string."""
        conn = get_connection()
        page = browser_context.new_page()

        try:
            from playwright_stealth import stealth_sync
            stealth_sync(page)
        except ImportError:
            pass

        # Create selector cache and element finder
        cache = SelectorCache(conn)
        cache.bootstrap_from_selectors()
        finder = ElementFinder(cache, settings)

        ctx = KernelContext(
            page=page, job=job, settings=settings, conn=conn,
            verbose=settings.get("automation", {}).get("verbose_logging", True),
            take_screenshot=take_screenshot,
            finder=finder,
        )

        state = State.SETUP
        history: list[tuple[State, StepResult]] = []

        try:
            while state not in (State.COMPLETE, State.CLEANUP):
                handler = self._get_handler(state)
                result = handler(ctx)
                history.append((state, result))
                self._log_transition(ctx, state, result)

                next_state = self._resolve_transition(state, result, ctx)
                if next_state == State.CLEANUP:
                    ctx.final_result = result
                state = next_state

            if state == State.CLEANUP:
                self._run_cleanup(ctx, history)

        except Exception as e:
            logger.exception(f"Unhandled error applying to job #{ctx.job_id or job.get('id', '?')}")
            console.print(f"  [red]Error during application: {e}[/]")
            if ctx.job_id:
                increment_retry_count(ctx.conn, ctx.job_id)
                update_job_status(ctx.conn, ctx.job_id, "failed")
                log_action(ctx.conn, "apply_error", str(e), ctx.app_id, ctx.job_id)
            if ctx.company and ctx.position:
                final_dir = move_application_dir(ctx.company, ctx.position, "failed")
                console.print(f"  [dim]Debug: {final_dir}[/]")
            ctx.final_status = "failed"
        finally:
            try:
                ctx.page.close()
            except Exception:
                pass

        return ctx.final_status

    # --- State machine internals ---

    def _get_handler(self, state: State):
        """Map state to handler method."""
        return {
            State.SETUP: self._handle_setup,
            State.NAVIGATE: self._handle_navigate,
            State.ROUTE: self._handle_route,
            State.DETECT_STRATEGY: self._handle_detect_strategy,
            State.FILL_SELECTOR: self._handle_fill_selector,
            State.FILL_VISION: self._handle_fill_vision,
            State.VERIFY: self._handle_verify,
            State.VERIFY_EMAIL: self._handle_verify_email,
            State.SOLVE_CAPTCHA: self._handle_solve_captcha,
            State.RECOVER_LOGIN: self._handle_recover_login,
            State.DETECT_AUTH_TYPE: self._handle_detect_auth_type,
            State.LOGIN_REGISTRY: self._handle_login_registry,
            State.REGISTER: self._handle_register,
            State.VERIFY_REGISTRATION: self._handle_verify_registration,
        }[state]

    def _resolve_transition(self, state: State, result: StepResult, ctx: KernelContext) -> State:
        """Resolve next state from current state + handler result."""
        key = (state, result.result)
        next_state = self.TRANSITIONS.get(key)

        if next_state is not None:
            return next_state

        # Dynamic: strategy detection -> fill strategy
        if state == State.DETECT_STRATEGY:
            return State.FILL_VISION if ctx.strategy == "vision" else State.FILL_SELECTOR

        # Dynamic: fill success -> verify if submitted, cleanup if not
        if state in (State.FILL_SELECTOR, State.FILL_VISION):
            return State.VERIFY if ctx.submitted else State.CLEANUP

        # Dynamic: CAPTCHA solved -> resume pre-captcha state
        if state == State.SOLVE_CAPTCHA:
            return ctx.pre_captcha_state or State.NAVIGATE

        logger.warning(f"No transition for ({state.value}, {result.result.value})")
        return State.CLEANUP

    def _log_transition(self, ctx: KernelContext, state: State, result: StepResult):
        """Log state transition to application_log and debug logger."""
        if ctx.app_id and ctx.job_id:
            detail = f"{state.value}->{result.result.value}"
            if result.message:
                detail += f": {result.message[:80]}"
            log_action(ctx.conn, "kernel_transition", detail, ctx.app_id, ctx.job_id)
        logger.debug(f"Kernel: {state.value} -> {result.result.value} ({result.message})")

    def _take_debug_screenshot(self, ctx: KernelContext, name: str = "debug_no_submit.png"):
        """Take a debug screenshot for diagnostics."""
        if not ctx.app_dir:
            return
        try:
            ctx.page.screenshot(path=str(ctx.app_dir / name), full_page=True)
        except Exception as e:
            logger.debug(f"Debug screenshot failed: {e}")

    # --- Handler adapters ---
    # Wrap existing handlers from the handlers package, adapting KernelContext
    # to their current signatures and updating context with results.

    def _handle_setup(self, ctx: KernelContext) -> StepResult:
        result = handle_setup(ctx.job, ctx.settings, ctx.conn)
        if result.result == HandlerResult.SUCCESS:
            ctx.url = result.metadata["url"]
            ctx.listing_url = result.metadata["listing_url"]
            ctx.app_dir = result.metadata["app_dir"]
            ctx.resume_file = result.metadata["resume_file"]
            ctx.cl_file = result.metadata["cl_file"]
            ctx.app_id = result.metadata["app_id"]
            ctx.company = result.metadata["company"]
            ctx.position = result.metadata["position"]
            ctx.job_id = result.metadata["job_id"]
        return result

    def _handle_navigate(self, ctx: KernelContext) -> StepResult:
        return handle_navigate(
            ctx.page, ctx.url, ctx.listing_url, ctx.settings,
            ctx.conn, ctx.app_id, ctx.job_id, ctx.verbose,
        )

    def _handle_route(self, ctx: KernelContext) -> StepResult:
        result = handle_route(
            ctx.page, ctx.url, ctx.listing_url, ctx.settings,
            ctx.conn, ctx.app_id, ctx.job_id, ctx.verbose,
            finder=ctx.finder,
        )
        if "page" in result.metadata:
            ctx.page = result.metadata["page"]
        if result.result == HandlerResult.SUCCESS:
            ctx.is_easy_apply = result.metadata.get("is_easy_apply_flow", False)
        return result

    def _handle_detect_strategy(self, ctx: KernelContext) -> StepResult:
        """Decide fill strategy. LLM injection point for future smart routing."""
        if ctx.is_easy_apply:
            strategy = "selector"
        elif "linkedin.com" in ctx.page.url.lower():
            strategy = "selector"
        elif ctx.settings.get("automation", {}).get("vision_agent"):
            strategy = "vision"
        else:
            strategy = "selector"
        ctx.strategy = strategy
        return StepResult(result=HandlerResult.SUCCESS, metadata={"strategy": strategy})

    def _handle_fill_selector(self, ctx: KernelContext) -> StepResult:
        result = handle_fill_selector(
            ctx.page, ctx.job, ctx.settings, ctx.resume_file, ctx.cl_file,
            ctx.is_easy_apply, ctx.conn, ctx.app_id, ctx.job_id,
            ctx.app_dir, ctx.take_screenshot,
            finder=ctx.finder,
        )
        ctx.submitted = result.metadata.get("submitted", False)
        ctx.form_answers_all = result.metadata.get("form_answers_all", {})
        return result

    def _handle_fill_vision(self, ctx: KernelContext) -> StepResult:
        result = handle_fill_vision(
            ctx.page, ctx.job, ctx.settings, ctx.resume_file, ctx.cl_file,
            ctx.conn, ctx.app_id, ctx.job_id, ctx.app_dir, ctx.take_screenshot,
            account_registry=ctx.account_registry,
        )
        if "page" in result.metadata:
            ctx.page = result.metadata["page"]
        ctx.submitted = result.metadata.get("submitted", False)
        return result

    def _handle_verify(self, ctx: KernelContext) -> StepResult:
        use_vision = ctx.strategy == "vision"
        return handle_verify(
            ctx.page, ctx.settings, ctx.app_dir, use_vision,
            ctx.conn, ctx.job_id, ctx.app_id,
        )

    def _handle_verify_email(self, ctx: KernelContext) -> StepResult:
        """Handle email verification (OTP / magic link)."""
        return handle_verification(
            ctx.page, ctx.settings, ctx.conn, ctx.app_id, ctx.job_id,
        )

    def _handle_solve_captcha(self, ctx: KernelContext) -> StepResult:
        """Attempt CAPTCHA solve. Currently inline in check_page_blockers;
        this handler exists for future decomposition."""
        if try_solve_captcha(ctx.page, ctx.settings):
            return StepResult(result=HandlerResult.SUCCESS)
        return StepResult(result=HandlerResult.FAILED, message="CAPTCHA not solved")

    def _handle_recover_login(self, ctx: KernelContext) -> StepResult:
        """Attempt login recovery."""
        recover = try_recover_login(
            ctx.page, ctx.url, ctx.listing_url,
            ctx.conn, ctx.app_id, ctx.job_id, ctx.settings,
        )
        if recover is None:
            return StepResult(result=HandlerResult.SUCCESS)
        return recover

    def _handle_detect_auth_type(self, ctx: KernelContext) -> StepResult:
        """Determine if the page is a login wall or registration wall."""
        # Lazy-init account registry when auto_register is enabled
        if ctx.settings.get("automation", {}).get("auto_register", False):
            if ctx.account_registry is None:
                try:
                    from .account_registry import AccountRegistry
                    ctx.account_registry = AccountRegistry()
                except ValueError as e:
                    logger.warning(f"AccountRegistry init failed: {e}")
        return handle_detect_auth_type(ctx.page, ctx.url, ctx.settings, ctx.account_registry)

    def _handle_login_registry(self, ctx: KernelContext) -> StepResult:
        """Log in using stored registry credentials."""
        from urllib.parse import urlparse
        domain = urlparse(ctx.page.url).hostname or ""
        return handle_login_registry(
            ctx.page, domain, ctx.settings, ctx.finder,
            ctx.account_registry, ctx.conn, ctx.app_id, ctx.job_id,
        )

    def _handle_register(self, ctx: KernelContext) -> StepResult:
        """Fill and submit the ATS registration form."""
        from urllib.parse import urlparse
        domain = urlparse(ctx.page.url).hostname or ""
        return handle_register(
            ctx.page, domain, ctx.settings, ctx.finder,
            ctx.account_registry, ctx.conn, ctx.app_id, ctx.job_id,
        )

    def _handle_verify_registration(self, ctx: KernelContext) -> StepResult:
        """Handle post-registration email verification."""
        from urllib.parse import urlparse
        domain = urlparse(ctx.page.url).hostname or ""
        return handle_verify_registration(
            ctx.page, domain, ctx.settings,
            ctx.conn, ctx.app_id, ctx.job_id, ctx.account_registry,
            company_hint=ctx.company,
        )

    # --- Cleanup ---

    def _run_cleanup(self, ctx: KernelContext, history: list[tuple[State, StepResult]]):
        """Centralized cleanup: DB writes, app dir moves, debug screenshots.

        Handles all terminal outcomes: DB writes, app dir moves, debug screenshots.
        """
        final = ctx.final_result
        if final is None:
            return

        last_state = history[-1][0] if history else None
        result_type = final.result

        # Setup failure: handler already set "skipped", no app_dir created
        if not ctx.app_dir:
            ctx.final_status = "skipped"
            return

        # SUCCESS: normal completion path (verify passed, or fill completed)
        if result_type == HandlerResult.SUCCESS:
            if not ctx.submitted:
                self._take_debug_screenshot(ctx)
            handle_cleanup(ctx.submitted, ctx.conn, ctx.job, ctx.app_id,
                           ctx.app_dir, ctx.form_answers_all, ctx.url)
            ctx.final_status = "applied" if ctx.submitted else "failed"
            return

        # ALREADY_APPLIED from vision fill
        if result_type == HandlerResult.ALREADY_APPLIED:
            update_job_status(ctx.conn, ctx.job_id, "applied")
            update_application(ctx.conn, ctx.app_id, submitted_at=datetime.now().isoformat())
            move_application_dir(ctx.company, ctx.position, "success")
            ctx.final_status = "applied"
            return

        # --- Failure cleanup ---

        # check_page_blockers already set DB status for navigate/route blockers.
        # DETECT_AUTH_TYPE falls back to REQUIRES_LOGIN when auto_register is off
        # or domain not allowed -- treat the same as a navigate blocker (no dir move).
        blocker_handled = (
            last_state in (State.NAVIGATE, State.ROUTE, State.DETECT_AUTH_TYPE)
            and result_type in (
                HandlerResult.CAPTCHA_DETECTED,
                HandlerResult.REQUIRES_LOGIN,
                HandlerResult.FAILED_ERROR,
            )
        )

        # handle_verify already set DB status on failure
        verify_handled = last_state == State.VERIFY and result_type == HandlerResult.FAILED

        # Map result to job status
        status = {
            HandlerResult.FAILED: "failed",
            HandlerResult.FAILED_DEAD_PAGE: "failed",
            HandlerResult.FAILED_ERROR: "failed",
            HandlerResult.FAILED_SELECTOR: "failed",
            HandlerResult.CAPTCHA_DETECTED: "failed_captcha",
            HandlerResult.REQUIRES_LOGIN: "needs_login",
        }.get(result_type, "failed")

        # Update DB if not already done by handler
        if not blocker_handled and not verify_handled:
            update_job_status(ctx.conn, ctx.job_id, status)

        # Determine whether to move app dir
        should_move = True

        # Navigate/route blockers: no dir move
        if blocker_handled:
            should_move = False

        # Vision fill REQUIRES_LOGIN: conditional dir move
        if last_state == State.FILL_VISION and result_type == HandlerResult.REQUIRES_LOGIN:
            should_move = bool(final.metadata.get("move_failed"))

        # Debug screenshot for verify/fill failures
        if last_state in (State.VERIFY, State.FILL_SELECTOR, State.FILL_VISION):
            self._take_debug_screenshot(ctx)

        if should_move and ctx.company and ctx.position:
            final_dir = move_application_dir(ctx.company, ctx.position, "failed")
            if last_state == State.VERIFY:
                console.print(f"  [dim]Debug: {final_dir}[/]")

        ctx.final_status = status
