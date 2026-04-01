"""Shared helpers for state handlers."""

import logging

from rich.console import Console

logger = logging.getLogger(__name__)
console = Console(force_terminal=True)


def _debug_dump_dom(page, debug_dir, console):
    """Dump DOM structure of all form widgets to JSON files for inspection."""
    import json as _json

    try:
        result = page.evaluate("""() => {
            const results = [];
            const candidates = document.querySelectorAll(
                '[class*="select"], [class*="dropdown"], [class*="combobox"], ' +
                '[role="combobox"], [role="listbox"], [aria-haspopup], ' +
                '[class*="Select"], [class*="Dropdown"], [class*="picker"]'
            );
            candidates.forEach(el => {
                const container = el.closest(
                    '[class*="field"], [class*="group"], [class*="row"], ' +
                    '[class*="form"], [class*="question"]'
                );
                const label = container
                    ? container.querySelector('label, [class*="label"]')
                    : null;
                results.push({
                    label: label ? label.innerText.trim() : '(no label)',
                    tag: el.tagName, className: el.className.substring(0, 120),
                    role: el.getAttribute('role'),
                    ariaHaspopup: el.getAttribute('aria-haspopup'),
                    id: el.id, outerHTML: el.outerHTML.substring(0, 300)
                });
            });

            document.querySelectorAll('select').forEach(el => {
                const container = el.closest(
                    '[class*="field"], [class*="group"], [class*="row"], ' +
                    '[class*="form"], [class*="question"]'
                );
                const label = container
                    ? container.querySelector('label, [class*="label"]')
                    : null;
                const options = Array.from(el.options).slice(0, 10).map(o => o.text);
                results.push({
                    label: label ? label.innerText.trim() : '(no label)',
                    tag: 'SELECT', className: el.className.substring(0, 120),
                    id: el.id, name: el.name, options: options,
                    outerHTML: el.outerHTML.substring(0, 500)
                });
            });

            const fields = [];
            document.querySelectorAll(
                '[class*="field"], [class*="form-group"], [class*="question"]'
            ).forEach(el => {
                const label = el.querySelector('label, [class*="label"]');
                const inputs = el.querySelectorAll(
                    'input, select, textarea, [role="combobox"], [role="listbox"]'
                );
                if (label && inputs.length > 0) {
                    fields.push({
                        label: label.innerText.trim().substring(0, 60),
                        containerClass: el.className.substring(0, 100),
                        containerTag: el.tagName,
                        inputTypes: Array.from(inputs).map(i => ({
                            tag: i.tagName, type: i.type || i.getAttribute('role') || '',
                            className: i.className.substring(0, 80),
                            name: i.name || i.id || ''
                        }))
                    });
                }
            });

            return {
                candidates: results, fields: fields,
                url: window.location.href, title: document.title
            };
        }""")

        dump_path = debug_dir / "avature_dom_dump.json"
        dump_path.write_text(_json.dumps(result, indent=2))
        console.print(f"  [bold yellow]  DOM dump: {dump_path} "
                       f"({len(result['candidates'])} dropdown candidates, "
                       f"{len(result['fields'])} field containers)[/]")
        for c in result["candidates"][:8]:
            console.print(f"    [dim]{c['label']!r:30s} <{c['tag']}> "
                          f"class={c['className'][:50]!r}[/]")
        for f in result["fields"][:12]:
            inputs = ", ".join(
                f"<{i['tag']}> {i['type']}" for i in f["inputTypes"]
            )
            console.print(f"    [dim]{f['label']!r:30s} -> {inputs}[/]")
    except Exception as e:
        console.print(f"  [red]DOM dump failed: {e}[/]")

    try:
        all_fields = page.evaluate("""() => {
            const fields = [];
            document.querySelectorAll(
                'input, select, textarea, [contenteditable="true"]'
            ).forEach(el => {
                if (!el.offsetParent && el.type !== 'hidden') return;
                const container = el.closest(
                    '[class*="field"], [class*="group"], [class*="row"], ' +
                    '[class*="form"], [class*="question"]'
                );
                const label = container
                    ? container.querySelector('label, [class*="label"]')
                    : null;
                fields.push({
                    label: label ? label.innerText.trim().substring(0, 60)
                                 : '(no label)',
                    tag: el.tagName, type: el.type || '',
                    name: el.name || '', id: el.id || '',
                    className: el.className.substring(0, 80),
                    value: (el.value || '').substring(0, 40),
                    placeholder: el.placeholder || ''
                });
            });
            return fields;
        }""")
        fields_path = debug_dir / "avature_all_fields.json"
        fields_path.write_text(_json.dumps(all_fields, indent=2))
        console.print(f"  [bold yellow]  All fields: {fields_path} "
                       f"({len(all_fields)} visible elements)[/]")
    except Exception as e:
        console.print(f"  [red]All-fields dump failed: {e}[/]")

    try:
        for sel in [
            '[class*="Select-control"]', '[class*="select-control"]',
            '[class*="avature-select"]', '[class*="dropdown-toggle"]',
            '[class*="custom-select"]', 'select',
            '[role="combobox"]', '[aria-haspopup="listbox"]',
        ]:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(800)
                options = page.evaluate("""() => {
                    const opts = document.querySelectorAll(
                        '[role="option"], [class*="option"], li[class*="item"], ' +
                        '.Select-menu-outer li, [class*="menu"] li, ' +
                        '[class*="listbox"] li'
                    );
                    return Array.from(opts).slice(0, 15).map(o => ({
                        tag: o.tagName, className: o.className.substring(0, 80),
                        role: o.getAttribute('role'),
                        text: o.innerText.trim().substring(0, 60),
                        outerHTML: o.outerHTML.substring(0, 200)
                    }));
                }""")
                if options:
                    click_path = debug_dir / "avature_dropdown_click.json"
                    click_path.write_text(_json.dumps(
                        {"trigger_selector": sel, "options": options}, indent=2
                    ))
                    console.print(f"  [bold yellow]  Dropdown click: {click_path} "
                                   f"({len(options)} options via {sel!r})[/]")
                    for o in options[:5]:
                        console.print(f"    [dim]{o['text']!r:30s} <{o['tag']}> "
                                      f"role={o['role']} class={o['className'][:40]!r}[/]")
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                page.screenshot(
                    path=str(debug_dir / "debug_dropdown_open.png"), full_page=True
                )
                break
    except Exception as e:
        console.print(f"  [red]Dropdown click test failed: {e}[/]")


