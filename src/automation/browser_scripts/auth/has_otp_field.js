() => {
    const inputs = document.querySelectorAll(
        'input[type="text"], input[type="number"], input[type="tel"]'
    );
    for (const inp of inputs) {
        const label = (inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || '').toLowerCase();
        const parentText = (inp.closest('label, div, fieldset')?.textContent || '').toLowerCase();
        if (['verification', 'code', 'otp', 'confirm', 'one-time'].some(
            (keyword) => label.includes(keyword) || parentText.includes(keyword)
        )) {
            return true;
        }
    }
    return false;
}
