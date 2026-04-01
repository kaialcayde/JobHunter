({sitePatterns, genericPatterns, loginPhrases}) => {
    const url = window.location.href.toLowerCase();
    if ((sitePatterns || []).some((pattern) => url.includes(pattern))) return true;

    if ((genericPatterns || []).some((pattern) => url.includes(pattern))) {
        if (document.querySelector('input[type="password"]')) return true;
    }

    const body = (document.body?.textContent || '').toLowerCase().slice(0, 2000);
    if ((loginPhrases || []).some((phrase) => body.includes(phrase))) {
        if (document.querySelector('input[type="password"]')) return true;
    }
    return false;
}
