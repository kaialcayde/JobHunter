"""Submission and recovery helpers for the vision agent."""

import json

from .client import _get_vision_client, _get_vision_detail, _get_vision_model, _take_screenshot
from .common import PRE_SUBMIT_SYSTEM, PRE_SUBMIT_USER, console, logger


def verify_submission(page, settings: dict) -> bool:
    """Use vision model to verify whether the page shows a real submission confirmation."""
    client = _get_vision_client(settings)
    model = _get_vision_model(settings)
    detail = _get_vision_detail(settings)
    screenshot_b64 = _take_screenshot(page)

    messages = [
        {"role": "system", "content": "You analyze screenshots of job application pages."},
        {"role": "user", "content": [
            {"type": "text", "text": """Look at this screenshot. Was a job application ACTUALLY submitted successfully?

Return ONLY valid JSON (no markdown fences):
{"submitted": true/false, "reasoning": "brief explanation"}

Be strict: if in doubt, return false."""},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{screenshot_b64}",
                "detail": detail,
            }},
        ]},
    ]

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.1,
            max_tokens=150,
            messages=messages,
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        result = json.loads(text)
        submitted = result.get("submitted", False)
        reasoning = result.get("reasoning", "")
        logger.info(f"Vision verification: submitted={submitted}, reason={reasoning}")
        return submitted
    except Exception as e:
        logger.warning(f"Vision verification failed: {e} -- assuming not submitted")
        return False


