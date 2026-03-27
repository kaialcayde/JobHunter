"""Single-job application state machine.

Handles the full lifecycle of applying to one job:
Setup -> Navigate -> Route (LinkedIn vs External) -> Fill -> Verify -> Cleanup
"""

import json
import logging
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from rich.console import Console

from ..db import (
    get_connection, update_job_status,
    insert_application, update_application, log_action,
    increment_retry_count
)
from ..core.document import save_application_metadata
from ..core.tailoring import infer_form_answers
from ..utils import get_application_dir, move_application_dir, TEMPLATES_DIR

from .detection import (
    detect_login_page, dismiss_modals, click_apply_button,
    click_next_button, click_submit_button
)
from .forms import extract_fields, fill_fields, extract_form_fields, fill_form_fields, handle_file_uploads
from .vision_agent import run_vision_agent, verify_submission
from .page_checks import (
    is_dead_page, is_listing_page, force_apply_click, check_page_blockers
)

logger = logging.getLogger(__name__)

console = Console(force_terminal=True)


def apply_single_job(context, job: dict, settings: dict, take_screenshot: bool):
    """Attempt to apply to a single job.

    This is the main state machine that drives a single application attempt:
    1. Setup: resolve URLs, create application record, open page
    2. Navigate: load page, check for blockers
    3. Route: click Apply, handle LinkedIn vs external
    4. Fill: extract fields, infer answers, fill forms
    5. Verify: screenshot, submit, verify with vision
    6. Cleanup: update DB status, move files
    """
    conn = get_connection()
    job_id = job["id"]
    url = job.get("url", "")
    listing_url = job.get("listing_url", "")

    # Prefer non-LinkedIn URL when both are available
    if url and "linkedin.com" in url.lower() and listing_url and "linkedin.com" not in listing_url.lower():
        url, listing_url = listing_url, url
    elif not url and listing_url:
        url = listing_url
        listing_url = ""

    if not url:
        console.print("  [yellow]No application URL -- skipping[/]")
        update_job_status(conn, job_id, "skipped")
        return

    update_job_status(conn, job_id, "applying")
    company = job.get("company", "Unknown")
    position = job.get("title", "Unknown")

    # --- Setup: resolve document paths ---
    app_dir = get_application_dir(company, position)
    resume_pdf = app_dir / "resume.pdf"
    resume_docx = app_dir / "resume.docx"
    cl_pdf = app_dir / "cover_letter.pdf"
    cl_docx = app_dir / "cover_letter.docx"

    resume_file = resume_pdf if resume_pdf.exists() else resume_docx if resume_docx.exists() else None
    cl_file = cl_pdf if cl_pdf.exists() else cl_docx if cl_docx.exists() else None

    if resume_file is None:
        base_resume = TEMPLATES_DIR / "base_resume.docx"
        if base_resume.exists():
            resume_file = base_resume
            console.print("  [dim]Using base resume template (no tailored version)[/]")
    if cl_file is None:
        base_cl = TEMPLATES_DIR / "base_cover_letter.docx"
        if base_cl.exists():
            cl_file = base_cl
            console.print("  [dim]Using base cover letter template (no tailored version)[/]")

    app_id = insert_application(conn, job_id,
                                str(resume_file) if resume_file else None,
                                str(cl_file) if cl_file else None)
    log_action(conn, "apply_started", f"URL: {url}", app_id, job_id)

    page = context.new_page()
    try:
        # Apply stealth patches to avoid bot detection
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(page)
        except ImportError:
            pass

        verbose = settings.get("automation", {}).get("verbose_logging", True)

        # --- Navigate: load page ---
        if verbose:
            console.print(f"  [dim]Loading: {url[:80]}[/]")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except PlaywrightTimeoutError:
            pass
        if verbose:
            console.print(f"  [dim]Page loaded: {page.url[:80]}[/]")

        # Check for blockers on initial page load
        block = check_page_blockers(page, url, listing_url, settings, conn, app_id, job_id, verbose)
        if block:
            return

        # --- Route: find and click Apply button ---
        dismiss_modals(page)
        if verbose:
            console.print("  [dim]Looking for Apply button...[/]")
        apply_result = click_apply_button(page)

        if not apply_result:
            dismiss_modals(page)
            page.wait_for_timeout(500)
            apply_result = click_apply_button(page)

        if not apply_result:
            console.print("  [dim]Apply button not found -- trying URL extraction...[/]")
            if force_apply_click(page):
                apply_result = True
                if len(page.context.pages) > 1:
                    latest = page.context.pages[-1]
                    if latest != page and latest.url != "about:blank":
                        apply_result = "new_tab"

        if verbose:
            console.print(f"  [dim]Apply button result: {apply_result}[/]")

        # Switch to new tab if apply opened one
        if apply_result == "new_tab" and len(page.context.pages) > 1:
            old_page = page
            page = page.context.pages[-1]
            page.wait_for_load_state("domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=2000)
            except PlaywrightTimeoutError:
                pass
            old_page.close()
            console.print(f"  [dim]Now on: {page.url[:80]}[/]")

        # Check for blockers after navigation
        block = check_page_blockers(page, url, listing_url, settings, conn, app_id, job_id, verbose)
        if block:
            return

        # Detect dead/empty LinkedIn pages
        if is_dead_page(page):
            if listing_url and listing_url != url and "linkedin.com" not in listing_url.lower():
                console.print(f"  [dim]LinkedIn dead page -- trying direct URL: {listing_url[:60]}[/]")
                page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except PlaywrightTimeoutError:
                    pass
            else:
                console.print("  [yellow]Landed on empty/dead LinkedIn page -- job may be expired[/]")
                update_job_status(conn, job_id, "failed")
                log_action(conn, "apply_failed", f"Dead page after apply: {page.url[:80]}", app_id, job_id)
                move_application_dir(company, position, "failed")
                return

        # Handle LinkedIn post-apply flow
        from .platforms.linkedin import handle_linkedin_post_apply
        linkedin_result = handle_linkedin_post_apply(page, apply_result, listing_url)
        if linkedin_result == "failed":
            update_job_status(conn, job_id, "failed")
            log_action(conn, "apply_failed",
                       f"Stuck on LinkedIn, no Easy Apply modal. "
                       f"apply_result={apply_result}, listing_url={listing_url}",
                       app_id, job_id)
            move_application_dir(company, position, "failed")
            return

        # --- Decide strategy ---
        use_vision = settings.get("automation", {}).get("vision_agent", False)
        on_linkedin = "linkedin.com" in page.url.lower()
        is_easy_apply_flow = (apply_result == "easy_apply" or linkedin_result == "easy_apply")
        form_answers_all = {}

        if use_vision and not on_linkedin:
            # -- VISION PATH: external ATS (Greenhouse, Lever, Workday, etc.) --

            still_on_listing = is_listing_page(page)
            if still_on_listing:
                console.print("  [yellow]Still on listing page -- extracting apply URL[/]")
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
                    update_job_status(conn, job_id, "failed")
                    log_action(conn, "apply_failed", "Stuck on listing page, Apply button unresponsive", app_id, job_id)
                    move_application_dir(company, position, "failed")
                    return

            console.print("  [magenta]External ATS detected -- using vision agent[/]")
            log_action(conn, "vision_handoff", f"External site: {page.url[:80]}", app_id, job_id)

            if detect_login_page(page):
                console.print("  [yellow]Login/signup page detected on ATS -- marking for later[/]")
                update_job_status(conn, job_id, "needs_login")
                log_action(conn, "needs_login", f"ATS requires login: {page.url[:80]}", app_id, job_id)
                move_application_dir(company, position, "failed")
                return

            # Ashby: form is behind an "Application" tab
            if "ashbyhq.com" in page.url.lower():
                try:
                    app_tab = page.locator('a:has-text("Application"), button:has-text("Application")').first
                    if app_tab.is_visible():
                        app_tab.click()
                        page.wait_for_timeout(1500)
                        console.print("  [dim]Clicked Ashby 'Application' tab[/]")
                except Exception as e:
                    logger.debug(f"Ashby Application tab click failed: {e}")

            # Scroll to first form field
            page.evaluate("""() => {
                const input = document.querySelector(
                    'input[type="text"], input[type="email"], input[type="tel"], textarea, input[type="file"]'
                );
                if (input) input.scrollIntoView({ block: 'center', behavior: 'instant' });
            }""")
            page.wait_for_timeout(300)

            # Pre-fill via DOM selectors before vision agent
            try:
                dom_fields = extract_form_fields(page)
                if dom_fields:
                    console.print(f"  [dim]DOM pre-fill: found {len(dom_fields)} fields[/]")
                    dom_answers = infer_form_answers(dom_fields, job, settings)
                    fill_form_fields(page, dom_fields, dom_answers)
                    handle_file_uploads(page, resume_file, cl_file)
                    page.wait_for_timeout(500)
            except Exception as e:
                console.print(f"  [dim]DOM pre-fill failed: {str(e)[:60]} -- vision agent will handle[/]")

            if take_screenshot:
                screenshot_path = app_dir / "pre_submit_screenshot.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                update_application(conn, app_id, screenshot_path=str(screenshot_path))

            vision_result = run_vision_agent(page, job, settings, resume_file, cl_file)
            if vision_result == "needs_login":
                console.print(f"  [yellow]Login required -- marking for later[/]")
                update_job_status(conn, job_id, "needs_login")
                log_action(conn, "needs_login", f"Vision agent detected login wall: {page.url}", app_id, job_id)
                return
            if vision_result == "already_applied":
                console.print(f"  [green]Already applied to this position -- marking as applied[/]")
                update_job_status(conn, job_id, "applied")
                update_application(conn, app_id, submitted_at=datetime.now().isoformat())
                log_action(conn, "already_applied", f"Previously applied: {page.url}", app_id, job_id)
                move_application_dir(company, position, "success")
                return
            submitted = bool(vision_result)
        else:
            # -- SELECTOR PATH: LinkedIn Easy Apply or vision disabled --
            max_pages = 10
            if is_easy_apply_flow:
                console.print("  [cyan]Easy Apply multi-step flow -- filling forms...[/]")

            for page_num in range(max_pages):
                console.print(f"  [dim]Step {page_num + 1}/{max_pages}...[/]")
                logger.info(f"Form page {page_num + 1} for job #{job_id} ({company} - {position})")

                # Guard: check if page context is alive
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

                # Extract form fields
                use_pw = is_easy_apply_flow
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

                # Infer answers via LLM
                try:
                    answers = infer_form_answers(fields, job, settings)
                except Exception as e:
                    logger.error(f"LLM form filling failed on page {page_num + 1}: {e}")
                    console.print(f"  [yellow]LLM form filling failed: {e} -- using empty answers[/]")
                    answers = {}
                form_answers_all.update(answers)
                logger.debug(f"Page {page_num + 1} answers: {json.dumps(answers, indent=2)}")

                # Fill form
                fill_fields(page, fields, answers, use_playwright=use_pw)
                handle_file_uploads(page, resume_file, cl_file)

                # Check if page context survived
                try:
                    page.evaluate("() => document.readyState")
                except Exception:
                    console.print("  [dim]Page navigated during upload -- waiting for reload[/]")
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except PlaywrightTimeoutError:
                        pass
                    continue

                # Next/continue button
                if not click_next_button(page):
                    console.print("  [dim]No Next button -- at submit page[/]")
                    break

                console.print("  [dim]Clicked Next -- loading next step...[/]")
                page.wait_for_timeout(1000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=3000)
                except PlaywrightTimeoutError:
                    pass

            # Screenshot before submit
            if take_screenshot:
                screenshot_path = app_dir / "pre_submit_screenshot.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                update_application(conn, app_id, screenshot_path=str(screenshot_path))

            submitted = click_submit_button(page)

        # --- Verify and record result ---
        if submitted:
            try:
                page.wait_for_load_state("networkidle", timeout=2000)
            except PlaywrightTimeoutError:
                pass
            confirm_path = app_dir / "confirmation_screenshot.png"
            page.screenshot(path=str(confirm_path), full_page=True)

            if use_vision:
                console.print("  [dim]Verifying submission with vision...[/]")
                actually_submitted = verify_submission(page, settings)
                if not actually_submitted:
                    console.print("  [yellow]Vision check: NOT actually submitted -- marking as failed[/]")
                    logger.warning(f"Vision verification rejected submission for {company} - {position}")
                    increment_retry_count(conn, job_id)
                    update_job_status(conn, job_id, "failed")
                    log_action(conn, "false_submission", "Vision verification rejected confirmation", app_id, job_id)
                    submitted = False

        if submitted:
            update_job_status(conn, job_id, "applied")
            answers_json = json.dumps(form_answers_all) if form_answers_all else None
            update_application(conn, app_id,
                               submitted_at=datetime.now().isoformat(),
                               form_answers_json=answers_json)
            log_action(conn, "applied", f"Submitted to {company}", app_id, job_id)
            if form_answers_all:
                log_action(conn, "form_answers", answers_json, app_id, job_id)
                console.print(f"  [dim]Stored {len(form_answers_all)} form answers in DB[/]")
            save_application_metadata(company, position, job, form_answers_all)
            final_dir = move_application_dir(company, position, "success")
            console.print(f"  [green]Successfully applied! (verified)[/]")
            console.print(f"  [dim]{final_dir}[/]")
        else:
            debug_path = app_dir / "debug_no_submit.png"
            try:
                page.screenshot(path=str(debug_path), full_page=True)
            except Exception as e:
                logger.debug(f"Debug screenshot failed: {e}")
            increment_retry_count(conn, job_id)
            update_job_status(conn, job_id, "failed")
            log_action(conn, "apply_failed", f"Could not complete application at {url}", app_id, job_id)
            final_dir = move_application_dir(company, position, "failed")
            console.print(f"  [red]Application failed[/]")
            console.print(f"  [dim]Debug: {final_dir / 'debug_no_submit.png'}[/]")

    except Exception as e:
        logger.exception(f"Unhandled error applying to job #{job_id}")
        console.print(f"  [red]Error during application: {e}[/]")
        increment_retry_count(conn, job_id)
        update_job_status(conn, job_id, "failed")
        log_action(conn, "apply_error", str(e), app_id, job_id)
        final_dir = move_application_dir(company, position, "failed")
        console.print(f"  [dim]Debug: {final_dir}[/]")
    finally:
        page.close()
