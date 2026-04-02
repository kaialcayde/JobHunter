(el) => {
    const clean = (value, maxLength = 240) => {
        const text = String(value || '')
            .replace(/\u00a0/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
        return text.length <= maxLength ? text : text.slice(0, maxLength).trim();
    };
    const summarize = (node) => {
        if (!node) return null;
        return {
            tag: node.tagName || '',
            id: node.id || '',
            className: clean(node.className || '', 180),
            role: node.getAttribute ? node.getAttribute('role') : null,
            ariaLabel: node.getAttribute ? clean(node.getAttribute('aria-label') || '', 180) : '',
            ariaLabelledby: node.getAttribute ? clean(node.getAttribute('aria-labelledby') || '', 180) : '',
            ariaControls: node.getAttribute ? clean(node.getAttribute('aria-controls') || node.getAttribute('aria-owns') || '', 180) : '',
            ariaExpanded: node.getAttribute ? node.getAttribute('aria-expanded') : null,
            placeholder: clean(node.placeholder || '', 120),
            text: clean(node.innerText || node.textContent || '', 240),
            outerHTML: clean(node.outerHTML || '', 500),
        };
    };

    const ancestors = [];
    let current = el.parentElement;
    for (let i = 0; i < 6 && current; i++) {
        ancestors.push(summarize(current));
        current = current.parentElement;
    }

    const rootChain = [];
    let root = el.getRootNode ? el.getRootNode() : null;
    while (root) {
        if (root.host) {
            rootChain.push(summarize(root.host));
            root = root.host.getRootNode ? root.host.getRootNode() : null;
        } else {
            break;
        }
    }

    return {
        self: summarize(el),
        previousSibling: summarize(el.previousElementSibling),
        nextSibling: summarize(el.nextElementSibling),
        closestField: summarize(el.closest('[class*="field"], [class*="Field"], [class*="form"], [class*="Form"], [class*="question"], [class*="Question"], [class*="input"], [class*="Input"], [role="group"], [role="radiogroup"], fieldset')),
        closestLabel: summarize(el.closest('label')),
        ancestors,
        rootChain,
    };
}
