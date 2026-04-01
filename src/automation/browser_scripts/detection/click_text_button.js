({textMatches, buttonSelector}) => {
    const buttons = document.querySelectorAll(
        buttonSelector || 'button, input[type="submit"], a, [role="button"]'
    );
    for (const match of textMatches || []) {
        for (const btn of buttons) {
            if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
            const text = (btn.textContent || btn.value || '').trim().toLowerCase();
            if (text === match || text.startsWith(match)) {
                btn.click();
                return true;
            }
        }
    }
    return false;
}
