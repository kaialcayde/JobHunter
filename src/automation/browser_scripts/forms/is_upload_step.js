() => {
    const text = (document.body?.innerText || '').toLowerCase();
    return (
        (text.includes('upload your resume') ||
         text.includes('please upload') ||
         text.includes('select your resume')) &&
        !!document.querySelector('input[type="file"]')
    );
}
