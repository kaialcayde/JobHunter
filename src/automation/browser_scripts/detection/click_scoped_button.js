({modalSelectors, selectors, textMatches, buttonSelector, scrollToBottom, scrollToTopOnMiss}) => {
    const modal = modalSelectors ? document.querySelector(modalSelectors) : null;
    const scope = (modal && modal.offsetWidth > 0) ? modal : document;

    if (scope === document && scrollToBottom && document.body) {
        window.scrollTo(0, document.body.scrollHeight);
    }

    for (const sel of selectors || []) {
        const btn = scope.querySelector(sel);
        if (btn && btn.offsetWidth > 0 && btn.offsetHeight > 0) {
            btn.scrollIntoView({ block: 'center' });
            btn.click();
            return true;
        }
    }

    const buttons = scope.querySelectorAll(
        buttonSelector || 'button, input[type="submit"], a, [role="button"]'
    );
    for (const match of textMatches || []) {
        for (const btn of buttons) {
            if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
            const text = (btn.textContent || btn.value || '').trim().toLowerCase();
            if (text === match || text.startsWith(match)) {
                btn.scrollIntoView({ block: 'center' });
                btn.click();
                return true;
            }
        }
    }

    if (scope === document && scrollToTopOnMiss) {
        window.scrollTo(0, 0);
    }
    return false;
}
