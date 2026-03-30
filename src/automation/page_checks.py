"""Page inspection utilities -- dead page, listing, access denied, CAPTCHA, login detection.

Also includes login recovery (cookie loading, session warmup, manual login)
and URL extraction fallbacks for stubborn Apply buttons.
"""

import json
import logging
import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from rich.console import Console

from ..db import get_connection, update_job_status, log_action
from ..utils import LINKEDIN_AUTH_STATE, SITE_AUTH_DIR

from .detection import detect_captcha, try_solve_captcha, detect_login_page
from .selectors import ATS_DOMAINS, LISTING_SIGNALS, LISTING_EXCEPTION_PATTERNS, FORCE_APPLY_SELECTORS
from .results import HandlerResult, StepResult

logger = logging.getLogger(__name__)

console = Console(force_terminal=True)


def is_dead_page(page) -> bool:
    """Detect if we've landed on a dead/empty LinkedIn page (footer page, expired listing).

    Only flags LinkedIn pages -- external ATS sites (Ashby, Greenhouse, etc.) are SPAs
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

        // Check total visible text -- LinkedIn footer pages have < 300 chars
        const cleaned = (body.innerText || '').replace(/\\s+/g, ' ').trim();
        if (cleaned.length < 300) return true;

        return false;
    }""")


def is_listing_page(page) -> bool:
    """Heuristic: check if we're still on a job listing/description page (not an application form).

    Returns True if the page looks like a listing with no form fields.
    """
    url = page.url.lower()

    # Greenhouse and similar ATS put job description AND form on the same page.
    # The form is below the fold but it's there -- not a listing-only page.
    if any(pattern in url for pattern in LISTING_EXCEPTION_PATTERNS):
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
    matches = sum(1 for s in LISTING_SIGNALS if s in body_text)
    return matches >= 2


def is_access_denied(page) -> bool:
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


def get_site_domain(url: str) -> str:
    """Extract the main domain from a URL (e.g. 'workday.com' from 'company.wd5.myworkdayjobs.com')."""
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    # Collapse common ATS subdomains to their base
    for ats in ATS_DOMAINS:
        if hostname.endswith(ats):
            return ats
    parts = hostname.rsplit(".", 2)
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def get_site_auth_path(url: str) -> Path:
    """Get the cookie storage path for a given site URL."""
    domain = get_site_domain(url)
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', domain)
    return SITE_AUTH_DIR / f"{safe}.json"


def force_apply_click(page) -> bool:
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
        except PlaywrightTimeoutError:
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

    for selector in FORCE_APPLY_SELECTORS:
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
                except PlaywrightTimeoutError:
                    pass

                # Check if window.open was called
                popup_url = page.evaluate("() => window.__captured_popup_url")
                if popup_url:
                    console.print(f"  [dim]Intercepted popup URL: {popup_url[:80]}[/]")
                    page.goto(popup_url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=2000)
                    except PlaywrightTimeoutError:
                        pass
                    return True

                # Check if URL changed from click
                return True
        except Exception as e:
            logger.debug(f"Force apply selector failed: {e}")
            continue

    return False


