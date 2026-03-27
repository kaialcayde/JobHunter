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

load_dotenv()

logger = logging.getLogger(__name__)

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


def _find_input_at_coords(page, x: int, y: int):
    """Find the nearest input/select/textarea element at given coordinates using DOM.

    Uses document.elementFromPoint(), then walks up the DOM to find the nearest
    form element. Returns a dict with element info or None.
    """
    return page.evaluate("""({x, y}) => {
        let el = document.elementFromPoint(x, y);
        if (!el) return null;

        // Walk up to find the nearest input, select, textarea, or contenteditable
        const formTags = ['INPUT', 'SELECT', 'TEXTAREA'];
        let candidate = el;
        for (let i = 0; i < 5; i++) {
            if (!candidate) break;
            if (formTags.includes(candidate.tagName)) break;
            if (candidate.getAttribute('contenteditable') === 'true') break;
            // Check siblings too (label click targets adjacent input)
            const next = candidate.nextElementSibling;
            if (next && formTags.includes(next.tagName)) { candidate = next; break; }
            const prev = candidate.previousElementSibling;
            if (prev && formTags.includes(prev.tagName)) { candidate = prev; break; }
            candidate = candidate.parentElement;
        }

        if (!candidate) return null;

        // If we didn't find a form element, search within the clicked element's parent
        if (!formTags.includes(candidate.tagName) && candidate.getAttribute('contenteditable') !== 'true') {
            // Search nearby: find closest input within the parent container
            const container = el.closest('div, fieldset, li, section, form') || el.parentElement;
            if (container) {
                const nearby = container.querySelector('input:not([type="hidden"]):not([type="submit"]):not([type="button"]), select, textarea');
                if (nearby) candidate = nearby;
                else return null;
            } else {
                return null;
            }
        }

        // Build a selector for this element
        let selector = '';
        if (candidate.id) selector = '#' + CSS.escape(candidate.id);
        else if (candidate.name) selector = candidate.tagName.toLowerCase() + '[name="' + candidate.name + '"]';
        else if (candidate.getAttribute('aria-label')) selector = candidate.tagName.toLowerCase() + '[aria-label="' + candidate.getAttribute('aria-label') + '"]';
        else if (candidate.placeholder) selector = candidate.tagName.toLowerCase() + '[placeholder="' + candidate.placeholder + '"]';
        else selector = null;

        return {
            tagName: candidate.tagName,
            type: candidate.type || '',
            selector: selector,
            value: candidate.value || '',
            id: candidate.id || '',
            name: candidate.name || ''
        };
    }""", {"x": x, "y": y})


def _dom_fill_fallback(page, x: int, y: int, text: str) -> bool:
    """Try to fill a field at coordinates using DOM methods (page.fill / JS dispatch).

    Returns True if the value was successfully set.
    """
    el_info = _find_input_at_coords(page, x, y)
    if not el_info or not el_info.get("selector"):
        return False

    selector = el_info["selector"]
    tag = el_info.get("tagName", "")

    try:
        el = page.query_selector(selector)
        if not el:
            return False

        # For native inputs/textareas, use page.fill() which handles React
        if tag in ("INPUT", "TEXTAREA"):
            try:
                page.fill(selector, text, timeout=3000)
                return True
            except Exception:
                pass

            # Fallback: JS value dispatch with React-compatible events
            page.evaluate("""({selector, value}) => {
                const el = document.querySelector(selector);
                if (!el) return;
                // Use native setter to bypass React's synthetic event system
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                )?.set || Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                )?.set;
                if (nativeSetter) nativeSetter.call(el, value);
                else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            }""", {"selector": selector, "value": text})
            return True

        return False
    except Exception:
        return False


