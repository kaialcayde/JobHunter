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
19. NEVER type into password fields (inputs with dots/bullets ••••). They are pre-filled by the system. If you see a password field, skip it entirely — do NOT include a "type" action for it.
"""

PRE_SUBMIT_SYSTEM = "You inspect job application screenshots for fill errors before submission."

PRE_SUBMIT_USER = """Look at this job application form screenshot.
Is it ready to submit? Check for:
- Visible REQUIRED fields that are empty or still showing placeholder text
- Red validation error messages
- Dropdowns still showing a default/unselected state (e.g. "Select...", "Choose...")

Return ONLY valid JSON (no markdown fences):
{"ready": true/false, "issues": ["issue1", "issue2"], "reasoning": "brief"}

If you see a confirmation/thank-you page, return {"ready": true, "issues": [], "reasoning": "Already submitted"}.
If in doubt, return ready: true — only flag obvious, clearly visible problems."""


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
        # Never type into password fields — they are pre-filled by the system via account_registry.
        # Overwriting them (e.g. with "N/A") causes ATS form validation to fail and reset the form.
        el_info = find_input_at_coords(page, x, y)
        if el_info and el_info.get("type") == "password":
            return f"Skipped password field at ({x}, {y}) [pre-filled by system]: {reasoning}"

        # Check if the field already has the desired value (avoid clearing pre-filled DOM values)
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
        # Check if already selected — expanded beyond React-Select to catch generic patterns
        already_selected = page.evaluate("""({x, y, text}) => {
            let el = document.elementFromPoint(x, y);
            if (!el) return false;
            const desired = text.toLowerCase();
            // React-Select: .single-value inside .select container
            const container = el.closest('.select, .select__container, .select__control, [class*="select"]');
            if (container) {
                const sv = container.querySelector('[class*="single-value"], [class*="singleValue"]');
                if (sv && sv.textContent.trim()) {
                    const current = sv.textContent.trim().toLowerCase();
                    if (current === desired || current.includes(desired) || desired.includes(current))
                        return true;
                }
            }
            // Generic: visible trigger/display element near coordinates shows the value
            const parent = el.closest('div, li, fieldset, label') || el.parentElement;
            if (parent) {
                const displayEls = parent.querySelectorAll('[class*="selected"], [class*="value"], [class*="trigger"]');
                for (const d of displayEls) {
                    const dt = d.textContent.trim().toLowerCase();
                    if (dt === desired || dt.includes(desired)) return true;
                }
            }
            return false;
        }""", {"x": x, "y": y, "text": text})
        if already_selected:
            return f"Skipped select '{text}' at ({x}, {y}) [already selected]: {reasoning}"

        # First try: DOM-based select for native <select> elements or React-Select.
        # Don't return immediately — verify the visible UI actually updated. Some ATS platforms
        # (e.g. Avature) use hidden native <select> + custom UI overlay; the native value gets
        # set but the overlay still shows the placeholder, causing vision to keep re-selecting.
        dom_ok = dom_select_fallback(page, x, y, text)
        if dom_ok:
            page.wait_for_timeout(300)
            visual_updated = page.evaluate("""({x, y, text}) => {
                let el = document.elementFromPoint(x, y);
                if (!el) return false;
                const desired = text.toLowerCase();
                const container = el.closest('[class*="select"], [class*="dropdown"], div, li') || el.parentElement;
                if (!container) return false;
                const triggers = container.querySelectorAll(
                    '[class*="selected"], [class*="value"], [class*="trigger"], [class*="singleValue"], span:not([class*="placeholder"])'
                );
                for (const t of triggers) {
                    const tt = t.textContent.trim().toLowerCase();
                    if (tt === desired || tt.includes(desired)) return true;
                }
                // No placeholder visible = something was selected
                const placeholder = container.querySelector('[class*="placeholder"], [class*="hint"]');
                if (!placeholder) return true;
                const pt = placeholder.textContent.trim().toLowerCase();
                return pt.length > 0 && !pt.includes('select') && !pt.includes('choose') && !pt.includes('pick');
            }""", {"x": x, "y": y, "text": text})
            if visual_updated:
                return f"Selected '{text}' at ({x}, {y}) [DOM select]: {reasoning}"
            # Visual didn't update — fall through to also try visual/coordinate interaction
            logger.debug(f"dom_select_fallback set value but UI unchanged — trying visual click too")

        # Guard: don't click <a> links at these coords — could be stepper navigation
        # (e.g. Avature step nav bar uses clickable <a> tags that navigate between steps).
        # Exception: <a> tags with explicit dropdown ARIA attributes (role=button, aria-haspopup,
        # aria-expanded) are dropdown triggers, not nav links, and should be allowed.
        try:
            is_nav_link = page.evaluate("""({x, y}) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return false;
                const a = el.tagName === 'A' ? el : el.closest('a');
                if (!a) return false;
                // ALLOW: explicit dropdown trigger attributes
                if (a.getAttribute('role') === 'button' ||
                    a.getAttribute('aria-haspopup') ||
                    a.getAttribute('aria-expanded') !== null) return false;
                // BLOCK: all other <a> tags (conservative — stepper links have no popup attrs)
                return true;
            }""", {"x": x, "y": y})
            if is_nav_link:
                logger.debug(f"select: ({x},{y}) hits a nav link — skipping coordinate click")
                return f"Skipped select '{text}' at ({x},{y}): coordinates target a navigation link"
        except Exception:
            pass

        # Use Playwright element-handle click (more reliable than page.mouse.click for ATS
        # like Avature whose custom components need native event dispatch, not synthetic mouse).
        clicked_open = False
        try:
            el_handle = page.evaluate_handle("""({x, y}) => {
                let el = document.elementFromPoint(x, y);
                for (let i = 0; i < 8; i++) {
                    if (!el) break;
                    const tag = el.tagName;
                    const role = el.getAttribute('role');
                    // NOTE: never walk up to <a> tags — they may be nav/stepper links
                    if (tag === 'BUTTON' || tag === 'SELECT' ||
                        role === 'combobox' || role === 'button' || role === 'listbox' ||
                        el.getAttribute('aria-haspopup') || el.getAttribute('tabindex') === '0') {
                        return el;
                    }
                    el = el.parentElement;
                }
                return document.elementFromPoint(x, y);
            }""", {"x": x, "y": y})
            el = el_handle.as_element()
            if el:
                el.click()
                clicked_open = True
        except Exception as e:
            logger.debug(f"Element-handle click failed: {e}")

        if not clicked_open:
            page.mouse.click(x, y)

        # Wait for dropdown options to appear (up to 2s) rather than a fixed sleep
        try:
            page.wait_for_selector(
                '[role="option"]:visible, [role="listbox"] li:visible, '
                'ul[class*="option"]:visible li, ul[class*="dropdown"]:visible li',
                timeout=2000
            )
        except Exception:
            page.wait_for_timeout(800)

        # Search for the matching option with progressively broader selectors
        option_selectors = [
            f'[role="option"]:has-text("{text}")',
            f'li:has-text("{text}")',
            f'[class*="option"]:has-text("{text}")',
            f'[class*="item"]:has-text("{text}")',
            f'[class*="menu"] *:has-text("{text}")',
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

        # Keyboard fallback: type to filter, then pick first visible matching option
        try:
            page.keyboard.type(text[:6], delay=50)
            page.wait_for_timeout(600)
            for opt_sel in ('[role="option"]:visible', 'li:visible[class*="option"]'):
                try:
                    opts = page.query_selector_all(opt_sel)
                    for opt in opts:
                        ot = opt.text_content() or ""
                        if text.lower() in ot.lower() and opt.is_visible():
                            opt.click()
                            page.wait_for_timeout(500)
                            return f"Selected '{text}' at ({x}, {y}) [type+click]: {reasoning}"
                except Exception:
                    pass
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
        except Exception as e:
            logger.debug(f"Keyboard select failed: {e}")
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            except Exception:
                pass

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
            # Try expect_file_chooser FIRST (before any click) so Playwright intercepts
            # the OS file dialog before it opens. Try a text-based button click first
            # (more reliable than coordinates), then fall back to coordinate click.
            upload_trigger_texts = [
                "From Device", "Browse", "Choose File", "Upload", "Attach", "Select File",
            ]
            triggered = False
            for trigger_text in upload_trigger_texts:
                try:
                    with page.expect_file_chooser(timeout=5000) as fc_info:
                        btn = page.get_by_role("button", name=trigger_text, exact=False).first
                        if btn.is_visible(timeout=500):
                            btn.click()
                            triggered = True
                    if triggered:
                        fc_info.value.set_files(str(resume_file))
                        page.wait_for_timeout(4000)  # Wait for AJAX upload to complete
                        return f"Uploaded resume via file chooser ({trigger_text}): {resume_file.name}"
                except Exception:
                    triggered = False
                    continue
            # Coordinate-based file chooser fallback
            try:
                with page.expect_file_chooser(timeout=3000) as fc_info:
                    page.mouse.click(x, y)
                fc_info.value.set_files(str(resume_file))
                page.wait_for_timeout(4000)
                return f"Uploaded resume via file chooser (coords): {resume_file.name}"
            except Exception:
                pass
            # Last resort: direct set_input_files on hidden file input
            file_inputs = page.query_selector_all('input[type="file"]')
            if file_inputs:
                file_inputs[0].set_input_files(str(resume_file))
                page.wait_for_timeout(3000)
                return f"Uploaded resume via input element: {resume_file.name}"
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


def pre_submit_sanity_check(page, settings: dict) -> dict | None:
    """Lightweight pre-submission vision check: are there obvious unfilled required fields or errors?

    Uses 'low' detail (~85 image tokens + ~300 text tokens) to keep cost minimal.
    Returns {"ready": bool, "issues": list, "reasoning": str} or None on failure.
    """
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
                    "detail": "auto",  # auto — better accuracy for reading dropdown values vs. low
                }}
            ]}
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

    # Pre-submit sanity check: catch obvious unfilled required fields before clicking Submit.
    # Uses 'low' detail (~400 tokens total) — cheap enough to run every time.
    check = pre_submit_sanity_check(page, settings)
    if check is not None and not check.get("ready", True):
        issues = check.get("issues", [])
        reasoning = check.get("reasoning", "")
        console.print(f"  [yellow]Pre-submit check: form not ready -- {reasoning}[/]")
        if issues:
            console.print(f"  [dim]  Issues: {'; '.join(issues[:3])}[/]")
        issue_text = "; ".join(issues) if issues else reasoning
        history.append(
            f"Pre-submit sanity check found problems: {issue_text}. "
            "Fix these fields before attempting Submit again."
        )
        return "continue"

    # Try DOM-based submit click — vision coordinate clicks often miss the submit button.
    # Require vision confirmation (not just URL change) to avoid false positives on
    # multi-step forms where "CONTINUE" is button[type="submit"] (e.g. Avature).
    from .detection import click_submit_button
    if click_submit_button(page):
        page.wait_for_timeout(2000)
        if verify_submission(page, settings):
            console.print("  [green]Application submitted via DOM click![/]")
            return "submitted"
        # Check if the click advanced to an email verification page
        try:
            page_text = page.evaluate("() => (document.body?.innerText || '').toLowerCase()")
            if any(kw in page_text for kw in [
                "verify your email", "check your email", "verification email",
                "email has been sent", "confirm your email",
            ]):
                return "needs_verification"
        except Exception:
            pass
        # Not submitted and not verification — return continue so vision agent handles
        # the new page (could be next step in multi-step form)
        return "continue"

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

    # Email verification required (e.g. Avature sends OTP after account creation step)
    if any(kw in reason_lower for kw in [
        "verify your email", "check your email", "verification email",
        "email verification", "confirm your email", "verification code",
        "sent to your email", "email has been sent",
    ]):
        console.print(f"  [yellow]Vision agent: email verification required[/]")
        return "needs_verification", page

    # DOM-based verification page detection
    try:
        page_text = page.evaluate("() => (document.body?.innerText || '').toLowerCase()")
        if any(kw in page_text for kw in [
            "verify your email", "check your email", "verification email sent",
            "confirm your email address", "email has been sent",
        ]):
            console.print(f"  [yellow]Vision agent: verification email page detected via DOM[/]")
            return "needs_verification", page
    except Exception:
        pass

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

    # Upload-only page regression (Avature multi-step): vision agent ended up back on
    # the resume upload step (step 1) due to session redirect or accidental navigation.
    # Re-advance past it instead of failing.
    if any(kw in reason_lower for kw in ["upload", "resume upload", "upload your resume", "upload step"]):
        try:
            visible_inputs = page.evaluate(
                "() => [...document.querySelectorAll('input:not([type=hidden]):not([type=file])')"
                ".filter(el => el.offsetParent !== null && el.type !== 'submit')].length"
            )
            if visible_inputs < 3:
                console.print(f"  [dim]Vision agent back on upload-only step — re-advancing[/]")
                from .detection import click_next_button
                if click_next_button(page):
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                    page.wait_for_timeout(1000)
                    history.append("Was stuck on the resume upload step. Advanced past it. Continue filling the application form fields now.")
                    return "continue", page
        except Exception:
            pass

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


# ── Avature Multi-Step Page Handler ────────────────────────────────

def _handle_avature_page(page, job, settings, resume_file, cl_file,
                         account_registry, history) -> str:
    """Handle Avature-specific page transitions deterministically (no vision needed).

    Called when a new Avature page is detected within the vision agent round loop.
    Handles:
    - /careers/Register: Fill form via DOM + credentials + click Save and continue
    - /careers/ApplicationMethods: Upload resume + click Continue

    Returns: "advanced" (page acted on, continue to next round),
             "done" (form submitted),
             "none" (unrecognized page, fall through to vision agent)
    """
    from .forms import extract_form_fields, fill_form_fields, handle_file_uploads
    from ..core.tailoring import infer_form_answers
    from .detection import click_next_button

    url = page.url
    console.print(f"  [dim]_handle_avature_page: url={url[-60:]}[/]")

    # ── Avature Register page ────────────────────────────────────────────────
    if "/careers/Register" in url:
        console.print("  [dim]Avature Register page detected — DOM fill + submit[/]")
        try:
            # Fill text inputs via generic extract (only text/textarea, skip selects
            # to avoid 5s timeouts on select2 widgets — avature prefill handles those)
            try:
                fields = extract_form_fields(page)
                console.print(f"  [dim]Avature Register: extracted {len(fields) if fields else 0} fields[/]")
            except Exception as fe:
                console.print(f"  [dim]Avature Register: extract_form_fields failed: {fe}[/]")
                fields = []
            if fields:
                import re as _re
                # Filter to only text-like fields to avoid select2 timeout waste.
                # Exclude: password (registry fills), Avature 172-* work experience
                # fields (avature.py handles all rows and always overwrites with
                # correct company/title/dates — generic fill misattributes these).
                _avature_we_id = _re.compile(r'^172-\d+-\d+$')
                text_fields = [f for f in fields
                               if f.get("type", "").lower() in
                               ("text", "email", "tel", "url", "textarea", "number",
                                "date", "month", "hidden")
                               and not _avature_we_id.match(f.get("id") or "")]
                if text_fields:
                    answers = infer_form_answers(text_fields, job, settings)
                    fill_form_fields(page, text_fields, answers)
            # Fill Avature-specific select2 + standard select widgets
            from .platforms import get_platform_prefill
            platform_prefill = get_platform_prefill(url)
            if platform_prefill:
                from ..config.loader import load_profile
                try:
                    _profile_data = load_profile()
                except Exception:
                    _profile_data = settings
                platform_prefill(page, _profile_data, settings)
            # Fill credentials (password fields) — must dispatch input/change events
            # so Avature's client-side validation re-evaluates
            if account_registry:
                from urllib.parse import urlparse as _up
                hostname = _up(url).hostname or ""
                creds = account_registry.get_credentials(hostname)  # any status (incl. fill_vision)
                if creds:
                    pw = creds.get("password", "")
                    pw_locators = page.locator('input[type="password"]').all()
                    console.print(f"  [dim]Avature: filling {len(pw_locators)} password field(s) with registry pw (len={len(pw)})[/]")
                    # Use Playwright locator.fill() which dispatches proper keyboard
                    # events that React's synthetic event system picks up.
                    for pw_loc in pw_locators:
                        try:
                            if pw_loc.is_visible(timeout=500):
                                pw_loc.fill(pw, timeout=3000)
                                pw_loc.dispatch_event("blur")
                                page.wait_for_timeout(100)
                        except Exception as _pwe:
                            logger.debug(f"Avature password locator fill failed: {_pwe}")
            page.wait_for_timeout(500)
            # Click Save and continue
            advanced = False
            for btn_name in ("Save and continue", "Continue", "Next"):
                try:
                    loc = page.get_by_role("button", name=btn_name, exact=False).first
                    loc.wait_for(state="visible", timeout=2000)
                    loc.click(timeout=3000)
                    advanced = True
                    break
                except Exception:
                    continue
            if not advanced:
                advanced = click_next_button(page)
            if advanced:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                # Check if the page actually advanced (URL changed)
                new_url = page.url
                if "/careers/Register" in new_url:
                    # Dump validation errors (Avature shows red text near unfilled fields)
                    errors = page.evaluate("""() => {
                        const errs = [];
                        // Find field containers with error state
                        document.querySelectorAll(
                            '.fieldSpec--error, [class*="error"], [class*="invalid"]'
                        ).forEach(el => {
                            // Walk up to find the field container and its label
                            const container = el.closest('.fieldSpec, [class*="field"], [class*="group"]') || el;
                            const label = container.querySelector('label');
                            const labelText = label ? label.innerText.trim().substring(0, 50) : '';
                            const errMsg = el.innerText?.trim().substring(0, 80) || '';
                            if (labelText || errMsg) {
                                errs.push(labelText + ' → ' + errMsg);
                            }
                        });
                        // Check empty required selects/inputs
                        document.querySelectorAll(
                            'select.select2-hidden-accessible, select.SelectFormField'
                        ).forEach(sel => {
                            if (!sel.value || sel.value === '0' || sel.value === '') {
                                const container = sel.closest('.fieldSpec, [class*="field"]');
                                const label = container
                                    ? container.querySelector('label')
                                    : document.querySelector('label[for="' + sel.id + '"]');
                                const txt = label ? label.innerText.trim().substring(0, 50) : sel.id;
                                errs.push('EMPTY_SELECT: ' + txt);
                            }
                        });
                        return errs.slice(0, 15);
                    }""")
                    if errors:
                        console.print(f"  [yellow]Avature Register: validation errors: {errors[:5]}[/]")
                    else:
                        console.print("  [yellow]Avature Register: page did NOT advance (no visible errors)[/]")
                    # Save debug screenshot
                    try:
                        import os
                        debug_path = os.path.join("data", "logs", "debug_avature_register_validation.png")
                        os.makedirs(os.path.dirname(debug_path), exist_ok=True)
                        page.screenshot(path=debug_path)
                        console.print(f"  [dim]Screenshot saved: {debug_path}[/]")
                    except Exception:
                        pass

                    # Handle "existing record" (duplicate email) — switch to login
                    error_text = " ".join(str(e) for e in (errors or []))
                    if "existing record" in error_text.lower():
                        console.print("  [cyan]Avature: account already exists — switching to login[/]")
                        # Look for "Sign In" / "Log In" link on Register page
                        login_clicked = False
                        for link_text in ["Sign In", "Log In", "Login", "Already have an account"]:
                            try:
                                link = page.get_by_role("link", name=link_text, exact=False).first
                                if link.is_visible(timeout=1000):
                                    link.click(timeout=3000)
                                    login_clicked = True
                                    break
                            except Exception:
                                continue
                        if not login_clicked:
                            # Try clicking any link that contains "sign in" or "log in" text
                            try:
                                login_clicked = page.evaluate("""() => {
                                    for (const a of document.querySelectorAll('a')) {
                                        const t = (a.textContent || '').toLowerCase();
                                        if (t.includes('sign in') || t.includes('log in') || t.includes('login')) {
                                            a.click();
                                            return true;
                                        }
                                    }
                                    return false;
                                }""")
                            except Exception:
                                pass
                        if login_clicked:
                            try:
                                page.wait_for_load_state("domcontentloaded", timeout=5000)
                            except Exception:
                                pass
                            page.wait_for_timeout(1000)
                            # Fill login form with registry credentials
                            if account_registry:
                                from urllib.parse import urlparse as _up2
                                _host = _up2(page.url).hostname or ""
                                creds = account_registry.get_credentials(_host)
                                if creds:
                                    email = creds.get("email", "")
                                    pw = creds.get("password", "")
                                    console.print(f"  [dim]Avature login: filling email={email[:3]}*** pw=len({len(pw)})[/]")
                                    # Fill email
                                    for sel in ['input[type="email"]', 'input[name*="email" i]', 'input[id*="email" i]',
                                                'input[name*="user" i]', 'input[id*="user" i]']:
                                        try:
                                            loc = page.locator(sel).first
                                            if loc.is_visible(timeout=500):
                                                loc.fill(email, timeout=2000)
                                                break
                                        except Exception:
                                            continue
                                    # Fill password
                                    for pw_loc in page.locator('input[type="password"]').all():
                                        try:
                                            if pw_loc.is_visible(timeout=500):
                                                pw_loc.fill(pw, timeout=2000)
                                                break
                                        except Exception:
                                            continue
                                    page.wait_for_timeout(300)
                                    # Submit login form
                                    for btn_name in ("Sign In", "Log In", "Login", "Submit"):
                                        try:
                                            btn = page.get_by_role("button", name=btn_name, exact=False).first
                                            btn.wait_for(state="visible", timeout=1500)
                                            btn.click(timeout=3000)
                                            break
                                        except Exception:
                                            continue
                                    try:
                                        page.wait_for_load_state("networkidle", timeout=8000)
                                    except Exception:
                                        pass
                                    page.wait_for_timeout(1500)
                                    console.print(f"  [dim]Avature login: submitted, now at {page.url[-60:]}[/]")
                                    history.append("Avature: logged in with existing account.")
                                    return "advanced"
                        console.print("  [yellow]Avature: could not switch to login — falling through[/]")
                    return "none"
                history.append("Avature Register page: filled via DOM and clicked Save and continue.")
                console.print("  [dim]Avature Register: submitted[/]")
                return "advanced"
        except Exception as e:
            logger.debug(f"Avature Register page handler failed: {e}")
        return "none"

    # ── Avature ApplicationMethods page ──────────────────────────────────────
    if "/careers/ApplicationMethods" in url:
        console.print("  [dim]Avature ApplicationMethods page detected — upload resume + Continue[/]")
        try:
            # Upload resume via file chooser if file input is present
            if resume_file:
                handle_file_uploads(page, resume_file, cl_file)
                page.wait_for_timeout(1000)
            # Click Continue
            advanced = False
            for btn_name in ("Continue", "Save and continue", "Next"):
                try:
                    loc = page.get_by_role("button", name=btn_name, exact=False).first
                    loc.wait_for(state="visible", timeout=2000)
                    loc.click(timeout=3000)
                    advanced = True
                    break
                except Exception:
                    continue
            if not advanced:
                advanced = click_next_button(page)
            if advanced:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                history.append("Avature ApplicationMethods: uploaded resume and clicked Continue.")
                console.print("  [dim]Avature ApplicationMethods: advanced[/]")
                return "advanced"
        except Exception as e:
            logger.debug(f"Avature ApplicationMethods handler failed: {e}")
        return "none"

    # ── Avature ApplicationForm page (Questions step) ────────────────────────
    # Avature's ApplicationForm step contains compliance Yes/No questions
    # (work authorization, visa sponsorship, prior employment, etc.).
    # These can be answered from the profile without vision.
    if "/careers/ApplicationForm" in url:
        console.print("  [dim]Avature ApplicationForm detected — answering compliance questions[/]")
        try:
            from ..config.loader import load_profile
            try:
                _profile_data = load_profile()
            except Exception:
                _profile_data = settings
            auth = _profile_data.get("work_authorization", {})
            authorized = auth.get("authorized_us", True)
            requires_sponsorship = auth.get("requires_sponsorship", False)
            address = _profile_data.get("personal", {}).get("address", {})
            country = address.get("country", "United States")

            answered = page.evaluate("""(args) => {
                const [authorized, requiresSponsor, country] = args;
                const restricted_countries = ['cuba', 'iran', 'north korea', 'syria',
                    'donetsk', 'luhansk', 'ukraine', 'crimea'];

                // Find all radio button groups on the page
                const groups = {};
                for (const radio of document.querySelectorAll('input[type="radio"]')) {
                    if (!radio.name) continue;
                    if (!groups[radio.name]) groups[radio.name] = [];
                    groups[radio.name].push(radio);
                }

                let filled = 0;
                for (const [name, radios] of Object.entries(groups)) {
                    // Already answered — skip
                    if (radios.some(r => r.checked)) continue;

                    // Find the question label (walk up to find label/fieldset text)
                    let questionText = '';
                    const first = radios[0];
                    const fieldset = first.closest('fieldset') || first.closest('.formField') || first.closest('[class*="Field"]');
                    if (fieldset) {
                        const legend = fieldset.querySelector('legend, label, [class*="label"], [class*="question"]');
                        if (legend) questionText = legend.innerText.toLowerCase();
                        else questionText = fieldset.innerText.toLowerCase();
                    }
                    if (!questionText) {
                        // Walk up 3 levels to find text
                        let el = first.parentElement;
                        for (let i = 0; i < 3 && el; i++) {
                            questionText = el.innerText.toLowerCase();
                            if (questionText.length > 10) break;
                            el = el.parentElement;
                        }
                    }

                    // Determine answer
                    let answer = null;
                    if (questionText.includes('legally authorized') || questionText.includes('authorized to work')) {
                        answer = authorized ? 'yes' : 'no';
                    } else if (questionText.includes('sponsorship') || questionText.includes('visa')) {
                        answer = requiresSponsor ? 'yes' : 'no';
                    } else if (questionText.includes('cuba') || questionText.includes('iran') ||
                               questionText.includes('north korea') || questionText.includes('syria') ||
                               questionText.includes('donetsk') || questionText.includes('luhansk') ||
                               questionText.includes('crimea')) {
                        answer = 'no';  // not in restricted regions
                    } else if (questionText.includes('national of') || questionText.includes('citizenship of')) {
                        answer = 'no';
                    } else if (questionText.includes('previously employed') || questionText.includes('previously work') ||
                               questionText.includes('current or former employee') || questionText.includes('been employed by')) {
                        answer = 'no';  // first-time applicant
                    } else if (questionText.includes('relative') || questionText.includes('family member') || questionText.includes('spouse')) {
                        answer = 'no';
                    } else if (questionText.includes('referral') || questionText.includes('referred by')) {
                        answer = 'no';
                    } else {
                        answer = 'no';  // safe default for unknown compliance questions
                    }

                    // Find and click the matching radio
                    for (const radio of radios) {
                        const lbl = document.querySelector(`label[for="${radio.id}"]`);
                        const lblText = (lbl ? lbl.innerText : radio.value || radio.nextSibling?.textContent || '').toLowerCase().trim();
                        if (lblText === answer || (answer === 'yes' && lblText.startsWith('yes')) ||
                            (answer === 'no' && lblText.startsWith('no'))) {
                            radio.click();
                            radio.dispatchEvent(new Event('change', {bubbles: true}));
                            filled++;
                            break;
                        }
                    }
                }
                return filled;
            }""", [authorized, requires_sponsorship, country])

            if answered:
                console.print(f"  [dim]Avature ApplicationForm: answered {answered} compliance question(s)[/]")

            # ── EEO questions (Ethnicity, Gender, Veteran, Disability) ────────
            # These appear on a sub-step of ApplicationForm. Selects use standard
            # <select> elements; Veteran/Disability use radio buttons.
            diversity = _profile_data.get("diversity", {})
            eeo_filled = page.evaluate("""(diversity) => {
                let filled = 0;

                // Handle <select> dropdowns (Ethnicity, Gender)
                for (const sel of document.querySelectorAll('select')) {
                    if (sel.value && sel.value !== '0' && sel.value !== '') continue;
                    const container = sel.closest('.formField, .fieldSpec, [class*="Field"]');
                    if (!container) continue;
                    const label = container.querySelector('label, legend, [class*="label"]');
                    const labelText = (label ? label.innerText : '').toLowerCase();

                    let targetText = '';
                    if (labelText.includes('ethnicity') || labelText.includes('race')) {
                        targetText = diversity.ethnicity || 'decline';
                    } else if (labelText.includes('gender') || labelText.includes('sex')) {
                        targetText = diversity.gender || 'decline';
                    }
                    if (!targetText) continue;

                    // Find best matching option
                    const target = targetText.toLowerCase();
                    let bestOption = null;
                    for (const opt of sel.options) {
                        const optText = opt.text.toLowerCase();
                        if (optText.includes('decline') || optText.includes('prefer not') ||
                            optText.includes('do not wish') || optText.includes('not to self')) {
                            if (!bestOption || target.includes('decline') || target.includes('prefer not')) {
                                bestOption = opt;
                            }
                        }
                        if (target && optText.includes(target)) {
                            bestOption = opt;
                            break;
                        }
                    }
                    // Default: pick "decline" / "prefer not to answer" option
                    if (!bestOption) {
                        for (const opt of sel.options) {
                            const t = opt.text.toLowerCase();
                            if (t.includes('decline') || t.includes('prefer not') || t.includes('not to self')) {
                                bestOption = opt;
                                break;
                            }
                        }
                    }
                    if (bestOption) {
                        sel.value = bestOption.value;
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        filled++;
                    }
                }

                // Handle radio button groups (Veteran Status, Disability)
                const groups = {};
                for (const radio of document.querySelectorAll('input[type="radio"]')) {
                    if (!radio.name) continue;
                    if (!groups[radio.name]) groups[radio.name] = [];
                    groups[radio.name].push(radio);
                }
                for (const [name, radios] of Object.entries(groups)) {
                    if (radios.some(r => r.checked)) continue;

                    let questionText = '';
                    const first = radios[0];
                    const fieldset = first.closest('fieldset, .formField, [class*="Field"]');
                    if (fieldset) {
                        const legend = fieldset.querySelector('legend, label, [class*="label"]');
                        if (legend) questionText = legend.innerText.toLowerCase();
                        else questionText = fieldset.innerText.substring(0, 200).toLowerCase();
                    }

                    let targetLabel = null;
                    if (questionText.includes('veteran')) {
                        const val = (diversity.veteran_status || '').toLowerCase();
                        if (val && val !== 'decline' && val !== 'prefer not') {
                            targetLabel = val;
                        } else {
                            targetLabel = 'not to answer';
                        }
                    } else if (questionText.includes('disability') || questionText.includes('handicap')) {
                        const val = (diversity.disability_status || '').toLowerCase();
                        if (val && val !== 'decline' && val !== 'prefer not') {
                            targetLabel = val;
                        } else {
                            targetLabel = 'not to answer';
                        }
                    }
                    if (!targetLabel) continue;

                    // Find matching radio by label text
                    for (const radio of radios) {
                        const lbl = document.querySelector('label[for="' + radio.id + '"]');
                        const lblText = (lbl ? lbl.innerText : radio.value || '').toLowerCase();
                        if (lblText.includes(targetLabel) || lblText.includes('prefer not') ||
                            lblText.includes('not to answer') || lblText.includes('do not wish') ||
                            lblText.includes('decline')) {
                            radio.click();
                            radio.dispatchEvent(new Event('change', {bubbles: true}));
                            filled++;
                            break;
                        }
                    }
                }
                return filled;
            }""", {
                "ethnicity": diversity.get("ethnicity", ""),
                "gender": diversity.get("gender", ""),
                "veteran_status": diversity.get("veteran_status", ""),
                "disability_status": diversity.get("disability_status", ""),
            })

            if eeo_filled:
                console.print(f"  [dim]Avature ApplicationForm: filled {eeo_filled} EEO question(s)[/]")
            page.wait_for_timeout(500)

            # Try to advance to next step
            advanced = False
            for btn_name in ("Continue", "Save and continue", "Next", "Submit"):
                try:
                    loc = page.get_by_role("button", name=btn_name, exact=False).first
                    loc.wait_for(state="visible", timeout=1500)
                    loc.click(timeout=3000)
                    advanced = True
                    break
                except Exception:
                    continue
            if not advanced:
                advanced = click_next_button(page)
            if advanced:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
                total_answered = (answered or 0) + (eeo_filled or 0)
                history.append(f"Avature ApplicationForm: answered {total_answered} question(s) (compliance={answered}, EEO={eeo_filled}) and clicked Continue.")
                console.print("  [dim]Avature ApplicationForm: advanced[/]")
                return "advanced"
        except Exception as e:
            logger.debug(f"Avature ApplicationForm handler failed: {e}")
        return "none"

    # ── Avature Finalize Application page ─────────────────────────────────
    # Last step — look for "Submit Application" or "Finalize" button
    if "/careers/Finalize" in url or "/careers/Submit" in url:
        console.print("  [dim]Avature Finalize page detected — clicking Submit[/]")
        try:
            advanced = False
            for btn_name in ("Submit Application", "Submit", "Finalize", "Confirm"):
                try:
                    loc = page.get_by_role("button", name=btn_name, exact=False).first
                    loc.wait_for(state="visible", timeout=2000)
                    loc.click(timeout=3000)
                    advanced = True
                    break
                except Exception:
                    continue
            if not advanced:
                advanced = click_next_button(page)
            if advanced:
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                history.append("Avature Finalize: clicked Submit Application.")
                console.print("  [green]Avature Finalize: submitted[/]")
                return "done"
        except Exception as e:
            logger.debug(f"Avature Finalize handler failed: {e}")
        return "none"

    return "none"


# ── Main Vision Agent Loop ─────────────────────────────────────────

def run_vision_agent(page, job: dict, settings: dict,
                     resume_file=None, cl_file=None,
                     initial_history: list = None,
                     account_registry=None) -> bool:
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
    history = list(initial_history) if initial_history else []
    # If initial_history provided, resume was pre-uploaded — block the model from re-uploading
    resume_already_uploaded = bool(initial_history)
    prev_batch_coords = set()
    repeat_count = 0
    type_loop_rounds = 0
    single_action_repeats = 0
    prev_single_action_key = None
    otp_round_count = 0
    consecutive_scrolls = 0
    _round_start_url = page.url  # track URL to detect page navigations between rounds

    console.print(f"  [magenta]Vision agent active (model: {model}, detail: {detail})[/]")

    # Pre-loop: handle Avature-specific pages (Register, ApplicationMethods,
    # ApplicationForm) before vision starts. The vision agent must NOT interact
    # with these pages — it always tries to fill fields and may accidentally
    # submit partial data (e.g. clicking "Save and continue" before passwords
    # are filled, or looping on radio buttons it can't click reliably).
    # Note: ApplicationForm has multiple sub-steps at the same URL path — allow
    # revisiting to handle each sub-step (compliance + EEO questions etc.).
    if "avature.net" in page.url:
        from urllib.parse import urlparse as _urlparse_init
        _avature_visited_init: dict[str, int] = {}  # path → visit count
        _avature_loops_init = 0
        _max_path_visits = 6  # allow up to 6 sub-steps per URL path (ApplicationForm multi-step)
        while _avature_loops_init < 10 and "avature.net" in page.url:
            _av_path_init = _urlparse_init(page.url).path
            _visit_count = _avature_visited_init.get(_av_path_init, 0)
            if _visit_count >= _max_path_visits:
                break  # genuine cycle or unexpected loop
            _avature_visited_init[_av_path_init] = _visit_count + 1
            _av_result_init = _handle_avature_page(
                page, job, settings, resume_file, cl_file, account_registry, history
            )
            _avature_loops_init += 1
            if _av_result_init == "done":
                return True
            if _av_result_init != "advanced":
                break  # "none" = not a known Avature page, let vision handle it
            page.wait_for_load_state("domcontentloaded", timeout=5000)
            page.wait_for_timeout(500)
        _round_start_url = page.url  # update start URL after Avature pre-handling

    for round_num in range(MAX_ROUNDS):
        try:
            # 0. Detect page navigation from previous round — URL path changed means a
            # new step/page was loaded (e.g. Avature Register → ApplicationMethods).
            _current_url = page.url
            if _current_url != _round_start_url:
                from urllib.parse import urlparse as _urlparse
                _prev_path = _urlparse(_round_start_url).path
                _curr_path = _urlparse(_current_url).path
                if _prev_path != _curr_path:
                    # Reset resume flag so model can upload on the new page
                    if resume_already_uploaded:
                        resume_already_uploaded = False
                        console.print(f"  [dim]New page detected — re-enabling resume upload[/]")
                    # Handle Avature-specific pages deterministically (no vision needed).
                    # Chain multiple page advances (e.g. ApplicationMethods → Register →
                    # ApplicationMethods → …) until we reach the application form.
                    if "avature.net" in _current_url:
                        _avature_loops = 0
                        _avature_visited: dict[str, int] = {}  # path → visit count
                        _max_inner_visits = 6
                        while _avature_loops < 10 and "avature.net" in page.url:
                            from urllib.parse import urlparse as _avp
                            _av_path = _avp(page.url).path
                            _av_visit_count = _avature_visited.get(_av_path, 0)
                            if _av_visit_count >= _max_inner_visits:
                                console.print(f"  [yellow]Avature page visit limit ({_av_path}) — falling through to vision[/]")
                                break
                            _avature_visited[_av_path] = _av_visit_count + 1
                            _av_result = _handle_avature_page(
                                page, job, settings, resume_file, cl_file,
                                account_registry, history
                            )
                            _avature_loops += 1
                            if _av_result == "done":
                                return True
                            if _av_result != "advanced":
                                break  # unrecognized Avature page — fall through to vision
                            try:
                                page.wait_for_load_state("domcontentloaded", timeout=5000)
                            except Exception:
                                pass
                            page.wait_for_timeout(500)
                            _round_start_url = page.url
                        repeat_count = 0
                        prev_batch_coords = None
                        resume_already_uploaded = False
                        continue
                _round_start_url = _current_url

            # 1. Screenshot + API call (with rate-limit retry)
            screenshot_b64 = _take_screenshot(page)
            # Save per-round screenshot for post-mortem debugging
            try:
                import pathlib
                _dbg_dir = pathlib.Path("data/logs")
                _dbg_dir.mkdir(parents=True, exist_ok=True)
                _round_shot = _dbg_dir / f"vision_round_{round_num+1}.png"
                _round_shot.write_bytes(base64.b64decode(screenshot_b64))
                # Debug pause: if debug_mode is set, wait for user to inspect browser
                if settings.get("automation", {}).get("debug_mode"):
                    console.print(f"\n  [bold yellow]DEBUG: Vision round {round_num+1} screenshot saved: {_round_shot}[/]")
                    console.print(f"  [bold yellow]  Inspect the browser, then press Enter to send screenshot to GPT-4o...[/]")
                    try:
                        input()
                    except EOFError:
                        pass
            except Exception:
                pass
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
                elif result == "needs_verification":
                    return "needs_verification"
                continue  # "continue" = false positive (e.g. multi-step Continue clicked)

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

                # Block re-upload if resume was already uploaded before this agent started
                if act_type == "upload_resume" and resume_already_uploaded:
                    skip_msg = f"Skipped re-upload at round {round_num+1}: resume was already uploaded before vision agent started"
                    round_results.append(skip_msg)
                    if vision_logging:
                        console.print(f"  [dim]    {i+1}. upload_resume: BLOCKED (already uploaded)[/]")
                    continue

                # Mark resume as uploaded after first successful upload_resume
                if act_type == "upload_resume":
                    resume_already_uploaded = True

                if vision_logging:
                    console.print(f"  [dim]    {i+1}. {act_type}: {reasoning[:70]}[/]")

                url_before = page.url
                try:
                    result = _execute_action(page, action, resume_file, cl_file)
                    round_results.append(result)
                except Exception as e:
                    logger.debug(f"Action execution error: {e}", exc_info=True)
                    round_results.append(f"Error executing {act_type}: {str(e)[:60]}")
                # Detect unintended page navigations (e.g. select action clicked a stepper link)
                url_after = page.url
                if url_before != url_after:
                    console.print(f"  [red]  !! Action {i+1} ({act_type}: {reasoning[:50]}) NAVIGATED: {url_before[-60:]} -> {url_after[-60:]}[/]")
                    logger.warning(f"Action {i+1} ({act_type}: {reasoning}) navigated from {url_before} to {url_after}")

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
