(patterns) => {
    const modal = document.querySelector(
        '.jobs-easy-apply-modal, .jobs-easy-apply-content, ' +
        '[role="dialog"], .artdeco-modal'
    );
    const scope = (modal && modal.offsetWidth > 0) ? modal : document;
    if (scope === document) {
        window.scrollTo(0, document.body.scrollHeight);
    }

    const clickable = scope.querySelectorAll(
        'button, a, input[type="submit"], [role="button"]'
    );
    for (const el of clickable) {
        if (el.offsetWidth === 0 || el.offsetHeight === 0) continue;
        const text = (el.textContent || el.value || '').trim().toLowerCase();
        for (const pattern of patterns || []) {
            if (text === pattern || text.startsWith(pattern)) {
                const tag = el.tagName.toLowerCase();
                if (el.id) return { selector: '#' + el.id, matched: text };
                const ariaLabel = el.getAttribute('aria-label');
                if (ariaLabel) {
                    return {
                        selector: '[aria-label="' + ariaLabel + '"]',
                        matched: text
                    };
                }
                const cleanText = text.substring(0, 50).replace(/"/g, '\\"');
                return {
                    selector: tag + ':has-text("' + cleanText + '")',
                    matched: text
                };
            }
        }
    }

    if (scope === document) window.scrollTo(0, 0);
    return null;
}
