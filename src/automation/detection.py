"""Page detection and navigation -- CAPTCHA, login, modals, and button clicking.

All selector-based checks are batched into single page.evaluate() calls
to minimize browser round-trips and eliminate per-selector waits.
"""

import logging

from rich.console import Console

from .platforms.linkedin import handle_share_profile, dismiss_linkedin_modals

logger = logging.getLogger(__name__)

console = Console(force_terminal=True)


def detect_captcha(page) -> bool:
    """Check if the page has a CAPTCHA or bot verification (single JS call)."""
    return page.evaluate("""() => {
        const selectors = [
            'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]',
            '.g-recaptcha', '#captcha', '[class*="captcha"]',
            'iframe[title*="reCAPTCHA"]',
            'iframe[src*="challenges.cloudflare.com"]',
            '[class*="cf-turnstile"]', '#challenge-running', '#challenge-form'
        ];
        for (const sel of selectors) {
            if (document.querySelector(sel)) return true;
        }
        const body = (document.body?.textContent || '').toLowerCase().slice(0, 2000);
        const phrases = [
            'verify you are human', 'additional verification required',
            "please verify you're not a robot", 'checking your browser'
        ];
        return phrases.some(p => body.includes(p));
    }""")


def try_solve_captcha(page, settings: dict) -> bool:
    """Attempt to solve a CAPTCHA if solving is enabled.

    Returns True if solved (page should be rechecked), False if not solved.
    """
    if not settings.get("automation", {}).get("captcha_solving", False):
        return False

    from .captcha_solver import solve_captcha
    solved = solve_captcha(page)
    if solved:
        # Wait for page to process the token and redirect/refresh
        page.wait_for_timeout(3000)
        if not detect_captcha(page):
            console.print("  [green]CAPTCHA solved![/]")
            return True

        # Token injected but page didn't auto-advance -- try multiple submit strategies
        submit_strategies = [
            # 1. Click any visible submit/verify button
            """() => {
                const btns = document.querySelectorAll('button, input[type="submit"], [role="button"]');
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').toLowerCase();
                    if (text.match(/submit|verify|continue|proceed|check/)) {
                        btn.click();
                        return 'clicked: ' + text.trim().substring(0, 30);
                    }
                }
                return null;
            }""",
            # 2. Submit the form containing the recaptcha response
            """() => {
                const ta = document.querySelector('[id*="g-recaptcha-response"]');
                if (ta) {
                    const form = ta.closest('form');
                    if (form) { form.submit(); return 'form.submit'; }
                }
                return null;
            }""",
            # 3. Submit any form on the page
            """() => {
                const form = document.querySelector('form');
                if (form) { form.submit(); return 'fallback form.submit'; }
                return null;
            }""",
            # 4. Click inside the reCAPTCHA iframe checkbox (triggers verification)
            None,  # handled separately below
        ]

        for strategy in submit_strategies:
            if strategy is None:
                # Try clicking the recaptcha checkbox iframe
                try:
                    frame = page.frame_locator('iframe[src*="recaptcha"]').first
                    frame.locator('#recaptcha-anchor').click(timeout=3000)
                except Exception:
                    continue
            else:
                result = page.evaluate(strategy)
                if not result:
                    continue

            page.wait_for_timeout(4000)
            if not detect_captcha(page):
                console.print("  [green]CAPTCHA solved![/]")
                return True

        console.print("  [yellow]CAPTCHA token injected but page unchanged[/]")
    return False


def detect_login_page(page) -> bool:
    """Detect if we've landed on a login/signup page (single JS call)."""
    return page.evaluate("""() => {
        const url = window.location.href.toLowerCase();
        const loginPatterns = [
            'linkedin.com/signup', 'linkedin.com/login', 'linkedin.com/checkpoint',
            'linkedin.com/uas/login', 'indeed.com/account/login', 'indeed.com/auth',
            'glassdoor.com/member/auth'
        ];
        if (loginPatterns.some(p => url.includes(p))) return true;

        const body = (document.body?.textContent || '').toLowerCase().slice(0, 2000);
        const loginPhrases = [
            'sign in to continue', 'sign in to see who you already know',
            'join linkedin', 'join now', 'log in to indeed', 'create an account'
        ];
        if (loginPhrases.some(p => body.includes(p))) {
            if (document.querySelector('input[type="password"]')) return true;
        }
        return false;
    }""")


