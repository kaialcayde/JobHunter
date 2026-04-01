({shadowHostSelectors, easyApplySelectors, modalSelector}) => {
    function getAllRoots() {
        const roots = [document];
        const shadowHosts = document.querySelectorAll(shadowHostSelectors);
        for (const host of shadowHosts) {
            if (host.shadowRoot) roots.push(host.shadowRoot);
        }
        const allElements = document.querySelectorAll('*');
        for (const el of allElements) {
            if (el.shadowRoot && !roots.includes(el.shadowRoot)) {
                roots.push(el.shadowRoot);
            }
        }
        return roots;
    }

    const roots = getAllRoots();

    for (const root of roots) {
        for (const selector of easyApplySelectors || []) {
            try {
                const el = root.querySelector(selector);
                if (el && el.offsetWidth > 0 && el.offsetHeight > 0) return true;
            } catch (error) {
            }
        }
    }

    for (const root of roots) {
        try {
            const dialogs = root.querySelectorAll(modalSelector);
            for (const dialog of dialogs) {
                if (dialog.offsetWidth === 0 || dialog.offsetHeight === 0) continue;
                const text = (dialog.textContent || '').toLowerCase();
                if (text.includes('apply to') || text.includes('submit application')) {
                    const inputs = dialog.querySelectorAll(
                        'input:not([type="hidden"]), textarea, select, input[type="file"], ' +
                        'button[aria-label*="upload"], button[aria-label*="Upload"]'
                    );
                    for (const inp of inputs) {
                        if (inp.offsetWidth > 0 && inp.offsetHeight > 0) return true;
                    }
                }
            }
        } catch (error) {
        }
    }

    const url = window.location.href.toLowerCase();
    if (url.includes('/apply') && url.includes('linkedin.com')) {
        const formFields = document.querySelectorAll(
            'input:not([type="hidden"]):not([type="submit"]), textarea, select, ' +
            '[role="dialog"], [role="form"], form'
        );
        for (const el of formFields) {
            if (el.offsetWidth > 0 && el.offsetHeight > 0) return true;
        }
    }

    return false;
}
