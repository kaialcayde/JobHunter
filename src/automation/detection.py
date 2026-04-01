"""Page detection and navigation -- CAPTCHA, login, modals, and button clicking.

Most browser-context DOM logic is delegated to `automation/browser_scripts/`
and evaluated as cached JS assets to minimize browser round-trips.
"""

import logging

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from rich.console import Console

from .browser_scripts import evaluate_script
from .platforms.linkedin import dismiss_linkedin_modals
from .selectors import (
    APPLY_BUTTON_PW_SELECTORS, APPLY_BUTTON_TEXTS,
    CAPTCHA_BODY_PHRASES, CAPTCHA_CHALLENGE_SELECTORS,
    CAPTCHA_KNOWN_PASSIVE_DOMAINS, CAPTCHA_PASSIVE_SELECTORS,
    LINKEDIN_MODAL_SCOPE_SELECTORS,
    LOGIN_BODY_PHRASES, LOGIN_GENERIC_URL_PATTERNS, LOGIN_SITE_PATTERNS,
    MODAL_DISMISS_SELECTORS, MODAL_DISMISS_TEXTS,
    NEXT_BUTTON_JS_SELECTORS, NEXT_BUTTON_JS_TEXTS,
    NEXT_BUTTON_PW_SELECTORS, NEXT_BUTTON_TEXTS,
    SUBMIT_BUTTON_JS_SELECTORS, SUBMIT_BUTTON_JS_TEXTS,
    SUBMIT_BUTTON_PW_SELECTORS, SUBMIT_BUTTON_TEXTS,
)

logger = logging.getLogger(__name__)

console = Console(force_terminal=True)


def detect_captcha(page) -> bool:
    """Check if the page has a CAPTCHA or bot verification (single JS call).

    Detects both visible CAPTCHA widgets and invisible reCAPTCHA (badge-only)
    which gates actions like clicking Apply on Paylocity and similar ATS sites.

    Avoids false positives when CAPTCHA scripts are loaded but the challenge
    has already auto-resolved (e.g. Ashby loads Cloudflare scripts always).
    """
    reason = evaluate_script(
        page,
        "detection/detect_captcha.js",
        {
            "challengeSelectors": CAPTCHA_CHALLENGE_SELECTORS,
            "passiveSelectors": CAPTCHA_PASSIVE_SELECTORS,
            "knownPassiveDomains": CAPTCHA_KNOWN_PASSIVE_DOMAINS,
            "bodyPhrases": CAPTCHA_BODY_PHRASES,
        },
    )
    if reason:
        console.print(f"  [dim]CAPTCHA trigger: {reason}[/]")
    return bool(reason)


def try_solve_captcha(page, settings: dict) -> bool:
    """Attempt to solve a CAPTCHA if solving is enabled.

    Always tries waiting for Cloudflare auto-challenges first (no API key needed).
    Returns True if solved (page should be rechecked), False if not solved.
    """
    # Always try auto-challenge resolution (no API key or settings needed)
    from .captcha_solver import _wait_for_cloudflare_auto_challenge
    if _wait_for_cloudflare_auto_challenge(page):
        console.print("  [green]Cloudflare challenge resolved automatically[/]")
        return True

    if not settings.get("automation", {}).get("captcha_solving", False):
        return False

    from .captcha_solver import solve_captcha
    solved = solve_captcha(page)
    if solved:
        # Wait for page to process the token and redirect/refresh
        page.wait_for_timeout(2000)
        if not detect_captcha(page):
            console.print("  [green]CAPTCHA solved![/]")
            return True

        # Token injected but page didn't auto-advance -- try multiple submit strategies
        submit_strategies = [
            "detection/captcha_submit_click.js",
            "detection/captcha_submit_form.js",
            "detection/captcha_submit_greenhouse.js",
            # 4. Click inside the reCAPTCHA iframe checkbox (triggers verification)
            None,  # handled separately below
        ]

        for i, strategy in enumerate(submit_strategies):
            if strategy is None:
                # Try clicking the recaptcha checkbox iframe
                try:
                    frame = page.frame_locator('iframe[src*="recaptcha"]').first
                    frame.locator('#recaptcha-anchor').click(timeout=3000)
                    logger.info(f"CAPTCHA submit strategy {i+1}: clicked recaptcha checkbox")
                except Exception as e:
                    logger.debug(f"CAPTCHA strategy {i+1} (recaptcha checkbox) failed: {e}")
                    continue
            else:
                result = evaluate_script(page, strategy)
                if not result:
                    continue
                logger.info(f"CAPTCHA submit strategy {i+1}: {result}")

            page.wait_for_timeout(2000)
            if not detect_captcha(page):
                console.print("  [green]CAPTCHA solved![/]")
                return True

        # Last resort: reload the page (some sites check CAPTCHA server-side on next load)
        logger.info("All submit strategies failed -- trying page reload")
        try:
            page.reload(wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)
            if not detect_captcha(page):
                console.print("  [green]CAPTCHA solved after reload![/]")
                return True
        except Exception as e:
            logger.warning(f"Reload after CAPTCHA failed: {e}")

        # Save debug screenshot so we can see what the CAPTCHA page looks like
        try:
            from pathlib import Path
            debug_dir = Path("data/logs")
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / "debug_captcha_unsolved.png"
            page.screenshot(path=str(debug_path), full_page=True)
            console.print(f"  [dim]Debug screenshot: {debug_path}[/]")
        except Exception as e:
            logger.debug(f"CAPTCHA debug screenshot failed: {e}")

        console.print("  [yellow]CAPTCHA token injected but page unchanged[/]")
    return False


