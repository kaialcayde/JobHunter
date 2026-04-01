(selector) => {
    const sel = document.querySelector(selector);
    if (!sel) return [];
    return Array.from(sel.options).map((o, i) => ({
        index: i,
        text: o.text.trim(),
        value: o.value
    }));
}
