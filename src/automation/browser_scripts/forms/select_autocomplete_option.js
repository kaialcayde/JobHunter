(el, desiredText) => {
    const normalize = (value) => String(value || '')
        .toLowerCase()
        .replace(/\u00a0/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    const isVisible = (node) => {
        if (!node || !node.ownerDocument) return false;
        const style = window.getComputedStyle(node);
        if (!style || style.display === 'none' || style.visibility === 'hidden') {
            return false;
        }
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    const target = normalize(desiredText);
    if (!target) return { status: 'missing-target' };

    const roots = [];
    const root = el.getRootNode ? el.getRootNode() : null;
    if (root && root.querySelector) roots.push(root);
    if (!roots.includes(document)) roots.push(document);

    const optionSelectors = [
        '[role="option"]',
        '[role="menuitem"]',
        '[role="menuitemradio"]',
        '[class*="suggestion"]',
        '[class*="Suggestion"]',
        '[class*="autocomplete"] li',
        '[class*="lookup"] li',
        'li',
    ].join(', ');

    const items = [];
    for (const queryRoot of roots) {
        let scope = queryRoot;
        const sibling = el.nextElementSibling;
        if (sibling && sibling.querySelectorAll && sibling.querySelectorAll(optionSelectors).length) {
            scope = sibling;
        }
        for (const node of scope.querySelectorAll(optionSelectors)) {
            if (!isVisible(node)) continue;
            const text = normalize(node.innerText || node.textContent || '');
            if (!text) continue;
            items.push({ node, text });
        }
        if (items.length) break;
    }

    if (!items.length) return { status: 'no-options' };
    if (items.some((item) => item.text === 'loading' || item.text === 'searching')) {
        return { status: 'loading' };
    }

    const exact = items.find((item) => item.text.includes(target) || target.includes(item.text));
    if (!exact) {
        return {
            status: 'available',
            options: items.slice(0, 10).map((item) => item.text),
        };
    }

    exact.node.click();
    return { status: 'selected', text: exact.text };
}