def _dom_select_fallback(page, x: int, y: int, text: str) -> bool:
    """Try to select an option using DOM methods for native <select> or React-Select.

    Handles:
    - Native <select> elements (page.select_option)
    - React-Select combobox inputs (type to filter + Enter)
    - Custom dropdown containers (click to open + click option)

    Returns True if selection was successful.
    """
    el_info = _find_input_at_coords(page, x, y)
    if not el_info:
        # Try to find a React-Select combobox near the click coordinates
        el_info = page.evaluate("""({x, y}) => {
            let el = document.elementFromPoint(x, y);
            if (!el) return null;
            // Walk up to find a select container
            let container = el.closest('.select, .select__container, .select__control, [class*="select"]');
            if (!container) container = el.closest('div');
            if (!container) return null;
            // Find the combobox input inside
            const input = container.querySelector('input[role="combobox"], input.select__input');
            if (input) return {
                tagName: 'INPUT', type: 'text', selector: input.id ? '#' + CSS.escape(input.id) : null,
                value: input.value || '', id: input.id || '', name: input.name || '',
                isCombobox: true
            };
            return null;
        }""", {"x": x, "y": y})
        if not el_info:
            return False

    tag = el_info.get("tagName", "")
    selector = el_info.get("selector")

    # Native <select> elements
    if tag == "SELECT" and selector:
        try:
            page.select_option(selector, label=text, timeout=3000)
            return True
        except Exception:
            pass
        try:
            options = page.evaluate("""(selector) => {
                const sel = document.querySelector(selector);
                if (!sel) return [];
                return Array.from(sel.options).map((o, i) => ({index: i, text: o.text.trim(), value: o.value}));
            }""", selector)
            text_lower = text.lower()
            for opt in options:
                if text_lower in opt["text"].lower():
                    page.select_option(selector, value=opt["value"], timeout=3000)
                    return True
        except Exception:
            pass

    # React-Select combobox inputs (Greenhouse, Lever, etc.)
    # These are <input role="combobox"> inside .select__control containers
    is_combobox = el_info.get("isCombobox", False)
    combobox_selector = selector if is_combobox else None

    if not is_combobox:
        # The element at coords may be a wrapper div, placeholder, etc.
        # Search for a nearby combobox input within the same container.
        combobox_selector = page.evaluate("""({x, y}) => {
            let el = document.elementFromPoint(x, y);
            if (!el) return null;
            // Walk up to find a select container
            const container = el.closest('.select, .select__container, .select__control, [class*="select"]')
                            || el.closest('div.field, div.form-group, div');
            if (!container) return null;
            const input = container.querySelector('input[role="combobox"], input.select__input');
            if (input && input.id) return '#' + CSS.escape(input.id);
            if (input && input.name) return 'input[name="' + input.name + '"]';
            return null;
        }""", {"x": x, "y": y})
        if combobox_selector:
            is_combobox = True

    if is_combobox and combobox_selector:
        try:
            el = page.query_selector(combobox_selector)
            if el:
                # Clear via JS (Control+a/Backspace breaks React-Select dropdown state)
                el.evaluate('e => e.value = ""')
                page.wait_for_timeout(100)
                el.click()
                page.wait_for_timeout(300)

                # Type to filter options
                page.keyboard.type(text, delay=50)
                page.wait_for_timeout(800)

                # Look for VISIBLE matching options (ignore hidden ones from other dropdowns)
                try:
                    options = page.query_selector_all('[role="option"]')
                    for opt in options:
                        if opt.is_visible():
                            opt.click()
                            page.wait_for_timeout(500)
                            return True
                except Exception:
                    pass

                # Fallback: press Enter to select first filtered result
                page.keyboard.press("Enter")
                page.wait_for_timeout(500)

                # Verify selection took effect (placeholder should be gone)
                selected = page.evaluate("""(selector) => {
                    const input = document.querySelector(selector);
                    if (!input) return false;
                    const container = input.closest('.select__control, .select, .select__container, [class*="select"]');
                    if (!container) return false;
                    const singleValue = container.querySelector('[class*="single-value"], [class*="singleValue"]');
                    if (singleValue && singleValue.textContent.trim()) return true;
                    const placeholder = container.querySelector('[class*="placeholder"]');
                    return placeholder && placeholder.textContent.trim() !== 'Select...';
                }""", combobox_selector)
                if selected:
                    return True

                # If typing didn't work, try clicking the dropdown arrow and finding option
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                toggle = page.evaluate("""(selector) => {
                    const input = document.querySelector(selector);
                    if (!input) return null;
                    const container = input.closest('.select, .select__container');
                    if (!container) return null;
                    const btn = container.querySelector('[aria-label="Toggle flyout"], .select__dropdown-indicator, .select__indicators button');
                    if (btn) { btn.click(); return true; }
                    return null;
                }""", combobox_selector)
                if toggle:
                    page.wait_for_timeout(800)
                    options = page.query_selector_all('[role="option"]')
                    text_lower = text.lower()
                    for opt in options:
                        if opt.is_visible():
                            opt_text = opt.text_content().strip().lower()
                            if text_lower in opt_text or opt_text in text_lower:
                                opt.click()
                                page.wait_for_timeout(500)
                                return True

                return False
        except Exception as e:
            logger.debug(f"React-Select fallback failed: {e}")

    return False


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
        el_info = _find_input_at_coords(page, x, y)
        if el_info and el_info.get("value", "").strip():
            existing = el_info["value"].strip().lower()
            desired = text.strip().lower()
            # If field already contains the desired text (or close enough), skip
            if existing == desired or desired in existing or existing in desired:
                return f"Skipped '{text[:50]}' at ({x}, {y}) [already filled]: {reasoning}"

        # Try DOM fill first (most reliable for React/controlled inputs)
        if el_info and el_info.get("selector"):
            if _dom_fill_fallback(page, x, y, text):
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
        el_info_after = _find_input_at_coords(page, x, y)
        value_set = False
        if el_info_after and el_info_after.get("value"):
            value_set = len(el_info_after["value"].strip()) > 0

        if not value_set:
            # Last resort: JS value dispatch
            if _dom_fill_fallback(page, x, y, text):
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
        if _dom_select_fallback(page, x, y, text):
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
                except Exception:
                    continue
        except Exception:
            pass

        # Try get_by_text for broader matching
        try:
            option = page.get_by_text(text, exact=False).first
            if option.is_visible():
                option.click(timeout=3000)
                page.wait_for_timeout(500)
                return f"Selected '{text}' at ({x}, {y}): {reasoning}"
        except Exception:
            pass

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
        except Exception:
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)

        return f"Selected '{text}' at ({x}, {y}) [type+enter]: {reasoning}"

    elif act == "check":
        # Try DOM-based click first (more reliable for hidden checkboxes)
        el_info = _find_input_at_coords(page, x, y)
        if el_info and el_info.get("tagName") == "INPUT" and el_info.get("type") == "checkbox" and el_info.get("selector"):
            try:
                el = page.query_selector(el_info["selector"])
                if el:
                    el.click()
                    page.wait_for_timeout(500)
                    return f"Checked checkbox at ({x}, {y}) [DOM]: {reasoning}"
            except Exception:
                pass
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
            except Exception:
                pass
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
            except Exception:
                pass
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


