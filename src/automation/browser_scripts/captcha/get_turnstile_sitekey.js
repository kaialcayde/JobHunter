() => {
    const el = document.querySelector('[class*="cf-turnstile"]');
    if (el) {
        const key = el.getAttribute('data-sitekey');
        if (key) return key;
    }

    const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
    if (iframe) {
        const match = iframe.src.match(/sitekey=([^&]+)/);
        if (match) return match[1];
    }

    const scripts = document.querySelectorAll('script');
    for (const script of scripts) {
        const text = script.textContent || '';
        const match = text.match(/turnstile[.]render\s*\([^)]*sitekey\s*:\s*['"]([^'"]+)['"]/);
        if (match) return match[1];
        const matchAttr = text.match(/data-sitekey\s*=\s*['"]([^'"]+)['"]/);
        if (matchAttr) return matchAttr[1];
    }

    const anyKey = document.querySelector('[data-sitekey]');
    if (anyKey) return anyKey.getAttribute('data-sitekey');
    return null;
}
