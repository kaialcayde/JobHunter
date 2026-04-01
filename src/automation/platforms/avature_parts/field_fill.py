"""Standard field-fill helpers for Avature widgets."""

from .common import logger


def _standard_select(page, select_id: str, label_text: str,
                     label_hint: str = "", force: bool = False) -> bool:
    """Fill a standard <select> element by ID using Playwright select_option."""
    try:
        el = page.query_selector(f'select[id="{select_id}"]')
        if not el:
            logger.debug(f"standard_select: #{select_id} not found ({label_hint})")
            return False
        current = el.evaluate('e => e.value')
        if not force and current and current != "" and current != "0":
            logger.debug(f"standard_select: #{select_id} already has value {current!r}")
            return False
        try:
            page.select_option(f'select[id="{select_id}"]', label=label_text, timeout=1000)
            logger.debug(f"standard_select: label match {label_text!r} on #{select_id}")
            return True
        except Exception:
            pass
        try:
            page.select_option(f'select[id="{select_id}"]', value=label_text, timeout=1000)
            logger.debug(f"standard_select: value match {label_text!r} on #{select_id}")
            return True
        except Exception:
            pass
        matched = page.evaluate("""(args) => {
            const [selId, text] = args;
            const sel = document.getElementById(selId);
            if (!sel) return false;
            const lc = text.toLowerCase();
            for (const opt of sel.options) {
                if (opt.text.toLowerCase().startsWith(lc)) {
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
            }
            for (const opt of sel.options) {
                if (opt.value.toLowerCase() === lc) {
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
            }
            let best = null;
            for (const opt of sel.options) {
                if (opt.text.toLowerCase().includes(lc)) {
                    if (!best || opt.text.length < best.text.length) {
                        best = opt;
                    }
                }
            }
            if (best) {
                sel.value = best.value;
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            return false;
        }""", [select_id, label_text])
        if matched:
            logger.debug(f"standard_select: JS match for {label_text!r} on #{select_id}")
        return matched
    except Exception as e:
        logger.debug(f"standard_select failed for #{select_id} ({label_hint}): {e}")
        return False


def _get_current_work_experience(work_exp_list: list[dict]) -> dict | None:
    """Return the current role, falling back to the first work experience entry."""
    for entry in work_exp_list:
        end_date = str(entry.get("end_date", "")).strip().lower()
        if end_date in ("present", "current", ""):
            return entry
    return work_exp_list[0] if work_exp_list else None


def _fill_text_field(page, input_id: str, value: str,
                     label_hint: str, filled: dict,
                     force: bool = False,
                     allow_hidden: bool = False) -> bool:
    """Fill a visible text input with React-friendly events."""
    if not value:
        return False
    el = page.query_selector(f'input[id="{input_id}"], textarea[id="{input_id}"]')
    if not el:
        return False
    try:
        is_visible = el.is_visible()
        if not is_visible and not allow_hidden:
            return False
        current = (el.evaluate('e => e.value') or "").strip()
        if not force and current:
            return False
        if current == value.strip():
            filled[label_hint] = value
            return True
        if is_visible:
            el.fill(value)
            el.dispatch_event("input")
            el.dispatch_event("change")
            el.dispatch_event("blur")
        else:
            page.evaluate("""(args) => {
                const [inputId, nextValue] = args;
                const field = document.getElementById(inputId);
                if (!field) return;
                const proto = field.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) {
                    setter.call(field, nextValue);
                } else {
                    field.value = nextValue;
                }
                field.dispatchEvent(new Event('input', {bubbles: true}));
                field.dispatchEvent(new Event('change', {bubbles: true}));
                field.dispatchEvent(new Event('blur', {bubbles: true}));
            }""", [input_id, value])
        filled[label_hint] = value
        logger.debug(f"text_field: filled #{input_id} with {value!r}")
        return True
    except Exception as e:
        logger.debug(f"text_field: failed for #{input_id} ({label_hint}): {e}")
        return False


def _get_input_value(page, input_id: str) -> str:
    """Read an input or textarea value by id."""
    try:
        return page.evaluate("""(inputId) => {
            const field = document.getElementById(inputId);
            return field ? (field.value || '') : '';
        }""", input_id) or ""
    except Exception:
        return ""


def _get_select2_rendered_text(page, select_id: str) -> str:
    """Read the visible select2 rendered text for a hidden select."""
    try:
        return page.evaluate("""(selectId) => {
            const rendered = document.getElementById(`select2-${selectId}-container`);
            return rendered ? (rendered.innerText || '').trim() : '';
        }""", select_id) or ""
    except Exception:
        return ""


def _is_select2(page, select_id: str) -> bool:
    """Check if a <select> element is wrapped by select2."""
    try:
        return page.evaluate("""(id) => {
            const el = document.getElementById(id);
            return el && el.classList.contains('select2-hidden-accessible');
        }""", select_id)
    except Exception:
        return False


def _fill_date_field(page, input_id: str, date_str: str,
                     label_hint: str, filled: dict,
                     force: bool = False) -> bool:
    """Fill a date/month input field on Avature."""
    el = page.query_selector(f'input[id="{input_id}"]')
    if not el or not el.is_visible():
        return False
    try:
        current = el.evaluate('e => e.value')
        if not force and current and current.strip():
            return False

        input_type = el.get_attribute("type") or "text"
        normalized = _normalize_date(date_str)
        if not normalized:
            return False

        if input_type == "month":
            el.fill(normalized)
            el.dispatch_event("change")
        elif input_type == "date":
            el.fill(normalized + "-01")
            el.dispatch_event("change")
        else:
            el.fill(normalized)
            el.dispatch_event("change")

        filled[label_hint] = normalized
        logger.debug(f"date_field: filled #{input_id} with {normalized!r}")
        return True
    except Exception as e:
        logger.debug(f"date_field: failed for #{input_id} ({label_hint}): {e}")
        return False


def _normalize_date(date_str: str) -> str | None:
    """Convert various date formats to YYYY-MM."""
    import re

    if not date_str or date_str.lower() in ("present", "current", "now"):
        return None

    m = re.match(r'^(\d{4})-(\d{1,2})', date_str)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"

    m = re.match(r'^(\d{1,2})/(\d{4})$', date_str)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"

    m = re.match(r'^(\d{4})/(\d{1,2})$', date_str)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"

    month_names = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        "january": "01", "february": "02", "march": "03", "april": "04",
        "june": "06", "july": "07", "august": "08", "september": "09",
        "october": "10", "november": "11", "december": "12",
    }
    m = re.match(r'^([A-Za-z]+)\s+(\d{4})$', date_str.strip())
    if m:
        month_key = m.group(1).lower()
        if month_key in month_names:
            return f"{m.group(2)}-{month_names[month_key]}"

    m = re.match(r'^(\d{4})$', date_str.strip())
    if m:
        return f"{m.group(1)}-01"

    return None
