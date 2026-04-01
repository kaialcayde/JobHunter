(modalSelector) => {
    const dialogs = document.querySelectorAll(modalSelector);
    for (const dialog of dialogs) {
        if (dialog.offsetWidth === 0 || dialog.offsetHeight === 0) continue;
        const text = (dialog.textContent || '').toLowerCase();
        if (text.includes('easy apply')) continue;
        return true;
    }
    return false;
}
