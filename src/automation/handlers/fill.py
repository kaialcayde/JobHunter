"""Kernel form-fill steps."""

import json
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ...core.tailoring import infer_form_answers
from ...db import log_action, update_application
from ..browser_scripts import evaluate_script, load_script
from ..detection import click_apply_button, click_next_button, click_submit_button, detect_login_page
from ..forms import extract_fields, fill_fields, handle_file_uploads
from ..page_checks import force_apply_click, is_listing_page
from ..results import HandlerResult, StepResult
from ..vision_agent import run_vision_agent
from .common import _debug_dump_dom, _fill_password_fields, console, logger


def _merge_fields(existing: list[dict], new_fields: list[dict]) -> list[dict]:
    """Merge extracted fields by stable id so later scroll passes can replace stale locators."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for field in existing + new_fields:
        key = field.get("id") or field.get("label") or f"field_{len(order)}"
        if key not in merged:
            order.append(key)
        merged[key] = field
    return [merged[key] for key in order]


def _fill_scrolled_sections(page, job: dict, settings: dict, existing_fields: list[dict]) -> list[dict]:
    """Scroll through single-page forms once so lazy sections can be extracted and prefilled."""
    merged_fields = list(existing_fields or [])
    fillable_types = {
        "text", "search", "email", "tel", "number", "url", "date", "textarea",
        "select", "radio", "checkbox_group", "checkbox",
    }
    try:
        viewport_height = page.evaluate("() => window.innerHeight || 900") or 900
    except Exception:
        viewport_height = 900

    for pass_num in range(4):
        try:
            scroll_before = page.evaluate("() => window.scrollY")
            page.evaluate("(delta) => window.scrollBy(0, delta)", int(max(viewport_height * 0.85, 450)))
            page.wait_for_timeout(500)
            scroll_after = page.evaluate("() => window.scrollY")
            if scroll_after <= scroll_before:
                break

            section_fields = extract_fields(page, use_playwright=True)
            if not section_fields:
                continue

            prior_count = len(merged_fields)
            merged_fields = _merge_fields(merged_fields, section_fields)
            fillable_fields = [field for field in section_fields if field.get("type") in fillable_types]
            if fillable_fields:
                section_answers = infer_form_answers(fillable_fields, job, settings)
                fill_fields(page, fillable_fields, section_answers, use_playwright=True)
            if len(merged_fields) > prior_count:
                console.print(
                    f"  [dim]Scrolled DOM fill pass {pass_num + 1}: +{len(merged_fields) - prior_count} fields "
                    f"({len(fillable_fields)} fillable)[/]"
                )
        except Exception as e:
            logger.debug(f"Scrolled DOM fill pass {pass_num + 1} failed: {e}")
            break

    return merged_fields


def handle_fill_vision(page, job: dict, settings: dict, resume_file, cl_file,
                       conn, app_id: int, job_id: int, app_dir: Path,
                       take_screenshot: bool, account_registry=None) -> StepResult:
    """Vision path: external ATS via GPT-4o screenshots."""
    debug_dir = Path("data/logs") / f"job_{job_id}" / datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_dir.mkdir(parents=True, exist_ok=True)
    dom_fields = []
    dom_answers = {}

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

    evaluate_script(page, "forms/scroll_first_control.js")
    page.wait_for_timeout(300)

    try:
        dom_fields = extract_fields(page, use_playwright=True)
        if dom_fields:
            console.print(f"  [dim]DOM pre-fill: found {len(dom_fields)} fields[/]")
            dom_answers = infer_form_answers(dom_fields, job, settings)
            fill_fields(page, dom_fields, dom_answers, use_playwright=True)
            uploaded_any = handle_file_uploads(page, resume_file, cl_file)
            if uploaded_any:
                page.wait_for_timeout(1000)
                refreshed_fields = extract_fields(page, use_playwright=True)
                if refreshed_fields:
                    text_like_types = {"text", "search", "email", "tel", "number", "url", "date", "textarea"}
                    refill_fields = [
                        field for field in refreshed_fields
                        if field.get("type") in text_like_types
                    ]
                    if refill_fields:
                        refill_answers = infer_form_answers(refill_fields, job, settings)
                        fill_fields(page, refill_fields, refill_answers, use_playwright=True)
                    dom_fields = refreshed_fields
                    dom_answers = infer_form_answers(dom_fields, job, settings)
            try:
                is_upload_step = evaluate_script(page, "forms/is_upload_step.js")
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
                            step2_fields = extract_fields(page, use_playwright=True)
                            if step2_fields:
                                import re as _re2

                                _avature_we_pat = _re2.compile(r'^172-\d+-\d+$')
                                step2_fields = [f for f in step2_fields if not _avature_we_pat.match(f.get("id") or "")]
                                step2_answers = infer_form_answers(step2_fields, job, settings)
                                fill_fields(page, step2_fields, step2_answers, use_playwright=True)
                                handle_file_uploads(page, resume_file, cl_file)
                                console.print(f"  [dim]Post-advance DOM fill: {len(step2_fields)} fields[/]")
                        except Exception as e:
                            console.print(f"  [dim]Post-advance DOM fill skipped: {str(e)[:60]}[/]")
                    else:
                        console.print("  [dim]Upload-step advance FAILED -- vision agent will handle[/]")
            except Exception as e:
                console.print(f"  [dim]Upload-step advance error: {e}[/]")
            dom_fields = _fill_scrolled_sections(page, job, settings, dom_fields)
            if dom_fields:
                dom_answers = infer_form_answers(dom_fields, job, settings)
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
        debug_shot = debug_dir / "debug_prefill.png"
        page.screenshot(path=str(debug_shot), full_page=True)
        console.print("\n  [bold yellow]DEBUG: DOM pre-fill complete.[/]")
        console.print(f"  [bold yellow]  Screenshot: {debug_shot}[/]")
        try:
            serializable_fields = [
                {k: v for k, v in field.items() if not k.startswith("_")}
                for field in dom_fields
            ]
            (debug_dir / "dom_fields.json").write_text(json.dumps(serializable_fields, indent=2))
            (debug_dir / "dom_answers.json").write_text(json.dumps(dom_answers, indent=2))
            console.print(f"  [bold yellow]  DOM fields: {debug_dir / 'dom_fields.json'} ({len(dom_fields)} entries)[/]")
            console.print(f"  [bold yellow]  DOM answers: {debug_dir / 'dom_answers.json'} ({len(dom_answers)} entries)[/]")
            custom_select_options = {}
            control_contexts = {}
            for field in dom_fields:
                locator = field.get("_locator")
                if not locator:
                    continue
                label = (field.get("label") or "").strip().lower()
                should_dump_context = (
                    field.get("type") == "custom_select"
                    or label.startswith(("input-", "field-", "select-", "custom_select_"))
                    or "show menu" in label
                )
                if should_dump_context:
                    try:
                        control_contexts[field.get("label") or field.get("id")] = locator.evaluate(
                            load_script("debug/describe_control_context.js")
                        )
                    except Exception:
                        pass
                if field.get("type") != "custom_select":
                    continue
                try:
                    locator.click(timeout=1500)
                    page.wait_for_timeout(300)
                    options = evaluate_script(page, "debug/list_open_dropdown_options.js")
                    if options:
                        custom_select_options[field.get("label") or field.get("id")] = options
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(100)
                except Exception:
                    continue
            if custom_select_options:
                (debug_dir / "dom_custom_select_options.json").write_text(json.dumps(custom_select_options, indent=2))
                console.print(f"  [bold yellow]  Custom select options: {debug_dir / 'dom_custom_select_options.json'}[/]")
            if control_contexts:
                (debug_dir / "dom_field_contexts.json").write_text(json.dumps(control_contexts, indent=2))
                console.print(f"  [bold yellow]  Control contexts: {debug_dir / 'dom_field_contexts.json'}[/]")
        except Exception as e:
            console.print(f"  [dim]DOM debug dump skipped: {str(e)[:60]}[/]")
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
        debug_dir=debug_dir,
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
