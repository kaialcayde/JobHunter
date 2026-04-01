({applyTexts}) => {
    const matchesApplyText = (text) =>
        (applyTexts || []).some((match) => text === match || text.startsWith(match));

    const links = document.querySelectorAll('a');
    for (const link of links) {
        if (link.offsetWidth === 0 || link.offsetHeight === 0) continue;
        const text = (link.textContent || '').trim().toLowerCase();
        if (matchesApplyText(text) && link.href && link.href.startsWith('http')) {
            return { type: 'link', href: link.href };
        }
    }

    const buttons = document.querySelectorAll(
        'button, [data-testid*="apply"], .apply-button, #apply-button, ' +
        '[data-testid*="interest"], .js-btn-apply'
    );
    for (const btn of buttons) {
        if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
        const text = (btn.textContent || '').trim().toLowerCase();
        if ((applyTexts || []).some((match) => text === match || text.includes(match))) {
            const tag = btn.tagName.toLowerCase();
            const href = tag === 'a' ? btn.href : null;
            if (href && href.startsWith('http')) return { type: 'link', href: href };
            return { type: 'button' };
        }
    }

    return null;
}
