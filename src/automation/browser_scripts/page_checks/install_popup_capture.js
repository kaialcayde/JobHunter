() => {
    window.__captured_popup_url = null;
    const origOpen = window.open;
    window.open = function(url) {
        window.__captured_popup_url = url;
        return origOpen.apply(this, arguments);
    };
    return true;
}
