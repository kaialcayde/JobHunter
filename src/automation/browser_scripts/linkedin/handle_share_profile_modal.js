(modalSelector) => {
    const dialogs = document.querySelectorAll(modalSelector);
    for (const dialog of dialogs) {
        if (dialog.offsetWidth === 0 || dialog.offsetHeight === 0) continue;
        const text = (dialog.textContent || '').toLowerCase();
        if (text.includes('share your profile') || text.includes('share profile')) {
            const buttons = dialog.querySelectorAll('button, a, [role="button"]');
            for (const btn of buttons) {
                if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
                const buttonText = (btn.textContent || '').trim().toLowerCase();
                if (buttonText.includes('continue')) {
                    btn.click();
                    return 'continue';
                }
            }
            for (const btn of buttons) {
                if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
                const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                if (label.includes('dismiss') || label.includes('close')) {
                    btn.click();
                    return 'dismissed';
                }
            }
        }
    }
    return null;
}
