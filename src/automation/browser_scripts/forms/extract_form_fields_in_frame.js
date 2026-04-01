() => {
    const fields = [];

    document.querySelectorAll(
        'input:not([type="hidden"]):not([type="submit"]):not([type="button"])'
    ).forEach((el) => {
        if (el.type === 'radio' || el.type === 'checkbox' || el.type === 'file') return;
        fields.push({
            id: el.id || el.name || el.getAttribute('aria-label') || 'input',
            selector: el.id ? '#' + CSS.escape(el.id) :
                el.name ? el.tagName.toLowerCase() + '[name="' + el.name + '"]' :
                el.tagName.toLowerCase(),
            label: el.getAttribute('aria-label') || el.placeholder || el.name || el.id || '',
            type: el.type || 'text',
            required: el.required,
            value: el.value || '',
            visible: true
        });
    });

    document.querySelectorAll('textarea, select').forEach((el) => {
        fields.push({
            id: el.id || el.name || el.tagName.toLowerCase(),
            selector: el.id ? '#' + CSS.escape(el.id) :
                el.name ? el.tagName.toLowerCase() + '[name="' + el.name + '"]' :
                el.tagName.toLowerCase(),
            label: el.getAttribute('aria-label') || el.name || el.id || '',
            type: el.tagName === 'SELECT' ? 'select' : 'textarea',
            required: el.required,
            visible: true,
            options: el.tagName === 'SELECT'
                ? Array.from(el.options).map((o) => o.text.trim()).filter((text) => text)
                : undefined
        });
    });

    return fields;
}
