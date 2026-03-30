"""Form field extraction, filling, and file upload handling.

Unified API:
    extract_fields(page, use_playwright=False) — single entry point for extraction
    fill_fields(page, fields, answers, use_playwright=False) — single entry point for filling
    find_input_at_coords(page, x, y) — DOM-based element lookup by coordinates
    dom_fill_fallback(page, x, y, text) — React-compatible fill at coordinates
    dom_select_fallback(page, x, y, text) — Select/combobox fill at coordinates
"""

import logging
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console(force_terminal=True)
logger = logging.getLogger(__name__)


# ── Unified Entry Points ────────────────────────────────────────────

def extract_fields(page, *, use_playwright: bool = False) -> list[dict]:
    """Unified form field extraction.

    Use use_playwright=True for shadow DOM (LinkedIn Easy Apply).
    Falls back to JS-based extraction if Playwright finds nothing.
    """
    if use_playwright:
        fields = extract_form_fields_playwright(page)
        if fields:
            return fields
        # Fallback to JS extraction
    return extract_form_fields(page)


def fill_fields(page, fields: list[dict], answers: dict, *, use_playwright: bool = False):
    """Unified form filling.

    Routes to Playwright-based or JS-based filling depending on the extraction method.
    """
    if use_playwright and any(f.get("_locator") for f in fields):
        fill_form_fields_playwright(page, fields, answers)
    else:
        fill_form_fields(page, fields, answers)


# ── Coordinate-Based DOM Helpers (used by vision_agent) ─────────────

def find_input_at_coords(page, x: int, y: int):
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


def dom_fill_fallback(page, x: int, y: int, text: str) -> bool:
    """Try to fill a field at coordinates using DOM methods (page.fill / JS dispatch).

    Returns True if the value was successfully set.
    """
    el_info = find_input_at_coords(page, x, y)
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


