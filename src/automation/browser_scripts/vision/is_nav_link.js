({x, y}) => {
    const el = document.elementFromPoint(x, y);
    if (!el) return false;
    const a = el.tagName === 'A' ? el : el.closest('a');
    if (!a) return false;
    if (
        a.getAttribute('role') === 'button' ||
        a.getAttribute('aria-haspopup') ||
        a.getAttribute('aria-expanded') !== null
    ) {
        return false;
    }
    return true;
}
