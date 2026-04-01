() => {
    const fields = [];
    const seen = new Set();
    let autoIdx = 0;

    const modal = document.querySelector(
        '.jobs-easy-apply-modal, .jobs-easy-apply-content, ' +
        '[role="dialog"], .artdeco-modal'
    );
    const scope = (modal && modal.offsetWidth > 0) ? modal : document;

    function getSelector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
        if (el.getAttribute('aria-label')) {
            return el.tagName.toLowerCase() + '[aria-label="' + el.getAttribute('aria-label') + '"]';
        }
        if (el.placeholder) return el.tagName.toLowerCase() + '[placeholder="' + el.placeholder + '"]';
        let path = el.tagName.toLowerCase();
        if (el.type) path += '[type="' + el.type + '"]';
        return path;
    }

    function getLabel(el) {
        if (el.id) {
            const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (label) return label.textContent.trim();
        }
        const parentLabel = el.closest('label');
        if (parentLabel) return parentLabel.textContent.trim();
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
        if (el.placeholder) return el.placeholder;
        const prev = el.previousElementSibling;
        if (prev && prev.tagName === 'LABEL') return prev.textContent.trim();
        return el.name || el.id || '';
    }

    function isVisible(el) {
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none' &&
            style.visibility !== 'hidden' &&
            rect.width > 0 &&
            rect.height > 0;
    }

    scope.querySelectorAll(
        'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="file"])'
    ).forEach((el) => {
        const type = el.type || 'text';
        if (type === 'radio') return;

        const selector = getSelector(el);
        const uniqueKey = selector + '_' + (el.name || '') + '_' + (el.id || '') + '_' + autoIdx++;
        if (seen.has(uniqueKey)) return;
        seen.add(uniqueKey);

        if (type === 'checkbox') {
            fields.push({
                id: el.id || el.name || 'checkbox_' + autoIdx,
                selector: selector,
                label: getLabel(el),
                type: 'checkbox',
                required: el.required,
                checked: el.checked,
                visible: isVisible(el)
            });
            return;
        }

        fields.push({
            id: el.id || el.name || el.getAttribute('aria-label') || 'input_' + autoIdx,
            selector: selector,
            label: getLabel(el),
            type: type,
            required: el.required,
            value: el.value || '',
            visible: isVisible(el)
        });
    });

    scope.querySelectorAll('textarea').forEach((el) => {
        const selector = getSelector(el);
        fields.push({
            id: el.id || el.name || 'textarea_' + autoIdx++,
            selector: selector,
            label: getLabel(el),
            type: 'textarea',
            required: el.required,
            maxLength: el.maxLength > 0 ? el.maxLength : null,
            visible: isVisible(el)
        });
    });

    scope.querySelectorAll('select').forEach((el) => {
        const selector = getSelector(el);
        const options = Array.from(el.options).map((o) => o.text.trim()).filter((text) => text);
        fields.push({
            id: el.id || el.name || 'select_' + autoIdx++,
            selector: selector,
            label: getLabel(el),
            type: 'select',
            required: el.required,
            options: options,
            visible: isVisible(el)
        });
    });

    scope.querySelectorAll(
        '[role="listbox"], [role="combobox"], [data-testid*="select"], [class*="select__control"]'
    ).forEach((el) => {
        const selector = getSelector(el);
        const uniqueKey = 'custom_' + selector;
        if (seen.has(uniqueKey)) return;
        seen.add(uniqueKey);

        let options = [];
        const optionEls = el.querySelectorAll('[role="option"]');
        if (optionEls.length > 0) {
            optionEls.forEach((option) => {
                const text = option.textContent.trim();
                if (text) options.push(text);
            });
        }

        let label = el.getAttribute('aria-label') || '';
        if (!label) {
            const labelledBy = el.getAttribute('aria-labelledby');
            if (labelledBy) {
                const labelEl = document.getElementById(labelledBy);
                if (labelEl) label = labelEl.textContent.trim();
            }
        }
        if (!label) label = getLabel(el);

        if (label || options.length > 0) {
            fields.push({
                id: el.id || el.getAttribute('aria-label') || 'custom_select_' + autoIdx++,
                selector: selector,
                label: label,
                type: 'custom_select',
                options: options,
                visible: isVisible(el)
            });
        }
    });

    const radioGroups = {};
    scope.querySelectorAll('input[type="radio"]').forEach((el) => {
        const name = el.name;
        if (!name) return;
        if (!radioGroups[name]) {
            radioGroups[name] = {
                id: name,
                selector: '[name="' + name + '"]',
                label: getLabel(el),
                type: 'radio',
                options: []
            };
        }
        const label = getLabel(el) || el.value;
        if (label && !radioGroups[name].options.includes(label)) {
            radioGroups[name].options.push(label);
        }
    });
    Object.values(radioGroups).forEach((group) => fields.push(group));

    scope.querySelectorAll('input[type="file"]').forEach((el) => {
        const id = el.id || el.name || 'file_' + autoIdx++;
        fields.push({
            id: id,
            selector: getSelector(el),
            label: getLabel(el),
            type: 'file',
            accept: el.accept || ''
        });
    });

    return fields;
}
