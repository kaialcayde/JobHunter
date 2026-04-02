() => {
    const results = [];
    const candidates = document.querySelectorAll(
        '[class*="select"], [class*="dropdown"], [class*="combobox"], ' +
        '[role="combobox"], [role="listbox"], [aria-haspopup], ' +
        '[class*="Select"], [class*="Dropdown"], [class*="picker"]'
    );
    candidates.forEach((el) => {
        const container = el.closest(
            '[class*="field"], [class*="group"], [class*="row"], ' +
            '[class*="form"], [class*="question"]'
        );
        const label = container
            ? container.querySelector('label, [class*="label"]')
            : null;
        results.push({
            label: label ? label.innerText.trim() : '(no label)',
            tag: el.tagName,
            className: String(el.className || '').substring(0, 120),
            role: el.getAttribute('role'),
            ariaHaspopup: el.getAttribute('aria-haspopup'),
            id: el.id,
            outerHTML: el.outerHTML.substring(0, 300),
        });
    });

    document.querySelectorAll('select').forEach((el) => {
        const container = el.closest(
            '[class*="field"], [class*="group"], [class*="row"], ' +
            '[class*="form"], [class*="question"]'
        );
        const label = container
            ? container.querySelector('label, [class*="label"]')
            : null;
        results.push({
            label: label ? label.innerText.trim() : '(no label)',
            tag: 'SELECT',
            className: String(el.className || '').substring(0, 120),
            id: el.id,
            name: el.name,
            options: Array.from(el.options).slice(0, 10).map((option) => option.text),
            outerHTML: el.outerHTML.substring(0, 500),
        });
    });

    const fields = [];
    document.querySelectorAll(
        '[class*="field"], [class*="form-group"], [class*="question"]'
    ).forEach((el) => {
        const label = el.querySelector('label, [class*="label"]');
        const inputs = el.querySelectorAll(
            'input, select, textarea, [role="combobox"], [role="listbox"]'
        );
        if (label && inputs.length > 0) {
            fields.push({
                label: label.innerText.trim().substring(0, 60),
                containerClass: String(el.className || '').substring(0, 100),
                containerTag: el.tagName,
                inputTypes: Array.from(inputs).map((input) => ({
                    tag: input.tagName,
                    type: input.type || input.getAttribute('role') || '',
                    className: String(input.className || '').substring(0, 80),
                    name: input.name || input.id || '',
                })),
            });
        }
    });

    return {
        candidates: results,
        fields,
        url: window.location.href,
        title: document.title,
    };
}
