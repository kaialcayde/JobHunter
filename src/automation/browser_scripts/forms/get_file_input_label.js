(el) => {
    if (el.id) {
        const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (label) return label.textContent.trim().toLowerCase();
    }
    const parent = el.closest('label, .field, .form-group, [class*="upload"], [class*="attachment"]');
    if (parent) {
        const heading = parent.querySelector('h3, h4, label, .field-label, [class*="label"]');
        if (heading) return heading.textContent.trim().toLowerCase();
        return parent.textContent.trim().toLowerCase().slice(0, 200);
    }
    return '';
}
