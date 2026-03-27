"""LinkedIn-specific automation workarounds.

Handles LinkedIn modals (Share your profile, messaging overlays, notifications),
Easy Apply modal detection, Apply button clicking, and post-apply flow.
"""

import logging
from pathlib import Path

from rich.console import Console

from ..selectors import (
    LINKEDIN_MODAL_SELECTORS,
    LINKEDIN_EASY_APPLY_SELECTORS,
    LINKEDIN_SHADOW_HOST_SELECTORS,
    LINKEDIN_OVERLAY_SELECTORS,
    LINKEDIN_APPLY_SELECTORS,
    LINKEDIN_APPLY_WAIT_SELECTORS,
    SHARE_PROFILE_CONTINUE_SELECTORS,
)

logger = logging.getLogger(__name__)

console = Console(force_terminal=True)


def _has_blocking_modal(page) -> bool:
    """Check if there's any visible modal/dialog blocking the page (not Easy Apply)."""
    return page.evaluate("""() => {
        const dialogs = document.querySelectorAll(
            '[role="dialog"], .artdeco-modal, .artdeco-modal-overlay, ' +
            '[data-test-modal], div[class*="modal"][class*="overlay"], ' +
            '.share-profile-modal'
        );
        for (const d of dialogs) {
            if (d.offsetWidth === 0 || d.offsetHeight === 0) continue;
            const text = (d.textContent || '').toLowerCase();
            // Don't count Easy Apply modals as blocking
            if (text.includes('easy apply')) continue;
            return true;
        }
        return false;
    }""")


def dismiss_all_linkedin_modals(page) -> bool:
    """Dismiss any blocking LinkedIn modal (Share Profile, notifications, etc.).

    The "Share your profile?" modal is special -- it has a "Continue" button that
    actually proceeds with the application. We click Continue instead of dismissing.

    Strategy order:
    1. Check for "Share your profile" modal -- click Continue (proceeds with apply)
    2. Press Escape (works on most LinkedIn modals)
    3. Try JS click on dismiss/close buttons
    4. Try clicking the overlay background

    Returns True if a modal was dismissed.
    """
    if not _has_blocking_modal(page):
        return False

    # Strategy 1: "Share your profile?" modal -- click Continue to proceed with apply
    share_handled = page.evaluate("""() => {
        const dialogs = document.querySelectorAll(
            '[role="dialog"], .artdeco-modal, .artdeco-modal-overlay, ' +
            '[data-test-modal], div[class*="modal"][class*="overlay"], ' +
            '.share-profile-modal'
        );
        for (const dialog of dialogs) {
            if (dialog.offsetWidth === 0 || dialog.offsetHeight === 0) continue;
            const text = (dialog.textContent || '').toLowerCase();
            if (text.includes('share your profile') || text.includes('share profile')) {
                // Click "Continue" button (proceeds with the application)
                const buttons = dialog.querySelectorAll('button, a, [role="button"]');
                for (const btn of buttons) {
                    if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
                    const t = (btn.textContent || '').trim().toLowerCase();
                    if (t.includes('continue')) {
                        btn.click();
                        return 'continue';
                    }
                }
                // Fallback: click the X to dismiss without sharing
                for (const btn of buttons) {
                    if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
                    const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                    if (label.includes('dismiss') || label.includes('close')) {
                        btn.click();
                        return 'dismissed';
                    }
                }
            }
        }
        return null;
    }""")

    if share_handled:
        console.print(f"  [dim]Share Profile modal: {share_handled}[/]")
        if share_handled == "continue":
            # Continue triggers an async redirect (new tab) -- wait for it and
            # do NOT fall through to Escape, which would cancel the redirect
            page.wait_for_timeout(2000)
            return True
        page.wait_for_timeout(500)
        if not _has_blocking_modal(page):
            return True

    # Strategy 2: Press Escape -- works on most LinkedIn modals
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    if not _has_blocking_modal(page):
        console.print("  [dim]Dismissed LinkedIn modal (Escape)[/]")
        return True

    # Strategy 3: Brute-force JS -- find ANY visible modal and click dismiss elements
    dismissed = page.evaluate("""() => {
        const dialogs = document.querySelectorAll(
            '[role="dialog"], .artdeco-modal, .artdeco-modal-overlay, ' +
            '[data-test-modal], div[class*="modal"][class*="overlay"]'
        );
        for (const dialog of dialogs) {
            if (dialog.offsetWidth === 0 || dialog.offsetHeight === 0) continue;
            const text = (dialog.textContent || '').toLowerCase();
            if (text.includes('easy apply')) continue;

            // Try every button/link in the modal
            const clickables = dialog.querySelectorAll('button, a, [role="button"]');
            for (const el of clickables) {
                if (el.offsetWidth === 0 || el.offsetHeight === 0) continue;
                const label = (el.getAttribute('aria-label') || '').toLowerCase();
                const t = (el.textContent || '').trim().toLowerCase();

                // Click anything that looks like a dismiss action
                if (label.includes('dismiss') || label.includes('close') ||
                    t.includes('no thanks') || t.includes('not now') ||
                    t.includes('dismiss') || t.includes('skip') ||
                    t === 'x' || t === '') {
                    // For empty-text buttons, check if it's small (likely an X button)
                    if (t === '' && el.offsetWidth > 60) continue;
                    el.click();
                    return true;
                }
            }
        }

        // Try overlay click
        const overlay = document.querySelector(
            '.artdeco-modal-overlay, .artdeco-modal-overlay--is-top-layer'
        );
        if (overlay) { overlay.click(); return true; }

        return false;
    }""")

    if dismissed:
        page.wait_for_timeout(300)
        if not _has_blocking_modal(page):
            console.print("  [dim]Dismissed LinkedIn modal (JS click)[/]")
            return True

    # Strategy 4: Escape again with a click on body first (focus the page)
    try:
        page.mouse.click(1, 1)  # Click top-left corner to focus
        page.wait_for_timeout(100)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        if not _has_blocking_modal(page):
            console.print("  [dim]Dismissed LinkedIn modal (focus+Escape)[/]")
            return True
    except Exception:
        pass

    console.print("  [yellow]Could not dismiss LinkedIn modal[/]")
    return False


