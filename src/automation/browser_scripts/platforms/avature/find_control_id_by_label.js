(args) => {
    const [labelText, tagName, fieldClass, inputTypes, datasetId, rowId] = args;
    const normalize = (value) => (value || '')
        .toLowerCase()
        .replace(/\*/g, ' ')
        .replace(/select an option/gi, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    const wanted = normalize(labelText);
    const wantedTokens = wanted.split(' ').filter((token) => token.length >= 3);
    const wantedTypes = new Set((inputTypes || []).map((kind) => String(kind).toLowerCase()));
    const wantedTag = String(tagName || '').toUpperCase();
    const seen = new Set();
    const matches = [];

    const isVisible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        if (!style || style.display === 'none' || style.visibility === 'hidden') {
            return false;
        }
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    const datasetMatches = (el) => {
        if (!datasetId) return true;
        const ident = el.id || el.name || '';
        if (!ident.startsWith(`${datasetId}-`)) return false;
        if (!rowId) return true;
        return ident.endsWith(`-${rowId}`);
    };

    const controlMatches = (el) => {
        if (!el || !el.id || el.tagName !== wantedTag) return false;
        if (fieldClass && !(el.className || '').includes(fieldClass)) return false;
        if (wantedTypes.size) {
            const kind = String(el.type || '').toLowerCase();
            if (!wantedTypes.has(kind)) return false;
        }
        return datasetMatches(el);
    };

    const push = (el, label, container, viaFor) => {
        if (!controlMatches(el)) return;
        if (seen.has(el.id)) return;
        seen.add(el.id);
        matches.push({
            id: el.id,
            label: label ? (label.innerText || '') : '',
            exactLabel: normalize(label ? label.innerText : '') === wanted,
            viaFor: !!viaFor,
            containerVisible: isVisible(container) || isVisible(label),
            sampleRow: /-sample$/i.test(el.id) || /-sample$/i.test(el.name || ''),
            hiddenType: String(el.type || '').toLowerCase() === 'hidden',
            classMatch: !fieldClass || (el.className || '').includes(fieldClass),
        });
    };

    for (const label of document.querySelectorAll('label')) {
        const labelNorm = normalize(label.innerText || '');
        const tokenMatch = wantedTokens.length >= 2 &&
            wantedTokens.every((token) => labelNorm.includes(token));
        if (!labelNorm || (!labelNorm.includes(wanted) && !wanted.includes(labelNorm) && !tokenMatch)) {
            continue;
        }
        const container = label.closest(
            '.datasetfieldSpec, .fieldSpec, [class*="field"], [class*="group"]'
        ) || label.parentElement;

        if (label.htmlFor) {
            push(document.getElementById(label.htmlFor), label, container, true);
        }

        const scope = container || label.parentElement || document;
        scope.querySelectorAll(wantedTag.toLowerCase()).forEach((el) => {
            push(el, label, container, false);
        });
    }

    return matches;
}
