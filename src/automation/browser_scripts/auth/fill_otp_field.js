(code) => {
    const inputs = document.querySelectorAll(
        'input[type="text"], input[type="number"], input[type="tel"]'
    );
    for (const inp of inputs) {
        const label = (inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || '').toLowerCase();
        const parentText = (inp.closest('label, div, fieldset')?.textContent || '').toLowerCase();
        if (['verification', 'code', 'otp', 'confirm', 'one-time'].some(
            (keyword) => label.includes(keyword) || parentText.includes(keyword)
        )) {
            inp.focus();
            inp.value = code;
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }
    }

    const active = document.activeElement;
    if (active && active.tagName === 'INPUT') {
        active.value = code;
        active.dispatchEvent(new Event('input', { bubbles: true }));
        active.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
    }
    return false;
}
