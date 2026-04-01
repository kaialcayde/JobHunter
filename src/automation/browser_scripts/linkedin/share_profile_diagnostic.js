() => {
    const result = { dialogs: [], continueButtons: [], bodySnippet: '' };
    const selectors = '[role="dialog"], .artdeco-modal, .artdeco-modal-overlay, [data-test-modal], div[class*="modal"], div[class*="overlay"]';
    const els = document.querySelectorAll(selectors);
    for (const el of els) {
        const visible = el.offsetWidth > 0 && el.offsetHeight > 0;
        const text = (el.textContent || '').toLowerCase().slice(0, 200);
        const classes = el.className || '';
        result.dialogs.push({
            tag: el.tagName,
            classes: String(classes).slice(0, 100),
            visible,
            hasShare: text.includes('share'),
            hasProfile: text.includes('profile')
        });
    }

    const buttons = document.querySelectorAll('button, a');
    for (const button of buttons) {
        const text = (button.textContent || '').trim().toLowerCase();
        if (text.includes('continue')) {
            result.continueButtons.push({
                tag: button.tagName,
                text: text.slice(0, 50),
                visible: button.offsetWidth > 0
            });
        }
    }

    result.bodySnippet = (document.body?.textContent || '').slice(0, 500);
    return result;
}
