"""Browser automation for job application form filling and submission using Playwright."""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console

from .database import (
    get_connection, get_jobs_by_status, update_job_status,
    insert_application, update_application, count_applications_today, log_action
)
from .document import (
    create_resume_docx, create_cover_letter_docx, convert_to_pdf, save_application_metadata
)
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
    max_per_role = automation.get("max_per_role", 0)
    max_per_location = automation.get("max_per_location", 0)
    distribution = automation.get("distribution", "round_robin")
    delay = automation.get("delay_between_applications_seconds", 30)
    take_screenshot = automation.get("screenshot_before_submit", True)

    conn = get_connection()

    # Check daily cap
    applied_today = count_applications_today(conn)
    if applied_today >= max_per_day:
        console.print(f"[yellow]Daily cap reached ({applied_today}/{max_per_day}). Stopping.[/]")
        conn.close()
        return

    remaining = max_per_day - applied_today

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

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            for i, job in enumerate(jobs):
                console.print(f"\n[bold]({i+1}/{len(jobs)}) {job['title']} at {job['company']}[/]")
                try:
                    _apply_to_single_job(context, job, settings, take_screenshot)
                except Exception as e:
                    console.print(f"  [red]Failed: {e}[/]")
                    update_job_status(conn, job["id"], "failed")
                    log_action(conn, "apply_failed", str(e), job_id=job["id"])

                if i < len(jobs) - 1:
                    console.print(f"  Waiting {delay}s before next application...")
                    time.sleep(delay)

            browser.close()

    except ImportError:
        console.print("[red]Playwright not installed. Run: pip install playwright && playwright install chromium[/]")

    conn.close()
    console.print("\n[bold green]Application round complete![/]")


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

        # Check for CAPTCHA
        if _detect_captcha(page):
            console.print("  [yellow]CAPTCHA detected — skipping[/]")
            update_job_status(conn, job_id, "failed_captcha")
            log_action(conn, "captcha_detected", url, app_id, job_id)
            return

        # Try to find and click "Apply" button if we're on a listing page
        _click_apply_button(page)

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
    """Check if the page has a CAPTCHA."""
    captcha_indicators = [
        'iframe[src*="recaptcha"]',
        'iframe[src*="hcaptcha"]',
        '.g-recaptcha',
        '#captcha',
        '[class*="captcha"]',
        'iframe[title*="reCAPTCHA"]',
    ]
    for selector in captcha_indicators:
        if page.query_selector(selector):
            return True
    return False


def _click_apply_button(page):
    """Try to find and click an 'Apply' button on a job listing page."""
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
                btn.click()
                page.wait_for_timeout(2000)
                return True
        except Exception:
            continue
    return False


def _extract_form_fields(page) -> list[dict]:
    """Extract all form fields from the current page using DOM inspection."""
    return page.evaluate("""() => {
        const fields = [];
        const seen = new Set();

        function getLabel(el) {
            // Check for associated label
            if (el.id) {
                const label = document.querySelector(`label[for="${el.id}"]`);
                if (label) return label.textContent.trim();
            }
            // Check parent label
            const parentLabel = el.closest('label');
            if (parentLabel) return parentLabel.textContent.trim();
            // Check aria-label
            if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
            // Check placeholder
            if (el.placeholder) return el.placeholder;
            // Check preceding sibling or parent text
            const prev = el.previousElementSibling;
            if (prev && prev.tagName === 'LABEL') return prev.textContent.trim();
            return el.name || el.id || '';
        }

        // Text inputs, emails, numbers, tel, etc.
        document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="file"])').forEach(el => {
            const id = el.id || el.name || `input_${fields.length}`;
            if (seen.has(id)) return;
            seen.add(id);

            const type = el.type || 'text';
            if (type === 'radio' || type === 'checkbox') return; // handled separately

            fields.push({
                id: id,
                selector: el.id ? `#${el.id}` : `[name="${el.name}"]`,
                label: getLabel(el),
                type: type,
                required: el.required,
                value: el.value || ''
            });
        });

        // Textareas
        document.querySelectorAll('textarea').forEach(el => {
            const id = el.id || el.name || `textarea_${fields.length}`;
            if (seen.has(id)) return;
            seen.add(id);
            fields.push({
                id: id,
                selector: el.id ? `#${el.id}` : `[name="${el.name}"]`,
                label: getLabel(el),
                type: 'textarea',
                required: el.required,
                maxLength: el.maxLength > 0 ? el.maxLength : null
            });
        });

        // Select dropdowns
        document.querySelectorAll('select').forEach(el => {
            const id = el.id || el.name || `select_${fields.length}`;
            if (seen.has(id)) return;
            seen.add(id);
            const options = Array.from(el.options).map(o => o.text.trim()).filter(t => t);
            fields.push({
                id: id,
                selector: el.id ? `#${el.id}` : `[name="${el.name}"]`,
                label: getLabel(el),
                type: 'select',
                required: el.required,
                options: options
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
            const id = el.id || el.name || `file_${fields.length}`;
            fields.push({
                id: id,
                selector: el.id ? `#${el.id}` : `[name="${el.name}"]`,
                label: getLabel(el),
                type: 'file',
                accept: el.accept || ''
            });
        });

        return fields;
    }""")


def _fill_form_fields(page, fields: list[dict], answers: dict):
    """Fill form fields with LLM-inferred answers."""
    for field in fields:
        field_id = field["id"]
        if field_id not in answers or field["type"] == "file":
            continue

        value = str(answers[field_id])
        selector = field.get("selector", "")
        if not selector:
            continue

        try:
            if field["type"] == "select":
                page.select_option(selector, label=value)
            elif field["type"] == "radio":
                # Find the radio with matching label text
                options = page.query_selector_all(f'input[name="{field_id}"]')
                for opt in options:
                    label = page.evaluate("(el) => { const l = el.closest('label'); return l ? l.textContent.trim() : el.value; }", opt)
                    if value.lower() in label.lower():
                        opt.click()
                        break
            elif field["type"] == "textarea":
                page.fill(selector, value)
            else:
                # Clear existing value and type new one
                page.fill(selector, value)
        except Exception as e:
            console.print(f"  [yellow]Could not fill '{field.get('label', field_id)}': {e}[/]")


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
    ]
    for selector in next_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                return True
        except Exception:
            continue
    return False


def _click_submit_button(page) -> bool:
    """Try to find and click the Submit/Apply button. Returns True if found."""
    submit_selectors = [
        'button:has-text("Submit")',
        'button:has-text("Submit Application")',
        'button:has-text("Apply")',
        'button:has-text("Send Application")',
        'input[type="submit"]',
        'button[type="submit"]',
        '[data-testid*="submit"]',
    ]
    for selector in submit_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                return True
        except Exception:
            continue
    return False