# Keep old names as aliases for backwards compatibility with imports
def handle_share_profile(page) -> bool:
    return dismiss_all_linkedin_modals(page)


def dismiss_linkedin_modals(page):
    dismiss_all_linkedin_modals(page)


def detect_easy_apply_modal(page) -> bool:
    """Check if a LinkedIn Easy Apply modal or SDUI apply flow is active.

    LinkedIn may render Easy Apply modals inside a shadow DOM host
    (#interop-outlet / [data-testid="interop-shadowdom"]), so we check
    both the regular DOM and any shadow roots.
    """
    return page.evaluate("""() => {
        // Helper: collect all roots (document + any shadow roots) to search
        function getAllRoots() {
            const roots = [document];
            // LinkedIn uses an interop shadow DOM host for the Easy Apply modal
            const shadowHosts = document.querySelectorAll(
                '#interop-outlet, [data-testid="interop-shadowdom"], ' +
                '[class*="interop"], [id*="shadow"]'
            );
            for (const host of shadowHosts) {
                if (host.shadowRoot) roots.push(host.shadowRoot);
            }
            // Also check any element with open shadow root
            const allElements = document.querySelectorAll('*');
            for (const el of allElements) {
                if (el.shadowRoot && !roots.includes(el.shadowRoot)) {
                    roots.push(el.shadowRoot);
                }
            }
            return roots;
        }

        const roots = getAllRoots();

        const selectors = [
            '.jobs-easy-apply-modal', '.jobs-easy-apply-content',
            '[role="dialog"][aria-label*="Easy Apply"]',
            '[role="dialog"][aria-label*="Apply to"]',
            '[role="dialog"] .jobs-easy-apply-form-element'
        ];
        for (const root of roots) {
            for (const sel of selectors) {
                try {
                    const el = root.querySelector(sel);
                    if (el && el.offsetWidth > 0 && el.offsetHeight > 0) return true;
                } catch(e) {}
            }
        }

        // Check for any visible dialog that contains apply form fields
        for (const root of roots) {
            try {
                const dialogs = root.querySelectorAll('[role="dialog"], .artdeco-modal');
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
            } catch(e) {}
        }

        // Detect LinkedIn SDUI apply flow (URL-based, not modal-based)
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
    }""")


