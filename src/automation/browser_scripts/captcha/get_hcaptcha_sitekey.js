() => {
    const el = document.querySelector('[data-hcaptcha-sitekey]') || document.querySelector('.h-captcha');
    return el ? (el.getAttribute('data-hcaptcha-sitekey') || el.getAttribute('data-sitekey')) : null;
}
