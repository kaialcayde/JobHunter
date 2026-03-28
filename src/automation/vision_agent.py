"""Vision-based browser agent using GPT-4o for external ATS form filling.

Takes screenshots, sends to vision model, gets a BATCH of actions back for all
visible fields, executes them all, then takes the next screenshot. Typically
completes a form in 3-5 rounds instead of 20-30 single-action steps.
"""

import base64
import json
import logging
import os
import time

from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console

load_dotenv()

logger = logging.getLogger(__name__)
console = Console(force_terminal=True)

# DOM fill helpers live in forms.py — import the public API
from .forms import find_input_at_coords, dom_fill_fallback, dom_select_fallback

MAX_ROUNDS = 15  # safety limit — each round fills multiple fields
MAX_CONSECUTIVE_SCROLLS = 2
VISION_MODEL_DEFAULT = "gpt-4o-mini"

SYSTEM_PROMPT = """You are a job application assistant controlling a web browser via screenshots.
You are applying to {position} at {company}.

## Candidate Info
{profile_summary}

## Instructions
Look at the screenshot and return actions for ALL visible form fields that need filling.
Return ONLY valid JSON (no markdown fences) with this structure:

{{
  "actions": [
    {{"action": "type", "x": <int>, "y": <int>, "text": "<value>", "reasoning": "<brief>"}},
    {{"action": "upload_resume", "x": <int>, "y": <int>, "reasoning": "<brief>"}},
    ...
  ],
  "status": "continue" | "done" | "stuck",
  "reasoning": "<overall status — what you filled, what's left>"
}}

## Actions (used inside the "actions" array)
- "click": click at (x, y) — buttons, links, radio buttons
- "type": click field at (x, y), clear it, then type text — text inputs, textareas
- "select": click dropdown at (x, y) to open it, then select the option matching "text"
- "check": click checkbox at (x, y) — for checkboxes, consent boxes, multi-select options
- "scroll": scroll the page in given "direction" ("up" or "down") to reveal more fields
- "upload_resume": click the resume upload area/button at (x, y), system handles the file
- "upload_cover_letter": click the cover letter upload area/button at (x, y), system handles the file

## Status
- "continue": there are more fields to fill (e.g. need to scroll down) or ready to submit
- "done": application was submitted (you see a confirmation/thank you page)
- "stuck": cannot proceed (CAPTCHA, login wall, error, unrecoverable)

## Critical Rules
1. Return actions for ALL visible unfilled fields at once, working TOP to BOTTOM. Do NOT return just one action — batch them all together.
2. If all visible fields are filled and you need to reveal more, include a single "scroll" action at the END of your actions array.
3. If all fields are filled and the Submit/Apply button is visible, include a "click" action for it.
4. Fill ALL required fields before attempting to click Apply/Submit. If the button looks greyed out or disabled, there are likely unfilled required fields — scroll up and check.
5. For CONSENT CHECKBOXES (e.g., "I consent to receiving text messages", terms of service): you MUST check these before Apply/Submit will be enabled. Use "check" action.
6. For DROPDOWNS/SELECT fields: use "select" action with the "text" field set to the option to pick.
7. For CHECKBOX GROUPS (e.g., "Select Azure services you know"): check each relevant one individually using "check" action.
8. For FILE UPLOADS labeled "Drop or select" or "Attach" or "Upload": use "upload_resume" or "upload_cover_letter" action and click the upload area.
9. For PRONOUNS: type the appropriate pronouns (e.g., "He/Him", "She/Her", "They/Them").
10. Fill form fields with the candidate's REAL info only. If you don't know the answer, type "N/A" — NEVER fabricate.
11. For diversity/EEO questions, select "Prefer not to answer" or "Decline to self-identify".
12. For "How did you hear about us", use "Job Board".
13. Click coordinates should target the CENTER of the element — especially the center of input fields, not their labels.
14. If fields you previously filled appear EMPTY in the screenshot, they may need re-filling. Include them again.
15. If you see a JOB LISTING page instead of an APPLICATION FORM, return a single "click" action for the "Apply" or "Apply Now" button.
16. If the form has fields already filled (non-placeholder text), skip those fields — do NOT re-fill them.
17. If you see a confirmation/thank you page, set status to "done" with an empty actions array.
18. If you see a CAPTCHA, login wall, or error you cannot resolve, set status to "stuck".
"""


