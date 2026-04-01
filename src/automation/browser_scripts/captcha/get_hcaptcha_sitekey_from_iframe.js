() => {
    const iframe = document.querySelector('iframe[src*="hcaptcha"]');
    if (!iframe) return null;
    const match = iframe.src.match(/sitekey=([^&]+)/);
    return match ? match[1] : null;
}
