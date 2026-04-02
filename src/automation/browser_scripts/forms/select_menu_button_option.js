(el, desiredText) => {
    const normalize = (value) => String(value || '')
        .toLowerCase()
        .replace(/\u00a0/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();

    const target = normalize(desiredText);
    if (!target) return { status: 'missing-target' };

    const menu = el.nextElementSibling;
    if (!menu) return { status: 'no-menu' };

    const candidates = [];
    const slot = menu.querySelector('slot');
    if (slot && slot.assignedElements) {
        for (const assigned of slot.assignedElements({ flatten: true })) {
            candidates.push(assigned);
        }
    }
    for (const node of menu.querySelectorAll('[role="menuitem"], [role="menuitemradio"], lightning-menu-item, [data-value]')) {
        candidates.push(node);
    }

    const items = [];
    for (const node of candidates) {
        const text = normalize(
            node.innerText ||
            node.textContent ||
            (node.getAttribute && (node.getAttribute('label') || node.getAttribute('value'))) ||
            ''
        );
        if (!text) continue;
        items.push({ node, text });
    }

    if (items.some((item) => item.text === 'loading')) {
        return { status: 'loading' };
    }

    const match = items.find((item) => item.text === target)
        || items.find((item) => item.text.includes(target) || target.includes(item.text));
    if (!match) {
        return {
            status: 'available',
            options: items.slice(0, 10).map((item) => item.text),
        };
    }

    match.node.click();
    return { status: 'selected', text: match.text };
}