# ── Settings Helpers ───────────────────────────────────────────────

def _get_vision_client(settings: dict) -> OpenAI:
    """Get OpenAI client for vision calls."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key == "your-openai-api-key-here":
        raise ValueError("OPENAI_API_KEY not set in .env file.")
    return OpenAI(api_key=api_key, timeout=60)


def _get_vision_model(settings: dict) -> str:
    """Get vision model from settings."""
    return settings.get("automation", {}).get("vision_model", VISION_MODEL_DEFAULT)


def _is_vision_logging(settings: dict) -> bool:
    """Check if vision agent logging is enabled."""
    return settings.get("automation", {}).get("vision_logging", True)


def _get_vision_detail(settings: dict) -> str:
    """Get image detail level from settings. 'low' = 85 tokens, 'high' = 12-17K tokens."""
    return settings.get("automation", {}).get("vision_detail", "high")


# ── Screenshot & API ───────────────────────────────────────────────

def _take_screenshot(page) -> str:
    """Take a screenshot and return as base64-encoded string."""
    screenshot_bytes = page.screenshot(type="png")
    return base64.b64encode(screenshot_bytes).decode("utf-8")


def _decide_actions(client: OpenAI, model: str, screenshot_b64: str,
                    system_prompt: str, history: list[str],
                    detail: str = "low") -> dict:
    """Send screenshot to vision model, get batch of actions back."""
    history_text = ""
    if history:
        recent = history[-10:]
        history_text = "\n\nPrevious rounds:\n" + "\n".join(f"- {h}" for h in recent)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text", "text": f"What actions should I take for all visible fields?{history_text}"},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{screenshot_b64}",
                "detail": detail,
            }}
        ]}
    ]

    response = client.chat.completions.create(
        model=model,
        temperature=0.1,
        max_tokens=2000,
        messages=messages,
    )

    text = response.choices[0].message.content.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    usage = response.usage
    logger.info(f"Vision API: {usage.prompt_tokens}+{usage.completion_tokens} tokens, model={model}")

    return json.loads(text)


# ── Action Execution ───────────────────────────────────────────────

def _execute_action(page, action: dict, resume_file, cl_file) -> str:
    """Execute a single action on the page. Returns description of what was done."""
    act = action.get("action", "stuck")
    x = action.get("x", 0)
    y = action.get("y", 0)
    text = action.get("text", "")
    reasoning = action.get("reasoning", "")

    if act == "click":
        page.mouse.click(x, y)
        page.wait_for_timeout(1000)
        return f"Clicked ({x}, {y}): {reasoning}"

    elif act == "type":
        # Check if the field already has the desired value (avoid clearing pre-filled DOM values)
        el_info = find_input_at_coords(page, x, y)
        if el_info and el_info.get("value", "").strip():
            existing = el_info["value"].strip().lower()
            desired = text.strip().lower()
            # If field already contains the desired text (or close enough), skip
            if existing == desired or desired in existing or existing in desired:
                return f"Skipped '{text[:50]}' at ({x}, {y}) [already filled]: {reasoning}"

        # Try DOM fill first (most reliable for React/controlled inputs)
        if el_info and el_info.get("selector"):
            if dom_fill_fallback(page, x, y, text):
                return f"Typed '{text[:50]}' at ({x}, {y}) [DOM fill]: {reasoning}"

        # Fallback: coordinate-based click + keyboard typing
        page.mouse.click(x, y)
        page.wait_for_timeout(300)
        page.mouse.click(x, y, click_count=3)
        page.wait_for_timeout(100)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(100)
        page.keyboard.type(text, delay=30)
        page.keyboard.press("Tab")
        page.wait_for_timeout(300)

        # Verify the value was set
        el_info_after = find_input_at_coords(page, x, y)
        value_set = False
        if el_info_after and el_info_after.get("value"):
            value_set = len(el_info_after["value"].strip()) > 0

        if not value_set:
            # Last resort: JS value dispatch
            if dom_fill_fallback(page, x, y, text):
                return f"Typed '{text[:50]}' at ({x}, {y}) [DOM fallback]: {reasoning}"

        return f"Typed '{text[:50]}' at ({x}, {y}): {reasoning}"

    elif act == "select":
        # Check if a React-Select at these coords already has the desired value
        already_selected = page.evaluate("""({x, y, text}) => {
            let el = document.elementFromPoint(x, y);
            if (!el) return false;
            const container = el.closest('.select, .select__container, .select__control, [class*="select"]');
            if (!container) return false;
            const sv = container.querySelector('[class*="single-value"], [class*="singleValue"]');
            if (sv && sv.textContent.trim()) {
                const current = sv.textContent.trim().toLowerCase();
                const desired = text.toLowerCase();
                return current === desired || current.includes(desired) || desired.includes(current);
            }
            return false;
        }""", {"x": x, "y": y, "text": text})
        if already_selected:
            return f"Skipped select '{text}' at ({x}, {y}) [already selected]: {reasoning}"

        # First try: DOM-based select for native <select> elements or React-Select
        if dom_select_fallback(page, x, y, text):
            return f"Selected '{text}' at ({x}, {y}) [DOM select]: {reasoning}"

        # Custom dropdown: click to open, then find option
        page.mouse.click(x, y)
        page.wait_for_timeout(1000)

        # Try to find and click the option by visible role="option" elements
        try:
            option_selectors = [
                f'[role="option"]:has-text("{text}")',
                f'li:has-text("{text}")',
                f'[class*="option"]:has-text("{text}")',
            ]
            for opt_sel in option_selectors:
                try:
                    opts = page.query_selector_all(opt_sel)
                    for opt in opts:
                        if opt.is_visible():
                            opt.click()
                            page.wait_for_timeout(500)
                            return f"Selected '{text}' at ({x}, {y}) [option click]: {reasoning}"
                except Exception as e:
                    logger.debug(f"Option selector {opt_sel} failed: {e}")
                    continue
        except Exception as e:
            logger.debug(f"Option search failed: {e}")

        # Try get_by_text for broader matching
        try:
            option = page.get_by_text(text, exact=False).first
            if option.is_visible():
                option.click(timeout=3000)
                page.wait_for_timeout(500)
                return f"Selected '{text}' at ({x}, {y}): {reasoning}"
        except Exception as e:
            logger.debug(f"get_by_text select failed: {e}")

        # Type to filter in the dropdown, then pick first visible option or press Enter
        try:
            page.keyboard.type(text, delay=50)
            page.wait_for_timeout(800)
            # Check for visible options after typing
            first_opt = page.query_selector('[role="option"]:visible, li[class*="option"]:visible')
            if first_opt and first_opt.is_visible():
                first_opt.click()
                page.wait_for_timeout(500)
                return f"Selected '{text}' at ({x}, {y}) [type+click]: {reasoning}"
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
        except Exception as e:
            logger.debug(f"Type-to-filter select failed: {e}")
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)

        return f"Selected '{text}' at ({x}, {y}) [type+enter]: {reasoning}"

    elif act == "check":
        # Try DOM-based click first (more reliable for hidden checkboxes)
        el_info = find_input_at_coords(page, x, y)
        if el_info and el_info.get("tagName") == "INPUT" and el_info.get("type") == "checkbox" and el_info.get("selector"):
            try:
                el = page.query_selector(el_info["selector"])
                if el:
                    el.click()
                    page.wait_for_timeout(500)
                    return f"Checked checkbox at ({x}, {y}) [DOM]: {reasoning}"
            except Exception as e:
                logger.debug(f"DOM checkbox click failed: {e}")
        # Fallback to coordinate click
        page.mouse.click(x, y)
        page.wait_for_timeout(500)
        return f"Checked checkbox at ({x}, {y}): {reasoning}"

    elif act == "scroll":
        direction = action.get("direction", "down")
        delta = -400 if direction == "up" else 400
        scroll_before = page.evaluate("() => window.scrollY")
        page.mouse.wheel(0, delta)
        page.wait_for_timeout(800)
        scroll_after = page.evaluate("() => window.scrollY")
        if scroll_before == scroll_after:
            return f"Scroll {direction} had NO EFFECT (already at {'bottom' if direction == 'down' else 'top'}). Do NOT scroll {direction} again."
        return f"Scrolled {direction}: {reasoning}"

    elif act == "upload_resume":
        if resume_file:
            page.mouse.click(x, y)
            page.wait_for_timeout(500)
            file_inputs = page.query_selector_all('input[type="file"]')
            if file_inputs:
                file_inputs[0].set_input_files(str(resume_file))
                page.wait_for_timeout(1500)
                return f"Uploaded resume: {resume_file}"
            try:
                with page.expect_file_chooser(timeout=3000) as fc_info:
                    page.mouse.click(x, y)
                file_chooser = fc_info.value
                file_chooser.set_files(str(resume_file))
                page.wait_for_timeout(1500)
                return f"Uploaded resume via file chooser: {resume_file}"
            except Exception as e:
                logger.debug(f"Resume file chooser failed: {e}")
        return "No resume file to upload or upload failed"

    elif act == "upload_cover_letter":
        if cl_file:
            page.mouse.click(x, y)
            page.wait_for_timeout(500)
            file_inputs = page.query_selector_all('input[type="file"]')
            if len(file_inputs) > 1:
                file_inputs[1].set_input_files(str(cl_file))
                page.wait_for_timeout(1500)
                return f"Uploaded cover letter: {cl_file}"
            elif file_inputs:
                file_inputs[0].set_input_files(str(cl_file))
                page.wait_for_timeout(1500)
                return f"Uploaded cover letter to first input: {cl_file}"
            try:
                with page.expect_file_chooser(timeout=3000) as fc_info:
                    page.mouse.click(x, y)
                file_chooser = fc_info.value
                file_chooser.set_files(str(cl_file))
                page.wait_for_timeout(1500)
                return f"Uploaded cover letter via file chooser: {cl_file}"
            except Exception as e:
                logger.debug(f"Cover letter file chooser failed: {e}")
        return "No cover letter file to upload or upload failed"

    elif act == "done":
        return "DONE: Application appears submitted"

    elif act == "stuck":
        return f"STUCK: {reasoning}"

    return f"Unknown action: {act}"


def _extract_batch_coords(actions: list[dict]) -> set[tuple[int, int]]:
    """Extract rounded (x, y) coordinates from a batch for repeat detection."""
    coords = set()
    for a in actions:
        if a.get("action") in ("type", "click", "check", "select", "upload_resume", "upload_cover_letter"):
            # Round to nearest 30px grid for fuzzy matching
            rx = round(a.get("x", 0) / 30) * 30
            ry = round(a.get("y", 0) / 30) * 30
            coords.add((rx, ry))
    return coords


# ── Submission Verification ────────────────────────────────────────

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

Signs of REAL submission:
- "Thank you for applying" / "Application submitted" / "Application received"
- Confirmation number or reference ID
- "You will hear from us" / "We will review your application"

Signs it was NOT submitted:
- Still showing a form with empty fields or a "Continue" / "Next" / "Apply" button
- Still on the job listing/description page
- Error messages or validation warnings
- Login or signup page
- The page is asking for information (email, name, resume, etc.)

Be strict: if in doubt, return false."""},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{screenshot_b64}",
                "detail": detail,
            }}
        ]}
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


