"""Application orchestration -- coordinates browser automation to submit job applications."""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

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
from ..utils import get_application_dir, move_application_dir, LINKEDIN_AUTH_STATE, SITE_AUTH_DIR, TEMPLATES_DIR, USER_AGENT

from .detection import detect_captcha, try_solve_captcha, detect_login_page, dismiss_modals, click_apply_button, click_next_button, click_submit_button
from .forms import extract_form_fields, fill_form_fields, handle_file_uploads
from .vision_agent import run_vision_agent, verify_submission

console = Console(force_terminal=True)


def _is_listing_page(page) -> bool:
    """Heuristic: check if we're still on a job listing/description page (not an application form).

    Returns True if the page looks like a listing with no form fields.
    """
    # If there are form inputs (text fields, selects, file uploads), it's likely a form
    form_inputs = page.query_selector_all(
        'input[type="text"], input[type="email"], input[type="tel"], '
        'textarea, select, input[type="file"]'
    )
    visible_inputs = [el for el in form_inputs if el.is_visible()]
    if len(visible_inputs) >= 2:
        return False  # Probably a form page

    # Check for common listing page indicators
    body_text = (page.text_content("body") or "").lower()[:5000]
    listing_signals = [
        "job description", "responsibilities", "qualifications",
        "about the role", "what you'll do", "requirements",
        "benefits", "life at", "about us",
    ]
    matches = sum(1 for s in listing_signals if s in body_text)
    return matches >= 2


