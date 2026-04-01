() => {
    const buttons = document.querySelectorAll('button, a');
    for (const button of buttons) {
        if (button.offsetWidth === 0 || button.offsetHeight === 0) continue;
        const text = (button.textContent || '').trim().toLowerCase();
        if (text === 'continue' || text.includes('continue')) {
            let el = button.parentElement;
            for (let i = 0; i < 10 && el; i++) {
                const parentText = (el.textContent || '').toLowerCase();
                if (parentText.includes('share') || parentText.includes('profile')) {
                    return true;
                }
                el = el.parentElement;
            }
        }
    }
    return false;
}
