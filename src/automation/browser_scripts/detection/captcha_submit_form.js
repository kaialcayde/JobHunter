() => {
    const ta = document.querySelector('[id*="g-recaptcha-response"]');
    if (ta) {
        const form = ta.closest('form');
        if (form) {
            form.submit();
            return 'form.submit';
        }
    }
    return null;
}
