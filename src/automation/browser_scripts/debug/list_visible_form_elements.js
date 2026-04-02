() => {
    const fields = [];
    document.querySelectorAll('input, select, textarea, [contenteditable="true"]').forEach((el) => {
        if (!el.offsetParent && el.type !== 'hidden') return;
        const container = el.closest(
            '[class*="field"], [class*="group"], [class*="row"], ' +
            '[class*="form"], [class*="question"]'
        );
        const label = container
            ? container.querySelector('label, [class*="label"]')
            : null;
        fields.push({
            label: label ? label.innerText.trim().substring(0, 60) : '(no label)',
            tag: el.tagName,
            type: el.type || '',
            name: el.name || '',
            id: el.id || '',
            className: String(el.className || '').substring(0, 80),
            value: String(el.value || '').substring(0, 40),
            placeholder: el.placeholder || '',
        });
    });
    return fields;
}
