() => {
    let sitekey = null;
    let enterprise = false;

    const el = document.querySelector('.g-recaptcha');
    if (el) sitekey = el.getAttribute('data-sitekey');

    if (!sitekey) {
        const iframe = document.querySelector('iframe[src*="recaptcha"]');
        if (iframe) {
            const match = iframe.src.match(/[?&]k=([^&]+)/);
            if (match) sitekey = match[1];
            if (iframe.src.includes('/enterprise')) enterprise = true;
        }
    }

    if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) enterprise = true;
    const scripts = document.querySelectorAll('script[src*="recaptcha"]');
    for (const script of scripts) {
        if (script.src.includes('enterprise')) {
            enterprise = true;
            break;
        }
    }

    return sitekey ? { sitekey, enterprise } : null;
}
