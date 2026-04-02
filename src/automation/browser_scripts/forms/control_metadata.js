(el) => {
    const cleanText = (value) => (value || '')
        .replace(/\u00a0/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    const stripMenuHints = (value) => cleanText(value)
        .replace(/\bshow menu\b/ig, ' ')
        .replace(/\bopen menu\b/ig, ' ')
        .replace(/\bchoose an option\b/ig, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    const textOf = (node) => stripMenuHints(node?.textContent || node?.innerText || '');
    const candidateText = (node) => {
        const text = stripMenuHints(node?.innerText || node?.textContent || '');
        if (!text) return '';
        return text.length <= 180 ? text : text.slice(0, 180).trim();
    };
    const isVisible = (node) => {
        if (!node || !node.ownerDocument) return false;
        const style = window.getComputedStyle(node);
        if (!style || style.display === 'none' || style.visibility === 'hidden') {
            return false;
        }
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const roots = [];
    const root = el.getRootNode && el.getRootNode();
    if (root && root.querySelector) roots.push(root);
    if (!roots.includes(document)) roots.push(document);

    const getByIds = (ids) => {
        const out = [];
        for (const rawId of ids || []) {
            const id = (rawId || '').trim();
            if (!id) continue;
            for (const queryRoot of roots) {
                try {
                    const match = queryRoot.getElementById
                        ? queryRoot.getElementById(id)
                        : queryRoot.querySelector('#' + CSS.escape(id));
                    if (match) {
                        const text = textOf(match);
                        if (text) out.push(text);
                        break;
                    }
                } catch (_) {}
            }
        }
        return out.join(' ').trim();
    };

    const labelSelectors = [
        'legend',
        'label',
        '[class*="label"]',
        '[class*="Label"]',
        '[class*="question"]',
        '[class*="Question"]',
        '[class*="prompt"]',
        '[class*="Prompt"]',
        '[class*="title"]',
        '[class*="Title"]',
        '[role="heading"]',
        'h1',
        'h2',
        'h3',
        'h4',
        'p',
        'span',
        'div',
    ].join(', ');

    const bestTextFromNode = (node, maxLength = 160) => {
        if (!node || !isVisible(node)) return '';
        const direct = candidateText(node);
        if (direct && direct.length <= maxLength) return direct;
        try {
            const nested = Array.from(node.querySelectorAll(labelSelectors))
                .filter((candidate) => candidate !== el && !candidate.contains(el) && isVisible(candidate))
                .map((candidate) => candidateText(candidate))
                .filter((text) => text && text.length <= maxLength)
                .sort((a, b) => a.length - b.length);
            return nested[0] || '';
        } catch (_) {
            return direct && direct.length <= maxLength ? direct : '';
        }
    };

    const previousSiblingText = (node, maxLength = 160) => {
        let sibling = node?.previousElementSibling || null;
        for (let i = 0; i < 4 && sibling; i++) {
            const text = bestTextFromNode(sibling, maxLength);
            if (text) return text;
            sibling = sibling.previousElementSibling;
        }
        return '';
    };

    const ownText = (node, maxLength = 160) => {
        if (!node) return '';
        const text = Array.from(node.childNodes || [])
            .filter((child) => {
                if (child === el) return false;
                if (child.nodeType === Node.ELEMENT_NODE && child.contains && child.contains(el)) return false;
                return true;
            })
            .map((child) => candidateText(child))
            .filter(Boolean)
            .join(' ')
            .trim();
        return text && text.length <= maxLength ? text : '';
    };

    const fieldContainer = el.closest(
        'fieldset, [role="group"], [role="radiogroup"], [class*="field"], [class*="Field"], [class*="group"], [class*="Group"], [class*="form-element"], [class*="FormElement"], [class*="question"], [class*="Question"]'
    );
    const hostChain = [];
    let hostRoot = el.getRootNode ? el.getRootNode() : null;
    while (hostRoot && hostRoot.host) {
        hostChain.push(hostRoot.host);
        hostRoot = hostRoot.host.getRootNode ? hostRoot.host.getRootNode() : null;
    }
    const humanize = (value) => stripMenuHints(String(value || '')
        .replace(/([a-z])([A-Z])/g, '$1 $2')
        .replace(/[-_]+/g, ' '));

    const optionLabel = (() => {
        if (el.labels && el.labels.length) {
            const text = textOf(el.labels[0]);
            if (text) return text;
        }
        if (el.id) {
            for (const queryRoot of roots) {
                try {
                    const explicit = queryRoot.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                    const text = textOf(explicit);
                    if (text) return text;
                } catch (_) {}
            }
        }
        const wrapped = el.closest('label');
        if (wrapped) {
            const text = textOf(wrapped);
            if (text) return text;
        }
        const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/);
        const labelled = getByIds(labelledBy);
        if (labelled) return labelled;
        const nextText = bestTextFromNode(el.nextElementSibling, 120);
        if (nextText) return nextText;
        const prevText = bestTextFromNode(el.previousElementSibling, 120);
        if (prevText) return prevText;
        return stripMenuHints(el.getAttribute('aria-label') || el.value || '');
    })();

    const hostLabel = (() => {
        for (const host of hostChain) {
            const dataId = humanize(host.getAttribute ? host.getAttribute('data-id') || '' : '');
            if (dataId) return dataId;

            const rawText = stripMenuHints(host.innerText || host.textContent || '');
            if (!rawText || rawText.length > 220) continue;
            const removals = [
                optionLabel,
                stripMenuHints(el.value || ''),
                stripMenuHints(el.placeholder || ''),
                stripMenuHints(el.getAttribute('aria-label') || ''),
                'this field is required',
                'required',
                'show menu',
            ].filter(Boolean);
            let text = rawText;
            for (const removal of removals) {
                const escaped = removal.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                text = text.replace(new RegExp(escaped, 'ig'), ' ');
            }
            text = stripMenuHints(text).replace(/\s*\*\s*/g, ' ').trim();
            if (text && text.length <= 180) return text;
        }
        return '';
    })();

    const containerLabel = (() => {
        if (!fieldContainer) return '';
        const rawText = stripMenuHints(fieldContainer.innerText || fieldContainer.textContent || '');
        if (!rawText || rawText.length > 220) return '';
        const removals = [
            optionLabel,
            stripMenuHints(el.value || ''),
            stripMenuHints(el.placeholder || ''),
            stripMenuHints(el.getAttribute('aria-label') || ''),
            'this field is required',
            'required',
            'show menu',
        ].filter(Boolean);
        let text = rawText;
        for (const removal of removals) {
            const escaped = removal.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            text = text.replace(new RegExp(escaped, 'ig'), ' ');
        }
        text = stripMenuHints(text).replace(/\s*\*\s*/g, ' ').trim();
        return text.length && text.length <= 180 ? text : '';
    })();

    const fieldLabel = (() => {
        if (el.labels && el.labels.length) {
            const text = textOf(el.labels[0]);
            if (text) return text;
        }
        if (el.id) {
            for (const queryRoot of roots) {
                try {
                    const explicit = queryRoot.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                    const text = textOf(explicit);
                    if (text) return text;
                } catch (_) {}
            }
        }
        const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/);
        const labelled = getByIds(labelledBy);
        if (labelled) return labelled;
        const ariaLabel = stripMenuHints(el.getAttribute('aria-label') || '');
        if (ariaLabel) return ariaLabel;
        const wrapped = el.closest('label');
        if (wrapped) {
            const text = textOf(wrapped);
            if (text) return text;
        }
        if (hostLabel) return hostLabel;
        if (containerLabel) return containerLabel;
        let current = el;
        let parent = el.parentElement;
        for (let i = 0; i < 7 && parent; i++) {
            const candidates = Array.from(parent.querySelectorAll(labelSelectors))
                .filter((candidate) => candidate !== el && !candidate.contains(el) && isVisible(candidate))
                .map((candidate) => candidateText(candidate))
                .filter((text) => text && text.length <= 160)
                .sort((a, b) => a.length - b.length);
            if (candidates.length) return candidates[0];

            const currentPrev = previousSiblingText(current, 160);
            if (currentPrev) return currentPrev;
            const parentPrev = previousSiblingText(parent, 160);
            if (parentPrev) return parentPrev;

            const currentOwn = ownText(current, 160);
            if (currentOwn) return currentOwn;
            const parentOwn = ownText(parent, 160);
            if (parentOwn) return parentOwn;

            current = parent;
            parent = parent.parentElement;
        }
        return hostLabel || containerLabel || stripMenuHints(el.placeholder || el.name || el.id || '');
    })();

    const fieldVisible = !!(
        isVisible(el) ||
        isVisible(fieldContainer) ||
        hostChain.some((host) => isVisible(host)) ||
        isVisible(el.closest('label')) ||
        isVisible(el.parentElement)
    );

    const groupInfo = (() => {
        let parent = el.parentElement;
        for (let i = 0; i < 8 && parent; i++) {
            const sameNameCount = el.name
                ? parent.querySelectorAll('input[name="' + CSS.escape(el.name) + '"]').length
                : 0;
            const sameTypeCount = parent.querySelectorAll('input[type="' + CSS.escape(el.type || '') + '"]').length;
            const role = (parent.getAttribute('role') || '').toLowerCase();
            if (
                parent.tagName === 'FIELDSET' ||
                role === 'radiogroup' ||
                role === 'group' ||
                sameNameCount > 1 ||
                sameTypeCount > 1
            ) {
                const labelledBy = (parent.getAttribute('aria-labelledby') || '').split(/\s+/);
                let groupLabel =
                    bestTextFromNode(parent.querySelector('legend, [role="heading"], [class*="question"], [class*="label"], [class*="title"]'), 160) ||
                    previousSiblingText(parent, 160) ||
                    ownText(parent, 160) ||
                    getByIds(labelledBy) ||
                    stripMenuHints(parent.getAttribute('aria-label') || '') ||
                    '';
                if (groupLabel === optionLabel) {
                    groupLabel = fieldLabel || groupLabel;
                }
                return {
                    id: el.name || parent.id || groupLabel || fieldLabel || optionLabel || el.id || el.type || '',
                    label: groupLabel || fieldLabel || optionLabel || el.name || el.id || '',
                };
            }
            parent = parent.parentElement;
        }
        return {
            id: el.name || el.id || fieldLabel || optionLabel || '',
            label: fieldLabel || optionLabel || el.name || el.id || '',
        };
    })();

    return {
        id: el.id || '',
        name: el.name || '',
        label: fieldLabel,
        optionLabel,
        groupId: groupInfo.id || '',
        groupLabel: groupInfo.label || '',
        type: el.type || el.tagName.toLowerCase(),
        required: !!el.required || el.getAttribute('aria-required') === 'true',
        value: el.value || '',
        placeholder: el.placeholder || '',
        checked: !!el.checked,
        contextLabel: hostLabel || containerLabel,
        selector: el.id
            ? '#' + CSS.escape(el.id)
            : (el.name ? el.tagName.toLowerCase() + '[name="' + el.name + '"]' : ''),
        fieldVisible,
    };
}
