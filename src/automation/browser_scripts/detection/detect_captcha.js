({challengeSelectors, passiveSelectors, knownPassiveDomains, bodyPhrases}) => {
    const hasFormContent = (() => {
        const inputs = document.querySelectorAll(
            'input:not([type="hidden"]):not([type="submit"]), textarea, select'
        );
        let visibleCount = 0;
        for (const inp of inputs) {
            if (inp.offsetWidth > 0 && inp.offsetHeight > 0) visibleCount++;
            if (visibleCount >= 2) return true;
        }
        return false;
    })();

    for (const sel of challengeSelectors || []) {
        const el = document.querySelector(sel);
        if (!el) continue;
        const isVisible = el.offsetWidth > 0 && el.offsetHeight > 0;
        if (isVisible) return 'challenge-visible:' + sel;
        if (!hasFormContent) return 'challenge-no-form:' + sel;
    }

    const gRecaptcha = document.querySelector('.g-recaptcha');
    if (gRecaptcha) {
        const dataSize = gRecaptcha.getAttribute('data-size');
        const isVisible = gRecaptcha.offsetWidth > 10 && gRecaptcha.offsetHeight > 10;
        if (dataSize === 'invisible' && hasFormContent) {
        } else if (!hasFormContent) {
            return 'g-recaptcha-no-form';
        } else if (isVisible) {
            return 'g-recaptcha-visible(w=' + gRecaptcha.offsetWidth + ',h=' + gRecaptcha.offsetHeight + ')';
        }
    }

    const isKnownPassive = (knownPassiveDomains || []).some(
        (domain) => window.location.hostname.includes(domain)
    );

    if (!hasFormContent && !isKnownPassive) {
        for (const sel of passiveSelectors || []) {
            if (document.querySelector(sel)) return 'passive:' + sel;
        }
    }

    if (!hasFormContent && !isKnownPassive) {
        const scripts = document.querySelectorAll(
            'script[src*="recaptcha"], script[src*="hcaptcha"], script[src*="challenges.cloudflare.com"]'
        );
        if (scripts.length > 0) return 'scripts-only';
    }

    const body = (document.body?.textContent || '').toLowerCase().slice(0, 2000);
    if ((bodyPhrases || []).some((phrase) => body.includes(phrase))) return 'body-phrase';
    return null;
}
