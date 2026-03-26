"""Lightweight vision-based browser agent using GPT-4o-mini.

Fallback for when CSS selector-based form filling fails. Takes screenshots,
sends to vision model, gets structured actions back, executes via Playwright.
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

MAX_STEPS = 30  # safety limit per application
VISION_MODEL_DEFAULT = "gpt-4o-mini"

SYSTEM_PROMPT = """You are a job application assistant controlling a web browser via screenshots.
You are applying to {position} at {company}.

## Candidate Info
{profile_summary}

## Instructions
Look at the screenshot and decide the SINGLE next action to take.
Return ONLY valid JSON (no markdown fences) with these fields:

{{
  "action": "click" | "type" | "select" | "check" | "scroll" | "upload_resume" | "upload_cover_letter" | "done" | "stuck",
  "x": <int pixel x-coordinate>,
  "y": <int pixel y-coordinate>,
  "text": "<text to type or option to select>",
  "direction": "up" | "down",
  "reasoning": "<brief explanation>"
}}

## Actions
- "click": click at (x, y) — buttons, links, radio buttons
- "type": click field at (x, y), clear it, then type text — text inputs, textareas
- "select": click dropdown at (x, y) to open it, then select the option matching "text"
- "check": click checkbox at (x, y) — for checkboxes, consent boxes, multi-select options
- "scroll": scroll the page in given direction to reveal more fields
- "upload_resume": click the resume upload area/button at (x, y), system handles the file
- "upload_cover_letter": click the cover letter upload area/button at (x, y), system handles the file
- "done": application was submitted (you see a confirmation/thank you page)
- "stuck": cannot proceed (CAPTCHA, login wall, error, unrecoverable)

## Critical Rules
1. SCROLL DOWN FIRST to see ALL form fields before filling anything. Many forms extend below the visible area.
2. Fill ALL required fields before attempting to click Apply/Submit. If the button looks greyed out or disabled, there are likely unfilled required fields — scroll up and check.
3. For CONSENT CHECKBOXES (e.g., "I consent to receiving text messages", terms of service): you MUST check these before Apply/Submit will be enabled. Use "check" action.
4. For DROPDOWNS/SELECT fields: use "select" action. Click the dropdown first, then the option will appear.
5. For CHECKBOX GROUPS (e.g., "Select Azure services you know"): check each relevant one individually using "check" action.
6. For FILE UPLOADS labeled "Drop or select" or "Attach" or "Upload": use "upload_resume" or "upload_cover_letter" action and click the upload area.
7. For PRONOUNS: type the appropriate pronouns (e.g., "He/Him", "She/Her", "They/Them").
8. Fill form fields with the candidate's REAL info only — NEVER fabricate.
9. For diversity/EEO questions, select "Prefer not to answer" or "Decline to self-identify".
10. For "How did you hear about us", use "Job Board".
11. Click coordinates should target the CENTER of the element.
12. If the page looks UNCHANGED after your last action, try a DIFFERENT approach (scroll, click elsewhere, etc.). Do NOT repeat the same action.
13. Work TOP to BOTTOM through the form systematically.
"""


def _get_vision_client(settings: dict) -> OpenAI:
    """Get OpenAI client for vision calls."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key == "your-openai-api-key-here":
        raise ValueError("OPENAI_API_KEY not set in .env file.")
    return OpenAI(api_key=api_key, timeout=60)


def _get_vision_model(settings: dict) -> str:
    """Get vision model from settings. Defaults to gpt-4o-mini (cheapest)."""
    return settings.get("automation", {}).get("vision_model", VISION_MODEL_DEFAULT)


def _is_vision_logging(settings: dict) -> bool:
    """Check if vision agent logging is enabled."""
    return settings.get("automation", {}).get("vision_logging", True)


def _take_screenshot(page) -> str:
    """Take a screenshot and return as base64-encoded string."""
    screenshot_bytes = page.screenshot(type="png")
    return base64.b64encode(screenshot_bytes).decode("utf-8")


