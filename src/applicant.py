"""Browser automation for job application form filling and submission using Playwright."""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console

from .database import (
    get_connection, get_jobs_by_status, update_job_status,
    insert_application, update_application, count_applications_today, log_action
)
from .document import save_application_metadata
from .profile import load_settings
from .tailoring import tailor_resume, tailor_cover_letter, infer_form_answers
from .utils import get_application_dir

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
            # Sequential mode — single browser
            _run_application_batch(jobs, settings, take_screenshot, label="")
        else:
            # Parallel mode — split jobs across worker threads, each with own browser
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        conn = get_connection()
        for i, job in enumerate(jobs):
            console.print(f"\n[bold]{label}({i+1}/{len(jobs)}) {job['title']} at {job['company']}[/]")
            try:
                _apply_to_single_job(context, job, settings, take_screenshot)
            except Exception as e:
                console.print(f"  [red]{label}Failed: {e}[/]")
                update_job_status(conn, job["id"], "failed")
                log_action(conn, "apply_failed", str(e), job_id=job["id"])

            # No delay between applications — CAPTCHA detection handles bot protection

        conn.close()
        browser.close()


def _apply_to_single_job(context, job: dict, settings: dict, take_screenshot: bool):
    """Attempt to apply to a single job."""
    conn = get_connection()
    job_id = job["id"]
    url = job.get("url", "")

    if not url:
        console.print("  [yellow]No application URL — skipping[/]")
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

    # Use PDF if available, otherwise DOCX
    resume_file = resume_pdf if resume_pdf.exists() else resume_docx if resume_docx.exists() else None
    cl_file = cl_pdf if cl_pdf.exists() else cl_docx if cl_docx.exists() else None

    # Create application record
    app_id = insert_application(conn, job_id,
                                str(resume_file) if resume_file else None,
                                str(cl_file) if cl_file else None)
    log_action(conn, "apply_started", f"URL: {url}", app_id, job_id)

    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)  # Let JS render

        # Check for CAPTCHA / bot detection
        if _detect_captcha(page):
            console.print("  [yellow]CAPTCHA / bot verification detected — skipping[/]")
            update_job_status(conn, job_id, "failed_captcha")
            log_action(conn, "captcha_detected", url, app_id, job_id)
            return

        # Check for login page (LinkedIn/Indeed redirect)
        if _detect_login_page(page):
            console.print("  [yellow]Landed on login page — skipping (needs direct apply URL)[/]")
            update_job_status(conn, job_id, "skipped")
            log_action(conn, "login_page_detected", url, app_id, job_id)
            return

        # Try to find and click "Apply" button if we're on a listing page
        apply_result = _click_apply_button(page)

        # If apply button opened a new tab, switch to it
        if apply_result == "new_tab" and len(page.context.pages) > 1:
            old_page = page
            page = page.context.pages[-1]
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(2000)
            old_page.close()
            console.print(f"  [dim]Now on: {page.url[:80]}[/]")

            # Check new page for CAPTCHA or login
            if _detect_captcha(page):
                console.print("  [yellow]CAPTCHA on apply page — skipping[/]")
                update_job_status(conn, job_id, "failed_captcha")
                log_action(conn, "captcha_detected", page.url, app_id, job_id)
                return
            if _detect_login_page(page):
                console.print("  [yellow]Redirected to login — skipping[/]")
                update_job_status(conn, job_id, "skipped")
                log_action(conn, "login_page_detected", page.url, app_id, job_id)
                return

        # Also check after clicking apply on the same page
        if _detect_login_page(page):
            console.print("  [yellow]Landed on login page — skipping[/]")
            update_job_status(conn, job_id, "skipped")
            log_action(conn, "login_page_detected", page.url, app_id, job_id)
            return

        # Process form pages
        form_answers_all = {}
        max_pages = 10  # safety limit

        for page_num in range(max_pages):
            console.print(f"  Processing form page {page_num + 1}...")

            # Extract form fields
            fields = _extract_form_fields(page)
            if not fields:
                console.print("  [yellow]No form fields found on this page.[/]")
                break

            console.print(f"  Found {len(fields)} form fields")

            # Use LLM to infer answers
            answers = infer_form_answers(fields, job, settings)
            form_answers_all.update(answers)

            # Fill in the form
            _fill_form_fields(page, fields, answers)

            # Handle file uploads
            _handle_file_uploads(page, resume_file, cl_file)

            # Check for next/continue button
            if not _click_next_button(page):
                break  # No more pages — we're at the submit page

            page.wait_for_timeout(2000)

        # Screenshot before submit
        if take_screenshot:
            screenshot_path = app_dir / "pre_submit_screenshot.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            update_application(conn, app_id, screenshot_path=str(screenshot_path))

        # Submit
        submitted = _click_submit_button(page)

        if submitted:
            page.wait_for_timeout(3000)
            # Screenshot confirmation
            confirm_path = app_dir / "confirmation_screenshot.png"
            page.screenshot(path=str(confirm_path), full_page=True)

            update_job_status(conn, job_id, "applied")
            update_application(conn, app_id,
                               submitted_at=datetime.now().isoformat(),
                               form_answers_json=json.dumps(form_answers_all))
            log_action(conn, "applied", f"Submitted to {company}", app_id, job_id)
            save_application_metadata(company, position, job, form_answers_all)
            console.print(f"  [green]Successfully applied![/]")
        else:
            # Save debug screenshot so we can see what the page looks like
            debug_path = app_dir / "debug_no_submit.png"
            try:
                page.screenshot(path=str(debug_path), full_page=True)
                console.print(f"  [yellow]Could not find submit button -- debug screenshot: {debug_path}[/]")
            except Exception:
                console.print("  [yellow]Could not find submit button — marking as failed[/]")
            update_job_status(conn, job_id, "failed")
            log_action(conn, "submit_button_not_found", url, app_id, job_id)

    except Exception as e:
        console.print(f"  [red]Error during application: {e}[/]")
        update_job_status(conn, job_id, "failed")
        log_action(conn, "apply_error", str(e), app_id, job_id)
    finally:
        page.close()