def run_vision_agent(page, job: dict, settings: dict,
                     resume_file=None, cl_file=None) -> bool:
    """Run the vision-based browser agent to complete a job application.

    Takes screenshots and asks the model to return ALL actions for visible fields
    at once. Executes the batch, then takes another screenshot for the next round.
    Typically completes a form in 3-5 rounds.
    """
    from rich.console import Console
    console = Console(force_terminal=True)

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
    history = []
    prev_batch_coords = set()  # for repeat detection across rounds
    repeat_count = 0  # how many consecutive rounds target the same fields
    type_loop_rounds = 0  # consecutive rounds where majority of actions are "type" refills
    single_action_repeats = 0  # consecutive rounds with the same single action
    prev_single_action_key = None  # (action_type, reasoning_prefix) of last single-action round
    otp_round_count = 0  # consecutive rounds referencing verification/OTP codes
    consecutive_scrolls = 0

    console.print(f"  [magenta]Vision agent active (model: {model}, detail: {detail})[/]")

    for round_num in range(MAX_ROUNDS):
        try:
            # 1. Screenshot
            screenshot_b64 = _take_screenshot(page)

            # 2. Ask model for ALL actions (with rate-limit retry)
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

            # 3. Check terminal states
            if status == "done":
                # Verify before trusting the model's claim
                actually_done = verify_submission(page, settings)
                if actually_done:
                    console.print("  [green]Vision agent: application submitted![/]")
                    return True
                else:
                    # Try DOM-based submit click before falling back to vision loop.
                    # Vision coordinate clicks often miss the submit button; DOM selectors
                    # are more reliable for the final submit step.
                    from .detection import click_submit_button
                    url_before = page.url
                    if click_submit_button(page):
                        page.wait_for_timeout(2000)
                        # Check if page changed (submit worked)
                        if page.url != url_before or verify_submission(page, settings):
                            console.print("  [green]Application submitted via DOM click![/]")
                            return True
                    # Check if an invisible CAPTCHA blocked the submit (e.g. Ashby spam error)
                    from .detection import detect_captcha, try_solve_captcha
                    if detect_captcha(page):
                        console.print("  [cyan]CAPTCHA detected after failed submit -- attempting solve[/]")
                        if try_solve_captcha(page, settings):
                            console.print("  [green]CAPTCHA solved -- retrying submit[/]")
                            # Re-run DOM pre-fill after CAPTCHA solve (page may have reloaded/cleared form)
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
                            except Exception:
                                pass
                            history.append("A CAPTCHA was blocking form submission. It has been solved. Fields have been re-filled via DOM. Try submitting again.")
                            page.wait_for_timeout(2000)
                            continue
                        else:
                            console.print("  [yellow]CAPTCHA blocked submit and could not be solved -- giving up[/]")
                            return False
                    console.print("  [yellow]Vision agent said 'done' but page still shows form -- continuing[/]")
                    history.append(
                        "You reported 'done' but the page still shows a form — the application was NOT submitted. "
                        "Look for a Submit/Apply/Send button and click it. If there are unfilled required fields, fill them first."
                    )
                    continue

            if status == "stuck":
                # Check if an invisible CAPTCHA is gating the page (e.g. Paylocity)
                from .detection import detect_captcha, try_solve_captcha
                if detect_captcha(page):
                    console.print(f"  [cyan]CAPTCHA detected while stuck -- attempting solve[/]")
                    if try_solve_captcha(page, settings):
                        console.print(f"  [green]CAPTCHA solved -- retrying[/]")
                        # Re-run DOM pre-fill after CAPTCHA solve (page may have reloaded/cleared form)
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
                        except Exception:
                            pass
                        history.append("CAPTCHA was blocking progress. It has been solved. Fields have been re-filled via DOM. Try clicking Apply again or fill the form.")
                        page.wait_for_timeout(2000)
                        continue
                    else:
                        manual_verification = settings.get("automation", {}).get("manual_verification", False)
                        if manual_verification:
                            console.print("  [bold yellow]Verification challenge detected! Browser is open for manual solving.[/]")
                            try:
                                input("  Solve the CAPTCHA/challenge in the browser, then press Enter to continue: ")
                            except EOFError:
                                pass
                            page.wait_for_timeout(1000)
                            history.append("The user manually solved the verification challenge. Continue filling the form or click Submit.")
                            continue
                        console.print(f"  [yellow]CAPTCHA detected but could not solve -- giving up[/]")
                        return False

                reason_lower = (overall_reasoning or "").lower()

                # Already applied — no point retrying
                if any(kw in reason_lower for kw in ["already applied", "already been submitted", "already submitted"]):
                    console.print(f"  [yellow]Vision agent: already applied to this position[/]")
                    return "already_applied"

                # Login pages are unrecoverable — bail immediately
                if any(kw in reason_lower for kw in [
                    "login", "log in", "sign in", "password", "credentials",
                    "create account", "create an account", "sign up", "signup",
                    "account creation",
                ]):
                    console.print(f"  [yellow]Vision agent stuck (login wall): {overall_reasoning}[/]")
                    return "needs_login"

                # Fallback: use DOM-based login detection (catches password fields + login phrases)
                from .detection import detect_login_page
                if detect_login_page(page):
                    console.print(f"  [yellow]Vision agent stuck (login page detected via DOM)[/]")
                    return "needs_login"

                # Listing page — try _force_apply_click to navigate to the actual form
                if any(kw in reason_lower for kw in ["job description", "job listing", "listing page", "not the application form"]):
                    from .applicant import _force_apply_click
                    console.print(f"  [dim]Stuck on listing -- trying force apply click...[/]")
                    if _force_apply_click(page):
                        # Check for new tab
                        if len(page.context.pages) > 1:
                            latest = page.context.pages[-1]
                            if latest != page and latest.url != "about:blank":
                                page = latest
                                page.wait_for_load_state("domcontentloaded")
                                console.print(f"  [dim]Navigated to: {page.url[:80]}[/]")
                        history.append("Successfully navigated away from the listing page to the application form. Fill out the form fields now.")
                        page.wait_for_timeout(1000)
                        continue
                    console.print(f"  [yellow]Could not leave listing page[/]")
                    return False

                # In early rounds, "stuck" is often a misread — retry with a fresh screenshot
                if round_num < 2:
                    console.print(f"  [yellow]Vision agent stuck (round {round_num+1}): {overall_reasoning} -- retrying[/]")
                    history.append(
                        f"You reported 'stuck' but this is round {round_num+1}. "
                        "Look again carefully: if you see form fields (name, email, resume upload, etc.), "
                        "this IS the application form, not a job listing. Fill the fields."
                    )
                    page.wait_for_timeout(2000)
                    continue
                console.print(f"  [yellow]Vision agent stuck: {overall_reasoning}[/]")
                return False

            if not actions:
                history.append("Round returned no actions. If form is complete, click Submit. If stuck, report stuck.")
                continue

            # 3b. OTP / verification code detection — these require email access we don't have
            otp_keywords = ["verification code", "verify code", "otp", "one-time", "confirmation code", "security code"]
            action_texts = " ".join(a.get("reasoning", "") for a in actions).lower()
            if any(kw in action_texts for kw in otp_keywords):
                otp_round_count += 1
                manual_otp = settings.get("automation", {}).get("manual_otp", False)
                if manual_otp and otp_round_count == 1:
                    # First OTP round — prompt user for the code
                    console.print("  [bold yellow]OTP/verification code required![/]")
                    try:
                        user_code = input("  Enter the verification code (or press Enter to skip): ").strip()
                    except EOFError:
                        user_code = ""
                    if user_code:
                        # Type the code into the focused field or find verification input
                        page.evaluate("""(code) => {
                            const inputs = document.querySelectorAll('input[type="text"], input[type="number"], input[type="tel"]');
                            for (const inp of inputs) {
                                const label = (inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || '').toLowerCase();
                                const parentText = (inp.closest('label, div, fieldset')?.textContent || '').toLowerCase();
                                if (['verification', 'code', 'otp', 'confirm'].some(k => label.includes(k) || parentText.includes(k))) {
                                    inp.focus();
                                    inp.value = code;
                                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                                    return;
                                }
                            }
                            // Fallback: use active element or first empty input
                            const active = document.activeElement;
                            if (active && active.tagName === 'INPUT') {
                                active.value = code;
                                active.dispatchEvent(new Event('input', {bubbles: true}));
                                active.dispatchEvent(new Event('change', {bubbles: true}));
                            }
                        }""", user_code)
                        console.print(f"  [green]Entered verification code[/]")
                        otp_round_count = 0  # reset — let vision agent continue
                        history.append("The user manually entered the verification code. Now click Submit/Continue to proceed.")
                        continue
                    else:
                        console.print(f"  [yellow]No code entered -- skipping[/]")
                        return "needs_login"
                elif otp_round_count >= 2:
                    console.print(f"  [yellow]Vision agent: OTP/verification code required -- cannot proceed[/]")
                    return "needs_login"
            else:
                otp_round_count = 0

            # 4. Repeat detection — compare this batch's target coords with previous batch
            current_coords = _extract_batch_coords(actions)
            if current_coords and current_coords == prev_batch_coords:
                repeat_count += 1
                # Same fields being targeted again — fields likely ARE filled but model can't see them
                if repeat_count >= 2:
                    # Fields have been "refilled" 2+ times — they're almost certainly already filled.
                    # Try DOM-based next/continue first (multi-step forms), then submit.
                    if vision_logging:
                        console.print(f"  [yellow]  Round {round_num+1}: fields targeted 3x -- attempting DOM next/submit[/]")
                    from .detection import click_next_button, click_submit_button
                    url_before = page.url
                    advanced = False
                    if click_next_button(page):
                        page.wait_for_timeout(2000)
                        if page.url != url_before:
                            advanced = True
                        else:
                            # URL same but page content may have changed (multi-step SPA)
                            advanced = True
                    if advanced:
                        # Reset repeat counters — we moved to a new step
                        repeat_count = 0
                        prev_batch_coords = None
                        single_action_repeats = 0
                        prev_single_action_key = None
                        history.append("Successfully clicked Next/Continue via DOM. Now on a new step — analyze the new page.")
                        continue
                    if click_submit_button(page):
                        page.wait_for_timeout(2000)
                        if page.url != url_before or verify_submission(page, settings):
                            console.print("  [green]Application submitted via DOM click (repeat bypass)![/]")
                            return True
                    # DOM next/submit didn't work — tell model to just click submit
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

            # 4b. Type-loop detection — if agent keeps doing mostly "type" actions for 5+ rounds,
            # the fields are likely filled but invisible to the model. Force DOM submit.
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
                from .detection import click_next_button, click_submit_button
                url_before = page.url
                if click_next_button(page):
                    page.wait_for_timeout(2000)
                    type_loop_rounds = 0
                    repeat_count = 0
                    prev_batch_coords = None
                    single_action_repeats = 0
                    prev_single_action_key = None
                    history.append("Successfully clicked Next/Continue via DOM. Now on a new step.")
                    continue
                if click_submit_button(page):
                    page.wait_for_timeout(2000)
                    if page.url != url_before or verify_submission(page, settings):
                        console.print("  [green]Application submitted via DOM click (type-loop bypass)![/]")
                        return True
                history.append(
                    "CRITICAL: You have been refilling the same fields for many rounds. The fields ARE filled -- "
                    "you cannot see the values due to rendering. STOP filling fields and click Submit/Continue/Next NOW. "
                    "If Submit does not work, report 'stuck'."
                )

            # 4c. Single-action stuck detection — if the agent returns the same single
            # action for 3+ rounds (e.g. clicking a radio button that won't respond),
            # skip that action and tell the model to move on.
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

                # Scroll cap within a batch
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
                    round_results.append(f"Error executing {act_type}: {str(e)[:60]}")

            # Add batch summary to history
            summary = f"Round {round_num+1}: executed {len(round_results)} actions: " + "; ".join(round_results)
            history.append(summary)

            if vision_logging:
                logger.info(summary)

            # After click actions, check if an invisible CAPTCHA appeared/is blocking
            has_clicks = any(a.get("action") == "click" for a in actions)
            if has_clicks:
                from .detection import detect_captcha, try_solve_captcha
                if detect_captcha(page):
                    console.print(f"  [cyan]CAPTCHA detected after click -- attempting solve[/]")
                    if try_solve_captcha(page, settings):
                        console.print(f"  [green]CAPTCHA solved![/]")
                        # Re-run DOM pre-fill (page may have reloaded/cleared form)
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
                        except Exception:
                            pass
                        history.append("CAPTCHA was blocking after click. Solved and form re-filled via DOM. Proceed with the form.")
                        page.wait_for_timeout(2000)
                    else:
                        history.append("CAPTCHA detected after click but could not solve.")

            # Wait between rounds — longer if we just clicked (page may navigate)
            time.sleep(1.0 if has_clicks else 0.5)

        except json.JSONDecodeError as e:
            logger.warning(f"Vision round {round_num+1}: invalid JSON from model: {e}")
            history.append("Error: model returned invalid JSON — try again with valid JSON")
            continue
        except Exception as e:
            logger.error(f"Vision round {round_num+1} error: {e}")
            history.append(f"Error: {str(e)[:80]}")
            continue

    console.print(f"  [yellow]Vision agent: hit round limit ({MAX_ROUNDS})[/]")
    return False
