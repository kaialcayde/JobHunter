"""Coordinate-based DOM fallback helpers used by the vision agent."""

import logging

from ..browser_scripts import evaluate_script

logger = logging.getLogger(__name__)


def find_input_at_coords(page, x: int, y: int):
    """Find the nearest input/select/textarea element at given coordinates."""
    return evaluate_script(page, "forms/find_input_at_coords.js", {"x": x, "y": y})


def dom_fill_fallback(page, x: int, y: int, text: str) -> bool:
    """Try to fill a field at coordinates using DOM methods."""
    el_info = find_input_at_coords(page, x, y)
    if not el_info or not el_info.get("selector"):
        return False

    selector = el_info["selector"]
    tag = el_info.get("tagName", "")

    try:
        el = page.query_selector(selector)
        if not el:
            return False

        if tag in ("INPUT", "TEXTAREA"):
            try:
                page.fill(selector, text, timeout=3000)
                return True
            except Exception:
                pass

            evaluate_script(
                page,
                "forms/set_native_value.js",
                {"selector": selector, "value": text},
            )
            return True

        if el_info.get("selector") == '[contenteditable="true"]':
            evaluate_script(
                page,
                "forms/set_contenteditable_value.js",
                {"selector": selector, "value": text},
            )
            return True

        return False
    except Exception:
        return False


def dom_select_fallback(page, x: int, y: int, text: str) -> bool:
    """Try to select an option using DOM methods for native <select> or React-Select."""
    el_info = find_input_at_coords(page, x, y)
    if not el_info:
        el_info = evaluate_script(page, "forms/find_combobox_at_coords.js", {"x": x, "y": y})
        if not el_info:
            return False

    tag = el_info.get("tagName", "")
    selector = el_info.get("selector")

    if tag == "SELECT" and selector:
        try:
            page.select_option(selector, label=text, timeout=3000)
            return True
        except Exception:
            pass
        try:
            options = evaluate_script(page, "forms/get_select_options.js", selector)
            text_lower = text.lower()
            for opt in options:
                if text_lower in opt["text"].lower():
                    page.select_option(selector, value=opt["value"], timeout=3000)
                    return True
        except Exception:
            pass

    is_combobox = el_info.get("isCombobox", False)
    combobox_selector = selector if is_combobox else None

    if not is_combobox:
        combobox_selector = evaluate_script(
            page,
            "forms/find_combobox_selector.js",
            {"x": x, "y": y},
        )
        if combobox_selector:
            is_combobox = True

    if is_combobox and combobox_selector:
        try:
            el = page.query_selector(combobox_selector)
            if el:
                el.evaluate('e => e.value = ""')
                page.wait_for_timeout(100)
                el.click()
                page.wait_for_timeout(300)

                page.keyboard.type(text, delay=50)
                page.wait_for_timeout(800)

                try:
                    options = page.query_selector_all('[role="option"]')
                    for opt in options:
                        if opt.is_visible():
                            opt.click()
                            page.wait_for_timeout(500)
                            return True
                except Exception:
                    pass

                page.keyboard.press("Enter")
                page.wait_for_timeout(500)

                selected = evaluate_script(
                    page,
                    "forms/is_combobox_selected.js",
                    combobox_selector,
                )
                if selected:
                    return True

                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                toggle = evaluate_script(page, "forms/toggle_combobox.js", combobox_selector)
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

    try:
        result = evaluate_script(
            page,
            "forms/select_hidden_by_proximity.js",
            {"x": x, "y": y, "text": text},
        )
        if result:
            page.wait_for_timeout(300)
            return True
    except Exception as e:
        logger.debug(f"Hidden select proximity search failed: {e}")

    return False
