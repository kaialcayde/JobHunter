(modalSelector) => {
    const dialogs = document.querySelectorAll(modalSelector);
    for (const dialog of dialogs) {
        if (dialog.offsetWidth === 0 || dialog.offsetHeight === 0) continue;
        const text = (dialog.textContent || '').toLowerCase();
        if (text.includes('share your profile') || text.includes('share profile')) {
            return true;
        }
    }
    return false;
}