# ── Extracted Helpers for run_vision_agent ──────────────────────────

def _dom_refill_after_captcha(page, job, settings, resume_file, cl_file):
    """Re-fill form via DOM after CAPTCHA solve (page may have reloaded/cleared fields)."""
    try:
        from .forms import extract_form_fields, fill_form_fields, handle_file_uploads
        from ..core.tailoring import infer_form_answers
        dom_fields = extract_form_fields(page)
        if dom_fields:
            console.print(f"  [dim]DOM re-fill after CAPTCHA: {len(dom_fields)} fields[/]")
            dom_answers = infer_form_answers(dom_fields, job, settings)
            fill_form_fields(page, dom_fields, dom_answers)
            handle_file_uploads(page, resume_file, cl_file)
            page.wait_for_timeout(500)
    except Exception as e:
        logger.debug(f"DOM re-fill after CAPTCHA failed: {e}")


def _try_dom_advance(page, settings, history, label: str):
    """Try DOM-based Next/Submit to break out of a vision agent loop.

    Returns "advanced" (moved to next step), "submitted" (form submitted),
    or None (nothing worked).
    """
    from .detection import click_next_button, click_submit_button
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


def _handle_done_status(page, settings, history, job, resume_file, cl_file):
    """Handle vision agent 'done' status.

    Returns:
        "submitted" — application confirmed submitted
        "captcha_failed" — CAPTCHA blocked and unsolvable
        "continue" — false positive, keep going
    """
    actually_done = verify_submission(page, settings)
    if actually_done:
        console.print("  [green]Vision agent: application submitted![/]")
        return "submitted"

    # Try DOM-based submit click — vision coordinate clicks often miss the submit button
    from .detection import click_submit_button
    url_before = page.url
    if click_submit_button(page):
        page.wait_for_timeout(2000)
        if page.url != url_before or verify_submission(page, settings):
            console.print("  [green]Application submitted via DOM click![/]")
            return "submitted"

    # Check if an invisible CAPTCHA blocked the submit
    from .detection import detect_captcha, try_solve_captcha
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
        else:
            console.print("  [yellow]CAPTCHA blocked submit and could not be solved -- giving up[/]")
            return "captcha_failed"

    console.print("  [yellow]Vision agent said 'done' but page still shows form -- continuing[/]")
    history.append(
        "You reported 'done' but the page still shows a form — the application was NOT submitted. "
        "Look for a Submit/Apply/Send button and click it. If there are unfilled required fields, fill them first."
    )
    return "continue"


