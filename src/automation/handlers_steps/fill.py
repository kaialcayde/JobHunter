"""Kernel form-fill steps."""

import json
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ...core.tailoring import infer_form_answers
from ...db import log_action, update_application
from ..detection import click_apply_button, click_next_button, click_submit_button, detect_login_page
from ..forms import extract_fields, extract_form_fields, fill_fields, fill_form_fields, handle_file_uploads
from ..page_checks import force_apply_click, is_listing_page
from ..results import HandlerResult, StepResult
from ..vision_agent import run_vision_agent
from .common import _debug_dump_dom, _fill_password_fields, console, logger


def handle_fill_vision(page, job: dict, settings: dict, resume_file, cl_file,
                       conn, app_id: int, job_id: int, app_dir: Path,
                       take_screenshot: bool, account_registry=None) -> StepResult:
    """Vision path: external ATS via GPT-4o screenshots."""
    still_on_listing = is_listing_page(page)
    if still_on_listing:
        console.print("  [yellow]Still on listing page -- extracting apply URL[/]")
        clicked = click_apply_button(page)
        if not clicked:
            force_apply_click(page)

        if len(page.context.pages) > 1:
            latest = page.context.pages[-1]
            if latest != page and latest.url != "about:blank":
                old_page = page
                page = latest
                page.wait_for_load_state("domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except PlaywrightTimeoutError:
                    pass
                old_page.close()
                console.print(f"  [dim]Now on: {page.url[:80]}[/]")

        if is_listing_page(page):
            console.print("  [yellow]Could not reach application form -- skipping[/]")
            log_action(conn, "apply_failed", "Stuck on listing page, Apply button unresponsive", app_id, job_id)
            return StepResult(
                result=HandlerResult.FAILED,
                message="Stuck on listing page",
                metadata={"page": page, "submitted": False},
            )

    console.print("  [magenta]External ATS detected -- using vision agent[/]")
    log_action(conn, "vision_handoff", f"External site: {page.url[:80]}", app_id, job_id)

    if detect_login_page(page):
        console.print("  [yellow]Login/signup page detected on ATS -- marking for later[/]")
        log_action(conn, "needs_login", f"ATS requires login: {page.url[:80]}", app_id, job_id)
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            message="ATS requires login",
            metadata={"page": page, "submitted": False, "move_failed": True},
        )

    if "ashbyhq.com" in page.url.lower():
        try:
            app_tab = page.locator('a:has-text("Application"), button:has-text("Application")').first
            if app_tab.is_visible():
                app_tab.click()
                page.wait_for_timeout(1500)
                console.print("  [dim]Clicked Ashby 'Application' tab[/]")
        except Exception as e:
            logger.debug(f"Ashby Application tab click failed: {e}")

    page.evaluate("""() => {
        const input = document.querySelector(
            'input[type="text"], input[type="email"], input[type="tel"], textarea, input[type="file"]'
        );
        if (input) input.scrollIntoView({ block: 'center', behavior: 'instant' });
    }""")
    page.wait_for_timeout(300)

    try:
        dom_fields = extract_form_fields(page)
        if dom_fields:
            console.print(f"  [dim]DOM pre-fill: found {len(dom_fields)} fields[/]")
            dom_answers = infer_form_answers(dom_fields, job, settings)
            fill_form_fields(page, dom_fields, dom_answers)
            handle_file_uploads(page, resume_file, cl_file)
            try:
                is_upload_step = page.evaluate("""() => {
                    const text = (document.body?.innerText || '').toLowerCase();
                    return (text.includes('upload your resume') || text.includes('please upload') ||
                            text.includes('select your resume')) &&
                           !!document.querySelector('input[type="file"]');
                }""")
                console.print(f"  [dim]Upload-step detection: {is_upload_step}[/]")
                if is_upload_step:
                    advanced = False
                    for btn_name in ("Save and continue", "Continue", "Next"):
                        try:
                            loc = page.get_by_role("button", name=btn_name, exact=False).first
                            loc.wait_for(state="visible", timeout=3000)
                            loc.click(timeout=3000)
                            advanced = True
                            console.print(f"  [dim]Upload-step: clicked '{btn_name}' button[/]")
                            break
                        except Exception:
                            continue
                    if not advanced:
                        advanced = click_next_button(page)
                        if advanced:
                            console.print("  [dim]Upload-step: advanced via click_next_button fallback[/]")
                    if advanced:
                        try:
                            page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        page.wait_for_timeout(2000)
                        console.print("  [dim]Advanced past upload-only step via Continue[/]")
                        _fill_password_fields(page, account_registry, settings)
                        try:
                            step2_fields = extract_form_fields(page)
                            if step2_fields:
                                import re as _re2

                                _avature_we_pat = _re2.compile(r'^172-\d+-\d+$')
                                step2_fields = [f for f in step2_fields if not _avature_we_pat.match(f.get("id") or "")]
                                step2_answers = infer_form_answers(step2_fields, job, settings)
                                fill_form_fields(page, step2_fields, step2_answers)
                                handle_file_uploads(page, resume_file, cl_file)
                                console.print(f"  [dim]Post-advance DOM fill: {len(step2_fields)} fields[/]")
                        except Exception as e:
                            console.print(f"  [dim]Post-advance DOM fill skipped: {str(e)[:60]}[/]")
                    else:
                        console.print("  [dim]Upload-step advance FAILED -- vision agent will handle[/]")
            except Exception as e:
                console.print(f"  [dim]Upload-step advance error: {e}[/]")
            page.wait_for_timeout(500)
    except Exception as e:
        console.print(f"  [dim]DOM pre-fill failed: {str(e)[:60]} -- vision agent will handle[/]")

    prefilled_account_fields = _fill_password_fields(page, account_registry, settings)

    platform_filled = {}
    try:
        from ..platforms import get_platform_prefill
        from ...config.loader import load_profile

        platform_prefill_fn = get_platform_prefill(page.url)
        if platform_prefill_fn:
            profile_data = load_profile()
            platform_filled = platform_prefill_fn(page, profile_data, settings) or {}
            if platform_filled:
                page.wait_for_timeout(300)
    except Exception as e:
        console.print(f"  [dim]Platform prefill error: {str(e)[:60]}[/]")
        logger.debug(f"Platform prefill failed: {e}")

    try:
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
    except Exception:
        pass

    debug_mode = settings.get("automation", {}).get("debug_mode", False)
    if debug_mode:
        import pathlib

        debug_dir = pathlib.Path("data/logs")
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_shot = debug_dir / "debug_prefill.png"
        page.screenshot(path=str(debug_shot), full_page=True)
        console.print("\n  [bold yellow]DEBUG: DOM pre-fill complete.[/]")
        console.print(f"  [bold yellow]  Screenshot: {debug_shot}[/]")
        if platform_filled:
            console.print(f"  [bold yellow]  Platform prefill filled: {list(platform_filled.keys())}[/]")
        _debug_dump_dom(page, debug_dir, console)
        console.print("  [bold yellow]  Inspect browser, then press Enter to start vision agent...[/]")
        try:
            input()
        except EOFError:
            pass

    if take_screenshot:
        screenshot_path = app_dir / "pre_submit_screenshot.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        update_application(conn, app_id, screenshot_path=str(screenshot_path))

    initial_history = None
    if prefilled_account_fields or platform_filled:
        parts = ["PRE-FILLED BY SYSTEM (do NOT re-fill or re-upload these):"]
        if prefilled_account_fields:
            parts.append(
                "Resume file has already been uploaded. "
                "First name, last name, email address, password, and confirm password fields "
                "have already been filled by the system. "
                "Do NOT use upload_resume action. Do NOT type into name/email/password fields."
            )
        if platform_filled:
            field_names = ", ".join(str(k) for k in platform_filled.keys())
            parts.append(f"The following fields were pre-filled by DOM automation and are already complete -- do NOT re-fill them: {field_names}.")
        parts.append(
            "Scroll down to find and fill any REMAINING empty fields. "
            "When all visible fields are filled, click 'Save and continue' or 'Next' to advance."
        )
        initial_history = [" ".join(parts)]

    vision_result = run_vision_agent(
        page,
        job,
        settings,
        resume_file,
        cl_file,
        initial_history=initial_history,
        account_registry=account_registry,
    )
    if vision_result == "needs_login":
        console.print("  [yellow]Login required -- marking for later[/]")
        log_action(conn, "needs_login", f"Vision agent detected login wall: {page.url}", app_id, job_id)
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            message="Vision agent detected login wall",
            metadata={"page": page, "submitted": False},
        )
    if vision_result == "already_applied":
        console.print("  [green]Already applied to this position -- marking as applied[/]")
        log_action(conn, "already_applied", f"Previously applied: {page.url}", app_id, job_id)
        return StepResult(
            result=HandlerResult.ALREADY_APPLIED,
            message="Already applied",
            metadata={"page": page, "submitted": True},
        )
    if vision_result == "needs_verification":
        console.print("  [yellow]Email verification required -- routing to VERIFY_EMAIL[/]")
        log_action(conn, "needs_verification", f"Email verification wall: {page.url}", app_id, job_id)
        return StepResult(
            result=HandlerResult.REQUIRES_VERIFICATION,
            message="Email verification required after account creation",
            metadata={"page": page, "submitted": False},
        )

    submitted = bool(vision_result)
    return StepResult(
        result=HandlerResult.SUCCESS,
        metadata={"page": page, "submitted": submitted},
    )