def _detect_captcha(page) -> bool:
    """Check if the page has a CAPTCHA or bot verification."""
    captcha_indicators = [
        'iframe[src*="recaptcha"]',
        'iframe[src*="hcaptcha"]',
        '.g-recaptcha',
        '#captcha',
        '[class*="captcha"]',
        'iframe[title*="reCAPTCHA"]',
        # Cloudflare Turnstile / challenge
        'iframe[src*="challenges.cloudflare.com"]',
        '[class*="cf-turnstile"]',
        '#challenge-running',
        '#challenge-form',
    ]
    for selector in captcha_indicators:
        if page.query_selector(selector):
            return True

    # Check page text for common verification messages
    body_text = (page.text_content("body") or "").lower()[:2000]
    if any(phrase in body_text for phrase in [
        "verify you are human",
        "additional verification required",
        "please verify you're not a robot",
        "checking your browser",
    ]):
        return True

    return False


def _detect_login_page(page) -> bool:
    """Detect if we've landed on a login/signup page instead of an application form."""
    url = page.url.lower()

    # Known login/signup URL patterns
    login_patterns = [
        "linkedin.com/signup",
        "linkedin.com/login",
        "linkedin.com/checkpoint",
        "linkedin.com/uas/login",
        "indeed.com/account/login",
        "indeed.com/auth",
        "glassdoor.com/member/auth",
    ]
    if any(pattern in url for pattern in login_patterns):
        return True

    # Check page content for login indicators
    body_text = (page.text_content("body") or "").lower()[:2000]
    login_phrases = [
        "sign in to continue",
        "sign in to see who you already know",
        "join linkedin",
        "join now",
        "log in to indeed",
        "create an account",
    ]
    # Must match login phrase AND have a password field (to avoid false positives)
    if any(phrase in body_text for phrase in login_phrases):
        if page.query_selector('input[type="password"]'):
            return True

    return False


def _dismiss_modals(page):
    """Try to close any sign-in modals or popups blocking the page."""
    modal_close_selectors = [
        'button[aria-label="Dismiss"]',
        'button[aria-label="Close"]',
        'button:has-text("Dismiss")',
        '[data-test-modal-close-btn]',
        '.modal__dismiss',
        '.artdeco-modal__dismiss',
        'button.msg-overlay-bubble-header__control--new-convo-btn',
        # Generic close buttons
        'button[class*="close"]',
        'button[class*="dismiss"]',
        '[aria-label="close"]',
    ]
    for selector in modal_close_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(500)
        except Exception:
            continue


