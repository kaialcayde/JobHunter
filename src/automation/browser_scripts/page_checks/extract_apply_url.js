({atsDomains}) => {
    const links = document.querySelectorAll('a[href]');
    for (const link of links) {
        const href = link.href;
        const text = (link.textContent || '').toLowerCase().trim();
        if (text.includes('apply') && href.startsWith('http')) {
            for (const domain of atsDomains || []) {
                if (href.includes(domain)) return href;
            }
            if (!href.includes(window.location.hostname)) return href;
        }
    }

    const buttons = document.querySelectorAll(
        'button[onclick], a[onclick], [data-apply-url], [data-href], [data-url]'
    );
    for (const btn of buttons) {
        const text = (btn.textContent || '').toLowerCase();
        if (!text.includes('apply')) continue;
        for (const attr of ['data-apply-url', 'data-href', 'data-url']) {
            const val = btn.getAttribute(attr);
            if (val && val.startsWith('http')) return val;
        }
        const onclick = btn.getAttribute('onclick') || '';
        const match = onclick.match(
            /(?:window\.open|location\.href|location\.assign)\s*\(\s*['"]([^'"]+)['"]/
        );
        if (match) return match[1];
    }

    return null;
}
