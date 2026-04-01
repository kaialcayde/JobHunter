(selector) => {
    const input = document.querySelector(selector);
    if (!input) return false;
    const container = input.closest('.select, .select__container');
    if (!container) return false;
    const btn = container.querySelector(
        '[aria-label="Toggle flyout"], .select__dropdown-indicator, .select__indicators button'
    );
    if (!btn) return false;
    btn.click();
    return true;
}
