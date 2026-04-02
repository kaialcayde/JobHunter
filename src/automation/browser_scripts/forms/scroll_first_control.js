() => {
    const input = document.querySelector(
        'input[type="text"], input[type="email"], input[type="tel"], textarea, input[type="file"]'
    );
    if (input) {
        input.scrollIntoView({ block: 'center', behavior: 'instant' });
        return true;
    }
    return false;
}