# ---------------------------------------------------------------------------
# Functions moved from detection.py (Phase 3)
# ---------------------------------------------------------------------------

def handle_share_profile_modal(page):
    """Wait for and handle LinkedIn's 'Share your profile?' modal.

    Uses Playwright's native .click() on the Continue button (not JS el.click())
    so that popup-opening handlers fire properly.

    Returns "new_tab" if a new tab opened, True if modal was handled, None if no modal.
    """
    # Brief wait for the modal animation to complete
    page.wait_for_timeout(1000)

    # Diagnostic: dump what's on the page to understand modal structure
    diag = page.evaluate("""() => {
        const result = { dialogs: [], continueButtons: [], bodySnippet: '' };

        // Check all potential modal containers
        const selectors = '[role="dialog"], .artdeco-modal, .artdeco-modal-overlay, [data-test-modal], div[class*="modal"], div[class*="overlay"]';
        const els = document.querySelectorAll(selectors);
        for (const el of els) {
            const visible = el.offsetWidth > 0 && el.offsetHeight > 0;
            const text = (el.textContent || '').toLowerCase().slice(0, 200);
            const classes = el.className || '';
            result.dialogs.push({ tag: el.tagName, classes: String(classes).slice(0, 100), visible, hasShare: text.includes('share'), hasProfile: text.includes('profile') });
        }

        // Check for any Continue button
        const btns = document.querySelectorAll('button, a');
        for (const b of btns) {
            const text = (b.textContent || '').trim().toLowerCase();
            if (text.includes('continue')) {
                result.continueButtons.push({ tag: b.tagName, text: text.slice(0, 50), visible: b.offsetWidth > 0 });
            }
        }

        result.bodySnippet = (document.body?.textContent || '').slice(0, 500);
        return result;
    }""")
    logger.info(f"Share Profile diagnostic: dialogs={len(diag.get('dialogs', []))}, "
                f"continueButtons={len(diag.get('continueButtons', []))}")
    for d in diag.get("dialogs", [])[:5]:
        logger.info(f"  Dialog: visible={d['visible']}, hasShare={d['hasShare']}, hasProfile={d['hasProfile']}, classes={d['classes'][:60]}")
    for b in diag.get("continueButtons", [])[:3]:
        logger.info(f"  Continue btn: tag={b['tag']}, text='{b['text']}', visible={b['visible']}")

    # Check if a Share Profile modal is visible -- use broad selectors since
    # LinkedIn's modal classes change frequently
    has_share_modal = page.evaluate("""() => {
        const dialogs = document.querySelectorAll(
            '[role="dialog"], .artdeco-modal, .artdeco-modal-overlay, ' +
            '[data-test-modal], .share-profile-modal, div[class*="modal"]'
        );
        for (const d of dialogs) {
            if (d.offsetWidth === 0 || d.offsetHeight === 0) continue;
            const text = (d.textContent || '').toLowerCase();
            if (text.includes('share your profile') || text.includes('share profile')) {
                return true;
            }
        }
        return false;
    }""")

    # Fallback: check if a visible Continue button exists anywhere on the page
    if not has_share_modal:
        has_share_modal = page.evaluate("""() => {
            const btns = document.querySelectorAll('button, a');
            for (const b of btns) {
                if (b.offsetWidth === 0 || b.offsetHeight === 0) continue;
                const text = (b.textContent || '').trim().toLowerCase();
                if (text === 'continue' || text.includes('continue')) {
                    // Check parent for share/profile context -- broader search
                    let el = b.parentElement;
                    for (let i = 0; i < 10 && el; i++) {
                        const parentText = (el.textContent || '').toLowerCase();
                        if (parentText.includes('share') || parentText.includes('profile')) {
                            return true;
                        }
                        el = el.parentElement;
                    }
                }
            }
            return false;
        }""")

    if not has_share_modal:
        return None

    console.print("  [dim]Share Profile modal detected[/]")

    # Find the Continue button using Playwright selectors -- need native .click()
    # so that popup-opening handlers (window.open) fire properly
    for selector in SHARE_PROFILE_CONTINUE_SELECTORS:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                # Use expect_page to catch the new tab from clicking Continue
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
                except Exception:
                    # Continue was clicked but no popup -- check for new tab or navigation
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

    # Fallback: dismiss the modal
    dismiss_all_linkedin_modals(page)
    return True


