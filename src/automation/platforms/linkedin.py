"""LinkedIn-specific automation workarounds.

Handles LinkedIn modals (Share your profile, messaging overlays, notifications)
and Easy Apply modal detection.
"""

from rich.console import Console

console = Console(force_terminal=True)


def _has_blocking_modal(page) -> bool:
    """Check if there's any visible modal/dialog blocking the page (not Easy Apply)."""
    return page.evaluate("""() => {
        const dialogs = document.querySelectorAll('[role="dialog"], .artdeco-modal');
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

    Strategy order:
    1. Press Escape (works on ALL LinkedIn modals)
    2. Try JS click on dismiss/close buttons
    3. Try clicking the overlay background

    Returns True if a modal was dismissed.
    """
    if not _has_blocking_modal(page):
        return False

    # Strategy 1: Press Escape -- most reliable, works on all LinkedIn modals
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    if not _has_blocking_modal(page):
        console.print("  [dim]Dismissed LinkedIn modal (Escape)[/]")
        return True

    # Strategy 2: Brute-force JS -- find ANY visible modal and click dismiss elements
    dismissed = page.evaluate("""() => {
        const dialogs = document.querySelectorAll('[role="dialog"], .artdeco-modal');
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

    # Strategy 3: Escape again with a click on body first (focus the page)
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
    """Check if a LinkedIn Easy Apply modal overlay is currently open."""
    return page.evaluate("""() => {
        const selectors = [
            '.jobs-easy-apply-modal', '.jobs-easy-apply-content',
            '[role="dialog"][aria-label*="Easy Apply"]',
            '[role="dialog"] .jobs-easy-apply-form-element'
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el && el.offsetWidth > 0 && el.offsetHeight > 0) return true;
        }
        return false;
    }""")
