(el) => {
    const cleanText = (value) => String(value || '')
        .replace(/\u00a0/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    const text = cleanText(
        el.innerText ||
        el.textContent ||
        el.getAttribute?.('aria-label') ||
        ''
    );
    const role = (el.getAttribute?.('role') || '').toLowerCase();
    const directInput = el.matches?.('input[type="radio"], input[type="checkbox"]') ? el : null;
    const nestedInput = directInput || el.querySelector?.('input[type="radio"], input[type="checkbox"]');
    const hostChain = [];
    let root = el.getRootNode ? el.getRootNode() : null;
    while (root && root.host) {
        hostChain.push({
            tag: root.host.tagName,
            id: root.host.id || '',
            className: String(root.host.className || '').slice(0, 120),
            text: cleanText(root.host.innerText || root.host.textContent || '').slice(0, 180),
        });
        root = root.host.getRootNode ? root.host.getRootNode() : null;
    }
    return {
        tag: el.tagName,
        id: el.id || '',
        name: el.getAttribute?.('name') || '',
        role,
        type: el.getAttribute?.('type') || '',
        ariaChecked: el.getAttribute?.('aria-checked') || '',
        checked: !!nestedInput?.checked,
        text: text.slice(0, 180),
        className: String(el.className || '').slice(0, 120),
        outerHTML: String(el.outerHTML || '').slice(0, 500),
        rootChain: hostChain,
    };
}
