() => {
    const ghForm = document.querySelector('#application_form, form[action*="applications"]');
    if (ghForm) {
        const btn = ghForm.querySelector('input[type="submit"], button[type="submit"]');
        if (btn) {
            btn.click();
            return 'greenhouse submit btn';
        }
        ghForm.submit();
        return 'greenhouse form.submit';
    }

    const form = document.querySelector('form');
    if (form) {
        form.submit();
        return 'fallback form.submit';
    }
    return null;
}