def _force_apply_click(page) -> bool:
    """More aggressive attempt to navigate to the apply page.

    Strategy:
    1. Extract application URL from the page (links, scripts, onclick handlers)
    2. Intercept window.open() calls and capture the target URL
    3. Fall back to JS click on the Apply button

    Returns True if navigation happened.
    """
    # --- Strategy 1: Find the apply URL embedded in the page ---
    apply_url = page.evaluate("""() => {
        // Check all links for apply/workday/greenhouse URLs
        const links = document.querySelectorAll('a[href]');
        for (const link of links) {
            const href = link.href;
            const text = (link.textContent || '').toLowerCase().trim();
            // Match "Apply" links pointing to ATS domains
            if (text.includes('apply') && href.startsWith('http')) {
                const ats = ['myworkdayjobs.com', 'workday.com', 'greenhouse.io',
                             'lever.co', 'icims.com', 'smartrecruiters.com',
                             'ashbyhq.com', 'taleo.net', 'jobvite.com'];
                for (const domain of ats) {
                    if (href.includes(domain)) return href;
                }
                // Also return any external link from an Apply button
                if (!href.includes(window.location.hostname)) return href;
            }
        }

        // Check onclick handlers and data attributes for URLs
        const buttons = document.querySelectorAll(
            'button[onclick], a[onclick], [data-apply-url], [data-href], [data-url]'
        );
        for (const btn of buttons) {
            const text = (btn.textContent || '').toLowerCase();
            if (!text.includes('apply')) continue;
            // Check data attributes
            for (const attr of ['data-apply-url', 'data-href', 'data-url']) {
                const val = btn.getAttribute(attr);
                if (val && val.startsWith('http')) return val;
            }
            // Check onclick for URLs
            const onclick = btn.getAttribute('onclick') || '';
            const match = onclick.match(/(?:window\\.open|location\\.href|location\\.assign)\\s*\\(\\s*['"]([^'"]+)['"]/);
            if (match) return match[1];
        }

        return null;
    }""")

    if apply_url:
        console.print(f"  [dim]Found apply URL in page: {apply_url[:80]}[/]")
        page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass
        return True

    # --- Strategy 2: Intercept window.open() by overriding it, then click ---
    page.evaluate("""() => {
        window.__captured_popup_url = null;
        const origOpen = window.open;
        window.open = function(url) {
            window.__captured_popup_url = url;
            return origOpen.apply(this, arguments);
        };
    }""")

    apply_selectors = [
        'a:has-text("Apply Now")',
        'button:has-text("Apply Now")',
        'a:has-text("Apply")',
        'button:has-text("Apply")',
        '[data-testid*="apply"]',
        '.apply-button',
        '#apply-button',
    ]

    for selector in apply_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                # Try getting the href directly for <a> tags
                tag = btn.evaluate("el => el.tagName.toLowerCase()")
                if tag == "a":
                    href = btn.get_attribute("href")
                    if href and href.startswith("http"):
                        console.print(f"  [dim]Direct navigation to: {href[:80]}[/]")
                        page.goto(href, wait_until="domcontentloaded", timeout=30000)
                        return True

                # Force JS click (bypasses overlays and event issues)
                btn.evaluate("el => el.click()")
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    pass

                # Check if window.open was called
                popup_url = page.evaluate("() => window.__captured_popup_url")
                if popup_url:
                    console.print(f"  [dim]Intercepted popup URL: {popup_url[:80]}[/]")
                    page.goto(popup_url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=2000)
                    except Exception:
                        pass
                    return True

                # Check if URL changed from click
                return True
        except Exception:
            continue

    return False


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

    parallel_per_site = automation.get("parallel_browsers_per_site", 1)

    try:
        if parallel_per_site <= 1:
            # Sequential mode -- single browser, all jobs
            _run_application_batch(jobs, settings, take_screenshot, label="")
        else:
            # Parallel mode -- group jobs by site, one browser per site
            by_site = {}
            for job in jobs:
                site = job.get("site", "unknown") or "unknown"
                by_site.setdefault(site, []).append(job)

            # Cap total concurrent browsers
            max_concurrent = min(len(by_site), parallel_per_site, 4)
            console.print(f"[bold]Running up to {max_concurrent} parallel browsers ({len(by_site)} sites)[/]")
            for site, site_jobs in by_site.items():
                console.print(f"  [dim]{site}: {len(site_jobs)} jobs[/]")

            with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
                futures = {}
                for site, site_jobs in by_site.items():
                    label = f"[{site}]"
                    futures[executor.submit(
                        _run_application_batch, site_jobs, settings, take_screenshot, label
                    )] = site

                for future in as_completed(futures):
                    site = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        console.print(f"  [red]{site} worker crashed: {e}[/]")

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
            "viewport": {"width": 1280, "height": 2400},
            "user_agent": USER_AGENT,
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


def _get_site_domain(url: str) -> str:
    """Extract the main domain from a URL (e.g. 'workday.com' from 'company.wd5.myworkdayjobs.com')."""
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    # Collapse common ATS subdomains to their base
    for ats in ["myworkdayjobs.com", "workday.com", "greenhouse.io", "lever.co",
                "icims.com", "taleo.net", "smartrecruiters.com", "ashbyhq.com"]:
        if hostname.endswith(ats):
            return ats
    parts = hostname.rsplit(".", 2)
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def _get_site_auth_path(url: str) -> Path:
    """Get the cookie storage path for a given site URL."""
    domain = _get_site_domain(url)
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', domain)
    return SITE_AUTH_DIR / f"{safe}.json"