def detect_login_page(page) -> bool:
    """Detect if we've landed on a login/signup page (single JS call)."""
    return evaluate_script(
        page,
        "detection/detect_login_page.js",
        {
            "sitePatterns": LOGIN_SITE_PATTERNS,
            "genericPatterns": LOGIN_GENERIC_URL_PATTERNS,
            "loginPhrases": LOGIN_BODY_PHRASES,
        },
    )


def dismiss_modals(page):
    """Try to close any modals or popups blocking the page.

    Delegates to platform-specific handlers for LinkedIn, then does a single
    JS sweep for generic modals.
    """
    is_linkedin = "linkedin.com" in (page.url or "")
    if is_linkedin:
        dismiss_linkedin_modals(page)

    evaluate_script(
        page,
        "detection/dismiss_generic_modals.js",
        {"selectors": MODAL_DISMISS_SELECTORS, "textMatches": MODAL_DISMISS_TEXTS},
    )


def _click_with_popup_detection(page, element):
    """Click an element and detect if it opens a new tab/popup.

    Returns True if clicked (same tab), "new_tab" if a popup opened, False on error.
    """
    url_before = page.url
    try:
        with page.context.expect_page(timeout=2000) as popup_info:
            element.click()
        new_page = popup_info.value
        new_page.wait_for_load_state("domcontentloaded")
        console.print(f"  [dim]Popup opened: {new_page.url[:80]}[/]")
        return "new_tab"
    except PlaywrightTimeoutError:
        page.wait_for_timeout(300)
        if page.url != url_before:
            return True
        if len(page.context.pages) > 1:
            latest = page.context.pages[-1]
            if latest != page and latest.url != "about:blank":
                console.print(f"  [dim]New tab detected: {latest.url[:80]}[/]")
                return "new_tab"
        return True
    except Exception as e:
        logger.debug(f"Click with popup detection failed: {e}")
        return False


