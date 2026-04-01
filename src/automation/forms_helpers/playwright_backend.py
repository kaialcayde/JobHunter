"""Playwright-driven form extraction and fill helpers."""

import logging

from rich.console import Console

console = Console(force_terminal=True)
logger = logging.getLogger(__name__)


def extract_form_fields_playwright(page) -> list[dict]:
    """Extract form fields using Playwright locators (pierces shadow DOM)."""
    fields = []
    seen_ids = set()

    input_types = ["text", "email", "tel", "number", "url", "date"]
    for input_type in input_types:
        try:
            locators = page.locator(f'input[type="{input_type}"]').all()
            for loc in locators:
                try:
                    if not loc.is_visible(timeout=300):
                        continue
                    attrs = loc.evaluate("""el => ({
                        id: el.id || el.name || el.getAttribute('aria-label') || '',
                        name: el.name || '',
                        label: (() => {
                            if (el.id) {
                                const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                                if (lbl) return lbl.textContent.trim();
                            }
                            let parent = el.parentElement;
                            for (let i = 0; i < 5 && parent; i++) {
                                const lbl = parent.querySelector('label');
                                if (lbl) return lbl.textContent.trim();
                                parent = parent.parentElement;
                            }
                            return el.getAttribute('aria-label') || el.placeholder || el.name || el.id || '';
                        })(),
                        type: el.type || 'text',
                        required: el.required || el.getAttribute('aria-required') === 'true',
                        value: el.value || '',
                        selector: el.id ? '#' + CSS.escape(el.id) : (el.name ? 'input[name="' + el.name + '"]' : '')
                    })""")
                    fid = attrs.get("id") or attrs.get("name") or f"input_{len(fields)}"
                    if fid in seen_ids:
                        continue
                    seen_ids.add(fid)
                    field_dict = {
                        "id": fid,
                        "selector": attrs.get("selector", ""),
                        "label": attrs.get("label", ""),
                        "type": attrs.get("type", "text"),
                        "required": attrs.get("required", False),
                        "value": attrs.get("value", ""),
                        "visible": True,
                    }
                    field_dict["_locator"] = loc
                    fields.append(field_dict)
                except Exception:
                    continue
        except Exception:
            continue

    try:
        for loc in page.locator("textarea").all():
            try:
                if not loc.is_visible(timeout=300):
                    continue
                attrs = loc.evaluate("""el => ({
                    id: el.id || el.name || '',
                    label: el.getAttribute('aria-label') || el.placeholder || el.name || '',
                    required: el.required,
                    selector: el.id ? '#' + CSS.escape(el.id) : (el.name ? 'textarea[name="' + el.name + '"]' : 'textarea')
                })""")
                fid = attrs.get("id") or f"textarea_{len(fields)}"
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                fields.append({
                    "id": fid,
                    "selector": attrs.get("selector", ""),
                    "label": attrs.get("label", ""),
                    "type": "textarea",
                    "required": attrs.get("required", False),
                    "visible": True,
                    "_locator": loc,
                })
            except Exception:
                continue
    except Exception:
        pass

    try:
        for loc in page.locator("select").all():
            try:
                if not loc.is_visible(timeout=300):
                    continue
                attrs = loc.evaluate("""el => ({
                    id: el.id || el.name || '',
                    label: el.getAttribute('aria-label') || el.name || '',
                    required: el.required,
                    options: Array.from(el.options).map(o => o.text.trim()).filter(t => t),
                    selector: el.id ? '#' + CSS.escape(el.id) : (el.name ? 'select[name="' + el.name + '"]' : 'select')
                })""")
                fid = attrs.get("id") or f"select_{len(fields)}"
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                fields.append({
                    "id": fid,
                    "selector": attrs.get("selector", ""),
                    "label": attrs.get("label", ""),
                    "type": "select",
                    "required": attrs.get("required", False),
                    "options": attrs.get("options", []),
                    "visible": True,
                    "_locator": loc,
                })
            except Exception:
                continue
    except Exception:
        pass

    try:
        for loc in page.locator('input[type="file"]').all():
            try:
                attrs = loc.evaluate("""el => ({
                    id: el.id || el.name || '',
                    label: el.getAttribute('aria-label') || el.name || '',
                    accept: el.accept || '',
                    selector: el.id ? '#' + CSS.escape(el.id) : 'input[type="file"]'
                })""")
                fid = attrs.get("id") or f"file_{len(fields)}"
                fields.append({
                    "id": fid,
                    "selector": attrs.get("selector", ""),
                    "label": attrs.get("label", ""),
                    "type": "file",
                    "accept": attrs.get("accept", ""),
                    "_locator": loc,
                })
            except Exception:
                continue
    except Exception:
        pass

    if fields:
        logger.info(f"Playwright extraction found {len(fields)} fields (shadow DOM aware)")
    return fields


def fill_form_fields_playwright(page, fields: list[dict], answers: dict):
    """Fill form fields using Playwright locators (pierces shadow DOM)."""
    for field in fields:
        fid = field["id"]
        if fid not in answers or field["type"] == "file":
            continue

        value = str(answers[fid])
        if not value or value == "N/A":
            continue

        loc = field.get("_locator")
        if not loc:
            continue

        try:
            if field["type"] == "select":
                loc.select_option(label=value, timeout=3000)
                console.print(f"  [dim]  Filled '{field.get('label', fid)}' = '{value[:30]}'[/]")
            elif field["type"] in ("text", "email", "tel", "number", "url", "date", "textarea"):
                loc.fill(value, timeout=3000)
                console.print(f"  [dim]  Filled '{field.get('label', fid)}' = '{value[:30]}'[/]")
            elif field["type"] == "checkbox":
                should_check = value.lower() in ("true", "yes", "1", "checked", "agree")
                is_checked = loc.is_checked()
                if should_check != is_checked:
                    loc.click(timeout=3000)
        except Exception as e:
            logger.warning(f"Failed to fill '{field.get('label', fid)}': {e}")
            console.print(f"  [dim]  Failed to fill '{field.get('label', fid)}': {str(e)[:40]}[/]")