def click_linkedin_apply(page):
    """Handle LinkedIn Apply button -- Easy Apply or external redirect.

    LinkedIn lazy-loads the Apply button, so we wait for it. External apply
    buttons are <button> elements that open a new tab via JS, NOT <a> tags.
    Returns True if clicked, "easy_apply" for Easy Apply, "new_tab" if a new
    tab opened, False if not found.
    """
    # Dismiss any blocking modals FIRST (Share Profile, etc.)
    dismiss_all_linkedin_modals(page)

    # Wait for the apply button area to load (LinkedIn lazy-loads this)
    apply_btn = None

    # Wait for the Apply button area to render (LinkedIn lazy-loads job content)
    # Single wait on the broadest selector, then check all selectors
    try:
        page.wait_for_selector(LINKEDIN_APPLY_WAIT_SELECTORS,
                               state="visible", timeout=5000)
    except Exception:
        pass  # Button may still exist under a different selector

    for selector in LINKEDIN_APPLY_SELECTORS:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                apply_btn = btn
                break
        except Exception:
            continue

    # If wait_for_selector didn't find it, try all selectors with modal dismissal
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
        # Diagnostic: log what's actually on the page
        diag = page.evaluate("""() => {
            const dialogs = document.querySelectorAll('[role="dialog"], .artdeco-modal');
            const visibleDialogs = [];
            for (const d of dialogs) {
                if (d.offsetWidth > 0) visibleDialogs.push(d.textContent.trim().slice(0, 100));
            }
            const buttons = document.querySelectorAll('button, a[class*="apply"], a[class*="jobs-"], a[aria-label]');
            const visibleButtons = [];
            for (const b of buttons) {
                if (b.offsetWidth > 0) {
                    const tag = b.tagName.toLowerCase();
                    const t = b.textContent.trim().slice(0, 40);
                    if (t) visibleButtons.push(tag === 'a' ? `<a>${t}` : t);
                }
            }
            return {
                url: window.location.href,
                dialogCount: visibleDialogs.length,
                dialogs: visibleDialogs.slice(0, 3),
                buttonTexts: visibleButtons.slice(0, 10),
                bodyLen: (document.body?.innerText || '').length
            };
        }""")
        logger.info(f"LinkedIn Apply button not found. Page diagnostic: {diag}")
        console.print(f"  [dim]No Apply button found on LinkedIn page[/]")
        console.print(f"  [dim]  Visible dialogs: {diag.get('dialogCount', 0)}, buttons: {diag.get('buttonTexts', [])[:5]}[/]")

        # Save debug screenshot so we can see what the page actually looks like
        try:
            debug_dir = Path("data/logs")
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / "debug_no_apply_button.png"
            page.screenshot(path=str(debug_path), full_page=True)
            console.print(f"  [dim]  Debug screenshot: {debug_path}[/]")
        except Exception:
            pass

        return False

    # Determine what kind of apply this is from the button text/attributes
    btn_text = (apply_btn.text_content() or "").strip().lower()
    is_easy_apply = "easy apply" in btn_text
    logger.info(f"LinkedIn Apply button found: text='{btn_text[:40]}', easy_apply={is_easy_apply}")
    console.print(f"  [dim]Found Apply button: '{btn_text[:30]}'[/]")

    if is_easy_apply:
        # Easy Apply stays on LinkedIn -- just click and the modal opens
        apply_btn.click()
        page.wait_for_timeout(500)
        console.print("  [dim]Clicked Easy Apply[/]")
        return "easy_apply"

    # External apply -- clicking opens a new tab or navigates away
    url_before = page.url
    btn_tag = apply_btn.evaluate("el => el.tagName.toLowerCase()")
    btn_href = apply_btn.get_attribute("href") if btn_tag == "a" else None
    logger.info(f"Apply button: tag={btn_tag}, href={btn_href}, text='{btn_text[:40]}'")

    # If it's an <a> with an external href, try direct navigation.
    # LinkedIn redirect URLs (linkedin.com/redir/redirect/?url=...) count as external.
    is_external_href = (btn_href and btn_href.startswith("http") and
                        ("linkedin.com" not in btn_href or "/redir/redirect" in btn_href
                         or "/safety/go" in btn_href))
    if is_external_href:
        # For LinkedIn safety/redirect URLs, extract the actual target URL
        nav_url = btn_href
        if "/safety/go" in btn_href or "/redir/redirect" in btn_href:
            from urllib.parse import urlparse, parse_qs, unquote
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
        except Exception:
            pass
        if page.url != url_before:
            return True

    # Click the Apply button -- use expect_page to catch popups from the click itself.
    # For <a> tags with same-page LinkedIn hrefs, prevent default navigation first,
    # otherwise the href causes a page refresh that destroys the Share Profile modal.
    tab_count_before = len(page.context.pages)
    is_same_page_link = (btn_tag == "a" and btn_href and
                         "linkedin.com" in btn_href and "/redir/redirect" not in btn_href
                         and "/safety/go" not in btn_href)
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
    except Exception:
        pass  # No popup -- check for modal or navigation below

    # LinkedIn often shows "Share your profile?" modal after clicking Apply.
    # Click Continue with a real Playwright click (JS el.click() doesn't trigger popups).
    share_continue = handle_share_profile_modal(page)
    if share_continue == "new_tab":
        return "new_tab"
    if share_continue is True:
        # Modal was handled (Continue clicked) but no new tab -- check for new tabs/navigation
        if len(page.context.pages) > tab_count_before:
            latest = page.context.pages[-1]
            if latest != page and latest.url != "about:blank":
                latest.wait_for_load_state("domcontentloaded")
                console.print(f"  [dim]External apply opened: {latest.url[:80]}[/]")
                return "new_tab"
        if page.url != url_before:
            return True

    # Check if clicking Apply or Continue opened a new tab
    if len(page.context.pages) > tab_count_before:
        latest = page.context.pages[-1]
        if latest != page and latest.url != "about:blank":
            latest.wait_for_load_state("domcontentloaded")
            console.print(f"  [dim]External apply opened: {latest.url[:80]}[/]")
            return "new_tab"

    # Check if the page navigated
    if page.url != url_before:
        console.print(f"  [dim]Navigated to: {page.url[:80]}[/]")
        return True

    # Check if an Easy Apply modal opened
    modal = page.query_selector('.jobs-easy-apply-modal, .jobs-easy-apply-content')
    if modal and modal.is_visible():
        console.print("  [dim]Easy Apply modal detected[/]")
        return True

    # Last resort: try to extract external URL from page and navigate directly
    ext_url = page.evaluate("""() => {
        const el = document.querySelector(
            'a[href*="externalApply"], [data-job-apply-url], [data-apply-url]'
        );
        if (el) return el.href || el.getAttribute('data-job-apply-url') || el.getAttribute('data-apply-url');
        const links = document.querySelectorAll('a[href]');
        for (const link of links) {
            const href = link.href || '';
            if (href.includes('externalApply') || href.includes('applyUrl')) return href;
        }
        return null;
    }""")
    if ext_url:
        console.print(f"  [dim]Found external apply URL: {ext_url[:80]}[/]")
        page.goto(ext_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass
        return True

    # Nothing worked -- save debug screenshot and return False so caller can try harder
    console.print("  [dim]Apply button clicked but nothing happened[/]")
    logger.warning(f"Apply click had no effect. tag={btn_tag}, href={btn_href}, url={page.url}")
    try:
        debug_dir = Path("data/logs")
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / "debug_apply_no_effect.png"
        page.screenshot(path=str(debug_path), full_page=True)
        console.print(f"  [dim]  Debug screenshot: {debug_path}[/]")
    except Exception:
        pass
    return False  # Let caller try force_apply_click fallback


def handle_linkedin_post_apply(page, apply_result, listing_url):
    """Handle post-apply-click logic for LinkedIn pages.

    After clicking Apply on LinkedIn, determines if we're in an Easy Apply flow
    (waits for modal) or need to navigate to an external ATS.

    Returns:
        "easy_apply" - Easy Apply modal is open and ready
        "navigated" - Successfully navigated away from LinkedIn to external ATS
        "failed" - Could not proceed (stuck on LinkedIn, no modal)
        None - Not on LinkedIn (caller should continue normally)
    """
    still_on_linkedin = "linkedin.com" in page.url.lower()
    if not still_on_linkedin:
        return None

    is_easy_apply_flow = (apply_result == "easy_apply")

    if is_easy_apply_flow:
        # Easy Apply was clicked -- wait for the modal to render (up to 3s)
        for _wait in range(6):
            if detect_easy_apply_modal(page):
                console.print("  [dim]Easy Apply modal is open[/]")
                return "easy_apply"
            page.wait_for_timeout(500)

        # Modal didn't appear -- save debug screenshot
        try:
            debug_dir = Path("data/logs")
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / "debug_easy_apply_no_modal.png"
            page.screenshot(path=str(debug_path), full_page=True)
            console.print(f"  [dim]  Debug screenshot: {debug_path}[/]")
        except Exception:
            pass
        console.print("  [yellow]Easy Apply clicked but modal didn't open -- retrying click[/]")

        # Retry: dismiss modals and click Easy Apply again
        from ..detection import dismiss_modals, click_apply_button
        dismiss_modals(page)
        retry_result = click_apply_button(page)
        if retry_result == "easy_apply":
            page.wait_for_timeout(1500)
            if detect_easy_apply_modal(page):
                return "easy_apply"
        console.print("  [yellow]Easy Apply modal still not open after retry[/]")
        return "failed"

    # Not Easy Apply -- check for modal or try alternate URL
    has_modal = detect_easy_apply_modal(page)
    logger.info(f"Still on LinkedIn: url={page.url[:80]}, easy_apply_modal={has_modal}, "
                f"listing_url={listing_url}, apply_result={apply_result}")
    if has_modal:
        return "easy_apply"

    # We're on LinkedIn but NOT in an Easy Apply flow -- try alternate URL
    if listing_url and "linkedin.com" not in listing_url.lower():
        console.print(f"  [dim]Stuck on LinkedIn -- navigating to company page: {listing_url[:60]}[/]")
        page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass
        if "linkedin.com" not in page.url.lower():
            return "navigated"
        if detect_easy_apply_modal(page):
            return "easy_apply"
    else:
        console.print(f"  [dim]No alternate URL available (listing_url={listing_url})[/]")

    # Save debug screenshot before giving up
    try:
        debug_dir = Path("data/logs")
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / "debug_stuck_linkedin.png"
        page.screenshot(path=str(debug_path), full_page=True)
        console.print(f"  [dim]  Debug screenshot: {debug_path}[/]")
    except Exception:
        pass

    page_tabs = len(page.context.pages)
    console.print(f"  [yellow]Could not leave LinkedIn -- skipping "
                  f"(tabs={page_tabs}, url={page.url[:60]})[/]")
    return "failed"
