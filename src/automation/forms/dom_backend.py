"""DOM-based extraction and fill helpers for generic forms."""

import logging

from rich.console import Console

from ..browser_scripts import evaluate_script
from .selects import _fill_custom_select, _fill_react_select

console = Console(force_terminal=True)
logger = logging.getLogger(__name__)


def extract_form_fields(page) -> list[dict]:
    """Extract all form fields from the current page using DOM inspection."""
    try:
        in_modal = evaluate_script(page, "forms/is_modal_open.js")
        if not in_modal:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(400)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
    except Exception:
        return []

    fields = evaluate_script(page, "forms/extract_form_fields.js")

    if not fields:
        for frame in page.frames[1:]:
            try:
                fields = evaluate_script(frame, "forms/extract_form_fields_in_frame.js")
                if fields:
                    console.print("  [dim]Found fields inside iframe[/]")
                    break
            except Exception:
                continue

    return fields


def fill_form_fields(page, fields: list[dict], answers: dict):
    """Fill form fields with LLM-inferred answers."""
    for field in fields:
        field_id = field["id"]
        if field_id not in answers or field["type"] == "file":
            continue

        if not field.get("visible", True):
            continue

        value = str(answers[field_id])
        selector = field.get("selector", "")
        if not selector:
            continue

        try:
            el = page.query_selector(selector)
            if not el:
                console.print(f"  [dim]Skipping '{field.get('label', field_id)}' - element not found[/]")
                continue

            el.scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(100)

            if field["type"] == "select":
                try:
                    page.select_option(selector, label=value, timeout=5000)
                except Exception:
                    options = field.get("options", []) or []
                    matched = None
                    value_lower = value.lower()
                    for option in options:
                        option_lower = option.lower()
                        if (
                            value_lower == option_lower
                            or value_lower in option_lower
                            or option_lower in value_lower
                        ):
                            matched = option
                            break
                    if matched:
                        page.select_option(selector, label=matched, timeout=5000)
                    else:
                        raise
            elif field["type"] == "custom_select":
                _fill_custom_select(page, el, value)
            elif field["type"] == "checkbox":
                should_check = value.lower() in ("true", "yes", "1", "checked", "agree")
                is_checked = field.get("checked", False)
                if should_check != is_checked:
                    el.click()
            elif field["type"] == "radio":
                options = page.query_selector_all(f'input[name="{field_id}"]')
                for opt in options:
                    label = page.evaluate(
                        "(el) => { const l = el.closest('label'); return l ? l.textContent.trim() : el.value; }",
                        opt,
                    )
                    if value.lower() in label.lower():
                        opt.scroll_into_view_if_needed(timeout=3000)
                        opt.click()
                        break
            elif field["type"] == "textarea":
                page.fill(selector, value, timeout=5000)
            else:
                is_combobox = page.evaluate("""(selector) => {
                    const el = document.querySelector(selector);
                    return el && (el.getAttribute('role') === 'combobox' ||
                                  el.classList.contains('select__input') ||
                                  !!el.closest('.select__control, .select__container'));
                }""", selector) or False

                if is_combobox:
                    _fill_react_select(page, el, value)
                else:
                    try:
                        page.fill(selector, value, timeout=5000)
                    except Exception:
                        el.click()
                        page.wait_for_timeout(100)
                        page.keyboard.type(value)
        except Exception as e:
            err_msg = str(e).split("\n")[0][:80]
            console.print(f"  [yellow]Could not fill '{field.get('label', field_id)}': {err_msg}[/]")
