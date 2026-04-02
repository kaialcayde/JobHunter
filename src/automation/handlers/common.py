"""Shared helpers for state handlers."""

import logging

from rich.console import Console

from ..browser_scripts import evaluate_script

logger = logging.getLogger(__name__)
console = Console(force_terminal=True)


def _debug_dump_dom(page, debug_dir, console):
    """Dump DOM structure of all form widgets to JSON files for inspection."""
    import json as _json

    try:
        result = evaluate_script(page, "debug/dump_form_widgets.js")

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
        all_fields = evaluate_script(page, "debug/list_visible_form_elements.js")
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
                options = evaluate_script(page, "debug/list_open_dropdown_options.js")
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
        use_alias = settings.get("automation", {}).get("use_email_aliases", False)
        desired_email = account_registry.desired_email(
            hostname,
            tenant=tenant,
            platform=platform,
            use_alias=use_alias,
        )
        if account_registry.has_account(hostname):
            creds = account_registry.get_credentials(hostname)
            status = (creds or {}).get("status", "")
            if (
                creds
                and desired_email
                and not use_alias
                and status in ("pending", "fill_vision")
                and creds.get("email") != desired_email
            ):
                account_registry.sync_email(hostname, desired_email)
                creds["email"] = desired_email
                logger.info(f"AccountRegistry: corrected stored email for {hostname}")
        else:
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