def _try_recover_login(page, original_url: str, listing_url: str, conn, app_id, job_id) -> bool:
    """Try to recover from a login page. Returns True if recovered.

    Strategy:
    1. LinkedIn with stored cookies: warm up session via /feed/, then retry
    2. Other sites with stored cookies: load cookies, retry
    3. No stored cookies: open visible browser, prompt user to log in, save cookies
    """
    current_url = page.url

    # --- LinkedIn recovery ---
    if "linkedin.com" in current_url.lower() and LINKEDIN_AUTH_STATE.exists():
        console.print("  [yellow]Login redirect -- warming up LinkedIn session...[/]")
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

        if not detect_login_page(page):
            # Session is active -- retry the job URL
            job_url = original_url
            li_match = re.search(r'(?:jobs/view/|currentJobId=|jobId=)(\d+)', original_url) or \
                       re.search(r'(?:jobs/view/|currentJobId=|jobId=)(\d+)', listing_url or "")
            if li_match:
                job_url = f"https://www.linkedin.com/jobs/view/{li_match.group(1)}/"
            console.print(f"  [dim]Session active -- retrying: {job_url[:70]}[/]")
            log_action(conn, "login_fallback", f"Retrying after session warmup: {job_url}", app_id, job_id)
            page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            if not detect_login_page(page):
                return True

    # --- Generic site recovery: check for stored cookies ---
    site_auth = _get_site_auth_path(current_url)
    if site_auth.exists():
        console.print(f"  [dim]Found stored cookies for {_get_site_domain(current_url)}, retrying...[/]")
        page.context.add_cookies(json.loads(site_auth.read_text()))
        page.goto(original_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        if not detect_login_page(page):
            return True
        console.print(f"  [yellow]Stored cookies expired for {_get_site_domain(current_url)}[/]")

    # --- Alternate URL fallback ---
    if listing_url and listing_url != original_url:
        console.print(f"  [yellow]Login wall -- trying alternate URL: {listing_url[:60]}[/]")
        log_action(conn, "login_fallback", f"Trying {listing_url}", app_id, job_id)
        page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        if not detect_login_page(page):
            return True

    # --- Auto-skip: never block the pipeline on manual input ---
    domain = _get_site_domain(current_url)
    console.print(f"  [yellow]Login required for {domain} -- auto-skipping[/]")
    return "needs_login"


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

        verbose = settings.get("automation", {}).get("verbose_logging", True)

        if verbose:
            console.print(f"  [dim]Loading: {url[:80]}[/]")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Wait for JS to finish rendering (up to 3s, but returns early if idle)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass  # Timed out waiting for idle -- page is usable enough
        if verbose:
            console.print(f"  [dim]Page loaded: {page.url[:80]}[/]")

        # Check for CAPTCHA / bot detection
        if detect_captcha(page):
            if verbose:
                console.print("  [dim]CAPTCHA detected, attempting solve...[/]")
            if not try_solve_captcha(page, settings):
                console.print("  [yellow]CAPTCHA / bot verification detected -- skipping[/]")
                update_job_status(conn, job_id, "failed_captcha")
                log_action(conn, "captcha_detected", url, app_id, job_id)
                return

        # Check for login page (LinkedIn/Indeed redirect)
        if detect_login_page(page):
            if verbose:
                console.print("  [dim]Login page detected[/]")
            result = _try_recover_login(page, url, listing_url, conn, app_id, job_id)
            if result == "needs_login":
                update_job_status(conn, job_id, "needs_login")
                log_action(conn, "needs_login", f"Login required: {page.url}", app_id, job_id)
                return
            elif not result:
                console.print(f"  [yellow]Could not bypass login -- skipping: {page.url[:80]}[/]")
                update_job_status(conn, job_id, "skipped")
                log_action(conn, "login_page_detected", url, app_id, job_id)
                return

        # Dismiss any modals (LinkedIn "Share your profile", messaging, etc.)
        dismiss_modals(page)

        # Try to find and click "Apply" button if we're on a listing page
        if verbose:
            console.print("  [dim]Looking for Apply button...[/]")
        url_before_apply = page.url
        apply_result = click_apply_button(page)
        if verbose:
            console.print(f"  [dim]Apply button result: {apply_result}[/]")

        # Dismiss any modals that may have appeared after clicking Apply
        dismiss_modals(page)

        # If apply button opened a new tab, switch to it
        if apply_result == "new_tab" and len(page.context.pages) > 1:
            old_page = page
            page = page.context.pages[-1]
            page.wait_for_load_state("domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            old_page.close()
            console.print(f"  [dim]Now on: {page.url[:80]}[/]")

            # Check new page for CAPTCHA or login
            if detect_captcha(page):
                if not try_solve_captcha(page, settings):
                    console.print("  [yellow]CAPTCHA on apply page -- skipping[/]")
                    update_job_status(conn, job_id, "failed_captcha")
                    log_action(conn, "captcha_detected", page.url, app_id, job_id)
                    return
            if detect_login_page(page):
                result = _try_recover_login(page, url, listing_url, conn, app_id, job_id)
                if result == "needs_login":
                    update_job_status(conn, job_id, "needs_login")
                    log_action(conn, "needs_login", f"Login required: {page.url}", app_id, job_id)
                    return
                elif not result:
                    console.print(f"  [yellow]Could not bypass login after apply -- skipping: {page.url[:80]}[/]")
                    update_job_status(conn, job_id, "skipped")
                    log_action(conn, "login_page_detected", page.url, app_id, job_id)
                    return

        # Also check after clicking apply on the same page
        if detect_login_page(page):
            result = _try_recover_login(page, url, listing_url, conn, app_id, job_id)
            if result == "needs_login":
                update_job_status(conn, job_id, "needs_login")
                log_action(conn, "needs_login", f"Login required: {page.url}", app_id, job_id)
                return
            elif not result:
                console.print(f"  [yellow]Could not bypass login -- skipping: {page.url[:80]}[/]")
                update_job_status(conn, job_id, "skipped")
                log_action(conn, "login_page_detected", page.url, app_id, job_id)
                return

        # Decide strategy: selectors stay on LinkedIn, vision takes over on external ATS
        use_vision = settings.get("automation", {}).get("vision_agent", False)
        on_linkedin = "linkedin.com" in page.url.lower()
        form_answers_all = {}

        if use_vision and not on_linkedin:
            # -- VISION PATH: we're on an external ATS (Greenhouse, Lever, Workday, etc.)

            # Check if we're still on the listing page (Apply button didn't navigate)
            still_on_listing = _is_listing_page(page)
            if still_on_listing and page.url == url_before_apply:
                console.print("  [yellow]Still on listing page -- extracting apply URL[/]")
                navigated = _force_apply_click(page)

                # Check for popup
                if len(page.context.pages) > 1:
                    latest = page.context.pages[-1]
                    if latest != page and latest.url != "about:blank":
                        old_page = page
                        page = latest
                        page.wait_for_load_state("domcontentloaded")
                        try:
                            page.wait_for_load_state("networkidle", timeout=2000)
                        except Exception:
                            pass
                        old_page.close()
                        console.print(f"  [dim]Now on: {page.url[:80]}[/]")

                # If still stuck on the listing page, skip -- vision agent can't help here
                if _is_listing_page(page) and page.url == url_before_apply:
                    console.print("  [yellow]Could not reach application form -- skipping[/]")
                    update_job_status(conn, job_id, "failed")
                    log_action(conn, "apply_failed", "Stuck on listing page, Apply button unresponsive", app_id, job_id)
                    final_dir = move_application_dir(company, position, "failed")
                    return

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

                # Check if page context is still alive (uploads can trigger navigation)
                try:
                    page.evaluate("() => document.readyState")
                except Exception:
                    console.print("  [dim]Page navigated during upload -- waiting for new page[/]")
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    continue

                # Check for next/continue button
                if not click_next_button(page):
                    break  # No more pages -- we're at the submit page

                # Wait for next page to render (use smart wait instead of fixed sleep)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass

            # Screenshot before submit
            if take_screenshot:
                screenshot_path = app_dir / "pre_submit_screenshot.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                update_application(conn, app_id, screenshot_path=str(screenshot_path))

            submitted = click_submit_button(page)

        # -- VERIFY SUBMISSION with vision (regardless of which path got us here)
        if submitted:
            # Wait for submission to process (page may redirect/update)
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
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
