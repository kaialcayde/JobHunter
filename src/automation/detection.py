"""Page detection and navigation -- CAPTCHA, login, modals, and button clicking."""

from rich.console import Console

from .platforms.linkedin import handle_share_profile, dismiss_linkedin_modals

console = Console(force_terminal=True)


def detect_captcha(page) -> bool:
    """Check if the page has a CAPTCHA or bot verification."""
    captcha_indicators = [
        'iframe[src*="recaptcha"]',
        'iframe[src*="hcaptcha"]',
        '.g-recaptcha',
        '#captcha',
        '[class*="captcha"]',
        'iframe[title*="reCAPTCHA"]',
        # Cloudflare Turnstile / challenge
        'iframe[src*="challenges.cloudflare.com"]',
        '[class*="cf-turnstile"]',
        '#challenge-running',
        '#challenge-form',
    ]
    for selector in captcha_indicators:
        if page.query_selector(selector):
            return True

    # Check page text for common verification messages
    body_text = (page.text_content("body") or "").lower()[:2000]
    if any(phrase in body_text for phrase in [
        "verify you are human",
        "additional verification required",
        "please verify you're not a robot",
        "checking your browser",
    ]):
        return True

    return False


def detect_login_page(page) -> bool:
    """Detect if we've landed on a login/signup page instead of an application form."""
    url = page.url.lower()

    # Known login/signup URL patterns
    login_patterns = [
        "linkedin.com/signup",
        "linkedin.com/login",
        "linkedin.com/checkpoint",
        "linkedin.com/uas/login",
        "indeed.com/account/login",
        "indeed.com/auth",
        "glassdoor.com/member/auth",
    ]
    if any(pattern in url for pattern in login_patterns):
        return True

    # Check page content for login indicators
    body_text = (page.text_content("body") or "").lower()[:2000]
    login_phrases = [
        "sign in to continue",
        "sign in to see who you already know",
        "join linkedin",
        "join now",
        "log in to indeed",
        "create an account",
    ]
    # Must match login phrase AND have a password field (to avoid false positives)
    if any(phrase in body_text for phrase in login_phrases):
        if page.query_selector('input[type="password"]'):
            return True

    return False


def dismiss_modals(page):
    """Try to close any modals or popups blocking the page.

    Delegates to platform-specific handlers for LinkedIn.
    """
    # LinkedIn-specific handling
    is_linkedin = "linkedin.com" in (page.url or "")
    if is_linkedin:
        handle_share_profile(page)
        dismiss_linkedin_modals(page)

    # Generic modal dismiss (works for all sites)
    modal_close_selectors = [
        'button[aria-label="Dismiss"]',
        'button[aria-label="Close"]',
        'button:has-text("Dismiss")',
        'button:has-text("Not now")',
        'button:has-text("No thanks")',
        'button:has-text("Skip")',
        '[data-test-modal-close-btn]',
        '.modal__dismiss',
        # Generic close buttons
        'button[class*="close"]',
        'button[class*="dismiss"]',
        '[aria-label="close"]',
    ]
    for selector in modal_close_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(500)
        except Exception:
            continue