def _handle_stuck_status(page, settings, history, overall_reasoning, round_num,
                         job, resume_file, cl_file):
    """Handle vision agent 'stuck' status.

    Returns (action, page) where action is:
        "continue" — recovered, keep going
        "needs_login" — login wall detected
        "already_applied" — already applied to this position
        False — genuinely stuck, give up
    page may differ from input if navigation happened.
    """
    from .detection import detect_captcha, try_solve_captcha, detect_login_page

    # CAPTCHA gating the page
    if detect_captcha(page):
        console.print(f"  [cyan]CAPTCHA detected while stuck -- attempting solve[/]")
        if try_solve_captcha(page, settings):
            console.print(f"  [green]CAPTCHA solved -- retrying[/]")
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

        console.print(f"  [yellow]CAPTCHA detected but could not solve -- giving up[/]")
        return False, page

    reason_lower = (overall_reasoning or "").lower()

    # Already applied — no point retrying
    if any(kw in reason_lower for kw in ["already applied", "already been submitted", "already submitted"]):
        console.print(f"  [yellow]Vision agent: already applied to this position[/]")
        return "already_applied", page

    # Login pages are unrecoverable — bail immediately
    if any(kw in reason_lower for kw in [
        "login", "log in", "sign in", "password", "credentials",
        "create account", "create an account", "sign up", "signup",
        "account creation",
    ]):
        console.print(f"  [yellow]Vision agent stuck (login wall): {overall_reasoning}[/]")
        return "needs_login", page

    # Fallback: use DOM-based login detection
    if detect_login_page(page):
        console.print(f"  [yellow]Vision agent stuck (login page detected via DOM)[/]")
        return "needs_login", page

    # Listing page — try force_apply_click to navigate to the actual form
    if any(kw in reason_lower for kw in ["job description", "job listing", "listing page", "not the application form"]):
        from .page_checks import force_apply_click
        console.print(f"  [dim]Stuck on listing -- trying force apply click...[/]")
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
        console.print(f"  [yellow]Could not leave listing page[/]")
        return False, page

    # In early rounds, "stuck" is often a misread — retry with a fresh screenshot
    if round_num < 2:
        console.print(f"  [yellow]Vision agent stuck (round {round_num+1}): {overall_reasoning} -- retrying[/]")
        history.append(
            f"You reported 'stuck' but this is round {round_num+1}. "
            "Look again carefully: if you see form fields (name, email, resume upload, etc.), "
            "this IS the application form, not a job listing. Fill the fields."
        )
        page.wait_for_timeout(2000)
        return "continue", page

    console.print(f"  [yellow]Vision agent stuck: {overall_reasoning}[/]")
    return False, page


