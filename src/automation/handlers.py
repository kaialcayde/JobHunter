"""Extracted handler functions for the single-job application state machine.

Each handler takes explicit inputs and returns StepResult.
No handler calls another handler -- orchestration stays in kernel.py.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from rich.console import Console

from ..db import (
    update_job_status, insert_application, update_application,
    log_action, increment_retry_count
)
from ..core.document import save_application_metadata
from ..core.tailoring import infer_form_answers
from ..utils import get_application_dir, move_application_dir, TEMPLATES_DIR

from .detection import (
    detect_login_page, dismiss_modals, click_apply_button,
    click_next_button, click_submit_button
)
from .email_poller import EmailPoller, find_otp_field
from .forms import extract_fields, fill_fields, extract_form_fields, fill_form_fields, handle_file_uploads
from .vision_agent import run_vision_agent, verify_submission
from .page_checks import is_dead_page, is_listing_page, force_apply_click, check_page_blockers, get_site_domain
from .results import HandlerResult, StepResult

logger = logging.getLogger(__name__)
console = Console(force_terminal=True)


def _debug_dump_dom(page, debug_dir, console):
    """Dump DOM structure of all form widgets to JSON files for inspection.

    Called during --debug pause after DOM pre-fill. Outputs:
      - avature_dom_dump.json: all dropdown/select candidates + form field map
      - avature_all_fields.json: every visible input/select/textarea with labels
      - avature_dropdown_click.json: options revealed by clicking first dropdown
    """
    import json as _json

    # 1. Dropdown candidates + form field map
    try:
        result = page.evaluate("""() => {
            const results = [];
            const candidates = document.querySelectorAll(
                '[class*="select"], [class*="dropdown"], [class*="combobox"], ' +
                '[role="combobox"], [role="listbox"], [aria-haspopup], ' +
                '[class*="Select"], [class*="Dropdown"], [class*="picker"]'
            );
            candidates.forEach(el => {
                const container = el.closest(
                    '[class*="field"], [class*="group"], [class*="row"], ' +
                    '[class*="form"], [class*="question"]'
                );
                const label = container
                    ? container.querySelector('label, [class*="label"]')
                    : null;
                results.push({
                    label: label ? label.innerText.trim() : '(no label)',
                    tag: el.tagName, className: el.className.substring(0, 120),
                    role: el.getAttribute('role'),
                    ariaHaspopup: el.getAttribute('aria-haspopup'),
                    id: el.id, outerHTML: el.outerHTML.substring(0, 300)
                });
            });

            // Native <select> elements
            document.querySelectorAll('select').forEach(el => {
                const container = el.closest(
                    '[class*="field"], [class*="group"], [class*="row"], ' +
                    '[class*="form"], [class*="question"]'
                );
                const label = container
                    ? container.querySelector('label, [class*="label"]')
                    : null;
                const options = Array.from(el.options).slice(0, 10).map(o => o.text);
                results.push({
                    label: label ? label.innerText.trim() : '(no label)',
                    tag: 'SELECT', className: el.className.substring(0, 120),
                    id: el.id, name: el.name, options: options,
                    outerHTML: el.outerHTML.substring(0, 500)
                });
            });

            // Form field containers
            const fields = [];
            document.querySelectorAll(
                '[class*="field"], [class*="form-group"], [class*="question"]'
            ).forEach(el => {
                const label = el.querySelector('label, [class*="label"]');
                const inputs = el.querySelectorAll(
                    'input, select, textarea, [role="combobox"], [role="listbox"]'
                );
                if (label && inputs.length > 0) {
                    fields.push({
                        label: label.innerText.trim().substring(0, 60),
                        containerClass: el.className.substring(0, 100),
                        containerTag: el.tagName,
                        inputTypes: Array.from(inputs).map(i => ({
                            tag: i.tagName, type: i.type || i.getAttribute('role') || '',
                            className: i.className.substring(0, 80),
                            name: i.name || i.id || ''
                        }))
                    });
                }
            });

            return {
                candidates: results, fields: fields,
                url: window.location.href, title: document.title
            };
        }""")

        dump_path = debug_dir / "avature_dom_dump.json"
        dump_path.write_text(_json.dumps(result, indent=2))
        console.print(f"  [bold yellow]  DOM dump: {dump_path} "
                       f"({len(result['candidates'])} dropdown candidates, "
                       f"{len(result['fields'])} field containers)[/]")
        for c in result["candidates"][:8]:
            console.print(f"    [dim]{c['label']!r:30s} <{c['tag']}> "
                          f"class={c['className'][:50]!r}[/]")
        for f in result["fields"][:12]:
            inputs = ", ".join(
                f"<{i['tag']}> {i['type']}" for i in f["inputTypes"]
            )
            console.print(f"    [dim]{f['label']!r:30s} -> {inputs}[/]")
    except Exception as e:
        console.print(f"  [red]DOM dump failed: {e}[/]")

    # 2. All visible form elements
    try:
        all_fields = page.evaluate("""() => {
            const fields = [];
            document.querySelectorAll(
                'input, select, textarea, [contenteditable="true"]'
            ).forEach(el => {
                if (!el.offsetParent && el.type !== 'hidden') return;
                const container = el.closest(
                    '[class*="field"], [class*="group"], [class*="row"], ' +
                    '[class*="form"], [class*="question"]'
                );
                const label = container
                    ? container.querySelector('label, [class*="label"]')
                    : null;
                fields.push({
                    label: label ? label.innerText.trim().substring(0, 60)
                                 : '(no label)',
                    tag: el.tagName, type: el.type || '',
                    name: el.name || '', id: el.id || '',
                    className: el.className.substring(0, 80),
                    value: (el.value || '').substring(0, 40),
                    placeholder: el.placeholder || ''
                });
            });
            return fields;
        }""")
        fields_path = debug_dir / "avature_all_fields.json"
        fields_path.write_text(_json.dumps(all_fields, indent=2))
        console.print(f"  [bold yellow]  All fields: {fields_path} "
                       f"({len(all_fields)} visible elements)[/]")
    except Exception as e:
        console.print(f"  [red]All-fields dump failed: {e}[/]")

    # 3. Try clicking first unfilled dropdown to reveal options structure
    try:
        for sel in [
            '[class*="Select-control"]', '[class*="select-control"]',
            '[class*="avature-select"]', '[class*="dropdown-toggle"]',
            '[class*="custom-select"]', 'select',
            '[role="combobox"]', '[aria-haspopup="listbox"]',
        ]:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(800)
                options = page.evaluate("""() => {
                    const opts = document.querySelectorAll(
                        '[role="option"], [class*="option"], li[class*="item"], ' +
                        '.Select-menu-outer li, [class*="menu"] li, ' +
                        '[class*="listbox"] li'
                    );
                    return Array.from(opts).slice(0, 15).map(o => ({
                        tag: o.tagName, className: o.className.substring(0, 80),
                        role: o.getAttribute('role'),
                        text: o.innerText.trim().substring(0, 60),
                        outerHTML: o.outerHTML.substring(0, 200)
                    }));
                }""")
                if options:
                    click_path = debug_dir / "avature_dropdown_click.json"
                    click_path.write_text(_json.dumps(
                        {"trigger_selector": sel, "options": options}, indent=2
                    ))
                    console.print(f"  [bold yellow]  Dropdown click: {click_path} "
                                   f"({len(options)} options via {sel!r})[/]")
                    for o in options[:5]:
                        console.print(f"    [dim]{o['text']!r:30s} <{o['tag']}> "
                                      f"role={o['role']} class={o['className'][:40]!r}[/]")
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                # Take screenshot with dropdown open
                page.screenshot(
                    path=str(debug_dir / "debug_dropdown_open.png"), full_page=True
                )
                break
    except Exception as e:
        console.print(f"  [red]Dropdown click test failed: {e}[/]")


def handle_setup(job: dict, settings: dict, conn) -> StepResult:
    """Resolve document paths and insert application record.

    Returns StepResult with metadata:
        - url: resolved application URL
        - listing_url: resolved listing URL
        - app_dir: application directory Path
        - resume_file: Path or None
        - cl_file: Path or None
        - app_id: inserted application row id
        - company: company name
        - position: position/title
        - job_id: job id
    """
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
        return StepResult(
            result=HandlerResult.FAILED,
            message="No application URL",
            metadata={"job_id": job_id}
        )

    update_job_status(conn, job_id, "applying")
    company = job.get("company", "Unknown")
    position = job.get("title", "Unknown")

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

    return StepResult(
        result=HandlerResult.SUCCESS,
        metadata={
            "url": url,
            "listing_url": listing_url,
            "app_dir": app_dir,
            "resume_file": resume_file,
            "cl_file": cl_file,
            "app_id": app_id,
            "company": company,
            "position": position,
            "job_id": job_id,
        }
    )


def handle_navigate(page, url: str, listing_url: str, settings: dict, conn, app_id: int, job_id: int, verbose: bool) -> StepResult:
    """Load the page and check for initial blockers.

    Returns StepResult. On block the caller should bail out; SUCCESS means page is usable.
    """
    if verbose:
        console.print(f"  [dim]Loading: {url[:80]}[/]")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=2000)
    except PlaywrightTimeoutError:
        pass
    if verbose:
        console.print(f"  [dim]Page loaded: {page.url[:80]}[/]")

    block = check_page_blockers(page, url, listing_url, settings, conn, app_id, job_id, verbose)
    if block is not None:
        return block

    return StepResult(result=HandlerResult.SUCCESS)


def handle_route(page, url: str, listing_url: str, settings: dict, conn,
                 app_id: int, job_id: int, verbose: bool, finder=None) -> StepResult:
    """Dismiss modals, click Apply, handle tab switch, dead-page, LinkedIn post-apply.

    Returns StepResult with metadata:
        - page: possibly updated page object (after tab switch)
        - apply_result: raw result from click_apply_button (bool or "new_tab"/"easy_apply")
        - is_easy_apply_flow: bool
        - linkedin_result: raw result from handle_linkedin_post_apply
        - company/position: for move_application_dir on failure
    """
    dismiss_modals(page)
    if verbose:
        console.print("  [dim]Looking for Apply button...[/]")
    apply_result = click_apply_button(page, finder=finder)

    if not apply_result:
        dismiss_modals(page)
        page.wait_for_timeout(500)
        apply_result = click_apply_button(page, finder=finder)

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
    if block is not None:
        # pass page back so kernel can close it
        block.metadata["page"] = page
        return block

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
            log_action(conn, "apply_failed", f"Dead page after apply: {page.url[:80]}", app_id, job_id)
            return StepResult(
                result=HandlerResult.FAILED_DEAD_PAGE,
                message="Dead LinkedIn page",
                metadata={"page": page}
            )

    # Handle LinkedIn post-apply flow
    from .platforms.linkedin import handle_linkedin_post_apply
    linkedin_result = handle_linkedin_post_apply(page, apply_result, listing_url)
    if linkedin_result == "failed":
        log_action(conn, "apply_failed",
                   f"Stuck on LinkedIn, no Easy Apply modal. "
                   f"apply_result={apply_result}, listing_url={listing_url}",
                   app_id, job_id)
        return StepResult(
            result=HandlerResult.FAILED,
            message="Stuck on LinkedIn listing page -- no Easy Apply modal",
            metadata={"page": page}
        )

    is_easy_apply_flow = (apply_result == "easy_apply" or linkedin_result == "easy_apply")

    return StepResult(
        result=HandlerResult.SUCCESS,
        metadata={
            "page": page,
            "apply_result": apply_result,
            "linkedin_result": linkedin_result,
            "is_easy_apply_flow": is_easy_apply_flow,
        }
    )


def handle_fill_vision(page, job: dict, settings: dict, resume_file, cl_file,
                       conn, app_id: int, job_id: int, app_dir: Path,
                       take_screenshot: bool, account_registry=None) -> StepResult:
    """Vision path: external ATS via GPT-4o screenshots.

    Returns StepResult with metadata:
        - submitted: bool
        - page: page (may be updated after tab switch)
    """
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
            log_action(conn, "apply_failed", "Stuck on listing page, Apply button unresponsive", app_id, job_id)
            return StepResult(
                result=HandlerResult.FAILED,
                message="Stuck on listing page",
                metadata={"page": page, "submitted": False}
            )

    console.print("  [magenta]External ATS detected -- using vision agent[/]")
    log_action(conn, "vision_handoff", f"External site: {page.url[:80]}", app_id, job_id)

    if detect_login_page(page):
        console.print("  [yellow]Login/signup page detected on ATS -- marking for later[/]")
        log_action(conn, "needs_login", f"ATS requires login: {page.url[:80]}", app_id, job_id)
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            message="ATS requires login",
            metadata={"page": page, "submitted": False, "move_failed": True}
        )

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
            # If this is a file-upload-only step (e.g. Avature "Select Your Resume"),
            # click Continue now so the vision agent starts on the next step instead
            # of looping trying to re-upload and clicking Continue via coordinates.
            # Use specific text detection rather than counting visible inputs (Avature
            # SPAs pre-render all step fields in the DOM even on step 1).
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
                    # Try direct Avature/SPA continue button with a longer visible timeout
                    # before falling back to click_next_button's 500ms checks.
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
                        # Extra wait for SPAs (e.g. Avature) that render step 2 content
                        # after network-idle fires — form fields appear after JS animations
                        page.wait_for_timeout(2000)
                        console.print("  [dim]Advanced past upload-only step via Continue[/]")
                        # After advancing, check for password fields (Avature hybrid
                        # registration+application: step 2 has Password + Confirm Password).
                        # Fill them via account_registry so the vision agent doesn't guess.
                        _fill_password_fields(page, account_registry, settings)
                        # Do a full DOM fill pass for step 2 fields so the vision agent
                        # sees pre-filled form and only needs to handle custom dropdowns
                        # and click "Save and continue" — minimizing navigation risk.
                        try:
                            step2_fields = extract_form_fields(page)
                            if step2_fields:
                                import re as _re2
                                # Exclude Avature work experience fields (172-*-*): avature.py
                                # handles these with title-matching. Generic LLM fill would
                                # misattribute values (zip code → company, etc.).
                                _avature_we_pat = _re2.compile(r'^172-\d+-\d+$')
                                step2_fields = [f for f in step2_fields
                                                if not _avature_we_pat.match(f.get("id") or "")]
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

    # Always fill password fields if present -- handles ATS platforms (e.g. Avature)
    # that embed account creation in the application form. Runs unconditionally so
    # passwords are filled even when the upload step was skipped (Avature auto-advances
    # after file upload on some runs). Vision agent sees ••••• and skips per rule 16.
    prefilled_account_fields = _fill_password_fields(page, account_registry, settings)

    # Platform-specific DOM pre-fill: fills non-standard widgets (Avature custom dropdowns,
    # etc.) that generic extract_form_fields misses. Runs AFTER generic fill and password
    # pre-fill, BEFORE the vision agent, so vision only sees unpredictable fields.
    platform_filled = {}
    try:
        from .platforms import get_platform_prefill
        platform_prefill_fn = get_platform_prefill(page.url)
        if platform_prefill_fn:
            from ..config.loader import load_profile
            profile_data = load_profile()
            platform_filled = platform_prefill_fn(page, profile_data, settings) or {}
            if platform_filled:
                page.wait_for_timeout(300)
    except Exception as e:
        console.print(f"  [dim]Platform prefill error: {str(e)[:60]}[/]")
        logger.debug(f"Platform prefill failed: {e}")

    # Scroll to top so vision agent coordinates are anchored to viewport top,
    # preventing coordinate drift between rounds when the form was scrolled.
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
    except Exception:
        pass

    # Debug pause: after all DOM pre-fill, before vision agent.
    # Triggered by --debug flag. Dumps DOM structure, saves screenshot, waits for Enter.
    debug_mode = settings.get("automation", {}).get("debug_mode", False)
    if debug_mode:
        import pathlib
        debug_dir = pathlib.Path("data/logs")
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_shot = debug_dir / "debug_prefill.png"
        page.screenshot(path=str(debug_shot), full_page=True)
        console.print(f"\n  [bold yellow]DEBUG: DOM pre-fill complete.[/]")
        console.print(f"  [bold yellow]  Screenshot: {debug_shot}[/]")
        if platform_filled:
            console.print(f"  [bold yellow]  Platform prefill filled: {list(platform_filled.keys())}[/]")
        # Auto-dump DOM structure for dropdown/widget inspection
        _debug_dump_dom(page, debug_dir, console)
        console.print(f"  [bold yellow]  Inspect browser, then press Enter to start vision agent...[/]")
        try:
            input()
        except EOFError:
            pass

    if take_screenshot:
        screenshot_path = app_dir / "pre_submit_screenshot.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        update_application(conn, app_id, screenshot_path=str(screenshot_path))

    # Build initial history context when account fields were pre-filled (e.g. Avature).
    # This prevents the vision agent from re-uploading the resume or refilling
    # credentials that are already in the form.
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

    vision_result = run_vision_agent(page, job, settings, resume_file, cl_file,
                                     initial_history=initial_history,
                                     account_registry=account_registry)
    if vision_result == "needs_login":
        console.print("  [yellow]Login required -- marking for later[/]")
        log_action(conn, "needs_login", f"Vision agent detected login wall: {page.url}", app_id, job_id)
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            message="Vision agent detected login wall",
            metadata={"page": page, "submitted": False}
        )
    if vision_result == "already_applied":
        console.print("  [green]Already applied to this position -- marking as applied[/]")
        log_action(conn, "already_applied", f"Previously applied: {page.url}", app_id, job_id)
        return StepResult(
            result=HandlerResult.ALREADY_APPLIED,
            message="Already applied",
            metadata={"page": page, "submitted": True}
        )
    if vision_result == "needs_verification":
        console.print("  [yellow]Email verification required -- routing to VERIFY_EMAIL[/]")
        log_action(conn, "needs_verification", f"Email verification wall: {page.url}", app_id, job_id)
        return StepResult(
            result=HandlerResult.REQUIRES_VERIFICATION,
            message="Email verification required after account creation",
            metadata={"page": page, "submitted": False}
        )

    submitted = bool(vision_result)
    return StepResult(
        result=HandlerResult.SUCCESS,
        metadata={"page": page, "submitted": submitted}
    )


def _fill_password_fields(page, account_registry, settings: dict) -> bool:
    """Fill account-creation fields (name, email, password) when present on the page.

    Used for ATS platforms (e.g. Avature) that embed account creation inside
    the multi-step application form. Pre-fills credentials fields so the vision
    agent never sees or guesses passwords and skips already-filled fields.

    Returns True if any account fields were filled, False otherwise.
    """
    if not account_registry:
        return False
    try:
        pw_inputs = page.query_selector_all('input[type="password"]')
        if not pw_inputs:
            return False
        from urllib.parse import urlparse
        from .account_registry import detect_ats_platform, extract_tenant, is_auto_register_allowed
        hostname = urlparse(page.url).hostname or ""
        if not is_auto_register_allowed(hostname, settings):
            return False
        platform = detect_ats_platform(hostname)
        tenant = extract_tenant(hostname, platform)
        # Use "fill_vision" status so has_account() (which checks "active"/"pending")
        # doesn't treat this as an established account on the next run.
        # Credentials are stored in case the account gets verified later.
        if account_registry.has_account(hostname):
            creds = account_registry.get_credentials(hostname)
        else:
            use_alias = settings.get("automation", {}).get("use_email_aliases", False)
            creds = account_registry.generate_credentials(hostname, tenant=tenant, platform=platform, use_alias=use_alias)
            # Override status so this partial registration isn't mistaken for an
            # active account on the next run. handle_register will set it to "pending".
            account_registry._conn.execute(
                "UPDATE accounts SET status='fill_vision' WHERE domain=?", (hostname,)
            )
            account_registry._conn.commit()

        # Fill password fields
        password = creds["password"]
        for pw_input in pw_inputs:
            try:
                pw_input.fill(password)
            except Exception as e:
                logger.debug(f"Password field fill failed: {e}")
        filled_count = len(pw_inputs)

        # Fill email field (account email, not a general form field)
        email = creds.get("email", "")
        if email:
            for sel in ['input[type="email"]', 'input[name*="email" i]', 'input[autocomplete="email"]']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.fill(email)
                        filled_count += 1
                        break
                except Exception:
                    continue

        # Fill first name and last name from profile
        try:
            from ..config.loader import load_profile
            profile_data = load_profile()
            personal = profile_data.get("personal", {})
            first_name = personal.get("first_name", "")
            last_name = personal.get("last_name", "")
        except Exception:
            first_name = last_name = ""

        if first_name:
            for sel in ['input[name*="first" i]', 'input[placeholder*="first name" i]',
                        'input[id*="first" i]', 'input[autocomplete="given-name"]']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.fill(first_name)
                        filled_count += 1
                        break
                except Exception:
                    continue

        if last_name:
            for sel in ['input[name*="last" i]', 'input[placeholder*="last name" i]',
                        'input[id*="last" i]', 'input[autocomplete="family-name"]']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.fill(last_name)
                        filled_count += 1
                        break
                except Exception:
                    continue

        console.print(f"  [dim]Pre-filled {filled_count} account creation field(s) via registry[/]")
        return filled_count > 0
    except Exception as e:
        logger.debug(f"_fill_password_fields failed: {e}")
        return False


def handle_fill_selector(page, job: dict, settings: dict, resume_file, cl_file,
                         is_easy_apply: bool, conn, app_id: int, job_id: int,
                         app_dir: Path, take_screenshot: bool, finder=None) -> StepResult:
    """Selector path: LinkedIn Easy Apply or vision disabled.

    Returns StepResult with metadata:
        - submitted: bool
        - form_answers_all: dict of all inferred answers
    """
    max_pages = 10
    if is_easy_apply:
        console.print("  [cyan]Easy Apply multi-step flow -- filling forms...[/]")

    form_answers_all = {}
    company = job.get("company", "Unknown")
    position = job.get("title", "Unknown")

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
        if not click_next_button(page, finder=finder):
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

    submitted = click_submit_button(page, finder=finder)
    return StepResult(
        result=HandlerResult.SUCCESS,
        metadata={"submitted": submitted, "form_answers_all": form_answers_all}
    )


def handle_verify(page, settings: dict, app_dir: Path, use_vision: bool,
                  conn, job_id: int, app_id: int) -> StepResult:
    """Verify submission result with vision check if enabled.

    Assumes submitted=True on entry (caller should only call this when submitted is True).
    Returns StepResult indicating whether the submission is confirmed or rejected.
    """
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
            logger.warning(f"Vision verification rejected submission for job #{job_id}")
            increment_retry_count(conn, job_id)
            update_job_status(conn, job_id, "failed")
            log_action(conn, "false_submission", "Vision verification rejected confirmation", app_id, job_id)
            return StepResult(
                result=HandlerResult.FAILED,
                message="Vision verification rejected confirmation"
            )

    return StepResult(result=HandlerResult.SUCCESS)


def handle_cleanup(submitted: bool, conn, job: dict, app_id: int, app_dir: Path,
                   form_answers_all: dict, url: str) -> StepResult:
    """Update DB status, move application directories, save metadata.

    Returns StepResult indicating final application outcome.
    """
    company = job.get("company", "Unknown")
    position = job.get("title", "Unknown")
    job_id = job["id"]

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
        console.print("  [green]Successfully applied! (verified)[/]")
        console.print(f"  [dim]{final_dir}[/]")
        return StepResult(result=HandlerResult.SUCCESS, metadata={"final_dir": final_dir})
    else:
        increment_retry_count(conn, job_id)
        update_job_status(conn, job_id, "failed")
        log_action(conn, "apply_failed", f"Could not complete application at {url}", app_id, job_id)
        final_dir = move_application_dir(company, position, "failed")
        console.print("  [red]Application failed[/]")
        console.print(f"  [dim]Debug: {final_dir / 'debug_no_submit.png'}[/]")
        return StepResult(result=HandlerResult.FAILED, metadata={"final_dir": final_dir})


def handle_verification(page, settings: dict, conn, app_id: int, job_id: int) -> StepResult:
    """Handle email verification when detected during navigation or form filling.

    Fallback chain:
    1. Email poller (if email_polling enabled)
    2. Manual terminal prompt (if manual_otp enabled)
    3. Mark as needs_login
    """
    auto_settings = settings.get("automation", {})
    domain = get_site_domain(page.url)

    # Try email poller first
    if auto_settings.get("email_polling"):
        console.print(f"  [cyan]Polling email for verification code from {domain}...[/]")
        poller = EmailPoller(
            imap_server=auto_settings.get("imap_server", "imap.gmail.com"),
            imap_port=auto_settings.get("imap_port", 993),
        )
        try:
            poller.connect()
            code = poller.request_verification(
                domain=domain,
                type="otp",
                timeout=auto_settings.get("email_poll_timeout", 120),
            )
            if code:
                otp_field = find_otp_field(page)
                if otp_field:
                    otp_field.fill(code)
                    console.print(f"  [green]OTP filled from email: {code[:2]}***[/]")
                    log_action(conn, "otp_filled", f"Email poller filled OTP for {domain}", app_id, job_id)
                    return StepResult(result=HandlerResult.SUCCESS, message=f"OTP filled from email")
                else:
                    console.print("  [yellow]Got OTP from email but no field found on page[/]")
            else:
                console.print("  [yellow]Email poller timed out -- no verification email received[/]")
        except Exception as e:
            logger.warning(f"Email poller failed: {e}")
            console.print(f"  [yellow]Email poller error: {e}[/]")
        finally:
            poller.disconnect()

    # Fallback: manual terminal prompt
    if auto_settings.get("manual_otp"):
        console.print(f"  [bold yellow]OTP/verification code required for {domain}![/]")
        try:
            code = input(f"  Enter the verification code (or press Enter to skip): ").strip()
        except EOFError:
            code = ""
        if code:
            otp_field = find_otp_field(page)
            if otp_field:
                otp_field.fill(code)
                console.print(f"  [green]OTP filled manually[/]")
                log_action(conn, "otp_filled", f"Manual OTP for {domain}", app_id, job_id)
                return StepResult(result=HandlerResult.SUCCESS, message="OTP filled manually")
            else:
                console.print("  [yellow]No OTP field found on page[/]")

    # No OTP method available or all failed
    console.print(f"  [yellow]Verification required for {domain} -- no OTP method succeeded[/]")
    log_action(conn, "verification_failed", f"No OTP method for {domain}", app_id, job_id)
    return StepResult(
        result=HandlerResult.REQUIRES_LOGIN,
        message=f"Verification required for {domain}, no OTP method available"
    )
