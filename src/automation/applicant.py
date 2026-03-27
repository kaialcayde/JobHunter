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
from .forms import extract_form_fields, extract_form_fields_playwright, fill_form_fields, fill_form_fields_playwright, handle_file_uploads
from .vision_agent import run_vision_agent, verify_submission

console = Console(force_terminal=True)


def _is_dead_page(page) -> bool:
    """Detect if we've landed on a dead/empty LinkedIn page (footer page, expired listing).

    Only flags LinkedIn pages — external ATS sites (Ashby, Greenhouse, etc.) are SPAs
    that may have minimal text initially while JS renders, so we never flag those.
    """
    url = page.url.lower()
    if "linkedin.com" not in url:
        return False  # Never flag external ATS pages as dead

    return page.evaluate("""() => {
        const body = document.body;
        if (!body) return true;

        // LinkedIn footer-only page: no main content area, just nav + footer links
        const main = document.querySelector(
            'main, .scaffold-layout__main, .jobs-search__job-details, ' +
            '.jobs-unified-top-card, .job-view-layout'
        );
        if (main && main.innerText.trim().length > 50) return false;

        // Check total visible text — LinkedIn footer pages have < 300 chars
        const cleaned = (body.innerText || '').replace(/\\s+/g, ' ').trim();
        if (cleaned.length < 300) return true;

        return false;
    }""")


def _is_listing_page(page) -> bool:
    """Heuristic: check if we're still on a job listing/description page (not an application form).

    Returns True if the page looks like a listing with no form fields.
    """
    url = page.url.lower()

    # Greenhouse and similar ATS put job description AND form on the same page.
    # The form is below the fold but it's there — not a listing-only page.
    if any(pattern in url for pattern in [
        "boards.greenhouse.io", "job-boards.greenhouse.io", "grnh.se",
        "/application", "/apply",
    ]):
        return False

    # Check for APPLICATION form inputs (not search/filter fields common on listing pages)
    form_count = page.evaluate("""() => {
        const inputs = document.querySelectorAll(
            'input[type="text"], input[type="email"], input[type="tel"], ' +
            'textarea, select, input[type="file"]'
        );
        let count = 0;
        for (const el of inputs) {
            // Check DOM presence with non-zero dimensions (not viewport visibility)
            if (el.offsetWidth === 0 && el.offsetHeight === 0 && el.getClientRects().length === 0) continue;
            // Exclude search/filter inputs (common on listing pages)
            const name = (el.name || '').toLowerCase();
            const id = (el.id || '').toLowerCase();
            const placeholder = (el.placeholder || '').toLowerCase();
            const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
            const allAttrs = name + ' ' + id + ' ' + placeholder + ' ' + ariaLabel;
            if (allAttrs.match(/search|filter|keyword|location|sort|query/)) continue;
            count++;
        }
        return count;
    }""")
    if form_count >= 2:
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
                             'ashbyhq.com', 'taleo.net', 'jobvite.com',
                             'adp.com', 'ultipro.com'];
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


def _get_round_robin_jobs(conn, remaining: int, max_per_role: int, max_per_location: int, status: str = "tailored") -> list[dict]:
    """Get jobs distributed evenly across roles and locations via round-robin."""
    all_jobs = get_jobs_by_status(conn, status, limit=500)
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

    # When tailoring is disabled, apply directly to "new" jobs using base templates
    tailoring_enabled = settings.get("tailoring", {}).get("enabled", True)
    job_status = "tailored" if tailoring_enabled else "new"

    # Get jobs based on distribution strategy
    if distribution == "round_robin":
        jobs = _get_round_robin_jobs(conn, remaining, max_per_role, max_per_location, status=job_status)
    else:
        jobs = get_jobs_by_status(conn, job_status, limit=remaining)

    if not jobs:
        console.print(f"[yellow]No {job_status} jobs ready for application.[/]")
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


