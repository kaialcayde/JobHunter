({x, y, text}) => {
    let el = document.elementFromPoint(x, y);
    if (!el) return false;
    const desired = text.toLowerCase();

    const container = el.closest('.select, .select__container, .select__control, [class*="select"]');
    if (container) {
        const sv = container.querySelector('[class*="single-value"], [class*="singleValue"]');
        if (sv && sv.textContent.trim()) {
            const current = sv.textContent.trim().toLowerCase();
            if (current === desired || current.includes(desired) || desired.includes(current)) {
                return true;
            }
        }
    }

    const parent = el.closest('div, li, fieldset, label') || el.parentElement;
    if (parent) {
        const displayEls = parent.querySelectorAll(
            '[class*="selected"], [class*="value"], [class*="trigger"]'
        );
        for (const displayEl of displayEls) {
            const displayText = displayEl.textContent.trim().toLowerCase();
            if (displayText === desired || displayText.includes(desired)) return true;
        }
    }

    return false;
}
