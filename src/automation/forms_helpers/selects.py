"""Custom select helpers shared by the DOM backend."""

from rich.console import Console

console = Console(force_terminal=True)


def _fill_react_select(page, el, value: str):
    """Handle React-Select combobox inputs."""
    def _try_type_and_select(text: str) -> bool:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            el.evaluate('e => e.value = ""')
            page.wait_for_timeout(100)
            el.click()
            page.wait_for_timeout(400)
            page.keyboard.type(text, delay=50)
            page.wait_for_timeout(800)

            options = page.query_selector_all('[role="option"]')
            for opt in options:
                try:
                    if opt.is_visible():
                        selected_text = opt.text_content().strip()
                        opt.click()
                        page.wait_for_timeout(300)
                        console.print(f"  [dim]React-Select: selected '{selected_text}'[/]")
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    if _try_type_and_select(value):
        return

    fallbacks = []
    value_lower = value.lower()
    if any(kw in value_lower for kw in ["job board", "job site", "online"]):
        fallbacks = ["LinkedIn", "Online", "Other"]
    elif any(kw in value_lower for kw in ["prefer not", "decline", "n/a"]):
        fallbacks = ["Prefer not to answer", "Decline", "Other"]
    else:
        first_word = value.split()[0] if value.split() else value
        if first_word != value:
            fallbacks = [first_word, "Other"]
        else:
            fallbacks = ["Other"]

    for fb in fallbacks:
        if _try_type_and_select(fb):
            return

    try:
        el.click()
        page.wait_for_timeout(500)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(300)
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(300)
        options = page.query_selector_all('[role="option"]')
        for opt in options:
            if opt.is_visible():
                opt.click()
                page.wait_for_timeout(300)
                console.print("  [dim]React-Select: selected first available option[/]")
                return
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
    except Exception:
        pass

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _fill_custom_select(page, el, value: str):
    """Handle custom dropdown/listbox components."""
    el.click()
    page.wait_for_timeout(500)

    option_selectors = [
        f'[role="option"]:has-text("{value}")',
        f'li:has-text("{value}")',
        f'[class*="option"]:has-text("{value}")',
    ]
    for opt_sel in option_selectors:
        try:
            opt = page.query_selector(opt_sel)
            if opt and opt.is_visible():
                opt.click()
                page.wait_for_timeout(300)
                return
        except Exception:
            continue

    try:
        page.keyboard.type(value[:20])
        page.wait_for_timeout(500)
        first_option = page.query_selector('[role="option"]')
        if first_option and first_option.is_visible():
            first_option.click()
            page.wait_for_timeout(300)
            return
    except Exception:
        pass

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
