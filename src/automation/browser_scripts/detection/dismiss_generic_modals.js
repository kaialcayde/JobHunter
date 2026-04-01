({selectors, textMatches}) => {
    for (const sel of selectors || []) {
        try {
            const btn = document.querySelector(sel);
            if (btn && btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                btn.click();
                return true;
            }
        } catch (error) {
        }
    }

    const buttons = document.querySelectorAll('button');
    for (const btn of buttons) {
        if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
        const text = (btn.textContent || '').trim().toLowerCase();
        if ((textMatches || []).some((match) => text === match || text.startsWith(match))) {
            btn.click();
            return true;
        }
    }
    return false;
}
