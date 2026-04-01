() => {
    const body = (document.body?.textContent || '').toLowerCase().slice(0, 2000);
    return body.includes('checking your browser') ||
           body.includes('verify you are human') ||
           !!document.querySelector('#challenge-running, #challenge-form, #cf-challenge-running');
}