# ── OTP Resolution ─────────────────────────────────────────────────

def _try_resolve_otp(page, settings: dict) -> str | None:
    """Try to resolve an OTP code via email poller, then manual prompt.

    Returns:
        The OTP code string if successfully entered on the page.
        None if all methods exhausted (caller should bail).
    """
    from .email_poller import EmailPoller, find_otp_field
    from .page_checks import get_site_domain

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
                    return code
                else:
                    console.print("  [yellow]Got OTP from email but no field found on page[/]")
            else:
                console.print("  [yellow]Email poller timed out[/]")
        except Exception as e:
            logger.warning(f"Email poller failed: {e}")
            console.print(f"  [yellow]Email poller error: {e}[/]")
        finally:
            poller.disconnect()

    # Fallback: manual terminal prompt
    if auto_settings.get("manual_otp"):
        console.print("  [bold yellow]OTP/verification code required![/]")
        try:
            user_code = input("  Enter the verification code (or press Enter to skip): ").strip()
        except EOFError:
            user_code = ""
        if user_code:
            otp_field = find_otp_field(page)
            if otp_field:
                otp_field.fill(user_code)
                console.print(f"  [green]Entered verification code[/]")
                return user_code
        else:
            console.print(f"  [yellow]No code entered -- skipping[/]")
            return None

    # Neither method available
    console.print(f"  [yellow]OTP required but no method available (enable email_polling or manual_otp)[/]")
    return None


