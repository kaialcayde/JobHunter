() => {
    const dialogs = document.querySelectorAll('[role="dialog"], .artdeco-modal');
    const visibleDialogs = [];
    for (const dialog of dialogs) {
        if (dialog.offsetWidth > 0) {
            visibleDialogs.push(dialog.textContent.trim().slice(0, 100));
        }
    }

    const buttons = document.querySelectorAll(
        'button, a[class*="apply"], a[class*="jobs-"], a[aria-label]'
    );
    const visibleButtons = [];
    for (const button of buttons) {
        if (button.offsetWidth > 0) {
            const tag = button.tagName.toLowerCase();
            const text = button.textContent.trim().slice(0, 40);
            if (text) visibleButtons.push(tag === 'a' ? '<a>' + text : text);
        }
    }

    return {
        url: window.location.href,
        dialogCount: visibleDialogs.length,
        dialogs: visibleDialogs.slice(0, 3),
        buttonTexts: visibleButtons.slice(0, 10),
        bodyLen: (document.body?.innerText || '').length
    };
}