def apply_to_single_job_by_id(job_id: int):
    """Apply to a specific job by database ID. Used for testing/debugging."""
    settings = load_settings()
    take_screenshot = settings.get("automation", {}).get("screenshot_before_submit", True)

    conn = get_connection()
    from ..db import get_job_by_id
    job = get_job_by_id(conn, job_id)
    if not job:
        console.print(f"[red]Job ID {job_id} not found in database.[/]")
        conn.close()
        return

    console.print(f"\n[bold blue]Applying to job #{job_id}:[/]")
    console.print(f"  Title: {job.get('title', '?')}")
    console.print(f"  Company: {job.get('company', '?')}")
    console.print(f"  URL: {job.get('url', '?')[:80]}")
    console.print(f"  Status: {job.get('status', '?')}")

    # Force status to 'tailored' so the application logic proceeds
    current_status = job.get("status", "")
    if current_status not in ("tailored", "new"):
        console.print(f"  [yellow]Resetting status from '{current_status}' to 'tailored'[/]")
        update_job_status(conn, job_id, "tailored")
        job["status"] = "tailored"

    conn.close()

    try:
        _run_application_batch([job], settings, take_screenshot, label=f"[test-{job_id}]")
    except ImportError:
        console.print("[red]Playwright not installed. Run: pip install playwright && playwright install chromium[/]")

    console.print(f"\n[bold]Done. Check job #{job_id} status with: python -m src list[/]")


def _run_application_batch(jobs: list[dict], settings: dict,
                           take_screenshot: bool, label: str = ""):
    """Process a batch of applications in a single browser instance.

    Each call launches its own Playwright browser, making it safe to run
    multiple batches in parallel threads.
    """
    from playwright.sync_api import sync_playwright

    auto = settings.get("automation", {})
    headless = auto.get("headless", True)
    # Force visible browser only for manual_login (needs browser interaction)
    # OTP and verification prompts are terminal-only, no need for visible browser
    if auto.get("manual_login"):
        headless = False

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
            console.print(f"\n[bold]{label}({i+1}/{len(jobs)}) [Job #{job['id']}] {job['title']} at {job['company']}[/]")
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


def _try_recover_login(page, original_url: str, listing_url: str, conn, app_id, job_id, settings=None) -> bool:
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
            page.wait_for_load_state("networkidle", timeout=2000)
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
                page.wait_for_load_state("networkidle", timeout=2000)
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
            page.wait_for_load_state("networkidle", timeout=2000)
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
            page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass
        if not detect_login_page(page):
            return True

    # --- Manual login: pause for user if enabled ---
    domain = _get_site_domain(current_url)
    manual_login = settings.get("automation", {}).get("manual_login", False) if settings else False
    if manual_login:
        console.print(f"  [bold yellow]Login required for {domain}! Browser is open for manual login.[/]")
        try:
            input(f"  Log in to {domain} in the browser, then press Enter to continue: ")
        except EOFError:
            pass
        page.wait_for_timeout(1000)
        if not detect_login_page(page):
            console.print(f"  [green]Login successful for {domain}![/]")
            # Save cookies for future use
            import json
            site_auth = _get_site_auth_path(current_url)
            site_auth.parent.mkdir(parents=True, exist_ok=True)
            cookies = page.context.cookies()
            site_auth.write_text(json.dumps(cookies, indent=2))
            console.print(f"  [dim]Cookies saved for {domain}[/]")
            return True
        console.print(f"  [yellow]Still on login page after manual login attempt[/]")

    console.print(f"  [yellow]Login required for {domain} -- auto-skipping[/]")
    return "needs_login"


def _is_access_denied(page) -> bool:
    """Detect 'Access Denied' or similar bot-block pages that aren't CAPTCHA/login."""
    try:
        return page.evaluate("""() => {
            const text = (document.body?.innerText || '').slice(0, 3000).toLowerCase();
            const title = (document.title || '').toLowerCase();
            const denied = ['access denied', 'access to this page has been denied',
                            '403 forbidden', 'you don\\'t have permission',
                            'request blocked', 'this page is not available',
                            'there has been a critical error', '500 internal server error',
                            'this site is experiencing technical difficulties'];
            for (const d of denied) {
                if (text.includes(d) || title.includes(d)) return true;
            }
            return false;
        }""")
    except Exception:
        return False