def pre_submit_sanity_check(page, settings: dict) -> dict | None:
    """Lightweight pre-submission vision check for obvious required fields or errors."""
    try:
        client = _get_vision_client(settings)
        model = _get_vision_model(settings)
        screenshot_b64 = _take_screenshot(page)

        messages = [
            {"role": "system", "content": PRE_SUBMIT_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": PRE_SUBMIT_USER},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{screenshot_b64}",
                    "detail": "auto",
                }},
            ]},
        ]

        response = client.chat.completions.create(
            model=model,
            temperature=0.1,
            max_tokens=200,
            messages=messages,
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        result = json.loads(text)
        usage = response.usage
        logger.info(
            f"Pre-submit check: ready={result.get('ready')}, "
            f"{usage.prompt_tokens}+{usage.completion_tokens} tokens"
        )
        return result
    except Exception as e:
        logger.warning(f"Pre-submit sanity check failed: {e} -- proceeding with submit")
        return None


def _dom_refill_after_captcha(page, job, settings, resume_file, cl_file):
    """Re-fill form via DOM after CAPTCHA solve."""
    try:
        from ..forms import extract_fields, fill_fields, handle_file_uploads
        from ...core.tailoring import infer_form_answers

        dom_fields = extract_fields(page, use_playwright=True)
        if dom_fields:
            console.print(f"  [dim]DOM re-fill after CAPTCHA: {len(dom_fields)} fields[/]")
            dom_answers = infer_form_answers(dom_fields, job, settings)
            fill_fields(page, dom_fields, dom_answers, use_playwright=True)
            handle_file_uploads(page, resume_file, cl_file)
            page.wait_for_timeout(500)
    except Exception as e:
        logger.debug(f"DOM re-fill after CAPTCHA failed: {e}")


def _try_dom_advance(page, settings, history, label: str):
    """Try DOM-based Next/Submit to break out of a vision agent loop."""
    from ..detection import click_next_button, click_submit_button

    url_before = page.url

    if click_next_button(page):
        page.wait_for_timeout(2000)
        history.append("Successfully clicked Next/Continue via DOM. Now on a new step.")
        return "advanced"

    if click_submit_button(page):
        page.wait_for_timeout(2000)
        if page.url != url_before or verify_submission(page, settings):
            console.print(f"  [green]Application submitted via DOM click ({label})![/]")
            return "submitted"

    return None


def _handle_done_status(page, settings, history, job, resume_file, cl_file, *, reported_done: bool = True):
    """Handle a vision agent completion or explicit submit attempt."""
    actually_done = verify_submission(page, settings)
    if actually_done:
        console.print("  [green]Vision agent: application submitted![/]")
        return "submitted"

    check = pre_submit_sanity_check(page, settings)
    if check is not None and not check.get("ready", True):
        issues = check.get("issues", [])
        reasoning = check.get("reasoning", "")
        console.print(f"  [yellow]Pre-submit check: form not ready -- {reasoning}[/]")
        if issues:
            console.print(f"  [dim]  Issues: {'; '.join(issues[:3])}[/]")
        issue_text = "; ".join(issues) if issues else reasoning
        history.append(f"Pre-submit sanity check found problems: {issue_text}. Fix these fields before attempting Submit again.")
        return "continue"

    from ..detection import click_submit_button, detect_captcha, try_solve_captcha

    if click_submit_button(page):
        page.wait_for_timeout(2000)
        if verify_submission(page, settings):
            console.print("  [green]Application submitted via DOM click![/]")
            return "submitted"
        try:
            page_text = page.evaluate("() => (document.body?.innerText || '').toLowerCase()")
            if any(kw in page_text for kw in [
                "verify your email", "check your email", "verification email",
                "email has been sent", "confirm your email",
            ]):
                return "needs_verification"
        except Exception:
            pass
        return "continue"

    if detect_captcha(page):
        console.print("  [cyan]CAPTCHA detected after failed submit -- attempting solve[/]")
        if try_solve_captcha(page, settings):
            console.print("  [green]CAPTCHA solved -- retrying submit[/]")
            _dom_refill_after_captcha(page, job, settings, resume_file, cl_file)
            history.append(
                "A CAPTCHA was blocking form submission. It has been solved. "
                "Fields have been re-filled via DOM. Try submitting again."
            )
            page.wait_for_timeout(2000)
            return "continue"
        console.print("  [yellow]CAPTCHA blocked submit and could not be solved -- giving up[/]")
        return "captcha_failed"

    if reported_done:
        console.print("  [yellow]Vision agent said 'done' but page still shows form -- continuing[/]")
        history.append(
            "You reported 'done' but the page still shows a form — the application was NOT submitted. "
            "Look for a Submit/Apply/Send button and click it. If there are unfilled required fields, fill them first."
        )
    else:
        console.print("  [dim]Submit intent did not submit the form -- continuing[/]")
        history.append(
            "A Submit action was attempted but the page still shows a form. "
            "If there are unfilled required fields, fill them before trying Submit again. "
            "Otherwise, click the real Submit button."
        )
    return "continue"


def _handle_stuck_status(page, settings, history, overall_reasoning, round_num,
                         job, resume_file, cl_file):
    """Handle vision agent 'stuck' status."""
    from ..detection import detect_captcha, detect_login_page, try_solve_captcha

    if detect_captcha(page):
        console.print("  [cyan]CAPTCHA detected while stuck -- attempting solve[/]")
        if try_solve_captcha(page, settings):
            console.print("  [green]CAPTCHA solved -- retrying[/]")
            _dom_refill_after_captcha(page, job, settings, resume_file, cl_file)
            history.append(
                "CAPTCHA was blocking progress. It has been solved. "
                "Fields have been re-filled via DOM. Try clicking Apply again or fill the form."
            )
            page.wait_for_timeout(2000)
            return "continue", page

        manual_verification = settings.get("automation", {}).get("manual_verification", False)
        if manual_verification:
            console.print("  [bold yellow]Verification challenge detected! Browser is open for manual solving.[/]")
            try:
                input("  Solve the CAPTCHA/challenge in the browser, then press Enter to continue: ")
            except EOFError:
                pass
            page.wait_for_timeout(1000)
            history.append("The user manually solved the verification challenge. Continue filling the form or click Submit.")
            return "continue", page

        console.print("  [yellow]CAPTCHA detected but could not solve -- giving up[/]")
        return False, page

    reason_lower = (overall_reasoning or "").lower()

    if any(kw in reason_lower for kw in [
        "verify your email", "check your email", "verification email",
        "email verification", "confirm your email", "verification code",
        "sent to your email", "email has been sent",
    ]):
        console.print("  [yellow]Vision agent: email verification required[/]")
        return "needs_verification", page

    try:
        page_text = page.evaluate("() => (document.body?.innerText || '').toLowerCase()")
        if any(kw in page_text for kw in [
            "verify your email", "check your email", "verification email sent",
            "confirm your email address", "email has been sent",
        ]):
            console.print("  [yellow]Vision agent: verification email page detected via DOM[/]")
            return "needs_verification", page
    except Exception:
        pass

    if any(kw in reason_lower for kw in ["already applied", "already been submitted", "already submitted"]):
        console.print("  [yellow]Vision agent: already applied to this position[/]")
        return "already_applied", page

    if any(kw in reason_lower for kw in [
        "login", "log in", "sign in", "password", "credentials",
        "create account", "create an account", "sign up", "signup",
        "account creation",
    ]):
        console.print(f"  [yellow]Vision agent stuck (login wall): {overall_reasoning}[/]")
        return "needs_login", page

    if detect_login_page(page):
        console.print("  [yellow]Vision agent stuck (login page detected via DOM)[/]")
        return "needs_login", page

    if any(kw in reason_lower for kw in ["job description", "job listing", "listing page", "not the application form"]):
        from ..page_checks import force_apply_click

        console.print("  [dim]Stuck on listing -- trying force apply click...[/]")
        if force_apply_click(page):
            if len(page.context.pages) > 1:
                latest = page.context.pages[-1]
                if latest != page and latest.url != "about:blank":
                    page = latest
                    page.wait_for_load_state("domcontentloaded")
                    console.print(f"  [dim]Navigated to: {page.url[:80]}[/]")
            history.append("Successfully navigated away from the listing page to the application form. Fill out the form fields now.")
            page.wait_for_timeout(1000)
            return "continue", page
        console.print("  [yellow]Could not leave listing page[/]")
        return False, page

    if any(kw in reason_lower for kw in ["upload", "resume upload", "upload your resume", "upload step"]):
        try:
            visible_inputs = page.evaluate(
                "() => [...document.querySelectorAll('input:not([type=hidden]):not([type=file])')"
                ".filter(el => el.offsetParent !== null && el.type !== 'submit')].length"
            )
            if visible_inputs < 3:
                console.print("  [dim]Vision agent back on upload-only step -- re-advancing[/]")
                from ..detection import click_next_button

                if click_next_button(page):
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                    page.wait_for_timeout(1000)
                    history.append("Was stuck on the resume upload step. Advanced past it. Continue filling the application form fields now.")
                    return "continue", page
        except Exception:
            pass

    if round_num < 2:
        console.print(f"  [yellow]Vision agent stuck (round {round_num+1}): {overall_reasoning} -- retrying[/]")
        history.append(
            f"You reported 'stuck' but this is round {round_num+1}. "
            "Look again carefully: if you see form fields, this IS the application form, not a job listing. Fill the fields."
        )
        page.wait_for_timeout(2000)
        return "continue", page

    console.print(f"  [yellow]Vision agent stuck: {overall_reasoning}[/]")
    return False, page