def _decide_action(client: OpenAI, model: str, screenshot_b64: str,
                   system_prompt: str, history: list[str]) -> dict:
    """Send screenshot to vision model, get structured action back."""
    # Build context from recent history
    history_text = ""
    if history:
        recent = history[-8:]  # last 8 actions for context
        history_text = "\n\nRecent actions taken:\n" + "\n".join(f"- {h}" for h in recent)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text", "text": f"What should I do next?{history_text}"},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{screenshot_b64}",
                "detail": "high"  # high detail so model can READ form labels and field values
            }}
        ]}
    ]

    response = client.chat.completions.create(
        model=model,
        temperature=0.1,  # low temp for precise actions
        max_tokens=300,
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
        page.mouse.click(x, y)
        page.wait_for_timeout(300)
        # Select all existing text and replace
        page.keyboard.press("Control+a" if "win" in page.context.browser.browser_type.name else "Meta+a")
        page.wait_for_timeout(100)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(100)
        page.keyboard.type(text, delay=30)
        page.wait_for_timeout(500)
        return f"Typed '{text[:50]}' at ({x}, {y}): {reasoning}"

    elif act == "select":
        # Click dropdown to open it
        page.mouse.click(x, y)
        page.wait_for_timeout(1000)
        # Try to find and click the option by text
        try:
            option = page.get_by_text(text, exact=False).first
            if option.is_visible():
                option.click(timeout=3000)
            else:
                raise Exception("Option not visible")
        except Exception:
            # Fallback: type the option text to filter, then press Enter
            page.keyboard.type(text, delay=50)
            page.wait_for_timeout(800)
            page.keyboard.press("Enter")
        page.wait_for_timeout(500)
        return f"Selected '{text}' at ({x}, {y}): {reasoning}"

    elif act == "check":
        page.mouse.click(x, y)
        page.wait_for_timeout(500)
        return f"Checked checkbox at ({x}, {y}): {reasoning}"

    elif act == "scroll":
        direction = action.get("direction", "down")
        delta = -400 if direction == "up" else 400
        page.mouse.wheel(0, delta)
        page.wait_for_timeout(800)
        return f"Scrolled {direction}: {reasoning}"

    elif act == "upload_resume":
        if resume_file:
            # Click the upload area first
            page.mouse.click(x, y)
            page.wait_for_timeout(500)
            # Find file input and upload
            file_inputs = page.query_selector_all('input[type="file"]')
            if file_inputs:
                file_inputs[0].set_input_files(str(resume_file))
                page.wait_for_timeout(1500)
                return f"Uploaded resume: {resume_file}"
            # Try via file chooser
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
            # Click the upload area first
            page.mouse.click(x, y)
            page.wait_for_timeout(500)
            # Find file inputs -- cover letter is usually 2nd file input
            file_inputs = page.query_selector_all('input[type="file"]')
            if len(file_inputs) > 1:
                file_inputs[1].set_input_files(str(cl_file))
                page.wait_for_timeout(1500)
                return f"Uploaded cover letter: {cl_file}"
            elif file_inputs:
                # Only one file input -- check if resume already uploaded
                file_inputs[0].set_input_files(str(cl_file))
                page.wait_for_timeout(1500)
                return f"Uploaded cover letter to first input: {cl_file}"
            # Try via file chooser
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


def verify_submission(page, settings: dict) -> bool:
    """Use vision model to verify whether the page shows a real submission confirmation.

    Takes a screenshot and asks the model: "Was this application actually submitted?"
    Returns True only if the model confirms a real confirmation/success message.
    """
    client = _get_vision_client(settings)
    model = _get_vision_model(settings)
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
                "detail": "high"
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

    Args:
        page: Playwright page object
        job: Job data dict (title, company, etc.)
        settings: App settings dict
        resume_file: Path to resume file for upload
        cl_file: Path to cover letter file for upload

    Returns:
        True if application was submitted successfully, False otherwise.
    """
    from rich.console import Console
    console = Console(force_terminal=True)

    from ..config import load_profile, get_profile_summary
    profile = load_profile()
    profile_summary = get_profile_summary(profile)

    company = job.get("company", "Unknown")
    position = job.get("title", "Unknown")

    system_prompt = SYSTEM_PROMPT.format(
        company=company,
        position=position,
        profile_summary=profile_summary,
    )

    client = _get_vision_client(settings)
    model = _get_vision_model(settings)
    vision_logging = _is_vision_logging(settings)
    history = []

    console.print(f"  [magenta]Vision agent active (model: {model})[/]")

    for step in range(MAX_STEPS):
        try:
            # 1. Screenshot
            screenshot_b64 = _take_screenshot(page)

            # 2. Ask model what to do
            action = _decide_action(client, model, screenshot_b64, system_prompt, history)
            act_type = action.get("action", "stuck")
            reasoning = action.get("reasoning", "")

            if vision_logging:
                logger.info(f"Vision step {step+1}: {act_type} - {reasoning}")
                console.print(f"  [dim]  Step {step+1}: {act_type} - {reasoning[:80]}[/]")

            # 3. Check terminal states
            if act_type == "done":
                console.print("  [green]Vision agent: application submitted![/]")
                return True

            if act_type == "stuck":
                console.print(f"  [yellow]Vision agent stuck: {reasoning}[/]")
                return False

            # 4. Execute action
            result = _execute_action(page, action, resume_file, cl_file)
            history.append(result)

            if vision_logging:
                logger.info(f"Vision step {step+1} result: {result}")

        except json.JSONDecodeError as e:
            logger.warning(f"Vision step {step+1}: invalid JSON from model: {e}")
            history.append(f"Error: model returned invalid JSON")
            continue
        except Exception as e:
            logger.error(f"Vision step {step+1} error: {e}")
            history.append(f"Error: {str(e)[:80]}")
            continue

    console.print(f"  [yellow]Vision agent: hit step limit ({MAX_STEPS})[/]")
    return False