def _fill_password_fields(page, account_registry, settings: dict) -> bool:
    """Fill account-creation fields when embedded in the application form."""
    if not account_registry:
        return False
    try:
        pw_inputs = page.query_selector_all('input[type="password"]')
        if not pw_inputs:
            return False
        from urllib.parse import urlparse

        from ..account_registry import detect_ats_platform, extract_tenant, is_auto_register_allowed

        current_url = page.url
        hostname = urlparse(current_url).hostname or ""
        if not is_auto_register_allowed(current_url, settings):
            return False
        platform = detect_ats_platform(current_url) or detect_ats_platform(hostname)
        tenant = extract_tenant(hostname, platform)
        if account_registry.has_account(hostname):
            creds = account_registry.get_credentials(hostname)
        else:
            use_alias = settings.get("automation", {}).get("use_email_aliases", False)
            creds = account_registry.generate_credentials(hostname, tenant=tenant, platform=platform, use_alias=use_alias)
            account_registry._conn.execute(
                "UPDATE accounts SET status='fill_vision' WHERE domain=?", (hostname,)
            )
            account_registry._conn.commit()

        password = creds["password"]
        for pw_input in pw_inputs:
            try:
                pw_input.fill(password)
            except Exception as e:
                logger.debug(f"Password field fill failed: {e}")
        filled_count = len(pw_inputs)

        email = creds.get("email", "")
        if email:
            for sel in ['input[type="email"]', 'input[name*="email" i]', 'input[autocomplete="email"]']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.fill(email)
                        filled_count += 1
                        break
                except Exception:
                    continue

        try:
            from ...config.loader import load_profile

            profile_data = load_profile()
            personal = profile_data.get("personal", {})
            first_name = personal.get("first_name", "")
            last_name = personal.get("last_name", "")
        except Exception:
            first_name = last_name = ""

        if first_name:
            for sel in ['input[name*="first" i]', 'input[placeholder*="first name" i]',
                        'input[id*="first" i]', 'input[autocomplete="given-name"]']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.fill(first_name)
                        filled_count += 1
                        break
                except Exception:
                    continue

        if last_name:
            for sel in ['input[name*="last" i]', 'input[placeholder*="last name" i]',
                        'input[id*="last" i]', 'input[autocomplete="family-name"]']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.fill(last_name)
                        filled_count += 1
                        break
                except Exception:
                    continue

        console.print(f"  [dim]Pre-filled {filled_count} account creation field(s) via registry[/]")
        return filled_count > 0
    except Exception as e:
        logger.debug(f"_fill_password_fields failed: {e}")
        return False
