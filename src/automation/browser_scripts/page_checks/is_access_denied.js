({deniedPhrases}) => {
    const text = (document.body?.innerText || '').slice(0, 3000).toLowerCase();
    const title = (document.title || '').toLowerCase();
    for (const phrase of deniedPhrases || []) {
        if (text.includes(phrase) || title.includes(phrase)) return true;
    }
    return false;
}
