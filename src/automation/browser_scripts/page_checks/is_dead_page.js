() => {
    const body = document.body;
    if (!body) return true;

    const main = document.querySelector(
        'main, .scaffold-layout__main, .jobs-search__job-details, ' +
        '.jobs-unified-top-card, .job-view-layout'
    );
    if (main && main.innerText.trim().length > 50) return false;

    const cleaned = (body.innerText || '').replace(/\s+/g, ' ').trim();
    return cleaned.length < 300;
}