def _click_apply_button(page):
    """Try to find and click an 'Apply' button on a job listing page.

    Handles LinkedIn external apply links (opens new tab) and standard apply buttons.
    """
    # First dismiss any modals/popups (LinkedIn sign-in, etc.)
    _dismiss_modals(page)

    # For LinkedIn: check if there's an external apply link (opens company's site)
    current_url = page.url
    if "linkedin.com" in current_url:
        # LinkedIn external apply buttons are <a> tags that open a new tab
        ext_apply = page.query_selector('a[href*="externalApply"], a.jobs-apply-button, a[data-tracking-control-name*="apply"]')
        if ext_apply:
            href = ext_apply.get_attribute("href")
            if href:
                console.print(f"  [dim]Following LinkedIn external apply link...[/]")
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                _dismiss_modals(page)
                return True

    apply_selectors = [
        'button:has-text("Apply")',
        'a:has-text("Apply")',
        'button:has-text("Apply Now")',
        'a:has-text("Apply Now")',
        'button:has-text("Easy Apply")',
        '[data-testid*="apply"]',
        '.apply-button',
        '#apply-button',
    ]

    for selector in apply_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                # Check if it's a link that opens externally
                tag = btn.evaluate("el => el.tagName.toLowerCase()")
                href = btn.get_attribute("href") if tag == "a" else None

                if href and ("http" in href) and ("linkedin.com" not in href):
                    # External apply link — navigate directly
                    console.print(f"  [dim]Following external apply link...[/]")
                    page.goto(href, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                    return True

                # Check if click opens a new page/tab
                pages_before = len(page.context.pages)
                btn.click()
                page.wait_for_timeout(2000)

                # If a new tab opened, switch to it
                if len(page.context.pages) > pages_before:
                    new_page = page.context.pages[-1]
                    console.print(f"  [dim]Switched to new tab: {new_page.url[:60]}...[/]")
                    # We can't switch 'page' reference here, but the caller
                    # should use context.pages[-1] — handled in _apply_to_single_job
                    return "new_tab"

                return True
        except Exception:
            continue
    return False


def _extract_form_fields(page) -> list[dict]:
    """Extract all form fields from the current page using DOM inspection."""
    # Scroll down to trigger lazy-loaded content
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(1000)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)

    fields = page.evaluate("""() => {
        const fields = [];
        const seen = new Set();
        let autoIdx = 0;

        function getSelector(el) {
            // Build a reliable selector - prefer id, then name, then aria-label, then generate a CSS path
            if (el.id) return '#' + CSS.escape(el.id);
            if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
            if (el.getAttribute('aria-label')) return el.tagName.toLowerCase() + '[aria-label="' + el.getAttribute('aria-label') + '"]';
            if (el.placeholder) return el.tagName.toLowerCase() + '[placeholder="' + el.placeholder + '"]';
            // Fallback: nth-of-type path
            let path = el.tagName.toLowerCase();
            if (el.type) path += '[type="' + el.type + '"]';
            return path;
        }

        function getLabel(el) {
            if (el.id) {
                const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (label) return label.textContent.trim();
            }
            const parentLabel = el.closest('label');
            if (parentLabel) return parentLabel.textContent.trim();
            if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
            if (el.placeholder) return el.placeholder;
            const prev = el.previousElementSibling;
            if (prev && prev.tagName === 'LABEL') return prev.textContent.trim();
            return el.name || el.id || '';
        }

        function isVisible(el) {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        }

        // Text inputs, emails, numbers, tel, etc.
        document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="file"])').forEach(el => {
            const type = el.type || 'text';
            if (type === 'radio' || type === 'checkbox') return;

            const selector = getSelector(el);
            const uniqueKey = selector + '_' + (el.name || '') + '_' + (el.id || '') + '_' + autoIdx++;
            if (seen.has(uniqueKey)) return;
            seen.add(uniqueKey);

            fields.push({
                id: el.id || el.name || el.getAttribute('aria-label') || 'input_' + autoIdx,
                selector: selector,
                label: getLabel(el),
                type: type,
                required: el.required,
                value: el.value || '',
                visible: isVisible(el)
            });
        });

        // Textareas
        document.querySelectorAll('textarea').forEach(el => {
            const selector = getSelector(el);
            fields.push({
                id: el.id || el.name || 'textarea_' + autoIdx++,
                selector: selector,
                label: getLabel(el),
                type: 'textarea',
                required: el.required,
                maxLength: el.maxLength > 0 ? el.maxLength : null,
                visible: isVisible(el)
            });
        });

        // Select dropdowns
        document.querySelectorAll('select').forEach(el => {
            const selector = getSelector(el);
            const options = Array.from(el.options).map(o => o.text.trim()).filter(t => t);
            fields.push({
                id: el.id || el.name || 'select_' + autoIdx++,
                selector: selector,
                label: getLabel(el),
                type: 'select',
                required: el.required,
                options: options,
                visible: isVisible(el)
            });
        });

        // Radio button groups
        const radioGroups = {};
        document.querySelectorAll('input[type="radio"]').forEach(el => {
            const name = el.name;
            if (!name) return;
            if (!radioGroups[name]) {
                radioGroups[name] = {
                    id: name,
                    selector: `[name="${name}"]`,
                    label: getLabel(el),
                    type: 'radio',
                    options: []
                };
            }
            const label = getLabel(el) || el.value;
            if (label && !radioGroups[name].options.includes(label)) {
                radioGroups[name].options.push(label);
            }
        });
        Object.values(radioGroups).forEach(g => fields.push(g));

        // File uploads
        document.querySelectorAll('input[type="file"]').forEach(el => {
            const id = el.id || el.name || 'file_' + autoIdx++;
            fields.push({
                id: id,
                selector: getSelector(el),
                label: getLabel(el),
                type: 'file',
                accept: el.accept || ''
            });
        });

        return fields;
    }""")

    # If no fields found on main page, check iframes
    if not fields:
        for frame in page.frames[1:]:
            try:
                fields = frame.evaluate("""() => {
                    const fields = [];
                    document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"])').forEach(el => {
                        if (el.type === 'radio' || el.type === 'checkbox' || el.type === 'file') return;
                        fields.push({
                            id: el.id || el.name || el.getAttribute('aria-label') || 'input',
                            selector: el.id ? '#' + CSS.escape(el.id) : el.name ? el.tagName.toLowerCase() + '[name="' + el.name + '"]' : el.tagName.toLowerCase(),
                            label: el.getAttribute('aria-label') || el.placeholder || el.name || el.id || '',
                            type: el.type || 'text', required: el.required, value: el.value || '', visible: true
                        });
                    });
                    document.querySelectorAll('textarea, select').forEach(el => {
                        fields.push({
                            id: el.id || el.name || el.tagName.toLowerCase(),
                            selector: el.id ? '#' + CSS.escape(el.id) : el.name ? el.tagName.toLowerCase() + '[name="' + el.name + '"]' : el.tagName.toLowerCase(),
                            label: el.getAttribute('aria-label') || el.name || el.id || '',
                            type: el.tagName === 'SELECT' ? 'select' : 'textarea',
                            required: el.required, visible: true,
                            options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => o.text.trim()).filter(t => t) : undefined
                        });
                    });
                    return fields;
                }""")
                if fields:
                    console.print(f"  [dim]Found fields inside iframe[/]")
                    break
            except Exception:
                continue

    return fields


