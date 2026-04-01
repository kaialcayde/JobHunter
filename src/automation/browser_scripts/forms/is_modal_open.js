() => {
    const modal = document.querySelector(
        '.jobs-easy-apply-modal, .jobs-easy-apply-content, ' +
        '[role="dialog"], .artdeco-modal'
    );
    return !!(modal && modal.offsetWidth > 0);
}
