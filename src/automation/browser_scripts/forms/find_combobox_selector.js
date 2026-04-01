({x, y}) => {
    let el = document.elementFromPoint(x, y);
    if (!el) return null;
    const container = el.closest('.select, .select__container, .select__control, [class*="select"]') ||
        el.closest('div.field, div.form-group, div');
    if (!container) return null;
    const input = container.querySelector('input[role="combobox"], input.select__input');
    if (input && input.id) return '#' + CSS.escape(input.id);
    if (input && input.name) return 'input[name="' + input.name + '"]';
    return null;
}