def dom_select_fallback(page, x: int, y: int, text: str) -> bool:
    """Try to select an option using DOM methods for native <select> or React-Select.

    Handles:
    - Native <select> elements (page.select_option)
    - React-Select combobox inputs (type to filter + Enter)
    - Custom dropdown containers (click to open + click option)

    Returns True if selection was successful.
    """
    el_info = find_input_at_coords(page, x, y)
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
    is_combobox = el_info.get("isCombobox", False)
    combobox_selector = selector if is_combobox else None

    if not is_combobox:
        combobox_selector = page.evaluate("""({x, y}) => {
            let el = document.elementFromPoint(x, y);
            if (!el) return null;
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

                # Verify selection took effect
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

    # Last resort: find a hidden native <select> by proximity to coordinates.
    # Some ATS platforms (e.g. Avature) wrap a hidden <select> inside a custom UI overlay.
    # elementFromPoint can't find hidden elements, so we search all <select>s by layout position.
    try:
        result = page.evaluate("""({x, y, text}) => {
            const selects = Array.from(document.querySelectorAll('select'));
            const textLower = text.toLowerCase();
            let best = null, bestDist = Infinity;
            for (const sel of selects) {
                // Walk up to find a visible ancestor for position reference
                let posEl = sel;
                while (posEl && posEl.getBoundingClientRect().width === 0) {
                    posEl = posEl.parentElement;
                }
                if (!posEl) continue;
                const rect = posEl.getBoundingClientRect();
                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                const dist = Math.hypot(cx - x, cy - y);
                if (dist < 150 && dist < bestDist) {
                    // Check if any option matches the desired text
                    const match = Array.from(sel.options).find(o =>
                        o.text.trim().toLowerCase().includes(textLower) ||
                        textLower.includes(o.text.trim().toLowerCase())
                    );
                    if (match) { best = {sel, value: match.value}; bestDist = dist; }
                }
            }
            if (!best) return null;
            const proto = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
            if (proto && proto.set) proto.set.call(best.sel, best.value);
            else best.sel.value = best.value;
            best.sel.dispatchEvent(new Event('change', {bubbles: true}));
            best.sel.dispatchEvent(new Event('input', {bubbles: true}));
            return best.value;
        }""", {"x": x, "y": y, "text": text})
        if result:
            page.wait_for_timeout(300)
            return True
    except Exception as e:
        logger.debug(f"Hidden select proximity search failed: {e}")

    return False


# ── Original Form Functions ─────────────────────────────────────────

def extract_form_fields_playwright(page) -> list[dict]:
    """Extract form fields using Playwright locators (pierces shadow DOM).

    Used for LinkedIn Easy Apply modals that render inside shadow DOM hosts.
    Falls back to the JS-based extract_form_fields if this finds nothing.
    """
    fields = []
    seen_ids = set()

    # Find all visible input fields
    input_types = ['text', 'email', 'tel', 'number', 'url', 'date']
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
                            // Walk up to find label in shadow DOM context
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
                    fid = attrs.get('id') or attrs.get('name') or f'input_{len(fields)}'
                    if fid in seen_ids:
                        continue
                    seen_ids.add(fid)
                    field_dict = {
                        'id': fid,
                        'selector': attrs.get('selector', ''),
                        'label': attrs.get('label', ''),
                        'type': attrs.get('type', 'text'),
                        'required': attrs.get('required', False),
                        'value': attrs.get('value', ''),
                        'visible': True,
                    }
                    field_dict['_locator'] = loc  # keep reference for filling (not serializable)
                    fields.append(field_dict)
                except Exception:
                    continue
        except Exception:
            continue

    # Textareas
    try:
        for loc in page.locator('textarea').all():
            try:
                if not loc.is_visible(timeout=300):
                    continue
                attrs = loc.evaluate("""el => ({
                    id: el.id || el.name || '',
                    label: el.getAttribute('aria-label') || el.placeholder || el.name || '',
                    required: el.required,
                    selector: el.id ? '#' + CSS.escape(el.id) : (el.name ? 'textarea[name="' + el.name + '"]' : 'textarea')
                })""")
                fid = attrs.get('id') or f'textarea_{len(fields)}'
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                fields.append({
                    'id': fid,
                    'selector': attrs.get('selector', ''),
                    'label': attrs.get('label', ''),
                    'type': 'textarea',
                    'required': attrs.get('required', False),
                    'visible': True,
                    '_locator': loc,
                })
            except Exception:
                continue
    except Exception:
        pass

    # Select dropdowns
    try:
        for loc in page.locator('select').all():
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
                fid = attrs.get('id') or f'select_{len(fields)}'
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                fields.append({
                    'id': fid,
                    'selector': attrs.get('selector', ''),
                    'label': attrs.get('label', ''),
                    'type': 'select',
                    'required': attrs.get('required', False),
                    'options': attrs.get('options', []),
                    'visible': True,
                    '_locator': loc,
                })
            except Exception:
                continue
    except Exception:
        pass

    # File uploads
    try:
        for loc in page.locator('input[type="file"]').all():
            try:
                attrs = loc.evaluate("""el => ({
                    id: el.id || el.name || '',
                    label: el.getAttribute('aria-label') || el.name || '',
                    accept: el.accept || '',
                    selector: el.id ? '#' + CSS.escape(el.id) : 'input[type="file"]'
                })""")
                fid = attrs.get('id') or f'file_{len(fields)}'
                fields.append({
                    'id': fid,
                    'selector': attrs.get('selector', ''),
                    'label': attrs.get('label', ''),
                    'type': 'file',
                    'accept': attrs.get('accept', ''),
                    '_locator': loc,
                })
            except Exception:
                continue
    except Exception:
        pass

    if fields:
        logger.info(f"Playwright extraction found {len(fields)} fields (shadow DOM aware)")
    return fields


def fill_form_fields_playwright(page, fields: list[dict], answers: dict):
    """Fill form fields using Playwright locators (pierces shadow DOM).

    Used for LinkedIn Easy Apply modals inside shadow DOM.
    """
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


def extract_form_fields(page) -> list[dict]:
    """Extract all form fields from the current page using DOM inspection."""
    # Scroll down to trigger lazy-loaded content (but NOT if inside a modal — scrolling
    # the main page behind a LinkedIn Easy Apply modal can dismiss it)
    try:
        in_modal = page.evaluate("""() => {
            const modal = document.querySelector(
                '.jobs-easy-apply-modal, .jobs-easy-apply-content, ' +
                '[role="dialog"], .artdeco-modal'
            );
            return !!(modal && modal.offsetWidth > 0);
        }""")
        if not in_modal:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(400)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
    except Exception:
        return []  # Page was destroyed (navigation during upload, etc.)

    fields = page.evaluate("""() => {
        const fields = [];
        const seen = new Set();
        let autoIdx = 0;

        // Scope search to modal if one is open (LinkedIn Easy Apply)
        const modal = document.querySelector(
            '.jobs-easy-apply-modal, .jobs-easy-apply-content, ' +
            '[role="dialog"], .artdeco-modal'
        );
        const scope = (modal && modal.offsetWidth > 0) ? modal : document;

        function getSelector(el) {
            // Build a reliable selector - prefer id, then name, then aria-label, then generate a CSS path
            if (el.id) return '#' + CSS.escape(el.id);
            if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
            if (el.getAttribute('aria-label')) return el.tagName.toLowerCase() + '[aria-label="' + el.getAttribute('aria-label') + '"]';
            if (el.placeholder) return el.tagName.toLowerCase() + '[placeholder="' + el.placeholder + '"]';
            // Fallback: nth-of-type path
            let path = el.tagName.toLowerCase();
            if (el.type) path += '[type="' + el.type + '"]';
            return path;
        }

        function getLabel(el) {
            if (el.id) {
                const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (label) return label.textContent.trim();
            }
            const parentLabel = el.closest('label');
            if (parentLabel) return parentLabel.textContent.trim();
            if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
            if (el.placeholder) return el.placeholder;
            const prev = el.previousElementSibling;
            if (prev && prev.tagName === 'LABEL') return prev.textContent.trim();
            return el.name || el.id || '';
        }

        function isVisible(el) {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        }

        // Text inputs, emails, numbers, tel, etc.
        scope.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="file"])').forEach(el => {
            const type = el.type || 'text';
            if (type === 'radio') return;

            const selector = getSelector(el);
            const uniqueKey = selector + '_' + (el.name || '') + '_' + (el.id || '') + '_' + autoIdx++;
            if (seen.has(uniqueKey)) return;
            seen.add(uniqueKey);

            if (type === 'checkbox') {
                fields.push({
                    id: el.id || el.name || 'checkbox_' + autoIdx,
                    selector: selector,
                    label: getLabel(el),
                    type: 'checkbox',
                    required: el.required,
                    checked: el.checked,
                    visible: isVisible(el)
                });
                return;
            }

            fields.push({
                id: el.id || el.name || el.getAttribute('aria-label') || 'input_' + autoIdx,
                selector: selector,
                label: getLabel(el),
                type: type,
                required: el.required,
                value: el.value || '',
                visible: isVisible(el)
            });
        });

        // Textareas
        scope.querySelectorAll('textarea').forEach(el => {
            const selector = getSelector(el);
            fields.push({
                id: el.id || el.name || 'textarea_' + autoIdx++,
                selector: selector,
                label: getLabel(el),
                type: 'textarea',
                required: el.required,
                maxLength: el.maxLength > 0 ? el.maxLength : null,
                visible: isVisible(el)
            });
        });

        // Select dropdowns (native)
        scope.querySelectorAll('select').forEach(el => {
            const selector = getSelector(el);
            const options = Array.from(el.options).map(o => o.text.trim()).filter(t => t);
            fields.push({
                id: el.id || el.name || 'select_' + autoIdx++,
                selector: selector,
                label: getLabel(el),
                type: 'select',
                required: el.required,
                options: options,
                visible: isVisible(el)
            });
        });

        // Custom dropdowns/listboxes (React/aria components like Greenhouse, Lever, etc.)
        scope.querySelectorAll('[role="listbox"], [role="combobox"], [data-testid*="select"], [class*="select__control"]').forEach(el => {
            const selector = getSelector(el);
            const uniqueKey = 'custom_' + selector;
            if (seen.has(uniqueKey)) return;
            seen.add(uniqueKey);

            // Try to get options from associated listbox or from aria
            let options = [];
            const optionEls = el.querySelectorAll('[role="option"]');
            if (optionEls.length > 0) {
                optionEls.forEach(o => {
                    const text = o.textContent.trim();
                    if (text) options.push(text);
                });
            }

            // Get label from aria-label, aria-labelledby, or parent context
            let label = el.getAttribute('aria-label') || '';
            if (!label) {
                const labelledBy = el.getAttribute('aria-labelledby');
                if (labelledBy) {
                    const labelEl = document.getElementById(labelledBy);
                    if (labelEl) label = labelEl.textContent.trim();
                }
            }
            if (!label) label = getLabel(el);

            if (label || options.length > 0) {
                fields.push({
                    id: el.id || el.getAttribute('aria-label') || 'custom_select_' + autoIdx++,
                    selector: selector,
                    label: label,
                    type: 'custom_select',
                    options: options,
                    visible: isVisible(el)
                });
            }
        });

        // Radio button groups
        const radioGroups = {};
        scope.querySelectorAll('input[type="radio"]').forEach(el => {
            const name = el.name;
            if (!name) return;
            if (!radioGroups[name]) {
                radioGroups[name] = {
                    id: name,
                    selector: `[name="${name}"]`,
                    label: getLabel(el),
                    type: 'radio',
                    options: []
                };
            }
            const label = getLabel(el) || el.value;
            if (label && !radioGroups[name].options.includes(label)) {
                radioGroups[name].options.push(label);
            }
        });
        Object.values(radioGroups).forEach(g => fields.push(g));

        // File uploads
        scope.querySelectorAll('input[type="file"]').forEach(el => {
            const id = el.id || el.name || 'file_' + autoIdx++;
            fields.push({
                id: id,
                selector: getSelector(el),
                label: getLabel(el),
                type: 'file',
                accept: el.accept || ''
            });
        });

        return fields;
    }""")

    # If no fields found on main page, check iframes
    if not fields:
        for frame in page.frames[1:]:
            try:
                fields = frame.evaluate("""() => {
                    const fields = [];
                    document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"])').forEach(el => {
                        if (el.type === 'radio' || el.type === 'checkbox' || el.type === 'file') return;
                        fields.push({
                            id: el.id || el.name || el.getAttribute('aria-label') || 'input',
                            selector: el.id ? '#' + CSS.escape(el.id) : el.name ? el.tagName.toLowerCase() + '[name="' + el.name + '"]' : el.tagName.toLowerCase(),
                            label: el.getAttribute('aria-label') || el.placeholder || el.name || el.id || '',
                            type: el.type || 'text', required: el.required, value: el.value || '', visible: true
                        });
                    });
                    document.querySelectorAll('textarea, select').forEach(el => {
                        fields.push({
                            id: el.id || el.name || el.tagName.toLowerCase(),
                            selector: el.id ? '#' + CSS.escape(el.id) : el.name ? el.tagName.toLowerCase() + '[name="' + el.name + '"]' : el.tagName.toLowerCase(),
                            label: el.getAttribute('aria-label') || el.name || el.id || '',
                            type: el.tagName === 'SELECT' ? 'select' : 'textarea',
                            required: el.required, visible: true,
                            options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => o.text.trim()).filter(t => t) : undefined
                        });
                    });
                    return fields;
                }""")
                if fields:
                    console.print(f"  [dim]Found fields inside iframe[/]")
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

        # Skip fields that were detected as not visible
        if not field.get("visible", True):
            continue

        value = str(answers[field_id])
        selector = field.get("selector", "")
        if not selector:
            continue

        try:
            # Try to find the element first
            el = page.query_selector(selector)
            if not el:
                console.print(f"  [dim]Skipping '{field.get('label', field_id)}' - element not found[/]")
                continue

            # Scroll into view and wait for visibility
            el.scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(100)

            if field["type"] == "select":
                page.select_option(selector, label=value, timeout=5000)
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
                    label = page.evaluate("(el) => { const l = el.closest('label'); return l ? l.textContent.trim() : el.value; }", opt)
                    if value.lower() in label.lower():
                        opt.scroll_into_view_if_needed(timeout=3000)
                        opt.click()
                        break
            elif field["type"] == "textarea":
                page.fill(selector, value, timeout=5000)
            else:
                # Check if this is a React-Select combobox disguised as text input
                is_combobox = page.evaluate("""(selector) => {
                    const el = document.querySelector(selector);
                    return el && (el.getAttribute('role') === 'combobox' ||
                                  el.classList.contains('select__input') ||
                                  !!el.closest('.select__control, .select__container'));
                }""", selector) or False

                if is_combobox:
                    _fill_react_select(page, el, value)
                else:
                    # Try fill first, fall back to click+type for stubborn inputs
                    try:
                        page.fill(selector, value, timeout=5000)
                    except Exception:
                        el.click()
                        page.wait_for_timeout(100)
                        page.keyboard.type(value)
        except Exception as e:
            err_msg = str(e).split("\n")[0][:80]
            console.print(f"  [yellow]Could not fill '{field.get('label', field_id)}': {err_msg}[/]")


def _fill_react_select(page, el, value: str):
    """Handle React-Select combobox inputs (Greenhouse, Lever, etc.).

    These are <input role="combobox"> inside .select__control containers.
    Type to filter options, then click the matching visible option.
    Falls back to alternate values if no match found.
    """
    def _try_type_and_select(text: str) -> bool:
        """Type text to filter, click first visible option. Returns True if selected."""
        try:
            # Close any open dropdown first
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            # Clear via JS and re-focus
            el.evaluate('e => e.value = ""')
            page.wait_for_timeout(100)
            el.click()
            page.wait_for_timeout(400)
            page.keyboard.type(text, delay=50)
            page.wait_for_timeout(800)

            options = page.query_selector_all('[role="option"]')
            for opt in options:
                try:
                    if opt.is_visible():
                        selected_text = opt.text_content().strip()
                        opt.click()
                        page.wait_for_timeout(300)
                        console.print(f"  [dim]React-Select: selected '{selected_text}'[/]")
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    # Try the exact value first
    if _try_type_and_select(value):
        return

    # Try common fallback values for "how did you hear" type fields
    fallbacks = []
    value_lower = value.lower()
    if any(kw in value_lower for kw in ["job board", "job site", "online"]):
        fallbacks = ["LinkedIn", "Online", "Other"]
    elif any(kw in value_lower for kw in ["prefer not", "decline", "n/a"]):
        fallbacks = ["Prefer not to answer", "Decline", "Other"]
    else:
        # Try first word, then "Other"
        first_word = value.split()[0] if value.split() else value
        if first_word != value:
            fallbacks = [first_word, "Other"]
        else:
            fallbacks = ["Other"]

    for fb in fallbacks:
        if _try_type_and_select(fb):
            return

    # Last resort: open dropdown and pick first available option
    try:
        el.click()
        page.wait_for_timeout(500)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(300)
        # Press down arrow then Enter to select first option
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(300)
        options = page.query_selector_all('[role="option"]')
        for opt in options:
            if opt.is_visible():
                opt.click()
                page.wait_for_timeout(300)
                console.print(f"  [dim]React-Select: selected first available option[/]")
                return
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
    except Exception:
        pass

    # Close dropdown if still open
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _fill_custom_select(page, el, value: str):
    """Handle custom dropdown/listbox components (React, Greenhouse, etc.)."""
    # Click to open the dropdown
    el.click()
    page.wait_for_timeout(500)

    # Try to find and click the matching option
    option_selectors = [
        f'[role="option"]:has-text("{value}")',
        f'li:has-text("{value}")',
        f'[class*="option"]:has-text("{value}")',
    ]
    for opt_sel in option_selectors:
        try:
            opt = page.query_selector(opt_sel)
            if opt and opt.is_visible():
                opt.click()
                page.wait_for_timeout(300)
                return
        except Exception:
            continue

    # Fallback: try typing into the dropdown to filter, then select first match
    try:
        page.keyboard.type(value[:20])
        page.wait_for_timeout(500)
        first_option = page.query_selector('[role="option"]')
        if first_option and first_option.is_visible():
            first_option.click()
            page.wait_for_timeout(300)
            return
    except Exception:
        pass

    # Last resort: press Escape to close dropdown without selecting
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def handle_file_uploads(page, resume_file: Optional[Path], cl_file: Optional[Path]):
    """Handle file upload fields -- detect resume/cover letter fields and upload.

    When labels are generic (e.g., 'Drop or select'), uses positional logic:
    first generic upload = resume, second = cover letter.
    """
    file_inputs = page.query_selector_all('input[type="file"]')
    generic_upload_idx = 0  # Track position for generic uploads

    for file_input in file_inputs:
        label = page.evaluate("""(el) => {
            if (el.id) {
                const label = document.querySelector(`label[for="${el.id}"]`);
                if (label) return label.textContent.trim().toLowerCase();
            }
            // Walk up to find context: form group, section heading, etc.
            const parent = el.closest('label, .field, .form-group, [class*="upload"], [class*="attachment"]');
            if (parent) {
                const heading = parent.querySelector('h3, h4, label, .field-label, [class*="label"]');
                if (heading) return heading.textContent.trim().toLowerCase();
                return parent.textContent.trim().toLowerCase().slice(0, 200);
            }
            return '';
        }""", file_input)

        try:
            # Determine which file to upload for this input
            upload_file: Optional[Path] = None
            upload_label = "file"
            if any(kw in label for kw in ["resume", "cv", "curriculum"]):
                upload_file = resume_file
                upload_label = "resume"
            elif any(kw in label for kw in ["cover letter", "cover_letter", "coverletter"]):
                upload_file = cl_file
                upload_label = "cover letter"
            else:
                # Generic: position-based
                if generic_upload_idx == 0:
                    upload_file = resume_file
                    upload_label = f"resume (position {generic_upload_idx + 1})"
                elif generic_upload_idx == 1:
                    upload_file = cl_file
                    upload_label = f"cover letter (position {generic_upload_idx + 1})"
                else:
                    upload_file = resume_file
                    upload_label = "file (defaulting to resume)"
                generic_upload_idx += 1

            if upload_file and upload_file.exists():
                # Try expect_file_chooser first: finds visible trigger button near the input
                # and intercepts the OS dialog that platforms like Avature open on click.
                uploaded = False
                trigger_texts = [
                    "From Device", "Browse", "Choose File", "Upload", "Attach", "Select File",
                ]
                for text in trigger_texts:
                    try:
                        trigger = page.get_by_role("button", name=text, exact=False).first
                        if trigger.is_visible(timeout=500):
                            with page.expect_file_chooser(timeout=5000) as fc_info:
                                trigger.click()
                            fc_info.value.set_files(str(upload_file))
                            page.wait_for_timeout(4000)
                            console.print(f"  Uploaded {upload_label} via file chooser ({text}): {upload_file.name}")
                            uploaded = True
                            break
                    except Exception:
                        continue
                if not uploaded:
                    file_input.set_input_files(str(upload_file))
                    page.wait_for_timeout(1500)
                    console.print(f"  Uploaded {upload_label}: {upload_file.name}")
        except Exception as e:
            if "navigation" in str(e).lower() or "destroyed" in str(e).lower():
                console.print(f"  [dim]Upload triggered page navigation -- continuing[/]")
                return  # Page navigated, stop processing stale file inputs
            console.print(f"  [yellow]File upload failed: {e}[/]")
