() => {
    return !!(
        document.querySelector('#filter-btn-handler') ||
        document.querySelector('button[aria-label*="Filter" i]') ||
        document.querySelector('input[placeholder*="search" i]')
    );
}
