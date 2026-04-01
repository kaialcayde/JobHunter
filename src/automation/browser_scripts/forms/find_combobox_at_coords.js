({x, y}) => {
    let el = document.elementFromPoint(x, y);
    if (!el) return null;
    let container = el.closest('.select, .select__container, .select__control, [class*="select"]');
    if (!container) container = el.closest('div');
    if (!container) return null;
    const input = container.querySelector('input[role="combobox"], input.select__input');
    if (!input) return null;
    return {
        tagName: 'INPUT',
        type: 'text',
        selector: input.id ? '#' + CSS.escape(input.id) : null,
        value: input.value || '',
        id: input.id || '',
        name: input.name || '',
        isCombobox: true
    };
}
