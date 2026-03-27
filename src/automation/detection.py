"""Page detection and navigation -- CAPTCHA, login, modals, and button clicking.

All selector-based checks are batched into single page.evaluate() calls
to minimize browser round-trips and eliminate per-selector waits.
"""

import logging

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from rich.console import Console

from .platforms.linkedin import dismiss_linkedin_modals
from .selectors import (
    APPLY_BUTTON_PW_SELECTORS,
    NEXT_BUTTON_PW_SELECTORS, NEXT_BUTTON_TEXTS,
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
    reason = page.evaluate("""() => {
        // Check if the page has visible form fields -- if so, the challenge
        // likely already passed and script-only detection would be a false positive
        const hasFormContent = (() => {
            const inputs = document.querySelectorAll(
                'input:not([type="hidden"]):not([type="submit"]), textarea, select'
            );
            let visibleCount = 0;
            for (const inp of inputs) {
                if (inp.offsetWidth > 0 && inp.offsetHeight > 0) visibleCount++;
                if (visibleCount >= 2) return true;
            }
            return false;
        })();

        // Challenge widget selectors — block if visible, skip if hidden with form content
        const challengeSelectors = [
            'iframe[src*="hcaptcha"]',
            '#captcha',
            'iframe[src*="challenges.cloudflare.com"]',
            '[class*="cf-turnstile"]', '#challenge-running', '#challenge-form'
        ];
        for (const sel of challengeSelectors) {
            const el = document.querySelector(sel);
            if (!el) continue;
            const isVisible = el.offsetWidth > 0 && el.offsetHeight > 0;
            if (isVisible) return 'challenge-visible:' + sel;
            if (!hasFormContent) return 'challenge-no-form:' + sel;
            // Hidden challenge widget + form content = likely passive/resolved, skip
        }

        // .g-recaptcha can be visible (blocking) or invisible (passive badge).
        // Only flag as blocking if it's a visible widget (has dimensions and is not invisible).
        // When form content exists, invisible reCAPTCHA is passive (e.g. Gem.com, Ashby).
        const gRecaptcha = document.querySelector('.g-recaptcha');
        if (gRecaptcha) {
            const dataSize = gRecaptcha.getAttribute('data-size');
            const isVisible = gRecaptcha.offsetWidth > 10 && gRecaptcha.offsetHeight > 10;
            if (dataSize === 'invisible' && hasFormContent) {
                // Invisible + form loaded = passive, skip
            } else if (!hasFormContent) {
                return 'g-recaptcha-no-form';
            } else if (isVisible) {
                return 'g-recaptcha-visible(w=' + gRecaptcha.offsetWidth + ',h=' + gRecaptcha.offsetHeight + ')';
            }
            // else: .g-recaptcha exists but is hidden/zero-size with form content = passive
        }

        // Known ATS domains with passive reCAPTCHA that isn't blocking (Ashby, Gem)
        const knownPassiveDomains = ['ashbyhq.com', 'gem.com'];
        const isKnownPassive = knownPassiveDomains.some(d => window.location.hostname.includes(d));

        // These selectors indicate reCAPTCHA/CAPTCHA presence but may be passive
        // (invisible reCAPTCHA badge, loaded-but-resolved scripts).
        // Only flag when no form content is visible AND not a known-passive domain.
        if (!hasFormContent && !isKnownPassive) {
            const passiveSelectors = [
                '.grecaptcha-badge',
                'iframe[src*="recaptcha"]',
                'iframe[title*="reCAPTCHA"]',
                '[class*="captcha"]'
            ];
            for (const sel of passiveSelectors) {
                if (document.querySelector(sel)) return 'passive:' + sel;
            }
        }

        // Script-only detection (invisible reCAPTCHA, pre-loaded Cloudflare, etc.)
        // Skip if the page already has form content -- the challenge was already passed
        // Also skip for known ATS domains with passive reCAPTCHA (Ashby, Gem)
        if (!hasFormContent && !isKnownPassive) {
            const scripts = document.querySelectorAll(
                'script[src*="recaptcha"], script[src*="hcaptcha"], script[src*="challenges.cloudflare.com"]'
            );
            if (scripts.length > 0) return 'scripts-only';
        }

        const body = (document.body?.textContent || '').toLowerCase().slice(0, 2000);
        const phrases = [
            'verify you are human', 'additional verification required',
            "please verify you're not a robot", 'checking your browser'
        ];
        if (phrases.some(p => body.includes(p))) return 'body-phrase';
        return null;
    }""")
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
            # 1. Click any visible submit/verify/apply button
            """() => {
                const btns = document.querySelectorAll('button, input[type="submit"], [role="button"], a.btn, a.button');
                for (const btn of btns) {
                    if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
                    const text = (btn.textContent || btn.value || '').toLowerCase().trim();
                    if (text.match(/^(submit|verify|continue|proceed|check|apply)/)) {
                        btn.click();
                        return 'clicked: ' + text.substring(0, 30);
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
            # 3. Greenhouse-specific: submit the application form or click Apply
            """() => {
                // Greenhouse uses #application_form or form with data-lakitu
                const ghForm = document.querySelector('#application_form, form[action*="applications"]');
                if (ghForm) {
                    const btn = ghForm.querySelector('input[type="submit"], button[type="submit"]');
                    if (btn) { btn.click(); return 'greenhouse submit btn'; }
                    ghForm.submit();
                    return 'greenhouse form.submit';
                }
                // Generic fallback
                const form = document.querySelector('form');
                if (form) { form.submit(); return 'fallback form.submit'; }
                return null;
            }""",
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
                result = page.evaluate(strategy)
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
    return page.evaluate("""() => {
        const url = window.location.href.toLowerCase();
        // Site-specific login URLs — these are definitive, no password field needed
        const sitePatterns = [
            'linkedin.com/signup', 'linkedin.com/login', 'linkedin.com/checkpoint',
            'linkedin.com/uas/login', 'indeed.com/account/login', 'indeed.com/auth',
            'glassdoor.com/member/auth', 'amazon.jobs/account/signin',
            'passport.amazon.jobs'
        ];
        if (sitePatterns.some(p => url.includes(p))) return true;

        // Generic URL patterns — require a password field to confirm it's a login page
        const genericPatterns = ['/login', '/signin', '/sign-in', '/auth/'];
        if (genericPatterns.some(p => url.includes(p))) {
            if (document.querySelector('input[type="password"]')) return true;
        }

        const body = (document.body?.textContent || '').toLowerCase().slice(0, 2000);
        const loginPhrases = [
            'sign in to continue', 'sign in to see who you already know',
            'join linkedin', 'join now', 'log in to indeed', 'create an account',
            'log in using your', 'log in to your account', 'sign in to your account',
            'enter your password'
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
        from .platforms.linkedin import click_linkedin_apply
        return click_linkedin_apply(page)


    # --- Non-LinkedIn: single JS call to find apply element ---
    result = page.evaluate("""() => {
        // Common apply button texts across ATS platforms
        const applyTexts = [
            'apply now', 'apply', 'apply for this job', 'apply for this position',
            "i'm interested", 'im interested', 'submit application', 'start application'
        ];

        // Check links first
        const links = document.querySelectorAll('a');
        for (const a of links) {
            if (a.offsetWidth === 0 || a.offsetHeight === 0) continue;
            const text = (a.textContent || '').trim().toLowerCase();
            if (applyTexts.some(t => text === t || text.startsWith(t)) && a.href && a.href.startsWith('http')) {
                return { type: 'link', href: a.href };
            }
        }

        // Check buttons
        const buttons = document.querySelectorAll(
            'button, [data-testid*="apply"], .apply-button, #apply-button, ' +
            '[data-testid*="interest"], .js-btn-apply'
        );
        for (const btn of buttons) {
            if (btn.offsetWidth === 0 || btn.offsetHeight === 0) continue;
            const text = (btn.textContent || '').trim().toLowerCase();
            if (applyTexts.some(t => text === t || text.includes(t))) {
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
            page.wait_for_load_state("networkidle", timeout=2000)
        except PlaywrightTimeoutError:
            pass
        return True

    # Button click with popup detection
    for selector in APPLY_BUTTON_PW_SELECTORS:
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
            logger.debug(f"Apply button selector failed: {e}")
            continue
    return False


def click_next_button(page) -> bool:
    """Try to find and click a Next/Continue button.

    Uses Playwright locators first (pierces shadow DOM for LinkedIn Easy Apply),
    then falls back to JS evaluation for non-shadow DOM cases.
    Returns True if found and clicked.
    """
    # -- Playwright locator approach (pierces shadow DOM) --
    # LinkedIn Easy Apply buttons have specific aria-labels
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
    clicked = page.evaluate("""() => {
        const modal = document.querySelector(
            '.jobs-easy-apply-modal, .jobs-easy-apply-content, ' +
            '[role="dialog"], .artdeco-modal'
        );
        const scope = (modal && modal.offsetWidth > 0) ? modal : document;
        if (scope === document) {
            window.scrollTo(0, document.body.scrollHeight);
        }

        const selectors = [
            'button[aria-label="Continue to next step"]',
            'button[aria-label="Next"]',
            'button[aria-label="Review your application"]',
            'button[aria-label="Review"]',
            'button[data-automation-id="bottom-navigation-next-button"]',
            '[data-testid*="next"]',
        ];
        for (const sel of selectors) {
            const btn = scope.querySelector(sel);
            if (btn && btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                btn.scrollIntoView({ block: 'center' });
                btn.click();
                return true;
            }
        }

        const textMatches = ['next', 'continue', 'review'];
        const buttons = scope.querySelectorAll('button, input[type="submit"], a');
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
    """Try to find and click the Submit/Apply button.

    Uses Playwright locators first (pierces shadow DOM for LinkedIn Easy Apply),
    then falls back to JS evaluation.
    Returns True if found and clicked.
    """
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
    clicked = page.evaluate("""() => {
        const modal = document.querySelector(
            '.jobs-easy-apply-modal, .jobs-easy-apply-content, ' +
            '[role="dialog"], .artdeco-modal'
        );
        const scope = (modal && modal.offsetWidth > 0) ? modal : document;
        if (scope === document) {
            window.scrollTo(0, document.body.scrollHeight);
        }

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
            const btn = scope.querySelector(sel);
            if (btn && btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                btn.scrollIntoView({ block: 'center' });
                btn.click();
                return true;
            }
        }

        const textMatches = [
            'submit application', 'submit', 'send application',
            'apply', 'complete', 'finish', 'done'
        ];
        const buttons = scope.querySelectorAll('button, input[type="submit"], a, [role="button"]');
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

        if (scope === document) window.scrollTo(0, 0);
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
    except Exception as e:
        logger.debug(f"Submit second-pass JS evaluation failed: {e}")

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
