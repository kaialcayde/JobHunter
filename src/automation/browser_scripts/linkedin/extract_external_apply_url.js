() => {
    const el = document.querySelector(
        'a[href*="externalApply"], [data-job-apply-url], [data-apply-url]'
    );
    if (el) {
        return el.href ||
            el.getAttribute('data-job-apply-url') ||
            el.getAttribute('data-apply-url');
    }

    const links = document.querySelectorAll('a[href]');
    for (const link of links) {
        const href = link.href || '';
        if (href.includes('externalApply') || href.includes('applyUrl')) return href;
    }
    return null;
}