def _fill_form_fields(page, fields: list[dict], answers: dict):
    """Fill form fields with LLM-inferred answers."""
    for field in fields:
        field_id = field["id"]
        if field_id not in answers or field["type"] == "file":
            continue

        # Skip fields that were detected as not visible
        if not field.get("visible", True):
            continue

        value = str(answers[field_id])
        selector = field.get("selector", "")
        if not selector:
            continue

        try:
            # Try to find the element first
            el = page.query_selector(selector)
            if not el:
                console.print(f"  [dim]Skipping '{field.get('label', field_id)}' - element not found[/]")
                continue

            # Scroll into view and wait for visibility
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(200)

            if field["type"] == "select":
                page.select_option(selector, label=value, timeout=5000)
            elif field["type"] == "radio":
                options = page.query_selector_all(f'input[name="{field_id}"]')
                for opt in options:
                    label = page.evaluate("(el) => { const l = el.closest('label'); return l ? l.textContent.trim() : el.value; }", opt)
                    if value.lower() in label.lower():
                        opt.scroll_into_view_if_needed()
                        opt.click()
                        break
            elif field["type"] == "textarea":
                page.fill(selector, value, timeout=5000)
            else:
                # Try fill first, fall back to click+type for stubborn inputs
                try:
                    page.fill(selector, value, timeout=5000)
                except Exception:
                    el.click()
                    page.wait_for_timeout(100)
                    page.keyboard.type(value)
        except Exception as e:
            err_msg = str(e).split("\n")[0][:80]
            console.print(f"  [yellow]Could not fill '{field.get('label', field_id)}': {err_msg}[/]")


