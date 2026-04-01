() => {
    const inputs = document.querySelectorAll(
        'input[type="text"], input[type="email"], input[type="tel"], ' +
        'textarea, select, input[type="file"]'
    );
    let count = 0;
    for (const el of inputs) {
        if (el.offsetWidth === 0 && el.offsetHeight === 0 && el.getClientRects().length === 0) {
            continue;
        }
        const name = (el.name || '').toLowerCase();
        const id = (el.id || '').toLowerCase();
        const placeholder = (el.placeholder || '').toLowerCase();
        const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
        const allAttrs = name + ' ' + id + ' ' + placeholder + ' ' + ariaLabel;
        if (allAttrs.match(/search|filter|keyword|location|sort|query/)) continue;
        count++;
    }
    return count;
}
