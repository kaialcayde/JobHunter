(token) => {
    const input = document.querySelector('[name="cf-turnstile-response"]');
    if (input) input.value = token;
    if (typeof turnstile !== 'undefined') {
        try { turnstile.execute(); } catch (error) {}
    }
    return true;
}
