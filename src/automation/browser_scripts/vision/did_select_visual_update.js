({x, y, text}) => {
    let el = document.elementFromPoint(x, y);
    if (!el) return false;
    const desired = text.toLowerCase();
    const container = el.closest('[class*="select"], [class*="dropdown"], div, li') || el.parentElement;
    if (!container) return false;

    const triggers = container.querySelectorAll(
        '[class*="selected"], [class*="value"], [class*="trigger"], [class*="singleValue"], span:not([class*="placeholder"])'
    );
    for (const trigger of triggers) {
        const triggerText = trigger.textContent.trim().toLowerCase();
        if (triggerText === desired || triggerText.includes(desired)) return true;
    }

    const placeholder = container.querySelector('[class*="placeholder"], [class*="hint"]');
    if (!placeholder) return true;
    const placeholderText = placeholder.textContent.trim().toLowerCase();
    return placeholderText.length > 0 &&
        !placeholderText.includes('select') &&
        !placeholderText.includes('choose') &&
        !placeholderText.includes('pick');
}
