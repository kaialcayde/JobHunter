"""LinkedIn-specific automation workarounds.

Handles LinkedIn modals (Share your profile, messaging overlays, notifications)
and Easy Apply modal detection.
"""

from rich.console import Console

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