def dismiss_modals(page):
    """Try to close any modals or popups blocking the page.

    Delegates to platform-specific handlers for LinkedIn, then does a single
    JS sweep for generic modals.
    """
    is_linkedin = "linkedin.com" in (page.url or "")
    if is_linkedin:
        handle_share_profile(page)
        dismiss_linkedin_modals(page)

    # Single JS call to dismiss generic modals
    page.evaluate("""() => {
        const selectors = [
            'button[aria-label="Dismiss"]', 'button[aria-label="Close"]',
            '[data-test-modal-close-btn]', '.modal__dismiss',
            'button[class*="close"]', 'button[class*="dismiss"]',
            '[aria-label="close"]'
        ];
        const textMatches = ['dismiss', 'not now', 'no thanks', 'skip'];

        for (const sel of selectors) {
            try {
                const btn = document.querySelector(sel);
                if (btn && btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                    btn.click();
                    return;
                }
            } catch(e) {}
        }

        // Text-based fallback
        const buttons = document.querySelectorAll('button');
        for (const btn of buttons) {
            if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
            const text = (btn.textContent || '').trim().toLowerCase();
            if (textMatches.some(m => text === m || text.startsWith(m))) {
                btn.click();
                return;
            }
        }
    }""")


def _click_linkedin_apply(page):
    """Handle LinkedIn Apply button -- Easy Apply or external redirect.

    LinkedIn lazy-loads the Apply button, so we wait for it. External apply
    buttons are <button> elements that open a new tab via JS, NOT <a> tags.
    Returns True if clicked, "new_tab" if a new tab opened, False if not found.
    """
    from .platforms.linkedin import dismiss_all_linkedin_modals

    # Dismiss any blocking modals FIRST (Share Profile, etc.)
    dismiss_all_linkedin_modals(page)

    # Wait for the apply button area to load (LinkedIn lazy-loads this)
    apply_btn = None
    apply_selectors = [
        # LinkedIn-specific classes (most reliable)
        '.jobs-apply-button',
        '.jobs-s-apply button',
        'button.jobs-apply-button',
        # Aria labels
        'button[aria-label*="Apply"]',
        'button[aria-label*="apply"]',
        # Data attributes
        '[data-tracking-control-name*="apply"]',
        # Text-based fallback
        'button:has-text("Easy Apply")',
        'button:has-text("Apply")',
    ]

    # Try each selector, giving the page a moment to render
    for attempt in range(3):
        for selector in apply_selectors:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    apply_btn = btn
                    break
            except Exception:
                continue
        if apply_btn:
            break
        # Button not found yet -- maybe a modal is still blocking, dismiss and retry
        if attempt == 0:
            page.wait_for_timeout(1000)
        elif attempt == 1:
            dismiss_all_linkedin_modals(page)
            page.wait_for_timeout(500)

    if not apply_btn:
        # Diagnostic: log what's actually on the page
        diag = page.evaluate("""() => {
            const dialogs = document.querySelectorAll('[role="dialog"], .artdeco-modal');
            const visibleDialogs = [];
            for (const d of dialogs) {
                if (d.offsetWidth > 0) visibleDialogs.push(d.textContent.trim().slice(0, 100));
            }
            const buttons = document.querySelectorAll('button');
            const visibleButtons = [];
            for (const b of buttons) {
                if (b.offsetWidth > 0) {
                    const t = b.textContent.trim().slice(0, 40);
                    if (t) visibleButtons.push(t);
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
        return True

    # External apply -- clicking opens a new tab or navigates away
    url_before = page.url
    try:
        with page.context.expect_page(timeout=3000) as popup_info:
            apply_btn.click()
        new_page = popup_info.value
        new_page.wait_for_load_state("domcontentloaded")
        try:
            new_page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        console.print(f"  [dim]External apply opened: {new_page.url[:80]}[/]")
        return "new_tab"
    except Exception:
        # No popup -- check if the page navigated or a new tab appeared silently
        page.wait_for_timeout(500)
        if page.url != url_before:
            console.print(f"  [dim]Navigated to: {page.url[:80]}[/]")
            return True
        if len(page.context.pages) > 1:
            latest = page.context.pages[-1]
            if latest != page and latest.url != "about:blank":
                console.print(f"  [dim]New tab detected: {latest.url[:80]}[/]")
                return "new_tab"

        # Button was clicked but nothing happened -- might be Easy Apply after all
        console.print("  [dim]Apply clicked but no navigation -- checking for modal...[/]")
        page.wait_for_timeout(500)
        # Check if an Easy Apply modal opened
        modal = page.query_selector('.jobs-easy-apply-modal, .jobs-easy-apply-content, [role="dialog"]')
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
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            return True

        console.print("  [dim]Apply button clicked but nothing happened[/]")
        return True  # Button was clicked, assume something happened


def click_apply_button(page):
    """Try to find and click an 'Apply' button on a job listing page.

    Handles LinkedIn external apply links (opens new tab) and standard apply buttons.
    Returns True if clicked, "new_tab" if a new tab opened, False if not found.
    """
    dismiss_modals(page)
    current_url = page.url
    on_linkedin = "linkedin.com" in current_url

    # --- LinkedIn path ---
    if on_linkedin:
        return _click_linkedin_apply(page)


    # --- Non-LinkedIn: single JS call to find apply element ---
    result = page.evaluate("""() => {
        // Check links first
        const links = document.querySelectorAll('a');
        for (const a of links) {
            if (a.offsetWidth === 0 || a.offsetHeight === 0) continue;
            const text = (a.textContent || '').trim().toLowerCase();
            if ((text === 'apply now' || text === 'apply') && a.href && a.href.startsWith('http')) {
                return { type: 'link', href: a.href };
            }
        }

        // Check buttons
        const buttons = document.querySelectorAll(
            'button, [data-testid*="apply"], .apply-button, #apply-button'
        );
        for (const btn of buttons) {
            if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
            const text = (btn.textContent || '').trim().toLowerCase();
            if (text.includes('apply')) {
                const tag = btn.tagName.toLowerCase();
                const href = tag === 'a' ? btn.href : null;
                if (href && href.startsWith('http')) return { type: 'link', href: href };
                return { type: 'button' };
            }
        }
        return null;
    }""")

    if not result:
        return False

    if result["type"] == "link":
        console.print(f"  [dim]Following apply link...[/]")
        page.goto(result["href"], wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        return True

    # Button click with popup detection
    apply_selectors = [
        'button:has-text("Apply Now")', 'button:has-text("Apply")',
        '[data-testid*="apply"]', '.apply-button', '#apply-button',
    ]
    for selector in apply_selectors:
        try:
            btn = page.query_selector(selector)
            if not btn or not btn.is_visible():
                continue

            url_before = page.url
            try:
                with page.context.expect_page(timeout=2000) as popup_info:
                    btn.click()
                new_page = popup_info.value
                new_page.wait_for_load_state("domcontentloaded")
                console.print(f"  [dim]Popup opened: {new_page.url[:80]}[/]")
                return "new_tab"
            except Exception:
                page.wait_for_timeout(300)
                if page.url != url_before:
                    return True
                if len(page.context.pages) > 1:
                    latest = page.context.pages[-1]
                    if latest != page and latest.url != "about:blank":
                        console.print(f"  [dim]New tab detected: {latest.url[:80]}[/]")
                        return "new_tab"
                return True
        except Exception:
            continue
    return False


def click_next_button(page) -> bool:
    """Try to find and click a Next/Continue button via single JS call.

    Returns True if found and clicked.
    """
    # Single JS call: scroll to bottom, find and click the button
    clicked = page.evaluate("""() => {
        window.scrollTo(0, document.body.scrollHeight);

        const selectors = [
            'button[aria-label="Continue to next step"]',
            'button[aria-label="Next"]',
            'button[data-automation-id="bottom-navigation-next-button"]',
            '[data-testid*="next"]',
        ];
        for (const sel of selectors) {
            const btn = document.querySelector(sel);
            if (btn && btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                btn.scrollIntoView({ block: 'center' });
                btn.click();
                return true;
            }
        }

        // Text-based search
        const textMatches = ['next', 'continue', 'review'];
        const buttons = document.querySelectorAll('button, input[type="submit"], a');
        for (const btn of buttons) {
            if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
            const text = (btn.textContent || btn.value || '').trim().toLowerCase();
            if (textMatches.some(m => text === m || text.startsWith(m + ' '))) {
                btn.scrollIntoView({ block: 'center' });
                btn.click();
                return true;
            }
        }
        return false;
    }""")

    if clicked:
        return True

    # Fallback: check iframes (some ATS embed forms)
    for frame in page.frames[1:]:
        try:
            clicked = frame.evaluate("""() => {
                const textMatches = ['next', 'continue', 'review'];
                const buttons = document.querySelectorAll('button, input[type="submit"], a');
                for (const btn of buttons) {
                    if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
                    const text = (btn.textContent || btn.value || '').trim().toLowerCase();
                    if (textMatches.some(m => text === m || text.startsWith(m + ' '))) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            if clicked:
                return True
        except Exception:
            continue

    return False


def click_submit_button(page) -> bool:
    """Try to find and click the Submit/Apply button via single JS call.

    Returns True if found and clicked.
    """
    clicked = page.evaluate("""() => {
        window.scrollTo(0, document.body.scrollHeight);

        // Priority selectors (exact matches first)
        const selectors = [
            'button[aria-label="Submit application"]', 'button[aria-label="Submit"]',
            '#submit_app', '#submit-application',
            'button[data-automation-id="submit"]',
            '.posting-btn-submit', 'button.postings-btn',
            '.iCIMS_Button', 'button.btn-submit',
            '[data-testid*="submit"]', '[data-testid*="apply"]',
            'input[type="submit"]', 'button[type="submit"]',
        ];
        for (const sel of selectors) {
            const btn = document.querySelector(sel);
            if (btn && btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                btn.scrollIntoView({ block: 'center' });
                btn.click();
                return true;
            }
        }

        // Text-based search (ordered by specificity)
        const textMatches = [
            'submit application', 'submit', 'send application',
            'apply', 'complete', 'finish', 'done'
        ];
        const buttons = document.querySelectorAll('button, input[type="submit"], a, [role="button"]');
        for (const match of textMatches) {
            for (const btn of buttons) {
                if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
                const text = (btn.textContent || btn.value || '').trim().toLowerCase();
                if (text === match || text.startsWith(match)) {
                    btn.scrollIntoView({ block: 'center' });
                    btn.click();
                    return true;
                }
            }
        }

        // Try top of page too
        window.scrollTo(0, 0);
        return false;
    }""")

    if clicked:
        return True

    # Second pass from top of page
    try:
        clicked = page.evaluate("""() => {
            const selectors = [
                'input[type="submit"]', 'button[type="submit"]',
                '[data-testid*="submit"]', '[class*="submit"]',
            ];
            for (const sel of selectors) {
                const btn = document.querySelector(sel);
                if (btn && btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                    btn.scrollIntoView({ block: 'center' });
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")
        if clicked:
            return True
    except Exception:
        pass

    # Fallback: check iframes
    for frame in page.frames[1:]:
        try:
            clicked = frame.evaluate("""() => {
                const textMatches = ['submit', 'apply', 'send application', 'complete'];
                const buttons = document.querySelectorAll('button, input[type="submit"], a, [role="button"]');
                for (const match of textMatches) {
                    for (const btn of buttons) {
                        if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
                        const text = (btn.textContent || btn.value || '').trim().toLowerCase();
                        if (text === match || text.startsWith(match)) {
                            btn.click();
                            return true;
                        }
                    }
                }
                return false;
            }""")
            if clicked:
                return True
        except Exception:
            continue

    return False
