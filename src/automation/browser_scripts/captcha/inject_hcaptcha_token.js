(token) => {
    const textareas = document.querySelectorAll(
        '[name="h-captcha-response"], [name="g-recaptcha-response"]'
    );
    textareas.forEach((ta) => {
        ta.value = token;
    });

    if (typeof hcaptcha !== 'undefined') {
        try { hcaptcha.execute(); } catch (error) {}
    }
    return true;
}