def click_apply_button(page):
    """Try to find and click an 'Apply' button on a job listing page.

    Handles LinkedIn external apply links (opens new tab) and standard apply buttons.
    Returns True if clicked, "new_tab" if a new tab opened, False if not found.
    """
    # First dismiss any modals/popups (LinkedIn sign-in, etc.)
    dismiss_modals(page)

    # For LinkedIn: check if there's an external apply link (opens company's site)
    current_url = page.url
    if "linkedin.com" in current_url:
        # LinkedIn external apply buttons are <a> tags that open a new tab
        ext_apply = page.query_selector('a[href*="externalApply"], a.jobs-apply-button, a[data-tracking-control-name*="apply"]')
        if ext_apply:
            href = ext_apply.get_attribute("href")
            if href:
                console.print(f"  [dim]Following LinkedIn external apply link...[/]")
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                dismiss_modals(page)
                return True

    apply_selectors = [
        'button:has-text("Apply")',
        'a:has-text("Apply")',
        'button:has-text("Apply Now")',
        'a:has-text("Apply Now")',
        'button:has-text("Easy Apply")',
        '[data-testid*="apply"]',
        '.apply-button',
        '#apply-button',
    ]

    for selector in apply_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                # Check if it's a link that opens externally
                tag = btn.evaluate("el => el.tagName.toLowerCase()")
                href = btn.get_attribute("href") if tag == "a" else None

                if href and ("http" in href) and ("linkedin.com" not in href):
                    # External apply link -- navigate directly
                    console.print(f"  [dim]Following external apply link...[/]")
                    page.goto(href, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                    return True

                # Check if click opens a new page/tab
                pages_before = len(page.context.pages)
                btn.click()
                page.wait_for_timeout(2000)

                # If a new tab opened, switch to it
                if len(page.context.pages) > pages_before:
                    new_page = page.context.pages[-1]
                    console.print(f"  [dim]Switched to new tab: {new_page.url[:60]}...[/]")
                    return "new_tab"

                return True
        except Exception:
            continue
    return False


def click_next_button(page) -> bool:
    """Try to find and click a Next/Continue button. Returns True if found."""
    next_selectors = [
        # LinkedIn Easy Apply
        'button[aria-label="Continue to next step"]',
        'button[aria-label="Next"]',
        '.jobs-easy-apply-modal button:has-text("Next")',
        '.jobs-easy-apply-content button:has-text("Next")',
        'button:has-text("Review")',
        # Generic
        'button:has-text("Next")',
        'button:has-text("Continue")',
        'input[type="submit"][value*="Next"]',
        'input[type="submit"][value*="Continue"]',
        'a:has-text("Next")',
        '[data-testid*="next"]',
        # Workday
        'button[data-automation-id="bottom-navigation-next-button"]',
    ]

    # Scroll down to reveal buttons below the fold
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(500)

    for selector in next_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.scroll_into_view_if_needed()
                btn.click()
                return True
        except Exception:
            continue

    # Check iframes
    for frame in page.frames[1:]:
        for selector in next_selectors:
            try:
                btn = frame.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    return True
            except Exception:
                continue

    return False


def click_submit_button(page) -> bool:
    """Try to find and click the Submit/Apply button. Returns True if found."""
    submit_selectors = [
        # LinkedIn Easy Apply
        'button[aria-label="Submit application"]',
        'button[aria-label="Submit"]',
        '.jobs-easy-apply-modal button:has-text("Submit application")',
        '.jobs-easy-apply-content button:has-text("Submit application")',
        # Generic
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
        'button:has-text("Apply")',
        'button:has-text("Send Application")',
        'button:has-text("Complete")',
        'button:has-text("Finish")',
        'button:has-text("Done")',
        'input[type="submit"]',
        'button[type="submit"]',
        '[data-testid*="submit"]',
        '[data-testid*="apply"]',
        # Greenhouse
        '#submit_app', '#submit-application',
        'input[value="Submit Application"]',
        'input[value="Submit"]',
        # Lever
        '.posting-btn-submit',
        'button.postings-btn',
        # Workday
        'button[data-automation-id="bottom-navigation-next-button"]',
        'button[data-automation-id="submit"]',
        # iCIMS
        '.iCIMS_Button', 'button.btn-submit',
        # Generic fallbacks
        'a:has-text("Submit")',
        'a:has-text("Apply")',
        '[role="button"]:has-text("Submit")',
        '[class*="submit"]',
    ]

    # Scroll down to reveal submit buttons below the fold
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(500)

    # Search main page
    for selector in submit_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.scroll_into_view_if_needed()
                btn.click()
                return True
        except Exception:
            continue

    # Scroll back to top and try again (button could be at top)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    for selector in submit_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.scroll_into_view_if_needed()
                btn.click()
                return True
        except Exception:
            continue

    # Fall back to iframes (Greenhouse, Lever, etc. embed forms in iframes)
    for frame in page.frames[1:]:
        for selector in submit_selectors:
            try:
                btn = frame.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    return True
            except Exception:
                continue

    return False
