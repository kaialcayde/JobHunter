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
                except Exception:
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
        except Exception:
            pass

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


def _handle_share_profile_modal(page):
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
    continue_selectors = [
        'button:has-text("Continue")',
        '[role="dialog"] button:has-text("Continue")',
        '.artdeco-modal button:has-text("Continue")',
        '[role="dialog"] a:has-text("Continue")',
        'a:has-text("Continue")',
    ]
    for selector in continue_selectors:
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
    from .platforms.linkedin import dismiss_all_linkedin_modals
    dismiss_all_linkedin_modals(page)
    return True


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
        # LinkedIn-specific classes (most reliable) -- match both <button> and <a>
        '.jobs-apply-button',
        '.jobs-s-apply button',
        '.jobs-s-apply a',
        'button.jobs-apply-button',
        'a.jobs-apply-button',
        # Aria labels -- both button and anchor
        'button[aria-label*="Apply"]',
        'a[aria-label*="Apply"]',
        'button[aria-label*="apply"]',
        'a[aria-label*="apply"]',
        # Data attributes
        '[data-tracking-control-name*="apply"]',
        # Text-based fallback -- both button and anchor
        'button:has-text("Easy Apply")',
        'button:has-text("Apply")',
        'a:has-text("Easy Apply")',
        'a:has-text("Apply")',
    ]

    # Wait for the Apply button area to render (LinkedIn lazy-loads job content)
    # Single wait on the broadest selector, then check all selectors
    try:
        page.wait_for_selector('.jobs-apply-button, .jobs-s-apply, [data-tracking-control-name*="apply"]',
                               state="visible", timeout=5000)
    except Exception:
        pass  # Button may still exist under a different selector

    for selector in apply_selectors:
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
        for selector in apply_selectors:
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
            from pathlib import Path
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
    share_continue = _handle_share_profile_modal(page)
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
        from pathlib import Path
        debug_dir = Path("data/logs")
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / "debug_apply_no_effect.png"
        page.screenshot(path=str(debug_path), full_page=True)
        console.print(f"  [dim]  Debug screenshot: {debug_path}[/]")
    except Exception:
        pass
    return False  # Let caller try _force_apply_click fallback


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
        except Exception:
            pass
        return True

    # Button click with popup detection
    apply_selectors = [
        'button:has-text("Apply Now")', 'button:has-text("Apply")',
        "button:has-text(\"I'm interested\")", 'button:has-text("Start application")',
        '[data-testid*="apply"]', '.apply-button', '#apply-button',
        '.js-btn-apply', '[data-testid*="interest"]',
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
    """Try to find and click a Next/Continue button.

    Uses Playwright locators first (pierces shadow DOM for LinkedIn Easy Apply),
    then falls back to JS evaluation for non-shadow DOM cases.
    Returns True if found and clicked.
    """
    # -- Playwright locator approach (pierces shadow DOM) --
    # LinkedIn Easy Apply buttons have specific aria-labels
    pw_selectors = [
        'button[aria-label="Continue to next step"]',
        'button[aria-label="Next"]',
        'button[aria-label="Review your application"]',
        'button[aria-label="Review"]',
        'button[data-automation-id="bottom-navigation-next-button"]',
    ]
    for sel in pw_selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=500):
                loc.click(timeout=3000)
                return True
        except Exception:
            continue

    # Text-based Playwright locators (also pierce shadow DOM)
    for text in ['Next', 'Continue', 'Review']:
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
    pw_selectors = [
        'button[aria-label="Submit application"]',
        'button[aria-label="Submit"]',
        '#submit_app', '#submit-application',
        'button[data-automation-id="submit"]',
        'input[type="submit"]', 'button[type="submit"]',
    ]
    for sel in pw_selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=500):
                loc.click(timeout=3000)
                return True
        except Exception:
            continue

    # Text-based Playwright locators
    for text in ['Submit application', 'Submit', 'Send application', 'Apply', 'Complete', 'Done']:
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