def handle_fill_selector(page, job: dict, settings: dict, resume_file, cl_file,
                         is_easy_apply: bool, conn, app_id: int, job_id: int,
                         app_dir: Path, take_screenshot: bool, finder=None) -> StepResult:
    """Selector path: LinkedIn Easy Apply or vision disabled."""
    max_pages = 10
    if is_easy_apply:
        console.print("  [cyan]Easy Apply multi-step flow -- filling forms...[/]")

    form_answers_all = {}
    company = job.get("company", "Unknown")
    position = job.get("title", "Unknown")

    for page_num in range(max_pages):
        console.print(f"  [dim]Step {page_num + 1}/{max_pages}...[/]")
        logger.info(f"Form page {page_num + 1} for job #{job_id} ({company} - {position})")

        try:
            page.evaluate("() => document.readyState")
        except Exception:
            console.print("  [dim]Page context lost -- waiting for navigation to settle[/]")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
                page.evaluate("() => document.readyState")
            except Exception:
                console.print("  [yellow]Page destroyed -- cannot continue form filling[/]")
                break

        use_pw = is_easy_apply
        fields = extract_fields(page, use_playwright=use_pw)
        if use_pw and fields and not any(f.get("_locator") for f in fields):
            use_pw = False

        if not fields:
            console.print("  [yellow]No form fields found on this page.[/]")
            logger.info(f"No form fields found on page {page_num + 1}")
            break

        field_summary = [f.get("label", f.get("id", "?")) for f in fields]
        console.print(f"  Found {len(fields)} form fields")
        logger.info(f"Page {page_num + 1}: {len(fields)} fields: {field_summary}")

        try:
            answers = infer_form_answers(fields, job, settings)
        except Exception as e:
            logger.error(f"LLM form filling failed on page {page_num + 1}: {e}")
            console.print(f"  [yellow]LLM form filling failed: {e} -- using empty answers[/]")
            answers = {}
        form_answers_all.update(answers)
        logger.debug(f"Page {page_num + 1} answers: {json.dumps(answers, indent=2)}")

        fill_fields(page, fields, answers, use_playwright=use_pw)
        handle_file_uploads(page, resume_file, cl_file)

        try:
            page.evaluate("() => document.readyState")
        except Exception:
            console.print("  [dim]Page navigated during upload -- waiting for reload[/]")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            continue

        if not click_next_button(page, finder=finder):
            console.print("  [dim]No Next button -- at submit page[/]")
            break

        console.print("  [dim]Clicked Next -- loading next step...[/]")
        page.wait_for_timeout(1000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except PlaywrightTimeoutError:
            pass

    if take_screenshot:
        screenshot_path = app_dir / "pre_submit_screenshot.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        update_application(conn, app_id, screenshot_path=str(screenshot_path))

    submitted = click_submit_button(page, finder=finder)
    return StepResult(
        result=HandlerResult.SUCCESS,
        metadata={"submitted": submitted, "form_answers_all": form_answers_all},
    )