def try_recover_login(page, original_url: str, listing_url: str, conn, app_id, job_id, settings=None) -> "StepResult | None":
    """Try to recover from a login page.

    Returns None if recovered (page is now usable).
    Returns StepResult on failure (needs_login or skipped).

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
        except PlaywrightTimeoutError:
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
            except PlaywrightTimeoutError:
                pass
            if not detect_login_page(page):
                return None  # recovered

    # --- Generic site recovery: check for stored cookies ---
    site_auth = get_site_auth_path(current_url)
    if site_auth.exists():
        console.print(f"  [dim]Found stored cookies for {get_site_domain(current_url)}, retrying...[/]")
        page.context.add_cookies(json.loads(site_auth.read_text()))
        page.goto(original_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except PlaywrightTimeoutError:
            pass
        if not detect_login_page(page):
            return None  # recovered
        console.print(f"  [yellow]Stored cookies expired for {get_site_domain(current_url)}[/]")

    # --- ATS auto-register short-circuit ---
    # If this domain supports auto-registration, don't try the alternate URL
    # (which is a job board listing page, useless for ATS registration).
    # Return REQUIRES_LOGIN so the kernel routes to DETECT_AUTH_TYPE → REGISTER.
    # Use full hostname (not collapsed domain) so *.avature.net patterns match bloomberg.avature.net.
    from urllib.parse import urlparse as _urlparse
    from .account_registry import is_auto_register_allowed
    _hostname = _urlparse(current_url).hostname or ""
    if _hostname and is_auto_register_allowed(_hostname, settings or {}):
        console.print(f"  [cyan]ATS domain -- skipping alternate URL, attempting auto-register flow[/]")
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            message=f"ATS login wall -- routing to auto-register for {_hostname}"
        )

    # --- Alternate URL fallback ---
    if listing_url and listing_url != original_url:
        console.print(f"  [yellow]Login wall -- trying alternate URL: {listing_url[:60]}[/]")
        log_action(conn, "login_fallback", f"Trying {listing_url}", app_id, job_id)
        page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except PlaywrightTimeoutError:
            pass
        if not detect_login_page(page):
            return None  # recovered

    # --- Manual login: pause for user if enabled ---
    domain = get_site_domain(current_url)
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
            site_auth = get_site_auth_path(current_url)
            site_auth.parent.mkdir(parents=True, exist_ok=True)
            cookies = page.context.cookies()
            site_auth.write_text(json.dumps(cookies, indent=2))
            console.print(f"  [dim]Cookies saved for {domain}[/]")
            return None  # recovered
        console.print(f"  [yellow]Still on login page after manual login attempt[/]")

    console.print(f"  [yellow]Login required for {domain} -- auto-skipping[/]")
    return StepResult(
        result=HandlerResult.REQUIRES_LOGIN,
        message=f"Login required for {domain}"
    )


def detect_registration_wall(page) -> bool:
    """Check if the current page is a registration/signup wall (not merely a login wall).

    Signals:
    - Two or more password fields (password + confirm password)
    - "Create Account", "Sign Up", "Register", "New User" text visible
    - No "Sign In" / "Welcome Back" text that would indicate a pure login wall
    """
    try:
        return page.evaluate("""() => {
            const text = document.body.innerText.toLowerCase();
            const pwFields = document.querySelectorAll('input[type="password"]');
            const hasConfirmPw = pwFields.length >= 2;

            const registerSignals = [
                'create account', 'create your account', 'sign up',
                'register', 'new user', 'join now', 'get started',
            ];
            const hasRegisterText = registerSignals.some(s => text.includes(s));

            // Registration: confirm-password field OR explicit register text
            return hasConfirmPw || hasRegisterText;
        }""")
    except Exception:
        return False


def check_page_blockers(page, url, listing_url, settings, conn, app_id, job_id, verbose) -> "StepResult | None":
    """Check for CAPTCHA, login walls, or access-denied pages.

    Returns None when the page is clear (not blocked).
    Returns a StepResult when blocked -- caller should stop processing this job.
    """
    # Wait briefly for client-side redirects (e.g. Amazon Jobs -> signin)
    try:
        page.wait_for_load_state("networkidle", timeout=2000)
    except PlaywrightTimeoutError:
        pass

    # Fast check: access denied / 403 pages (no point trying CAPTCHA or vision)
    if is_access_denied(page):
        console.print("  [yellow]Access denied / blocked by site -- skipping[/]")
        update_job_status(conn, job_id, "failed")
        log_action(conn, "access_denied", f"Blocked: {page.url[:80]}", app_id, job_id)
        return StepResult(
            result=HandlerResult.FAILED_ERROR,
            message="Access denied / blocked by site"
        )

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
                    return None  # continue processing
            console.print("  [yellow]CAPTCHA / bot verification detected -- skipping[/]")
            try:
                page.screenshot(path="data/logs/debug_captcha_blocked.png")
            except Exception as e:
                logger.debug(f"CAPTCHA debug screenshot failed: {e}")
            update_job_status(conn, job_id, "failed_captcha")
            log_action(conn, "captcha_detected", url, app_id, job_id)
            return StepResult(
                result=HandlerResult.CAPTCHA_DETECTED,
                message="CAPTCHA / bot verification detected"
            )

    if detect_login_page(page):
        if verbose:
            console.print("  [dim]Login page detected[/]")
        recover_result = try_recover_login(page, url, listing_url, conn, app_id, job_id, settings)
        if recover_result is not None:
            # Recovery failed -- recover_result is a StepResult indicating why
            if recover_result.result == HandlerResult.REQUIRES_LOGIN:
                update_job_status(conn, job_id, "needs_login")
                log_action(conn, "needs_login", f"Login required: {page.url}", app_id, job_id)
            else:
                console.print(f"  [yellow]Could not bypass login -- skipping: {page.url[:80]}[/]")
                update_job_status(conn, job_id, "skipped")
                log_action(conn, "login_page_detected", url, app_id, job_id)
            return recover_result
        # Recovery succeeded -- re-check for blockers on the new page (e.g. CAPTCHA on Indeed)
        if is_access_denied(page):
            console.print("  [yellow]Access denied on recovered page -- skipping[/]")
            update_job_status(conn, job_id, "failed")
            log_action(conn, "access_denied", f"Blocked after login recovery: {page.url[:80]}", app_id, job_id)
            return StepResult(
                result=HandlerResult.FAILED_ERROR,
                message="Access denied on recovered page"
            )
        if detect_captcha(page):
            if verbose:
                console.print("  [dim]CAPTCHA on recovered page, attempting solve...[/]")
            if not try_solve_captcha(page, settings):
                console.print("  [yellow]CAPTCHA on recovered page -- skipping[/]")
                update_job_status(conn, job_id, "failed_captcha")
                log_action(conn, "captcha_detected", f"After login recovery: {page.url[:80]}", app_id, job_id)
                return StepResult(
                    result=HandlerResult.CAPTCHA_DETECTED,
                    message="CAPTCHA on recovered page"
                )

    return None  # not blocked
