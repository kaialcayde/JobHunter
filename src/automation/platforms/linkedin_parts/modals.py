"""LinkedIn modal handling helpers."""

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .common import (
    LINKEDIN_EASY_APPLY_SELECTORS,
    LINKEDIN_MODAL_SELECTORS,
    LINKEDIN_OVERLAY_SELECTORS,
    LINKEDIN_SHADOW_HOST_SELECTORS,
    SHARE_PROFILE_CONTINUE_SELECTORS,
    console,
    evaluate_script,
    logger,
)


def _has_blocking_modal(page) -> bool:
    """Check if there's any visible modal/dialog blocking the page."""
    return evaluate_script(page, "linkedin/has_blocking_modal.js", LINKEDIN_MODAL_SELECTORS)


def dismiss_all_linkedin_modals(page) -> bool:
    """Dismiss any blocking LinkedIn modal."""
    if not _has_blocking_modal(page):
        return False

    share_handled = evaluate_script(
        page,
        "linkedin/handle_share_profile_modal.js",
        LINKEDIN_MODAL_SELECTORS,
    )

    if share_handled:
        console.print(f"  [dim]Share Profile modal: {share_handled}[/]")
        if share_handled == "continue":
            page.wait_for_timeout(2000)
            return True
        page.wait_for_timeout(500)
        if not _has_blocking_modal(page):
            return True

    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    if not _has_blocking_modal(page):
        console.print("  [dim]Dismissed LinkedIn modal (Escape)[/]")
        return True

    dismissed = evaluate_script(
        page,
        "linkedin/dismiss_modal.js",
        {
            "modalSelector": '[role="dialog"], .artdeco-modal, .artdeco-modal-overlay, [data-test-modal], div[class*="modal"][class*="overlay"]',
            "overlaySelector": LINKEDIN_OVERLAY_SELECTORS,
        },
    )

    if dismissed:
        page.wait_for_timeout(300)
        if not _has_blocking_modal(page):
            console.print("  [dim]Dismissed LinkedIn modal (JS click)[/]")
            return True

    try:
        page.mouse.click(1, 1)
        page.wait_for_timeout(100)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        if not _has_blocking_modal(page):
            console.print("  [dim]Dismissed LinkedIn modal (focus+Escape)[/]")
            return True
    except Exception as e:
        logger.debug(f"Focus+Escape modal dismiss failed: {e}")

    console.print("  [yellow]Could not dismiss LinkedIn modal[/]")
    return False


def handle_share_profile(page) -> bool:
    """Backward-compatible alias."""
    return dismiss_all_linkedin_modals(page)


def dismiss_linkedin_modals(page):
    """Backward-compatible alias."""
    dismiss_all_linkedin_modals(page)


def detect_easy_apply_modal(page) -> bool:
    """Check if a LinkedIn Easy Apply modal or SDUI apply flow is active."""
    return evaluate_script(
        page,
        "linkedin/detect_easy_apply_modal.js",
        {
            "shadowHostSelectors": LINKEDIN_SHADOW_HOST_SELECTORS,
            "easyApplySelectors": LINKEDIN_EASY_APPLY_SELECTORS,
            "modalSelector": '[role="dialog"], .artdeco-modal',
        },
    )


def handle_share_profile_modal(page):
    """Wait for and handle LinkedIn's 'Share your profile?' modal."""
    page.wait_for_timeout(1000)

    diag = evaluate_script(page, "linkedin/share_profile_diagnostic.js")
    logger.info(f"Share Profile diagnostic: dialogs={len(diag.get('dialogs', []))}, "
                f"continueButtons={len(diag.get('continueButtons', []))}")
    for d in diag.get("dialogs", [])[:5]:
        logger.info(f"  Dialog: visible={d['visible']}, hasShare={d['hasShare']}, hasProfile={d['hasProfile']}, classes={d['classes'][:60]}")
    for b in diag.get("continueButtons", [])[:3]:
        logger.info(f"  Continue btn: tag={b['tag']}, text='{b['text']}', visible={b['visible']}")

    has_share_modal = evaluate_script(
        page,
        "linkedin/has_share_profile_modal.js",
        '[role="dialog"], .artdeco-modal, .artdeco-modal-overlay, [data-test-modal], .share-profile-modal, div[class*="modal"]',
    )

    if not has_share_modal:
        has_share_modal = evaluate_script(page, "linkedin/has_share_context_continue.js")

    if not has_share_modal:
        return None

    console.print("  [dim]Share Profile modal detected[/]")

    for selector in SHARE_PROFILE_CONTINUE_SELECTORS:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                tab_count = len(page.context.pages)
                url_before = page.url
                try:
                    with page.context.expect_page(timeout=5000) as popup_info:
                        btn.click()
                    new_page = popup_info.value
                    new_page.wait_for_load_state("domcontentloaded")
                    if new_page.url != "about:blank":
                        console.print(f"  [dim]External apply opened: {new_page.url[:80]}[/]")
                        return "new_tab"
                except PlaywrightTimeoutError:
                    page.wait_for_timeout(1000)
                    if len(page.context.pages) > tab_count:
                        latest = page.context.pages[-1]
                        if latest != page and latest.url != "about:blank":
                            latest.wait_for_load_state("domcontentloaded")
                            console.print(f"  [dim]External apply opened: {latest.url[:80]}[/]")
                            return "new_tab"
                    if page.url != url_before:
                        console.print(f"  [dim]Navigated to: {page.url[:80]}[/]")
                        return True
                return True
        except Exception:
            continue

    dismiss_all_linkedin_modals(page)
    return True
