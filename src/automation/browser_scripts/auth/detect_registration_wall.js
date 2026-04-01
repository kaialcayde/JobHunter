() => {
    const text = (document.body?.innerText || '').toLowerCase();
    const pwFields = document.querySelectorAll('input[type="password"]');
    const hasConfirmPw = pwFields.length >= 2;

    const registerSignals = [
        'create account', 'create your account', 'sign up',
        'register', 'new user', 'join now', 'get started'
    ];
    const hasRegisterText = registerSignals.some((signal) => text.includes(signal));
    return hasConfirmPw || hasRegisterText;
}