def _handle_file_uploads(page, resume_file: Optional[Path], cl_file: Optional[Path]):
    """Handle file upload fields — detect resume/cover letter fields and upload."""
    file_inputs = page.query_selector_all('input[type="file"]')
    for file_input in file_inputs:
        label = page.evaluate("""(el) => {
            if (el.id) {
                const label = document.querySelector(`label[for="${el.id}"]`);
                if (label) return label.textContent.trim().toLowerCase();
            }
            const parent = el.closest('label, .field, .form-group, [class*="upload"]');
            return parent ? parent.textContent.trim().toLowerCase() : '';
        }""", file_input)

        try:
            if any(kw in label for kw in ["resume", "cv", "curriculum"]):
                if resume_file and resume_file.exists():
                    file_input.set_input_files(str(resume_file))
                    console.print(f"  Uploaded resume: {resume_file.name}")
            elif any(kw in label for kw in ["cover letter", "cover_letter", "coverletter"]):
                if cl_file and cl_file.exists():
                    file_input.set_input_files(str(cl_file))
                    console.print(f"  Uploaded cover letter: {cl_file.name}")
            elif resume_file and resume_file.exists():
                # Generic file upload — default to resume
                file_input.set_input_files(str(resume_file))
                console.print(f"  Uploaded file (defaulting to resume): {resume_file.name}")
        except Exception as e:
            console.print(f"  [yellow]File upload failed: {e}[/]")


def _click_next_button(page) -> bool:
    """Try to find and click a Next/Continue button. Returns True if found."""
    next_selectors = [
        'button:has-text("Next")',
        'button:has-text("Continue")',
        'input[type="submit"][value*="Next"]',
        'input[type="submit"][value*="Continue"]',
        'a:has-text("Next")',
        '[data-testid*="next"]',
        # Workday
        'button[data-automation-id="bottom-navigation-next-button"]',
    ]

    # Scroll down to reveal buttons below the fold
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(500)

    for selector in next_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.scroll_into_view_if_needed()
                btn.click()
                return True
        except Exception:
            continue

    # Check iframes
    for frame in page.frames[1:]:
        for selector in next_selectors:
            try:
                btn = frame.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    return True
            except Exception:
                continue

    return False


def _click_submit_button(page) -> bool:
    """Try to find and click the Submit/Apply button. Returns True if found."""
    submit_selectors = [
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
        'button:has-text("Apply")',
        'button:has-text("Send Application")',
        'button:has-text("Complete")',
        'button:has-text("Finish")',
        'button:has-text("Done")',
        'input[type="submit"]',
        'button[type="submit"]',
        '[data-testid*="submit"]',
        '[data-testid*="apply"]',
        # Greenhouse
        '#submit_app', '#submit-application',
        'input[value="Submit Application"]',
        'input[value="Submit"]',
        # Lever
        '.posting-btn-submit',
        'button.postings-btn',
        # Workday
        'button[data-automation-id="bottom-navigation-next-button"]',
        'button[data-automation-id="submit"]',
        # iCIMS
        '.iCIMS_Button', 'button.btn-submit',
        # Generic fallbacks
        'a:has-text("Submit")',
        'a:has-text("Apply")',
        '[role="button"]:has-text("Submit")',
        '[class*="submit"]',
    ]

    # Scroll down to reveal submit buttons below the fold
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(500)

    # Search main page
    for selector in submit_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.scroll_into_view_if_needed()
                btn.click()
                return True
        except Exception:
            continue

    # Scroll back to top and try again (button could be at top)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    for selector in submit_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.scroll_into_view_if_needed()
                btn.click()
                return True
        except Exception:
            continue

    # Fall back to iframes (Greenhouse, Lever, etc. embed forms in iframes)
    for frame in page.frames[1:]:
        for selector in submit_selectors:
            try:
                btn = frame.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    return True
            except Exception:
                continue

    return False
