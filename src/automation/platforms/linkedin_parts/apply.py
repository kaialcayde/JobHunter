"""LinkedIn Apply-button flows."""

from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .common import (
    LINKEDIN_APPLY_SELECTORS,
    LINKEDIN_APPLY_WAIT_SELECTORS,
    console,
    evaluate_script,
    logger,
)
from .modals import detect_easy_apply_modal, dismiss_all_linkedin_modals, handle_share_profile_modal


def click_linkedin_apply(page):
    """Handle LinkedIn Apply button."""
    dismiss_all_linkedin_modals(page)

    apply_btn = None

    try:
        page.wait_for_selector(LINKEDIN_APPLY_WAIT_SELECTORS, state="visible", timeout=5000)
    except PlaywrightTimeoutError:
        pass

    for selector in LINKEDIN_APPLY_SELECTORS:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                apply_btn = btn
                break
        except Exception:
            continue

    if not apply_btn:
        dismiss_all_linkedin_modals(page)
        page.wait_for_timeout(1000)
        for selector in LINKEDIN_APPLY_SELECTORS:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    apply_btn = btn
                    break
            except Exception:
                continue

    if not apply_btn:
        diag = evaluate_script(page, "linkedin/missing_apply_button_diagnostic.js")
        logger.info(f"LinkedIn Apply button not found. Page diagnostic: {diag}")
        console.print("  [dim]No Apply button found on LinkedIn page[/]")
        console.print(f"  [dim]  Visible dialogs: {diag.get('dialogCount', 0)}, buttons: {diag.get('buttonTexts', [])[:5]}[/]")
        try:
            debug_dir = Path("data/logs")
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / "debug_no_apply_button.png"
            page.screenshot(path=str(debug_path), full_page=True)
            console.print(f"  [dim]  Debug screenshot: {debug_path}[/]")
        except Exception as e:
            logger.debug(f"Debug screenshot failed: {e}")
        return False

    btn_text = (apply_btn.text_content() or "").strip().lower()
    is_easy_apply = "easy apply" in btn_text
    logger.info(f"LinkedIn Apply button found: text='{btn_text[:40]}', easy_apply={is_easy_apply}")
    console.print(f"  [dim]Found Apply button: '{btn_text[:30]}'[/]")

    if is_easy_apply:
        apply_btn.click()
        page.wait_for_timeout(500)
        console.print("  [dim]Clicked Easy Apply[/]")
        return "easy_apply"

    url_before = page.url
    btn_tag = apply_btn.evaluate("el => el.tagName.toLowerCase()")
    btn_href = apply_btn.get_attribute("href") if btn_tag == "a" else None
    logger.info(f"Apply button: tag={btn_tag}, href={btn_href}, text='{btn_text[:40]}'")

    is_external_href = (
        btn_href and btn_href.startswith("http")
        and ("linkedin.com" not in btn_href or "/redir/redirect" in btn_href or "/safety/go" in btn_href)
    )
    if is_external_href:
        nav_url = btn_href
        if "/safety/go" in btn_href or "/redir/redirect" in btn_href:
            parsed = urlparse(btn_href)
            qs = parse_qs(parsed.query)
            target = qs.get("url", [None])[0]
            if target:
                nav_url = unquote(target)
                logger.info(f"Decoded LinkedIn redirect -> {nav_url}")
        console.print(f"  [dim]Apply link href -- navigating directly: {nav_url[:80]}[/]")
        page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except PlaywrightTimeoutError:
            pass
        if page.url != url_before:
            return True

    tab_count_before = len(page.context.pages)
    is_same_page_link = (
        btn_tag == "a" and btn_href and "linkedin.com" in btn_href
        and "/redir/redirect" not in btn_href and "/safety/go" not in btn_href
    )
    if is_same_page_link:
        apply_btn.evaluate("el => el.addEventListener('click', e => e.preventDefault(), {once: true})")
    try:
        with page.context.expect_page(timeout=3000) as popup_info:
            apply_btn.click()
        new_page = popup_info.value
        new_page.wait_for_load_state("domcontentloaded")
        if new_page.url != "about:blank":
            console.print(f"  [dim]External apply opened: {new_page.url[:80]}[/]")
            return "new_tab"
    except PlaywrightTimeoutError:
        pass

    share_continue = handle_share_profile_modal(page)
    if share_continue == "new_tab":
        return "new_tab"
    if share_continue is True:
        if len(page.context.pages) > tab_count_before:
            latest = page.context.pages[-1]
            if latest != page and latest.url != "about:blank":
                latest.wait_for_load_state("domcontentloaded")
                console.print(f"  [dim]External apply opened: {latest.url[:80]}[/]")
                return "new_tab"
        if page.url != url_before:
            return True

    if len(page.context.pages) > tab_count_before:
        latest = page.context.pages[-1]
        if latest != page and latest.url != "about:blank":
            latest.wait_for_load_state("domcontentloaded")
            console.print(f"  [dim]External apply opened: {latest.url[:80]}[/]")
            return "new_tab"

    if page.url != url_before:
        console.print(f"  [dim]Navigated to: {page.url[:80]}[/]")
        return True

    modal = page.query_selector(".jobs-easy-apply-modal, .jobs-easy-apply-content")
    if modal and modal.is_visible():
        console.print("  [dim]Easy Apply modal detected[/]")
        return True

    ext_url = evaluate_script(page, "linkedin/extract_external_apply_url.js")
    if ext_url:
        console.print(f"  [dim]Found external apply URL: {ext_url[:80]}[/]")
        page.goto(ext_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except PlaywrightTimeoutError:
            pass
        return True

    console.print("  [dim]Apply button clicked but nothing happened[/]")
    logger.warning(f"Apply click had no effect. tag={btn_tag}, href={btn_href}, url={page.url}")
    try:
        debug_dir = Path("data/logs")
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / "debug_apply_no_effect.png"
        page.screenshot(path=str(debug_path), full_page=True)
        console.print(f"  [dim]  Debug screenshot: {debug_path}[/]")
    except Exception as e:
        logger.debug(f"Debug screenshot failed: {e}")
    return False


def handle_linkedin_post_apply(page, apply_result, listing_url):
    """Handle post-apply-click logic for LinkedIn pages."""
    still_on_linkedin = "linkedin.com" in page.url.lower()
    if not still_on_linkedin:
        return None

    is_easy_apply_flow = apply_result == "easy_apply"

    if is_easy_apply_flow:
        for _wait in range(6):
            if detect_easy_apply_modal(page):
                console.print("  [dim]Easy Apply modal is open[/]")
                return "easy_apply"
            page.wait_for_timeout(500)

        try:
            debug_dir = Path("data/logs")
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / "debug_easy_apply_no_modal.png"
            page.screenshot(path=str(debug_path), full_page=True)
            console.print(f"  [dim]  Debug screenshot: {debug_path}[/]")
        except Exception as e:
            logger.debug(f"Debug screenshot failed: {e}")
        console.print("  [yellow]Easy Apply clicked but modal didn't open -- retrying click[/]")

        from ...detection import click_apply_button, dismiss_modals

        dismiss_modals(page)
        retry_result = click_apply_button(page)
        if retry_result == "easy_apply":
            page.wait_for_timeout(1500)
            if detect_easy_apply_modal(page):
                return "easy_apply"
        console.print("  [yellow]Easy Apply modal still not open after retry[/]")
        return "failed"

    has_modal = detect_easy_apply_modal(page)
    logger.info(f"Still on LinkedIn: url={page.url[:80]}, easy_apply_modal={has_modal}, "
                f"listing_url={listing_url}, apply_result={apply_result}")
    if has_modal:
        return "easy_apply"

    if listing_url and "linkedin.com" not in listing_url.lower():
        console.print(f"  [dim]Stuck on LinkedIn -- navigating to company page: {listing_url[:60]}[/]")
        page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except PlaywrightTimeoutError:
            pass
        if "linkedin.com" not in page.url.lower():
            return "navigated"
        if detect_easy_apply_modal(page):
            return "easy_apply"
    else:
        console.print(f"  [dim]No alternate URL available (listing_url={listing_url})[/]")

    try:
        debug_dir = Path("data/logs")
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / "debug_stuck_linkedin.png"
        page.screenshot(path=str(debug_path), full_page=True)
        console.print(f"  [dim]  Debug screenshot: {debug_path}[/]")
    except Exception as e:
        logger.debug(f"Debug screenshot failed: {e}")

    page_tabs = len(page.context.pages)
    console.print(f"  [yellow]Could not leave LinkedIn -- skipping (tabs={page_tabs}, url={page.url[:60]})[/]")
    return "failed"