def click_apply_button(page, finder=None):
    """Try to find and click an 'Apply' button on a job listing page.

    Handles LinkedIn external apply links (opens new tab) and standard apply buttons.
    When an ElementFinder is provided, it replaces the PW selector loop for button finding.
    Returns True if clicked, "new_tab" if a new tab opened, False if not found.
    """
    dismiss_modals(page)
    current_url = page.url
    on_linkedin = "linkedin.com" in current_url

    # --- LinkedIn path ---
    if on_linkedin:
        from .platforms.linkedin import click_linkedin_apply
        return click_linkedin_apply(page)

    # --- Non-LinkedIn: single JS call to find apply element ---
    result = evaluate_script(
        page,
        "detection/find_apply_target.js",
        {"applyTexts": APPLY_BUTTON_TEXTS},
    )

    if result and result["type"] == "link":
        console.print(f"  [dim]Following apply link...[/]")
        page.goto(result["href"], wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except PlaywrightTimeoutError:
            pass
        return True

    # --- Find and click the apply button ---

    # Try ElementFinder first (escalation pipeline with caching)
    if finder:
        from .page_checks import get_site_domain
        domain = get_site_domain(page.url)
        el_result = finder.find_element(page, "apply_button", domain)
        if el_result and el_result.element:
            click_result = _click_with_popup_detection(page, el_result.element)
            if click_result:
                return click_result

    # Fallback: original PW selector loop (when no finder or finder failed)
    for selector in APPLY_BUTTON_PW_SELECTORS:
        try:
            btn = page.query_selector(selector)
            if not btn or not btn.is_visible():
                continue
            click_result = _click_with_popup_detection(page, btn)
            if click_result:
                return click_result
        except Exception as e:
            logger.debug(f"Apply button selector failed: {e}")
            continue

    return False


def click_next_button(page, finder=None) -> bool:
    """Try to find and click a Next/Continue button.

    When an ElementFinder is provided, uses the escalation pipeline (cache -> heuristic
    -> role -> text). Otherwise falls back to PW locators + JS evaluation.
    Returns True if found and clicked.
    """
    # --- ElementFinder path (levels 1-4 cover PW + JS logic) ---
    if finder:
        from .page_checks import get_site_domain
        domain = get_site_domain(page.url)
        result = finder.find_element(page, "next_button", domain)
        if result and result.element:
            try:
                result.element.scroll_into_view_if_needed(timeout=1000)
                result.element.click(timeout=3000)
                return True
            except Exception as e:
                logger.debug(f"ElementFinder next_button click failed: {e}")
    else:
        # -- Playwright locator approach (pierces shadow DOM) --
        for sel in NEXT_BUTTON_PW_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.click(timeout=3000)
                    return True
            except Exception:
                continue

        # Text-based Playwright locators (also pierce shadow DOM)
        for text in NEXT_BUTTON_TEXTS:
            try:
                loc = page.get_by_role("button", name=text, exact=False).first
                if loc.is_visible(timeout=500):
                    loc.click(timeout=3000)
                    return True
            except Exception:
                continue

        # -- JS fallback for non-shadow-DOM cases --
        clicked = evaluate_script(
            page,
            "detection/click_scoped_button.js",
            {
                "modalSelectors": LINKEDIN_MODAL_SCOPE_SELECTORS,
                "selectors": NEXT_BUTTON_JS_SELECTORS,
                "textMatches": NEXT_BUTTON_JS_TEXTS,
                "buttonSelector": 'button, input[type="submit"], a',
                "scrollToBottom": True,
                "scrollToTopOnMiss": False,
            },
        )

        if clicked:
            return True

    # Fallback: check iframes (some ATS embed forms)
    for frame in page.frames[1:]:
        try:
            clicked = evaluate_script(
                frame,
                "detection/click_text_button.js",
                {
                    "textMatches": NEXT_BUTTON_JS_TEXTS,
                    "buttonSelector": 'button, input[type="submit"], a',
                },
            )
            if clicked:
                return True
        except Exception:
            continue

    return False


def click_submit_button(page, finder=None) -> bool:
    """Try to find and click the Submit/Apply button.

    When an ElementFinder is provided, uses the escalation pipeline (cache -> heuristic
    -> role -> text). Otherwise falls back to PW locators + JS evaluation.
    Returns True if found and clicked.
    """
    # --- ElementFinder path ---
    if finder:
        from .page_checks import get_site_domain
        domain = get_site_domain(page.url)
        result = finder.find_element(page, "submit_button", domain)
        if result and result.element:
            try:
                result.element.scroll_into_view_if_needed(timeout=1000)
                result.element.click(timeout=3000)
                return True
            except Exception as e:
                logger.debug(f"ElementFinder submit_button click failed: {e}")
    else:
        # -- Playwright locator approach (pierces shadow DOM) --
        for sel in SUBMIT_BUTTON_PW_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.click(timeout=3000)
                    return True
            except Exception:
                continue

        # Text-based Playwright locators
        for text in SUBMIT_BUTTON_TEXTS:
            try:
                loc = page.get_by_role("button", name=text, exact=False).first
                if loc.is_visible(timeout=500):
                    loc.click(timeout=3000)
                    return True
            except Exception:
                continue

        # -- JS fallback for non-shadow-DOM cases --
        clicked = evaluate_script(
            page,
            "detection/click_scoped_button.js",
            {
                "modalSelectors": LINKEDIN_MODAL_SCOPE_SELECTORS,
                "selectors": SUBMIT_BUTTON_JS_SELECTORS,
                "textMatches": SUBMIT_BUTTON_JS_TEXTS,
                "buttonSelector": 'button, input[type="submit"], a, [role="button"]',
                "scrollToBottom": True,
                "scrollToTopOnMiss": True,
            },
        )

        if clicked:
            return True

        # Second pass from top of page
        try:
            clicked = evaluate_script(
                page,
                "detection/click_scoped_button.js",
                {
                    "modalSelectors": None,
                    "selectors": [
                        'input[type="submit"]', 'button[type="submit"]',
                        '[data-testid*="submit"]', '[class*="submit"]',
                    ],
                    "textMatches": [],
                    "buttonSelector": 'button, input[type="submit"], a, [role="button"]',
                    "scrollToBottom": False,
                    "scrollToTopOnMiss": False,
                },
            )
            if clicked:
                return True
        except Exception as e:
            logger.debug(f"Submit second-pass JS evaluation failed: {e}")

    # Fallback: check iframes
    for frame in page.frames[1:]:
        try:
            clicked = evaluate_script(
                frame,
                "detection/click_text_button.js",
                {
                    "textMatches": ['submit', 'apply', 'send application', 'complete'],
                    "buttonSelector": 'button, input[type="submit"], a, [role="button"]',
                },
            )
            if clicked:
                return True
        except Exception:
            continue

    return False
