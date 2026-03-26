"""Form field extraction, filling, and file upload handling."""

from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console(force_terminal=True)


def extract_form_fields(page) -> list[dict]:
    """Extract all form fields from the current page using DOM inspection."""
    # Scroll down to trigger lazy-loaded content
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(1000)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)

    fields = page.evaluate("""() => {
        const fields = [];
        const seen = new Set();
        let autoIdx = 0;

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
        document.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="file"])').forEach(el => {
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
        document.querySelectorAll('textarea').forEach(el => {
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
        document.querySelectorAll('select').forEach(el => {
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
        document.querySelectorAll('[role="listbox"], [role="combobox"], [data-testid*="select"], [class*="select__control"]').forEach(el => {
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
        document.querySelectorAll('input[type="radio"]').forEach(el => {
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
        document.querySelectorAll('input[type="file"]').forEach(el => {
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
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(200)

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
                        opt.scroll_into_view_if_needed()
                        opt.click()
                        break
            elif field["type"] == "textarea":
                page.fill(selector, value, timeout=5000)
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
            if any(kw in label for kw in ["resume", "cv", "curriculum"]):
                if resume_file and resume_file.exists():
                    file_input.set_input_files(str(resume_file))
                    console.print(f"  Uploaded resume: {resume_file.name}")
            elif any(kw in label for kw in ["cover letter", "cover_letter", "coverletter"]):
                if cl_file and cl_file.exists():
                    file_input.set_input_files(str(cl_file))
                    console.print(f"  Uploaded cover letter: {cl_file.name}")
            else:
                # Generic file upload -- use position: first=resume, second=cover letter
                if generic_upload_idx == 0 and resume_file and resume_file.exists():
                    file_input.set_input_files(str(resume_file))
                    console.print(f"  Uploaded resume (position {generic_upload_idx + 1}): {resume_file.name}")
                elif generic_upload_idx == 1 and cl_file and cl_file.exists():
                    file_input.set_input_files(str(cl_file))
                    console.print(f"  Uploaded cover letter (position {generic_upload_idx + 1}): {cl_file.name}")
                elif resume_file and resume_file.exists():
                    file_input.set_input_files(str(resume_file))
                    console.print(f"  Uploaded file (defaulting to resume): {resume_file.name}")
                generic_upload_idx += 1
        except Exception as e:
            console.print(f"  [yellow]File upload failed: {e}[/]")
