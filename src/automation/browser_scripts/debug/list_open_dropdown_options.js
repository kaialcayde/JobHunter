() => {
    const options = document.querySelectorAll(
        '[role="option"], [class*="option"], li[class*="item"], ' +
        '.Select-menu-outer li, [class*="menu"] li, ' +
        '[class*="listbox"] li'
    );
    return Array.from(options).slice(0, 15).map((option) => ({
        tag: option.tagName,
        className: String(option.className || '').substring(0, 80),
        role: option.getAttribute('role'),
        text: (option.innerText || '').trim().substring(0, 60),
        outerHTML: option.outerHTML.substring(0, 200),
    }));
}
