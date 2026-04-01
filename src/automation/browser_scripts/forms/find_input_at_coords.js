({x, y}) => {
    let el = document.elementFromPoint(x, y);
    if (!el) return null;

    function resolveLabelTarget(node) {
        if (!node) return null;
        const label = node.tagName === 'LABEL' ? node : node.closest('label');
        if (!label) return null;
        const forId = label.getAttribute('for');
        if (forId) {
            const target = document.getElementById(forId);
            if (target) return target;
        }
        return label.querySelector('input, select, textarea, [contenteditable="true"]');
    }

    const formTags = ['INPUT', 'SELECT', 'TEXTAREA'];
    let candidate = el;
    for (let i = 0; i < 5; i++) {
        if (!candidate) break;
        if (formTags.includes(candidate.tagName)) break;
        if (candidate.getAttribute('contenteditable') === 'true') break;
        const labelTarget = resolveLabelTarget(candidate);
        if (labelTarget) {
            candidate = labelTarget;
            break;
        }
        const childField = candidate.querySelector &&
            candidate.querySelector('input, select, textarea, [contenteditable="true"]');
        if (childField) {
            candidate = childField;
            break;
        }
        const next = candidate.nextElementSibling;
        if (next) {
            if (formTags.includes(next.tagName)) {
                candidate = next;
                break;
            }
            const nextField = resolveLabelTarget(next) ||
                next.querySelector?.('input, select, textarea, [contenteditable="true"]');
            if (nextField) {
                candidate = nextField;
                break;
            }
        }
        const prev = candidate.previousElementSibling;
        if (prev) {
            if (formTags.includes(prev.tagName)) {
                candidate = prev;
                break;
            }
            const prevField = resolveLabelTarget(prev) ||
                prev.querySelector?.('input, select, textarea, [contenteditable="true"]');
            if (prevField) {
                candidate = prevField;
                break;
            }
        }
        candidate = candidate.parentElement;
    }

    if (!candidate) return null;

    if (!formTags.includes(candidate.tagName) && candidate.getAttribute('contenteditable') !== 'true') {
        const container = el.closest('div, fieldset, li, section, form') || el.parentElement;
        if (container) {
            const nearby = resolveLabelTarget(container) || container.querySelector(
                'input:not([type="hidden"]):not([type="submit"]):not([type="button"]), ' +
                'select, textarea, [contenteditable="true"]'
            );
            if (nearby) candidate = nearby;
            else return null;
        } else {
            return null;
        }
    }

    let selector = '';
    if (candidate.id) selector = '#' + CSS.escape(candidate.id);
    else if (candidate.name) selector = candidate.tagName.toLowerCase() + '[name="' + candidate.name + '"]';
    else if (candidate.getAttribute('aria-label')) selector = candidate.tagName.toLowerCase() + '[aria-label="' + candidate.getAttribute('aria-label') + '"]';
    else if (candidate.placeholder) selector = candidate.tagName.toLowerCase() + '[placeholder="' + candidate.placeholder + '"]';
    else if (candidate.getAttribute('contenteditable') === 'true') selector = '[contenteditable="true"]';
    else selector = null;

    return {
        tagName: candidate.tagName,
        type: candidate.type || '',
        selector: selector,
        value: candidate.value || '',
        id: candidate.id || '',
        name: candidate.name || ''
    };
}
