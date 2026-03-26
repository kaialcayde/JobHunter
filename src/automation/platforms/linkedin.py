"""LinkedIn-specific automation workarounds.

Handles LinkedIn modals (Share your profile, messaging overlays, notifications)
and Easy Apply modal detection.
"""

from rich.console import Console

console = Console(force_terminal=True)


def handle_share_profile(page) -> bool:
    """Detect and accept the 'Share your profile' modal on LinkedIn.

    Strategy: click the accept/share button rather than dismissing.
    Falls back to dismiss if accept button not found.
    Returns True if a modal was handled.
    """
    for attempt in range(3):
        handled = False

        # Look for the share profile modal via heading text
        try:
            share_modal = page.evaluate("""() => {
                const modals = document.querySelectorAll(
                    '[role="dialog"], .artdeco-modal, .share-profile-modal'
                );
                for (const modal of modals) {
                    const text = (modal.textContent || '').toLowerCase();
                    if (text.includes('share') && text.includes('profile')) {
                        return true;
                    }
                }
                return false;
            }""")
        except Exception:
            share_modal = False

        if not share_modal:
            return handled

        # Try to click the accept/share button (primary action)
        accept_selectors = [
            # Primary share/accept buttons
            '[role="dialog"] button:has-text("Share")',
            '.artdeco-modal button:has-text("Share")',
            '[role="dialog"] button:has-text("Yes")',
            '.artdeco-modal button:has-text("Yes")',
            '[role="dialog"] button:has-text("Send")',
            '.artdeco-modal button:has-text("Send")',
            # Primary action button (usually the share button)
            '[role="dialog"] button.artdeco-button--primary',
            '.artdeco-modal button.artdeco-button--primary',
        ]

        for selector in accept_selectors:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(500)
                    console.print("  [dim]Accepted LinkedIn 'Share your profile'[/]")
                    handled = True
                    break
            except Exception:
                continue

        if handled:
            # Verify modal is gone
            page.wait_for_timeout(500)
            try:
                still_open = page.evaluate("""() => {
                    const modals = document.querySelectorAll(
                        '[role="dialog"]:not([aria-hidden="true"]), '
                        '.artdeco-modal:not([aria-hidden="true"])'
                    );
                    for (const m of modals) {
                        const text = (m.textContent || '').toLowerCase();
                        if (text.includes('share') && text.includes('profile') &&
                            m.offsetWidth > 0 && m.offsetHeight > 0) {
                            return true;
                        }
                    }
                    return false;
                }""")
            except Exception:
                still_open = False

            if not still_open:
                return True
            continue

        # Fallback: try dismiss/close if accept button not found
        dismiss_selectors = [
            '[role="dialog"] button:has-text("No thanks")',
            '[role="dialog"] button:has-text("Not now")',
            '[role="dialog"] button:has-text("Dismiss")',
            '[role="dialog"] button:has-text("Skip")',
            '[role="dialog"] button[aria-label="Dismiss"]',
            '[role="dialog"] button[aria-label="Close"]',
            '.artdeco-modal button:has-text("No thanks")',
            '.artdeco-modal button:has-text("Not now")',
            '.artdeco-modal__dismiss',
        ]

        for selector in dismiss_selectors:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(500)
                    console.print("  [dim]Dismissed LinkedIn 'Share your profile'[/]")
                    handled = True
                    break
            except Exception:
                continue

        # JS fallback: find any modal with share+profile text and click first button
        if not handled:
            try:
                handled = page.evaluate("""() => {
                    const modals = document.querySelectorAll(
                        '[role="dialog"], .artdeco-modal'
                    );
                    for (const modal of modals) {
                        const text = (modal.textContent || '').toLowerCase();
                        if (text.includes('share') && text.includes('profile')) {
                            // Try primary button first, then any button
                            const primary = modal.querySelector(
                                'button.artdeco-button--primary'
                            );
                            if (primary) { primary.click(); return true; }
                            const dismiss = modal.querySelector(
                                'button[aria-label="Dismiss"], button[aria-label="Close"]'
                            );
                            if (dismiss) { dismiss.click(); return true; }
                        }
                    }
                    return false;
                }""")
                if handled:
                    page.wait_for_timeout(500)
            except Exception:
                pass

        if not handled:
            break

    return handled


def dismiss_linkedin_modals(page):
    """Handle LinkedIn-specific modals: messaging overlays, notifications, etc."""
    linkedin_selectors = [
        # Messaging overlay close
        'button.msg-overlay-bubble-header__control--new-convo-btn',
        'button[data-control-name="overlay.close_conversation_window"]',
        # Notification overlay
        'button:has-text("Not now")',
        # Collaborative articles
        '.artdeco-modal__actionbar button:has-text("Not now")',
        '.artdeco-modal__actionbar button:has-text("No thanks")',
        # Generic artdeco modal dismiss
        '.artdeco-modal__dismiss',
        'button[aria-label="Dismiss"]',
        'button[aria-label="Close"]',
    ]

    for selector in linkedin_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(500)
        except Exception:
            continue


def detect_easy_apply_modal(page) -> bool:
    """Check if a LinkedIn Easy Apply modal overlay is currently open."""
    selectors = [
        '.jobs-easy-apply-modal',
        '.jobs-easy-apply-content',
        '[role="dialog"][aria-label*="Easy Apply"]',
        '[role="dialog"] .jobs-easy-apply-form-element',
    ]
    for selector in selectors:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                return True
        except Exception:
            continue
    return False
