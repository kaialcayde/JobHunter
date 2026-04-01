({selector, value}) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    el.focus();
    el.textContent = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return true;
}
