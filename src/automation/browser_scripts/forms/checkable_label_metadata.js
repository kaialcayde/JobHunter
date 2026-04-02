(el) => {
    const cleanText = (value) => String(value || '')
        .replace(/\u00a0/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    const questionPrefix = (value) => {
        const text = cleanText(value)
            .replace(/\bselection is required\b/ig, ' ')
            .replace(/\s+/g, ' ')
            .trim();
        if (!text) return '';
        const questionIndex = text.indexOf('?');
        if (questionIndex >= 0) {
            return text.slice(0, questionIndex + 1).trim();
        }
        const requiredIndex = text.indexOf('*');
        if (requiredIndex > 0) {
            return text.slice(0, requiredIndex).trim();
        }
        return text;
    };
    const roots = [];
    const root = el.getRootNode && el.getRootNode();
    if (root && root.querySelector) roots.push(root);
    if (!roots.includes(document)) roots.push(document);

    const getById = (id) => {
        const lookupId = String(id || '').trim();
        if (!lookupId) return null;
        for (const queryRoot of roots) {
            try {
                const match = queryRoot.getElementById
                    ? queryRoot.getElementById(lookupId)
                    : queryRoot.querySelector('#' + CSS.escape(lookupId));
                if (match) return match;
            } catch (_) {}
        }
        return null;
    };

    const hostChain = [];
    let hostRoot = el.getRootNode ? el.getRootNode() : null;
    while (hostRoot && hostRoot.host) {
        hostChain.push(hostRoot.host);
        hostRoot = hostRoot.host.getRootNode ? hostRoot.host.getRootNode() : null;
    }

    const linkedId = el.getAttribute('for') || '';
    const linkedInput = getById(linkedId);
    const optionLabel = cleanText(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
    const className = String(el.className || '');
    const inputType = (linkedInput?.getAttribute?.('type') || '').toLowerCase();
    const radioLike = inputType === 'radio' || /radio/i.test(className);
    const checkboxLike = inputType === 'checkbox' || /checkbox/i.test(className);

    let groupLabel = '';
    for (const host of hostChain) {
        const text = cleanText(host.innerText || host.textContent || '');
        if (!text) continue;
        const candidate = questionPrefix(text);
        if (!candidate) continue;
        if (/selection is required/i.test(text) || /\?/.test(text) || /\*/.test(text)) {
            groupLabel = candidate;
            break;
        }
    }
    if (!groupLabel) {
        const parentText = questionPrefix(el.parentElement?.innerText || el.parentElement?.textContent || '');
        groupLabel = parentText || optionLabel;
    }

    groupLabel = groupLabel
        .replace(/\bselection is required\b/ig, ' ')
        .replace(/\s*\*\s*/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();

    return {
        linkedId,
        optionLabel,
        groupId: linkedInput?.getAttribute?.('name') || groupLabel || linkedId || optionLabel,
        groupLabel,
        inputType,
        radioLike,
        checkboxLike,
        checked: !!linkedInput?.checked,
        required: !!linkedInput?.required || /\*/.test(groupLabel),
    };
}