def _check_page_blockers(page, url, listing_url, settings, conn, app_id, job_id, verbose) -> bool:
    """Check for CAPTCHA, login walls, or access-denied pages. Returns True if blocked."""
    # Wait briefly for client-side redirects (e.g. Amazon Jobs -> signin)
    try:
        page.wait_for_load_state("networkidle", timeout=2000)
    except Exception:
        pass

    # Fast check: access denied / 403 pages (no point trying CAPTCHA or vision)
    if _is_access_denied(page):
        console.print("  [yellow]Access denied / blocked by site -- skipping[/]")
        update_job_status(conn, job_id, "failed")
        log_action(conn, "access_denied", f"Blocked: {page.url[:80]}", app_id, job_id)
        return True

    if detect_captcha(page):
        if verbose:
            console.print("  [dim]CAPTCHA detected, attempting solve...[/]")
        # SPA pages (Ashby, Gem) may trigger passive CAPTCHA before form hydrates.
        # Wait briefly and re-check -- if form content appears, the trigger clears.
        page.wait_for_timeout(2000)
        if not detect_captcha(page):
            if verbose:
                console.print("  [dim]CAPTCHA cleared after SPA hydration[/]")
        elif not try_solve_captcha(page, settings):
            manual_verification = settings.get("automation", {}).get("manual_verification", False)
            if manual_verification:
                console.print("  [bold yellow]Verification challenge detected! Browser is open for manual solving.[/]")
                try:
                    input("  Solve the CAPTCHA/challenge in the browser, then press Enter to continue: ")
                except EOFError:
                    pass
                page.wait_for_timeout(1000)
                if not detect_captcha(page):
                    console.print("  [green]Challenge solved manually![/]")
                    return False  # continue processing
            console.print("  [yellow]CAPTCHA / bot verification detected -- skipping[/]")
            try:
                page.screenshot(path="data/logs/debug_captcha_blocked.png")
            except Exception:
                pass
            update_job_status(conn, job_id, "failed_captcha")
            log_action(conn, "captcha_detected", url, app_id, job_id)
            return True

    if detect_login_page(page):
        if verbose:
            console.print("  [dim]Login page detected[/]")
        result = _try_recover_login(page, url, listing_url, conn, app_id, job_id, settings)
        if result == "needs_login":
            update_job_status(conn, job_id, "needs_login")
            log_action(conn, "needs_login", f"Login required: {page.url}", app_id, job_id)
            return True
        elif not result:
            console.print(f"  [yellow]Could not bypass login -- skipping: {page.url[:80]}[/]")
            update_job_status(conn, job_id, "skipped")
            log_action(conn, "login_page_detected", url, app_id, job_id)
            return True
        # Recovery succeeded — re-check for blockers on the new page (e.g. CAPTCHA on Indeed)
        if _is_access_denied(page):
            console.print("  [yellow]Access denied on recovered page -- skipping[/]")
            update_job_status(conn, job_id, "failed")
            log_action(conn, "access_denied", f"Blocked after login recovery: {page.url[:80]}", app_id, job_id)
            return True
        if detect_captcha(page):
            if verbose:
                console.print("  [dim]CAPTCHA on recovered page, attempting solve...[/]")
            if not try_solve_captcha(page, settings):
                console.print("  [yellow]CAPTCHA on recovered page -- skipping[/]")
                update_job_status(conn, job_id, "failed_captcha")
                log_action(conn, "captcha_detected", f"After login recovery: {page.url[:80]}", app_id, job_id)
                return True

    return False


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
            page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass  # Timed out waiting for idle -- page is usable enough
        if verbose:
            console.print(f"  [dim]Page loaded: {page.url[:80]}[/]")

        # Single check for blockers (CAPTCHA / login) on initial page load
        block = _check_page_blockers(page, url, listing_url, settings, conn, app_id, job_id, verbose)
        if block:
            return

        # Try to find and click "Apply" button if we're on a listing page
        dismiss_modals(page)
        if verbose:
            console.print("  [dim]Looking for Apply button...[/]")
        url_before_apply = page.url
        apply_result = click_apply_button(page)

        # If Apply button not found, try harder: dismiss modals again and retry,
        # then fall back to _force_apply_click which extracts URLs from the page
        if not apply_result:
            dismiss_modals(page)
            page.wait_for_timeout(500)
            apply_result = click_apply_button(page)

        if not apply_result:
            console.print("  [dim]Apply button not found -- trying URL extraction...[/]")
            if _force_apply_click(page):
                apply_result = True
                # Check if a new tab was opened
                if len(page.context.pages) > 1:
                    latest = page.context.pages[-1]
                    if latest != page and latest.url != "about:blank":
                        apply_result = "new_tab"

        if verbose:
            console.print(f"  [dim]Apply button result: {apply_result}[/]")

        # If apply button opened a new tab, switch to it
        if apply_result == "new_tab" and len(page.context.pages) > 1:
            old_page = page
            page = page.context.pages[-1]
            page.wait_for_load_state("domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                pass
            old_page.close()
            console.print(f"  [dim]Now on: {page.url[:80]}[/]")

        # Single check for blockers after navigation (covers new tab, same-page redirect, etc.)
        block = _check_page_blockers(page, url, listing_url, settings, conn, app_id, job_id, verbose)
        if block:
            return

        # Detect dead/empty LinkedIn pages (footer page, expired listing)
        if _is_dead_page(page):
            # Before giving up, try the alternate URL if we have one
            if listing_url and listing_url != url and "linkedin.com" not in listing_url.lower():
                console.print(f"  [dim]LinkedIn dead page -- trying direct URL: {listing_url[:60]}[/]")
                page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    pass
            else:
                console.print("  [yellow]Landed on empty/dead LinkedIn page -- job may be expired[/]")
                update_job_status(conn, job_id, "failed")
                log_action(conn, "apply_failed", f"Dead page after apply: {page.url[:80]}", app_id, job_id)
                final_dir = move_application_dir(company, position, "failed")
                return

        # If still stuck on LinkedIn after clicking Apply (no Easy Apply modal opened),
        # try to navigate to the company's ATS directly.
        # EXCEPTION: if apply_result == "easy_apply", we KNOW an Easy Apply modal was
        # triggered -- wait for it to render instead of giving up.
        still_on_linkedin = "linkedin.com" in page.url.lower()
        is_easy_apply_flow = (apply_result == "easy_apply")

        if still_on_linkedin and is_easy_apply_flow:
            # Easy Apply was clicked -- wait for the modal to render (up to 3s)
            from .platforms.linkedin import detect_easy_apply_modal
            for _wait in range(6):
                if detect_easy_apply_modal(page):
                    console.print("  [dim]Easy Apply modal is open[/]")
                    break
                page.wait_for_timeout(500)
            else:
                # Modal didn't appear -- save debug screenshot
                try:
                    debug_dir = Path("data/logs")
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    debug_path = debug_dir / "debug_easy_apply_no_modal.png"
                    page.screenshot(path=str(debug_path), full_page=True)
                    console.print(f"  [dim]  Debug screenshot: {debug_path}[/]")
                except Exception:
                    pass
                console.print("  [yellow]Easy Apply clicked but modal didn't open -- retrying click[/]")
                # Retry: dismiss modals and click Easy Apply again
                dismiss_modals(page)
                retry_result = click_apply_button(page)
                if retry_result == "easy_apply":
                    page.wait_for_timeout(1500)
                    if not detect_easy_apply_modal(page):
                        console.print("  [yellow]Easy Apply modal still not open after retry -- failing[/]")
                        update_job_status(conn, job_id, "failed")
                        log_action(conn, "apply_failed", "Easy Apply modal never opened", app_id, job_id)
                        final_dir = move_application_dir(company, position, "failed")
                        return

        elif still_on_linkedin:
            from .platforms.linkedin import detect_easy_apply_modal
            has_modal = detect_easy_apply_modal(page)
            logger.info(f"Still on LinkedIn: url={page.url[:80]}, easy_apply_modal={has_modal}, "
                        f"listing_url={listing_url}, apply_result={apply_result}")
            if not has_modal:
                # We're on LinkedIn but NOT in an Easy Apply flow -- try alternate URL
                if listing_url and "linkedin.com" not in listing_url.lower():
                    console.print(f"  [dim]Stuck on LinkedIn -- navigating to company page: {listing_url[:60]}[/]")
                    page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=2000)
                    except Exception:
                        pass
                    still_on_linkedin = "linkedin.com" in page.url.lower()
                else:
                    console.print(f"  [dim]No alternate URL available (listing_url={listing_url})[/]")

                if still_on_linkedin and not detect_easy_apply_modal(page):
                    # Save debug screenshot before giving up
                    try:
                        debug_dir = Path("data/logs")
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        debug_path = debug_dir / "debug_stuck_linkedin.png"
                        page.screenshot(path=str(debug_path), full_page=True)
                        console.print(f"  [dim]  Debug screenshot: {debug_path}[/]")
                    except Exception:
                        pass
                    page_tabs = len(page.context.pages)
                    console.print(f"  [yellow]Could not leave LinkedIn -- skipping "
                                  f"(tabs={page_tabs}, url={page.url[:60]})[/]")
                    update_job_status(conn, job_id, "failed")
                    log_action(conn, "apply_failed",
                               f"Stuck on LinkedIn, no Easy Apply modal. "
                               f"apply_result={apply_result}, listing_url={listing_url}, tabs={page_tabs}",
                               app_id, job_id)
                    final_dir = move_application_dir(company, position, "failed")
                    return

        # Decide strategy: selectors stay on LinkedIn, vision takes over on external ATS
        use_vision = settings.get("automation", {}).get("vision_agent", False)
        on_linkedin = "linkedin.com" in page.url.lower()
        form_answers_all = {}

        if use_vision and not on_linkedin:
            # -- VISION PATH: we're on an external ATS (Greenhouse, Lever, Workday, etc.)

            # Check if we're on a listing page (Apply button led here but form isn't loaded)
            still_on_listing = _is_listing_page(page)
            if still_on_listing:
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
                if _is_listing_page(page):
                    console.print("  [yellow]Could not reach application form -- skipping[/]")
                    update_job_status(conn, job_id, "failed")
                    log_action(conn, "apply_failed", "Stuck on listing page, Apply button unresponsive", app_id, job_id)
                    final_dir = move_application_dir(company, position, "failed")
                    return

            console.print("  [magenta]External ATS detected -- using vision agent[/]")
            log_action(conn, "vision_handoff", f"External site: {page.url[:80]}", app_id, job_id)

            # Check for login/signup pages before wasting vision agent rounds
            if detect_login_page(page):
                console.print("  [yellow]Login/signup page detected on ATS -- marking for later[/]")
                update_job_status(conn, job_id, "needs_login")
                log_action(conn, "needs_login", f"ATS requires login: {page.url[:80]}", app_id, job_id)
                final_dir = move_application_dir(company, position, "failed")
                return

            # Ashby: form is behind an "Application" tab — click it via Playwright
            if "ashbyhq.com" in page.url.lower():
                try:
                    app_tab = page.locator('a:has-text("Application"), button:has-text("Application")').first
                    if app_tab.is_visible():
                        app_tab.click()
                        page.wait_for_timeout(1500)
                        console.print("  [dim]Clicked Ashby 'Application' tab[/]")
                except Exception:
                    pass

            # Scroll to the first form field so the vision agent's initial
            # screenshot shows the form (not just the job description header).
            # Greenhouse and similar ATS put description + form on one page.
            page.evaluate("""() => {
                const input = document.querySelector(
                    'input[type="text"], input[type="email"], input[type="tel"], textarea, input[type="file"]'
                );
                if (input) input.scrollIntoView({ block: 'center', behavior: 'instant' });
            }""")
            page.wait_for_timeout(300)

            # Pre-fill via DOM selectors before vision agent takes over.
            # Playwright's fill() dispatches proper input/change events that work
            # with React controlled inputs (vision agent's coordinate typing often fails).
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

            # Screenshot before vision takes over
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
                final_dir = move_application_dir(company, position, "success")
                return
            submitted = bool(vision_result)
        else:
            # -- SELECTOR PATH: LinkedIn Easy Apply or vision disabled
            max_pages = 10  # safety limit
            if is_easy_apply_flow:
                console.print("  [cyan]Easy Apply multi-step flow -- filling forms...[/]")

            for page_num in range(max_pages):
                console.print(f"  [dim]Step {page_num + 1}/{max_pages}...[/]")
                logger.info(f"Form page {page_num + 1} for job #{job_id} ({company} - {position})")

                # Guard: check if page context is alive before doing anything
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

                # Extract form fields -- use Playwright locators for Easy Apply
                # (shadow DOM) and JS-based extraction for everything else
                use_pw = is_easy_apply_flow
                if use_pw:
                    fields = extract_form_fields_playwright(page)
                    if not fields:
                        # Fallback to JS extraction
                        fields = extract_form_fields(page)
                        use_pw = False
                else:
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
                if use_pw:
                    fill_form_fields_playwright(page, fields, answers)
                else:
                    fill_form_fields(page, fields, answers)

                # Handle file uploads
                handle_file_uploads(page, resume_file, cl_file)

                # Check if page context is still alive (uploads can trigger navigation)
                try:
                    page.evaluate("() => document.readyState")
                except Exception:
                    console.print("  [dim]Page navigated during upload -- waiting for reload[/]")
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    continue

                # Check for next/continue button
                if not click_next_button(page):
                    console.print("  [dim]No Next button -- at submit page[/]")
                    break  # No more pages -- we're at the submit page

                console.print("  [dim]Clicked Next -- loading next step...[/]")
                # Wait for next page/modal content to render
                page.wait_for_timeout(1000)
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
                page.wait_for_load_state("networkidle", timeout=2000)
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
            answers_json = json.dumps(form_answers_all) if form_answers_all else None
            update_application(conn, app_id,
                               submitted_at=datetime.now().isoformat(),
                               form_answers_json=answers_json)
            log_action(conn, "applied", f"Submitted to {company}", app_id, job_id)
            if form_answers_all:
                log_action(conn, "form_answers", answers_json, app_id, job_id)
                console.print(f"  [dim]Stored {len(form_answers_all)} form answers in DB[/]")
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
