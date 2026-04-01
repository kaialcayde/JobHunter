() => {
    const text = (document.body?.innerText || '').toLowerCase();
    const hasFileUpload = !!document.querySelector('input[type="file"]');
    const appSignals = [
        'upload your resume', 'upload resume', 'attach resume',
        'personal information', 'select your resume',
        'finalize application', 'work experience'
    ];
    const hasAppText = appSignals.some((signal) => text.includes(signal));
    const pwFields = document.querySelectorAll('input[type="password"]');
    const isRegistrationForm = pwFields.length >= 2;
    return (hasFileUpload || hasAppText) && !isRegistrationForm;
}
