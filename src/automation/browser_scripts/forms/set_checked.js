(el, checked) => {
    const desired = !!checked;
    const dispatch = () => {
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('click', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));
    };
    const explicitLabel = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    const targets = [
        explicitLabel,
        el.closest('label'),
        el.closest('[role="radio"], [role="checkbox"], .slds-radio, .slds-checkbox, [class*="radio"], [class*="checkbox"]'),
        el,
    ].filter(Boolean);

    for (const target of targets) {
        try {
            target.click();
            if (!!el.checked === desired) {
                return true;
            }
        } catch (_) {}
    }

    el.checked = desired;
    dispatch();
    return !!el.checked === desired;
}
