(token) => {
    document.querySelectorAll('[id*="g-recaptcha-response"]').forEach((ta) => {
        ta.style.display = '';
        ta.value = token;
    });

    try {
        const widgets = document.querySelectorAll('.g-recaptcha, [data-callback]');
        for (const el of widgets) {
            const cbName = el.getAttribute('data-callback');
            if (cbName && typeof window[cbName] === 'function') {
                window[cbName](token);
                return true;
            }
        }

        if (typeof grecaptcha !== 'undefined') {
            if (grecaptcha.enterprise && grecaptcha.enterprise.execute) {
                try { grecaptcha.enterprise.execute(); } catch (error) {}
            }
            if (grecaptcha.render && grecaptcha.getResponse) {
                for (let i = 0; i < 5; i++) {
                    try {
                        const resp = document.querySelector('#g-recaptcha-response-' + i);
                        if (resp) resp.value = token;
                    } catch (error) {}
                }
            }
        }

        const cbNames = [
            'onCaptchaSuccess', 'captchaCallback', 'recaptchaCallback',
            'onRecaptchaSuccess', 'handleCaptcha', 'verifyCaptcha'
        ];
        for (const name of cbNames) {
            if (typeof window[name] === 'function') {
                window[name](token);
                return true;
            }
        }

        window.dispatchEvent(new CustomEvent('captcha-solved', { detail: token }));
    } catch (error) {
    }

    return true;
}
