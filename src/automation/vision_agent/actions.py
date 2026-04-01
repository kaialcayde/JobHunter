"""Action execution helpers for the vision agent."""

from ..browser_scripts import evaluate_script
from ..forms import dom_fill_fallback, dom_select_fallback, find_input_at_coords
from .common import logger


def _execute_action(page, action: dict, resume_file, cl_file) -> str:
    """Execute a single action on the page."""
    act = action.get("action", "stuck")
    x = action.get("x", 0)
    y = action.get("y", 0)
    text = action.get("text", "")
    reasoning = action.get("reasoning", "")

    if act == "click":
        page.mouse.click(x, y)
        page.wait_for_timeout(1000)
        return f"Clicked ({x}, {y}): {reasoning}"

    if act == "type":
        el_info = find_input_at_coords(page, x, y)
        if el_info and el_info.get("type") == "password":
            return f"Skipped password field at ({x}, {y}) [pre-filled by system]: {reasoning}"

        if el_info and el_info.get("value", "").strip():
            existing = el_info["value"].strip().lower()
            desired = text.strip().lower()
            if existing == desired or desired in existing or existing in desired:
                return f"Skipped '{text[:50]}' at ({x}, {y}) [already filled]: {reasoning}"

        if el_info and el_info.get("selector") and dom_fill_fallback(page, x, y, text):
            return f"Typed '{text[:50]}' at ({x}, {y}) [DOM fill]: {reasoning}"

        page.mouse.click(x, y)
        page.wait_for_timeout(300)
        page.mouse.click(x, y, click_count=3)
        page.wait_for_timeout(100)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(100)
        page.keyboard.type(text, delay=30)
        page.keyboard.press("Tab")
        page.wait_for_timeout(300)

        el_info_after = find_input_at_coords(page, x, y)
        value_set = bool(el_info_after and el_info_after.get("value") and len(el_info_after["value"].strip()) > 0)
        if not value_set and dom_fill_fallback(page, x, y, text):
            return f"Typed '{text[:50]}' at ({x}, {y}) [DOM fallback]: {reasoning}"

        return f"Typed '{text[:50]}' at ({x}, {y}): {reasoning}"

    if act == "select":
        already_selected = evaluate_script(page, "vision/is_option_already_selected.js", {"x": x, "y": y, "text": text})
        if already_selected:
            return f"Skipped select '{text}' at ({x}, {y}) [already selected]: {reasoning}"

        dom_ok = dom_select_fallback(page, x, y, text)
        if dom_ok:
            page.wait_for_timeout(300)
            visual_updated = evaluate_script(page, "vision/did_select_visual_update.js", {"x": x, "y": y, "text": text})
            if visual_updated:
                return f"Selected '{text}' at ({x}, {y}) [DOM select]: {reasoning}"
            logger.debug("dom_select_fallback set value but UI unchanged — trying visual click too")

        try:
            is_nav_link = evaluate_script(page, "vision/is_nav_link.js", {"x": x, "y": y})
            if is_nav_link:
                logger.debug(f"select: ({x},{y}) hits a nav link — skipping coordinate click")
                return f"Skipped select '{text}' at ({x},{y}): coordinates target a navigation link"
        except Exception:
            pass

        clicked_open = False
        try:
            el_handle = page.evaluate_handle("""({x, y}) => {
                let el = document.elementFromPoint(x, y);
                for (let i = 0; i < 8; i++) {
                    if (!el) break;
                    const tag = el.tagName;
                    const role = el.getAttribute('role');
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

        try:
            page.wait_for_selector(
                '[role="option"]:visible, [role="listbox"] li:visible, '
                'ul[class*="option"]:visible li, ul[class*="dropdown"]:visible li',
                timeout=2000,
            )
        except Exception:
            page.wait_for_timeout(800)

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

    if act == "check":
        el_info = find_input_at_coords(page, x, y)
        if el_info and el_info.get("tagName") == "INPUT" and el_info.get("type") in ("checkbox", "radio") and el_info.get("selector"):
            try:
                el = page.query_selector(el_info["selector"])
                if el:
                    el.click()
                    page.wait_for_timeout(500)
                    return f"Checked option at ({x}, {y}) [DOM]: {reasoning}"
            except Exception as e:
                logger.debug(f"DOM checkbox/radio click failed: {e}")
        page.mouse.click(x, y)
        page.wait_for_timeout(500)
        return f"Checked option at ({x}, {y}): {reasoning}"

    if act == "scroll":
        direction = action.get("direction", "down")
        delta = -400 if direction == "up" else 400
        scroll_before = page.evaluate("() => window.scrollY")
        page.mouse.wheel(0, delta)
        page.wait_for_timeout(800)
        scroll_after = page.evaluate("() => window.scrollY")
        if scroll_before == scroll_after:
            edge = "bottom" if direction == "down" else "top"
            return f"Scroll {direction} had NO EFFECT (already at {edge}). Do NOT scroll {direction} again."
        return f"Scrolled {direction}: {reasoning}"

    if act == "upload_resume":
        if resume_file:
            upload_trigger_texts = ["From Device", "Browse", "Choose File", "Upload", "Attach", "Select File"]
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
                        page.wait_for_timeout(4000)
                        return f"Uploaded resume via file chooser ({trigger_text}): {resume_file.name}"
                except Exception:
                    triggered = False
                    continue
            try:
                with page.expect_file_chooser(timeout=3000) as fc_info:
                    page.mouse.click(x, y)
                fc_info.value.set_files(str(resume_file))
                page.wait_for_timeout(4000)
                return f"Uploaded resume via file chooser (coords): {resume_file.name}"
            except Exception:
                pass
            file_inputs = page.query_selector_all('input[type="file"]')
            if file_inputs:
                file_inputs[0].set_input_files(str(resume_file))
                page.wait_for_timeout(3000)
                return f"Uploaded resume via input element: {resume_file.name}"
        return "No resume file to upload or upload failed"

    if act == "upload_cover_letter":
        if cl_file:
            page.mouse.click(x, y)
            page.wait_for_timeout(500)
            file_inputs = page.query_selector_all('input[type="file"]')
            if len(file_inputs) > 1:
                file_inputs[1].set_input_files(str(cl_file))
                page.wait_for_timeout(1500)
                return f"Uploaded cover letter: {cl_file}"
            if file_inputs:
                file_inputs[0].set_input_files(str(cl_file))
                page.wait_for_timeout(1500)
                return f"Uploaded cover letter to first input: {cl_file}"
            try:
                with page.expect_file_chooser(timeout=3000) as fc_info:
                    page.mouse.click(x, y)
                fc_info.value.set_files(str(cl_file))
                page.wait_for_timeout(1500)
                return f"Uploaded cover letter via file chooser: {cl_file}"
            except Exception as e:
                logger.debug(f"Cover letter file chooser failed: {e}")
        return "No cover letter file to upload or upload failed"

    if act == "done":
        return "DONE: Application appears submitted"

    if act == "stuck":
        return f"STUCK: {reasoning}"

    return f"Unknown action: {act}"


def _extract_batch_coords(actions: list[dict]) -> set[tuple[int, int]]:
    """Extract rounded (x, y) coordinates from a batch for repeat detection."""
    coords = set()
    for a in actions:
        if a.get("action") in ("type", "click", "check", "select", "upload_resume", "upload_cover_letter"):
            rx = round(a.get("x", 0) / 30) * 30
            ry = round(a.get("y", 0) / 30) * 30
            coords.add((rx, ry))
    return coords
