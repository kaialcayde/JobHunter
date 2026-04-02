({x, y, text}) => {
    const desired = String(text || '').trim().toLowerCase();
    const cleanText = (value) => String(value || '')
        .replace(/\u00a0/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    const isVisible = (node) => {
        if (!node || !node.ownerDocument) return false;
        const style = window.getComputedStyle(node);
        if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const nodeText = (node) => cleanText(
        node?.getAttribute?.('aria-label') ||
        node?.innerText ||
        node?.textContent ||
        ''
    );
    const semanticWeight = (node) => {
        if (!node || !node.tagName) return 0;
        const role = (node.getAttribute('role') || '').toLowerCase();
        const className = String(node.className || '').toLowerCase();
        if (node.tagName === 'INPUT') return 60;
        if (node.tagName === 'LABEL') return 55;
        if (role === 'radio' || role === 'checkbox') return 50;
        if (node.hasAttribute('aria-checked')) return 45;
        if (className.includes('radio') || className.includes('checkbox')) return 35;
        if (node.tagName === 'BUTTON') return 25;
        return 10;
    };
    const checkedState = (node) => {
        if (!node) return false;
        if (node.matches?.('input[type="checkbox"], input[type="radio"]')) return !!node.checked;
        const ariaChecked = (node.getAttribute?.('aria-checked') || '').toLowerCase();
        if (ariaChecked === 'true') return true;
        const descendantInput = node.querySelector?.('input[type="checkbox"], input[type="radio"]');
        if (descendantInput) return !!descendantInput.checked;
        const descendantRole = node.querySelector?.('[role="radio"], [role="checkbox"], [aria-checked="true"]');
        if (descendantRole) return true;
        return /\b(selected|checked|active)\b/i.test(String(node.className || ''));
    };
    const resolveLabelTarget = (node) => {
        if (!node) return null;
        const label = node.tagName === 'LABEL' ? node : node.closest?.('label');
        if (!label) return null;
        const forId = label.getAttribute('for');
        if (forId) {
            const target = document.getElementById(forId);
            if (target) return target;
        }
        return label.querySelector('input[type="checkbox"], input[type="radio"]');
    };
    const collectCandidate = (node, distance, candidates, seen) => {
        if (!node || seen.has(node)) return;
        const role = (node.getAttribute?.('role') || '').toLowerCase();
        const className = String(node.className || '').toLowerCase();
        const isCheckable = (
            node.matches?.('input[type="checkbox"], input[type="radio"], label, button') ||
            role === 'radio' ||
            role === 'checkbox' ||
            node.hasAttribute?.('aria-checked') ||
            className.includes('radio') ||
            className.includes('checkbox')
        );
        if (!isCheckable || !isVisible(node)) return;
        const textValue = nodeText(node);
        const desiredMatch = desired && textValue ? (
            textValue.toLowerCase().includes(desired) || desired.includes(textValue.toLowerCase())
        ) : false;
        const score = semanticWeight(node) + (desiredMatch ? 80 : 0) - distance;
        candidates.push({ node, score, textValue });
        seen.add(node);

    };

    const collectFromNearbyText = (candidates, seen) => {
        if (!desired) return;
        const textNodes = document.querySelectorAll(
            'label, button, [role="radio"], [role="checkbox"], [aria-checked], span, div'
        );
        for (const node of textNodes) {
            if (!isVisible(node)) continue;
            const textValue = nodeText(node);
            if (!textValue) continue;
            const lower = textValue.toLowerCase();
            if (!lower.includes(desired) && !desired.includes(lower)) continue;
            if (textValue.length > 120) continue;
            const rect = node.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            const distance = Math.hypot(cx - x, cy - y);
            if (distance > 220) continue;
            let current = node;
            for (let depth = 0; depth < 6 && current; depth += 1) {
                collectCandidate(current, distance + depth * 2, candidates, seen);
                current = current.parentElement;
            }
        }
    };

    const offsets = [
        [0, 0], [12, 0], [-12, 0], [0, 12], [0, -12],
        [20, 0], [-20, 0], [0, 20], [0, -20],
        [28, 0], [-28, 0], [0, 28], [0, -28],
    ];
    const candidates = [];
    const seen = new Set();

    for (const [dx, dy] of offsets) {
        const px = x + dx;
        const py = y + dy;
        const distance = Math.hypot(dx, dy);
        let current = document.elementFromPoint(px, py);
        for (let depth = 0; depth < 7 && current; depth += 1) {
            collectCandidate(current, distance + depth * 2, candidates, seen);
            current = current.parentElement;
        }
    }
    collectFromNearbyText(candidates, seen);

    candidates.sort((a, b) => b.score - a.score);
    for (const candidate of candidates) {
        const node = candidate.node;
        try {
            const linkedInput = resolveLabelTarget(node);
            if (checkedState(linkedInput) || checkedState(node)) {
                return {
                    clicked: true,
                    checked: true,
                    tag: node.tagName,
                    text: candidate.textValue,
                };
            }
            node.click();
            let finalChecked = checkedState(linkedInput) || checkedState(node);
            if (!finalChecked && linkedInput && linkedInput !== node) {
                try {
                    linkedInput.click();
                } catch (_) {}
                finalChecked = checkedState(linkedInput) || checkedState(node);
            }
            if (finalChecked || !desired || candidate.textValue.toLowerCase().includes(desired)) {
                return {
                    clicked: true,
                    checked: finalChecked,
                    tag: node.tagName,
                    text: candidate.textValue,
                };
            }
        } catch (_) {}
    }

    return { clicked: false };
}
