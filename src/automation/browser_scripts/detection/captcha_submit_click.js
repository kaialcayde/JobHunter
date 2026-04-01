() => {
    const btns = document.querySelectorAll(
        'button, input[type="submit"], [role="button"], a.btn, a.button'
    );
    for (const btn of btns) {
        if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
        const text = (btn.textContent || btn.value || '').toLowerCase().trim();
        if (text.match(/^(submit|verify|continue|proceed|check|apply)/)) {
            btn.click();
            return 'clicked: ' + text.substring(0, 30);
        }
    }
    return null;
}
