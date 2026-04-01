({x, y, text}) => {
    const selects = Array.from(document.querySelectorAll('select'));
    const textLower = text.toLowerCase();
    let best = null;
    let bestDist = Infinity;

    for (const sel of selects) {
        let posEl = sel;
        while (posEl && posEl.getBoundingClientRect().width === 0) {
            posEl = posEl.parentElement;
        }
        if (!posEl) continue;
        const rect = posEl.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const dist = Math.hypot(cx - x, cy - y);
        if (dist < 150 && dist < bestDist) {
            const match = Array.from(sel.options).find((option) =>
                option.text.trim().toLowerCase().includes(textLower) ||
                textLower.includes(option.text.trim().toLowerCase())
            );
            if (match) {
                best = { sel, value: match.value };
                bestDist = dist;
            }
        }
    }

    if (!best) return null;
    const proto = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
    if (proto && proto.set) proto.set.call(best.sel, best.value);
    else best.sel.value = best.value;
    best.sel.dispatchEvent(new Event('change', { bubbles: true }));
    best.sel.dispatchEvent(new Event('input', { bubbles: true }));
    return best.value;
}