# ── Main Vision Agent Loop ─────────────────────────────────────────

def run_vision_agent(page, job: dict, settings: dict,
                     resume_file=None, cl_file=None) -> bool:
    """Run the vision-based browser agent to complete a job application.

    Takes screenshots and asks the model to return ALL actions for visible fields
    at once. Executes the batch, then takes another screenshot for the next round.
    Typically completes a form in 3-5 rounds.
    """
    from ..config import load_profile, get_profile_summary
    from ..db import get_connection as get_db_conn, get_saved_answers
    profile = load_profile()
    profile_summary = get_profile_summary(profile)

    # Load saved answers from the answer bank
    db_conn = get_db_conn()
    saved_answers = get_saved_answers(db_conn)
    db_conn.close()
    answered = {q: a for q, a in saved_answers.items() if a != "N/A"}
    answer_bank_text = ""
    if answered:
        lines = [f'  - "{q}": "{a}"' for q, a in answered.items()]
        answer_bank_text = "\n\n## Pre-Answered Questions (use these exact answers when you see matching fields)\n" + "\n".join(lines)

    company = job.get("company", "Unknown")
    position = job.get("title", "Unknown")

    system_prompt = SYSTEM_PROMPT.format(
        company=company,
        position=position,
        profile_summary=profile_summary,
    ) + answer_bank_text

    client = _get_vision_client(settings)
    model = _get_vision_model(settings)
    detail = _get_vision_detail(settings)
    vision_logging = _is_vision_logging(settings)

    # Loop state
    history = []
    prev_batch_coords = set()
    repeat_count = 0
    type_loop_rounds = 0
    single_action_repeats = 0
    prev_single_action_key = None
    otp_round_count = 0
    consecutive_scrolls = 0

    console.print(f"  [magenta]Vision agent active (model: {model}, detail: {detail})[/]")

    for round_num in range(MAX_ROUNDS):
        try:
            # 1. Screenshot + API call (with rate-limit retry)
            screenshot_b64 = _take_screenshot(page)
            for attempt in range(3):
                try:
                    response = _decide_actions(client, model, screenshot_b64,
                                               system_prompt, history, detail=detail)
                    break
                except Exception as api_err:
                    if "429" in str(api_err) and attempt < 2:
                        wait = (attempt + 1) * 1.0
                        logger.warning(f"Vision round {round_num+1}: rate limited, retrying in {wait}s")
                        time.sleep(wait)
                    else:
                        raise

            status = response.get("status", "continue")
            actions = response.get("actions", [])
            overall_reasoning = response.get("reasoning", "")

            if vision_logging:
                logger.info(f"Vision round {round_num+1}: status={status}, {len(actions)} actions - {overall_reasoning}")
                console.print(f"  [dim]  Round {round_num+1}: {len(actions)} actions, status={status}[/]")

            # 2. Handle terminal states
            if status == "done":
                result = _handle_done_status(page, settings, history, job, resume_file, cl_file)
                if result == "submitted":
                    return True
                elif result == "captcha_failed":
                    return False
                continue  # "continue" = false positive

            if status == "stuck":
                result, page = _handle_stuck_status(
                    page, settings, history, overall_reasoning, round_num,
                    job, resume_file, cl_file
                )
                if result == "continue":
                    continue
                elif result in ("needs_login", "already_applied"):
                    return result
                return False  # genuinely stuck

            if not actions:
                history.append("Round returned no actions. If form is complete, click Submit. If stuck, report stuck.")
                continue

            # 3. OTP / verification code detection
            otp_keywords = ["verification code", "verify code", "otp", "one-time", "confirmation code", "security code"]
            action_texts = " ".join(a.get("reasoning", "") for a in actions).lower()
            if any(kw in action_texts for kw in otp_keywords):
                otp_round_count += 1
                if otp_round_count == 1:
                    otp_code = _try_resolve_otp(page, settings)
                    if otp_code:
                        otp_round_count = 0
                        history.append("Verification code was entered automatically. Now click Submit/Continue to proceed.")
                        continue
                    elif otp_code is None:
                        # All methods exhausted or skipped
                        return "needs_login"
                elif otp_round_count >= 2:
                    console.print(f"  [yellow]Vision agent: OTP/verification code required -- cannot proceed[/]")
                    return "needs_login"
            else:
                otp_round_count = 0

            # 4. Loop detection — coordinate repeat
            current_coords = _extract_batch_coords(actions)
            if current_coords and current_coords == prev_batch_coords:
                repeat_count += 1
                if repeat_count >= 2:
                    if vision_logging:
                        console.print(f"  [yellow]  Round {round_num+1}: fields targeted 3x -- attempting DOM next/submit[/]")
                    advance = _try_dom_advance(page, settings, history, "repeat bypass")
                    if advance == "advanced":
                        repeat_count = 0
                        prev_batch_coords = None
                        single_action_repeats = 0
                        prev_single_action_key = None
                        continue
                    if advance == "submitted":
                        return True
                    history.append(
                        "CRITICAL: You have targeted the same fields 3 times. The fields ARE filled — "
                        "you cannot see the values due to rendering. STOP trying to fill fields. "
                        "Scroll down and click the Submit/Apply/Continue/Next button NOW."
                    )
                else:
                    history.append(
                        "WARNING: You targeted the same fields as last round but they are still empty. "
                        "Your coordinates may be off — try clicking more precisely at the CENTER of each "
                        "input field (not the label). If fields still won't fill, report 'stuck'."
                    )
                if vision_logging:
                    console.print(f"  [yellow]  Round {round_num+1}: same fields targeted again, warning model[/]")
            else:
                repeat_count = 0
            prev_batch_coords = current_coords

            # 4b. Type-loop detection
            type_actions = sum(1 for a in actions if a.get("action") in ("type", "click"))
            if type_actions >= len(actions) * 0.5 and any(
                "re-fill" in r or "refill" in r
                or "appears empty" in r or "appears incorrect" in r
                or "not filled" in r or "required but" in r
                for a in actions
                for r in [a.get("reasoning", "").lower()]
            ):
                type_loop_rounds += 1
            else:
                type_loop_rounds = 0

            if type_loop_rounds >= 4:
                if vision_logging:
                    console.print(f"  [yellow]  Round {round_num+1}: type-loop detected ({type_loop_rounds} rounds) -- forcing DOM next/submit[/]")
                advance = _try_dom_advance(page, settings, history, "type-loop bypass")
                if advance == "advanced":
                    type_loop_rounds = 0
                    repeat_count = 0
                    prev_batch_coords = None
                    single_action_repeats = 0
                    prev_single_action_key = None
                    continue
                if advance == "submitted":
                    return True
                history.append(
                    "CRITICAL: You have been refilling the same fields for many rounds. The fields ARE filled -- "
                    "you cannot see the values due to rendering. STOP filling fields and click Submit/Continue/Next NOW. "
                    "If Submit does not work, report 'stuck'."
                )

            # 4c. Single-action stuck detection
            if len(actions) == 1:
                a = actions[0]
                action_key = (a.get("action", ""), a.get("reasoning", "")[:40])
                if action_key == prev_single_action_key:
                    single_action_repeats += 1
                else:
                    single_action_repeats = 1
                    prev_single_action_key = action_key
                if single_action_repeats >= 3:
                    if vision_logging:
                        console.print(f"  [yellow]  Round {round_num+1}: single action repeated {single_action_repeats}x -- skipping[/]")
                    history.append(
                        f"CRITICAL: You have tried '{a.get('reasoning', '')[:60]}' for {single_action_repeats} rounds "
                        "but it is not working. SKIP this element entirely. Look for other unfilled fields "
                        "or scroll down to find the Submit button. If the form is complete, click Submit."
                    )
                    continue  # skip executing this round's actions
            else:
                single_action_repeats = 0
                prev_single_action_key = None

            # 5. Execute all actions in the batch
            round_results = []
            scroll_count_this_round = 0

            for i, action in enumerate(actions):
                act_type = action.get("action", "unknown")
                reasoning = action.get("reasoning", "")

                if act_type == "scroll":
                    scroll_count_this_round += 1
                    consecutive_scrolls += 1
                    if consecutive_scrolls > MAX_CONSECUTIVE_SCROLLS:
                        round_results.append("Scroll skipped (too many consecutive scrolls)")
                        continue
                else:
                    consecutive_scrolls = 0

                if vision_logging:
                    console.print(f"  [dim]    {i+1}. {act_type}: {reasoning[:70]}[/]")

                try:
                    result = _execute_action(page, action, resume_file, cl_file)
                    round_results.append(result)
                except Exception as e:
                    logger.debug(f"Action execution error: {e}", exc_info=True)
                    round_results.append(f"Error executing {act_type}: {str(e)[:60]}")

            summary = f"Round {round_num+1}: executed {len(round_results)} actions: " + "; ".join(round_results)
            history.append(summary)
            if vision_logging:
                logger.info(summary)

            # 6. Post-click CAPTCHA check
            has_clicks = any(a.get("action") == "click" for a in actions)
            if has_clicks:
                from .detection import detect_captcha, try_solve_captcha
                if detect_captcha(page):
                    console.print(f"  [cyan]CAPTCHA detected after click -- attempting solve[/]")
                    if try_solve_captcha(page, settings):
                        console.print(f"  [green]CAPTCHA solved![/]")
                        _dom_refill_after_captcha(page, job, settings, resume_file, cl_file)
                        history.append("CAPTCHA was blocking after click. Solved and form re-filled via DOM. Proceed with the form.")
                        page.wait_for_timeout(2000)
                    else:
                        history.append("CAPTCHA detected after click but could not solve.")

            # Wait between rounds
            time.sleep(1.0 if has_clicks else 0.5)

        except json.JSONDecodeError as e:
            logger.warning(f"Vision round {round_num+1}: invalid JSON from model: {e}")
            history.append("Error: model returned invalid JSON — try again with valid JSON")
            continue
        except Exception as e:
            logger.exception(f"Vision round {round_num+1} error")
            history.append(f"Error: {str(e)[:80]}")
            continue

    console.print(f"  [yellow]Vision agent: hit round limit ({MAX_ROUNDS})[/]")
    return False
