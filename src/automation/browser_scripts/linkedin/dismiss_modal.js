(args) => {
    const dialogs = document.querySelectorAll(args.modalSelector);
    for (const dialog of dialogs) {
        if (dialog.offsetWidth === 0 || dialog.offsetHeight === 0) continue;
        const text = (dialog.textContent || '').toLowerCase();
        if (text.includes('easy apply')) continue;

        const clickables = dialog.querySelectorAll('button, a, [role="button"]');
        for (const el of clickables) {
            if (el.offsetWidth === 0 || el.offsetHeight === 0) continue;
            const label = (el.getAttribute('aria-label') || '').toLowerCase();
            const buttonText = (el.textContent || '').trim().toLowerCase();
            if (
                label.includes('dismiss') || label.includes('close') ||
                buttonText.includes('no thanks') || buttonText.includes('not now') ||
                buttonText.includes('dismiss') || buttonText.includes('skip') ||
                buttonText === 'x' || buttonText === ''
            ) {
                if (buttonText === '' && el.offsetWidth > 60) continue;
                el.click();
                return true;
            }
        }
    }

    const overlay = document.querySelector(args.overlaySelector);
    if (overlay) {
        overlay.click();
        return true;
    }
    return false;
}
