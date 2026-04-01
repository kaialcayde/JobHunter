(selector) => {
    const input = document.querySelector(selector);
    if (!input) return false;
    const container = input.closest('.select__control, .select, .select__container, [class*="select"]');
    if (!container) return false;
    const singleValue = container.querySelector('[class*="single-value"], [class*="singleValue"]');
    if (singleValue && singleValue.textContent.trim()) return true;
    const placeholder = container.querySelector('[class*="placeholder"]');
    return !!(placeholder && placeholder.textContent.trim() !== 'Select...');
}
