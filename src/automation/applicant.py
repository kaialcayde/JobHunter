"""Application orchestration -- coordinates browser automation to submit job applications."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

logger = logging.getLogger(__name__)

from rich.console import Console

from ..db import (
    get_connection, get_jobs_by_status, update_job_status,
    insert_application, update_application, count_applications_today, log_action,
    increment_retry_count
)
from ..core.document import save_application_metadata
from ..config import load_settings
from ..core.tailoring import infer_form_answers
from ..utils import get_application_dir, move_application_dir, LINKEDIN_AUTH_STATE, TEMPLATES_DIR

from .detection import detect_captcha, detect_login_page, dismiss_modals, click_apply_button, click_next_button, click_submit_button
from .forms import extract_form_fields, fill_form_fields, handle_file_uploads
from .vision_agent import run_vision_agent, verify_submission

console = Console(force_terminal=True)


def _get_round_robin_jobs(conn, remaining: int, max_per_role: int, max_per_location: int) -> list[dict]:
    """Get jobs distributed evenly across roles and locations via round-robin."""
    all_jobs = get_jobs_by_status(conn, "tailored", limit=500)
    if not all_jobs:
        return []

    # Group by role and location
    by_role = {}
    by_location = {}
    for job in all_jobs:
        role = job.get("search_role", "unknown")
        loc = job.get("search_location", "unknown")
        by_role.setdefault(role, []).append(job)
        by_location.setdefault(loc, []).append(job)

    selected = []
    selected_ids = set()
    role_counts = {}
    location_counts = {}

    # Round-robin: cycle through roles, then within each role cycle locations
    roles = list(by_role.keys())
    if not roles:
        return all_jobs[:remaining]

    role_idx = 0
    stale_rounds = 0

    while len(selected) < remaining and stale_rounds < len(roles):
        role = roles[role_idx % len(roles)]
        role_idx += 1

        # Check per-role cap
        if max_per_role > 0 and role_counts.get(role, 0) >= max_per_role:
            stale_rounds += 1
            continue

        # Find next unselected job for this role
        found = False
        for job in by_role.get(role, []):
            if job["id"] in selected_ids:
                continue
            loc = job.get("search_location", "unknown")

            # Check per-location cap
            if max_per_location > 0 and location_counts.get(loc, 0) >= max_per_location:
                continue

            selected.append(job)
            selected_ids.add(job["id"])
            role_counts[role] = role_counts.get(role, 0) + 1
            location_counts[loc] = location_counts.get(loc, 0) + 1
            found = True
            stale_rounds = 0
            break

        if not found:
            stale_rounds += 1

    return selected


def apply_to_jobs():
    """Main application loop -- process jobs that have been tailored and apply."""
    settings = load_settings()
    automation = settings.get("automation", {})
    max_per_day = automation.get("max_applications_per_day", 25)
    max_per_round = automation.get("max_applications_per_round", 0)  # 0 = no round cap
    max_per_role = automation.get("max_per_role", 0)
    max_per_location = automation.get("max_per_location", 0)
    distribution = automation.get("distribution", "round_robin")
    take_screenshot = automation.get("screenshot_before_submit", True)

    conn = get_connection()

    # Check daily cap
    applied_today = count_applications_today(conn)
    if applied_today >= max_per_day:
        console.print(f"[yellow]Daily cap reached ({applied_today}/{max_per_day}). Stopping.[/]")
        conn.close()
        return

    remaining = max_per_day - applied_today

    # Apply per-round cap (overrides daily remaining if lower)
    if max_per_round > 0 and max_per_round < remaining:
        console.print(f"[dim]Per-round cap: {max_per_round} (daily remaining was {remaining})[/]")
        remaining = max_per_round

    # Get jobs based on distribution strategy
    if distribution == "round_robin":
        jobs = _get_round_robin_jobs(conn, remaining, max_per_role, max_per_location)
    else:
        jobs = get_jobs_by_status(conn, "tailored", limit=remaining)

    if not jobs:
        console.print("[yellow]No tailored jobs ready for application.[/]")
        conn.close()
        return

    # Show distribution breakdown
    role_breakdown = {}
    loc_breakdown = {}
    for j in jobs:
        r = j.get("search_role", "?")
        l = j.get("search_location", "?")
        role_breakdown[r] = role_breakdown.get(r, 0) + 1
        loc_breakdown[l] = loc_breakdown.get(l, 0) + 1

    console.print(f"\n[bold blue]Applying to {len(jobs)} jobs (daily cap: {remaining} remaining)[/]")
    console.print(f"  By role: {role_breakdown}")
    console.print(f"  By location: {loc_breakdown}")

    parallel_workers = automation.get("parallel_browsers", 1)
    # Cap workers at job count and a sane max
    parallel_workers = min(parallel_workers, len(jobs), 4)

    try:
        if parallel_workers <= 1:
            # Sequential mode -- single browser
            _run_application_batch(jobs, settings, take_screenshot, label="")
        else:
            # Parallel mode -- split jobs across worker threads, each with own browser
            batches = [[] for _ in range(parallel_workers)]
            for i, job in enumerate(jobs):
                batches[i % parallel_workers].append(job)

            console.print(f"[bold]Running {parallel_workers} parallel browsers[/]")

            with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
                futures = {}
                for idx, batch in enumerate(batches):
                    if not batch:
                        continue
                    label = f"[W{idx+1}]"
                    futures[executor.submit(
                        _run_application_batch, batch, settings, take_screenshot, label
                    )] = idx

                for future in as_completed(futures):
                    worker_idx = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        console.print(f"  [red]Worker {worker_idx+1} crashed: {e}[/]")

    except ImportError:
        console.print("[red]Playwright not installed. Run: pip install playwright && playwright install chromium[/]")

    conn.close()
    console.print("\n[bold green]Application round complete![/]")


def _run_application_batch(jobs: list[dict], settings: dict,
                           take_screenshot: bool, label: str = ""):
    """Process a batch of applications in a single browser instance.

    Each call launches its own Playwright browser, making it safe to run
    multiple batches in parallel threads.
    """
    from playwright.sync_api import sync_playwright

    headless = settings.get("automation", {}).get("headless", True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        context_kwargs = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        if LINKEDIN_AUTH_STATE.exists():
            context_kwargs["storage_state"] = str(LINKEDIN_AUTH_STATE)
            console.print(f"  [dim]{label}Loaded LinkedIn auth state[/]")

        context = browser.new_context(**context_kwargs)

        conn = get_connection()
        for i, job in enumerate(jobs):
            console.print(f"\n[bold]{label}({i+1}/{len(jobs)}) {job['title']} at {job['company']}[/]")
            try:
                _apply_to_single_job(context, job, settings, take_screenshot)
            except Exception as e:
                console.print(f"  [red]{label}Failed: {e}[/]")
                increment_retry_count(conn, job["id"])
                update_job_status(conn, job["id"], "failed")
                log_action(conn, "apply_failed", str(e), job_id=job["id"])

        # Re-save auth state to capture refreshed cookies
        try:
            context.storage_state(path=str(LINKEDIN_AUTH_STATE))
        except Exception:
            pass

        conn.close()
        browser.close()


def _apply_to_single_job(context, job: dict, settings: dict, take_screenshot: bool):
    """Attempt to apply to a single job."""
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

    # Get application directory with tailored docs
    app_dir = get_application_dir(company, position)
    resume_pdf = app_dir / "resume.pdf"
    resume_docx = app_dir / "resume.docx"
    cl_pdf = app_dir / "cover_letter.pdf"
    cl_docx = app_dir / "cover_letter.docx"

    # Use PDF if available, otherwise DOCX, otherwise base template
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

    # Create application record
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

        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)  # Let JS render

        # Check for CAPTCHA / bot detection
        if detect_captcha(page):
            console.print("  [yellow]CAPTCHA / bot verification detected -- skipping[/]")
            update_job_status(conn, job_id, "failed_captcha")
            log_action(conn, "captcha_detected", url, app_id, job_id)
            return

        # Check for login page (LinkedIn/Indeed redirect)
        if detect_login_page(page):
            if listing_url and listing_url != url:
                console.print(f"  [yellow]Login wall -- trying alternate URL: {listing_url[:60]}[/]")
                log_action(conn, "login_fallback", f"Trying {listing_url}", app_id, job_id)
                page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                if detect_login_page(page):
                    console.print("  [yellow]Alternate URL also requires login -- skipping[/]")
                    update_job_status(conn, job_id, "skipped")
                    log_action(conn, "login_page_detected", listing_url, app_id, job_id)
                    return
            else:
                console.print("  [yellow]Landed on login page -- skipping (needs direct apply URL)[/]")
                update_job_status(conn, job_id, "skipped")
                log_action(conn, "login_page_detected", url, app_id, job_id)
                return

        # Dismiss any modals (LinkedIn "Share your profile", messaging, etc.)
        dismiss_modals(page)

        # Try to find and click "Apply" button if we're on a listing page
        apply_result = click_apply_button(page)

        # Dismiss any modals that may have appeared after clicking Apply
        dismiss_modals(page)

        # If apply button opened a new tab, switch to it
        if apply_result == "new_tab" and len(page.context.pages) > 1:
            old_page = page
            page = page.context.pages[-1]
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(2000)
            old_page.close()
            console.print(f"  [dim]Now on: {page.url[:80]}[/]")

            # Check new page for CAPTCHA or login
            if detect_captcha(page):
                console.print("  [yellow]CAPTCHA on apply page -- skipping[/]")
                update_job_status(conn, job_id, "failed_captcha")
                log_action(conn, "captcha_detected", page.url, app_id, job_id)
                return
            if detect_login_page(page):
                if listing_url and listing_url != url:
                    console.print(f"  [yellow]Login wall after apply -- trying alternate: {listing_url[:60]}[/]")
                    page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                    if detect_login_page(page):
                        console.print("  [yellow]Alternate also requires login -- skipping[/]")
                        update_job_status(conn, job_id, "skipped")
                        log_action(conn, "login_page_detected", listing_url, app_id, job_id)
                        return
                else:
                    console.print("  [yellow]Redirected to login -- skipping[/]")
                    update_job_status(conn, job_id, "skipped")
                    log_action(conn, "login_page_detected", page.url, app_id, job_id)
                    return

        # Also check after clicking apply on the same page
        if detect_login_page(page):
            if listing_url and listing_url != url:
                console.print(f"  [yellow]Login wall -- trying alternate: {listing_url[:60]}[/]")
                page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                if detect_login_page(page):
                    console.print("  [yellow]Alternate also requires login -- skipping[/]")
                    update_job_status(conn, job_id, "skipped")
                    log_action(conn, "login_page_detected", listing_url, app_id, job_id)
                    return
            else:
                console.print("  [yellow]Landed on login page -- skipping[/]")
                update_job_status(conn, job_id, "skipped")
                log_action(conn, "login_page_detected", page.url, app_id, job_id)
                return

        # Decide strategy: selectors stay on LinkedIn, vision takes over on external ATS
        use_vision = settings.get("automation", {}).get("vision_agent", False)
        on_linkedin = "linkedin.com" in page.url.lower()
        form_answers_all = {}

        if use_vision and not on_linkedin:
            # -- VISION PATH: we're on an external ATS (Greenhouse, Lever, Workday, etc.)
            # Selectors can't reliably handle these -- let vision agent drive
            console.print("  [magenta]External ATS detected -- using vision agent[/]")
            log_action(conn, "vision_handoff", f"External site: {page.url[:80]}", app_id, job_id)

            # Screenshot before vision takes over
            if take_screenshot:
                screenshot_path = app_dir / "pre_submit_screenshot.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                update_application(conn, app_id, screenshot_path=str(screenshot_path))

            submitted = run_vision_agent(page, job, settings, resume_file, cl_file)
        else:
            # -- SELECTOR PATH: LinkedIn Easy Apply or vision disabled
            max_pages = 10  # safety limit

            for page_num in range(max_pages):
                console.print(f"  Processing form page {page_num + 1}...")
                logger.info(f"Form page {page_num + 1} for job {job_id} ({company} - {position})")

                # Extract form fields
                fields = extract_form_fields(page)
                if not fields:
                    console.print("  [yellow]No form fields found on this page.[/]")
                    logger.info(f"No form fields found on page {page_num + 1}")
                    break

                field_summary = [f.get("label", f.get("id", "?")) for f in fields]
                console.print(f"  Found {len(fields)} form fields")
                logger.info(f"Page {page_num + 1}: {len(fields)} fields: {field_summary}")

                # Use LLM to infer answers
                try:
                    answers = infer_form_answers(fields, job, settings)
                except Exception as e:
                    logger.error(f"LLM form filling failed on page {page_num + 1}: {e}")
                    console.print(f"  [yellow]LLM form filling failed: {e} -- using empty answers[/]")
                    answers = {}
                form_answers_all.update(answers)
                logger.debug(f"Page {page_num + 1} answers: {json.dumps(answers, indent=2)}")

                # Fill in the form
                fill_form_fields(page, fields, answers)

                # Handle file uploads
                handle_file_uploads(page, resume_file, cl_file)

                # Check for next/continue button
                if not click_next_button(page):
                    break  # No more pages -- we're at the submit page

                page.wait_for_timeout(2000)

            # Screenshot before submit
            if take_screenshot:
                screenshot_path = app_dir / "pre_submit_screenshot.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                update_application(conn, app_id, screenshot_path=str(screenshot_path))

            submitted = click_submit_button(page)

        # -- VERIFY SUBMISSION with vision (regardless of which path got us here)
        if submitted:
            page.wait_for_timeout(3000)
            confirm_path = app_dir / "confirmation_screenshot.png"
            page.screenshot(path=str(confirm_path), full_page=True)

            # Vision verification: don't trust selectors/agent blindly
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
            update_application(conn, app_id,
                               submitted_at=datetime.now().isoformat(),
                               form_answers_json=json.dumps(form_answers_all))
            log_action(conn, "applied", f"Submitted to {company}", app_id, job_id)
            save_application_metadata(company, position, job,
                                      form_answers_all)
            # Move to success folder
            final_dir = move_application_dir(company, position, "success")
            console.print(f"  [green]Successfully applied! (verified)[/]")
            console.print(f"  [dim]{final_dir}[/]")
        else:
            # Not submitted -- save debug screenshot and mark as failed
            debug_path = app_dir / "debug_no_submit.png"
            try:
                page.screenshot(path=str(debug_path), full_page=True)
            except Exception:
                pass
            increment_retry_count(conn, job_id)
            update_job_status(conn, job_id, "failed")
            log_action(conn, "apply_failed", f"Could not complete application at {url}", app_id, job_id)
            # Move to failed folder
            final_dir = move_application_dir(company, position, "failed")
            console.print(f"  [red]Application failed[/]")
            console.print(f"  [dim]Debug: {final_dir / 'debug_no_submit.png'}[/]")

    except Exception as e:
        console.print(f"  [red]Error during application: {e}[/]")
        increment_retry_count(conn, job_id)
        update_job_status(conn, job_id, "failed")
        log_action(conn, "apply_error", str(e), app_id, job_id)
        final_dir = move_application_dir(company, position, "failed")
        console.print(f"  [dim]Debug: {final_dir}[/]")
    finally:
        page.close()
