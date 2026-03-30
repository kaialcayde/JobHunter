"""Registration-specific handler functions for the kernel.

These handlers are stateless workers. Only the kernel advances state.
All functions return StepResult; they never modify workflow state directly.
"""

import logging

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from rich.console import Console

from ..db import log_action
from .results import HandlerResult, StepResult

logger = logging.getLogger(__name__)
console = Console(force_terminal=True)


# ------------------------------------------------------------------
# Auth-type detection
# ------------------------------------------------------------------

def handle_detect_auth_type(page, url: str, settings: dict, account_registry=None) -> StepResult:
    """Determine if the current page is a login wall or a registration wall.

    Called when NAVIGATE returns REQUIRES_LOGIN. Differentiates:
    - Registration wall: "Create Account", "Sign Up", confirm-password field
    - Login wall: standard sign-in form (attempts to navigate to registration page)

    Only attempts registration if auto_register is enabled AND domain is
    on the auto_register_domains allowlist.
    """
    from urllib.parse import urlparse
    from .account_registry import is_auto_register_allowed
    from .page_checks import get_site_domain, detect_registration_wall

    auto_register = settings.get("automation", {}).get("auto_register", False)
    if not auto_register:
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            message="auto_register is disabled"
        )

    # Use full hostname for pattern matching (*.avature.net won't match collapsed avature.net)
    hostname = urlparse(page.url).hostname or ""
    domain = get_site_domain(page.url)  # used as display/log key

    if not is_auto_register_allowed(hostname, settings):
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            message=f"Domain {hostname} not in auto_register_domains allowlist"
        )

    # Check for existing account first -- try login before attempting registration
    if account_registry is not None and account_registry.has_account(hostname):
        console.print(f"  [cyan]Existing account found for {domain} -- attempting registry login[/]")
        return StepResult(
            result=HandlerResult.REQUIRES_EXISTING_LOGIN,
            metadata={"domain": hostname},
            message=f"Existing account found for {domain} -- attempting login"
        )

    # Try to navigate to the registration/application form.
    # Many ATS sites show a combined "Login OR Create Account" page. Always try to click
    # a Create Account / Sign Up link first so we land on the actual form before deciding
    # whether it's a registration form or a direct application form (e.g. Avature's
    # "CREATE PROFILE" drops straight into the multi-step application, no separate
    # account creation step + email verification).
    REGISTER_LINK_TEXTS = [
        "Create Profile", "Create Account", "Create an Account",
        "Sign Up", "Register", "New User", "Join", "Get Started",
    ]
    for text in REGISTER_LINK_TEXTS:
        for role in ("link", "button"):
            try:
                el = page.get_by_role(role, name=text, exact=False).first
                if el.is_visible(timeout=500):
                    el.click()
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except PlaywrightTimeoutError:
                        pass
                    # Check application form FIRST -- it's more specific than registration wall.
                    # "Or Sign if you are already registered" contains "register" as a substring
                    # which can falsely trigger detect_registration_wall on application pages.
                    if _is_application_form(page):
                        console.print(f"  [cyan]Navigated to application form on {domain} -- handing off to fill agent[/]")
                        return StepResult(
                            result=HandlerResult.SUCCESS,
                            metadata={"domain": hostname},
                            message=f"Navigated to application form on {domain}"
                        )
                    if detect_registration_wall(page):
                        console.print(f"  [cyan]Navigated to registration page on {domain}[/]")
                        return StepResult(
                            result=HandlerResult.REQUIRES_REGISTRATION,
                            metadata={"domain": hostname},
                            message=f"Navigated to registration page on {domain}"
                        )
                    # Clicked but didn't land on a recognizable form -- keep searching
            except Exception:
                continue

    # No navigation link found -- check if the current page IS already a registration form
    if detect_registration_wall(page):
        console.print(f"  [cyan]Registration wall detected on {domain} -- attempting auto-registration[/]")
        return StepResult(
            result=HandlerResult.REQUIRES_REGISTRATION,
            metadata={"domain": hostname},
            message=f"Registration wall detected on {domain}"
        )

    return StepResult(result=HandlerResult.REQUIRES_LOGIN, message=f"Login wall on {domain} (not a registration wall)")


# ------------------------------------------------------------------
# Registration form fill
# ------------------------------------------------------------------

def _is_application_form(page) -> bool:
    """Detect if the page is an application form (resume upload, personal info steps, etc.)
    rather than an account registration form (email + password + confirm-password).

    Used to distinguish ATS platforms like Avature that combine profile creation with
    the application itself — no separate account registration + email verification needed.
    """
    try:
        return page.evaluate("""() => {
            const text = (document.body?.innerText || '').toLowerCase();
            const hasFileUpload = !!document.querySelector('input[type="file"]');
            const appSignals = ['upload your resume', 'upload resume', 'attach resume',
                                'personal information', 'select your resume',
                                'finalize application', 'work experience'];
            const hasAppText = appSignals.some(s => text.includes(s));
            const pwFields = document.querySelectorAll('input[type="password"]');
            const isRegistrationForm = pwFields.length >= 2;  // confirm-password = registration
            return (hasFileUpload || hasAppText) && !isRegistrationForm;
        }""")
    except Exception:
        return False


def handle_login_registry(page, domain: str, settings: dict, finder,
                          account_registry, conn, app_id: int, job_id: int) -> StepResult:
    """Log in to an ATS tenant portal using stored registry credentials.

    Called when DETECT_AUTH_TYPE returns REQUIRES_EXISTING_LOGIN (account exists
    in registry). Fills email + password and submits the login form.

    Returns:
        SUCCESS  -- login succeeded (kernel retries NAVIGATE)
        FAILED   -- login failed (kernel routes to REGISTER to re-register)
    """
    from ..db import log_action

    log_action(conn, "login_registry_start", f"Attempting registry login on {domain}", app_id, job_id)

    creds = account_registry.get_credentials(domain)
    if not creds:
        logger.warning(f"handle_login_registry: no credentials for {domain}")
        return StepResult(
            result=HandlerResult.FAILED,
            metadata={"domain": domain},
            message=f"No credentials found for {domain}"
        )

    if finder:
        email_el = finder.find_element(page, "email_field", domain)
        if email_el:
            try:
                email_el.element.fill(creds["email"])
            except Exception as e:
                logger.debug(f"handle_login_registry: email fill failed: {e}")

        pw_el = finder.find_element(page, "password_field", domain)
        if pw_el:
            try:
                account_registry.fill_credential(page, pw_el.selector_used, "password", domain)
            except Exception as e:
                logger.debug(f"handle_login_registry: password fill failed: {e}")

        submit_el = finder.find_element(page, "submit_button", domain)
        if submit_el:
            try:
                submit_el.element.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except PlaywrightTimeoutError:
                    pass
            except Exception as e:
                logger.debug(f"handle_login_registry: submit click failed: {e}")

    # Check if login succeeded (no longer on login page)
    from .detection import detect_login_page
    if not detect_login_page(page):
        account_registry.mark_active(domain)
        log_action(conn, "login_registry_success", f"Registry login succeeded on {domain}", app_id, job_id)
        console.print(f"  [green]Registry login succeeded on {domain}[/]")
        return StepResult(
            result=HandlerResult.SUCCESS,
            metadata={"domain": domain},
            message=f"Registry login succeeded on {domain}"
        )

    # Login failed -- kernel routes to REGISTER via FAILED transition
    console.print(f"  [yellow]Registry login failed on {domain} -- will attempt re-registration[/]")
    log_action(conn, "login_registry_failed", f"Registry login failed on {domain}", app_id, job_id)
    try:
        page.screenshot(path=f"data/logs/debug_login_registry_failed_{domain}.png")
    except Exception:
        pass
    return StepResult(
        result=HandlerResult.FAILED,
        metadata={"domain": domain},
        message=f"Registry login failed on {domain}"
    )


def handle_register(page, domain: str, settings: dict, finder,
                    account_registry, conn, app_id: int, job_id: int) -> StepResult:
    """Fill and submit an ATS registration form.

    Steps:
    1. Generate + store credentials FIRST (password never lost)
    2. Fill name fields from profile
    3. Fill email (plus-alias)
    4. Fill password + confirm password (via secure fill, never exposed)
    5. Submit
    """
    from ..config.loader import load_profile
    from .account_registry import detect_ats_platform, extract_tenant

    platform = detect_ats_platform(domain)
    tenant = extract_tenant(domain, platform)

    # Step 1: reuse existing credentials if present (e.g. fill_vision status from a prior partial run),
    # otherwise generate fresh ones. This ensures the same password is used across retries.
    existing = account_registry.get_credentials(domain)
    if existing:
        creds = existing
        logger.debug(f"handle_register: reusing existing credentials for {domain}")
        log_action(conn, "register_start", f"Registering on {domain} (reusing credentials)", app_id, job_id)
    else:
        use_alias = settings.get("automation", {}).get("use_email_aliases", False)
        creds = account_registry.generate_credentials(domain, tenant=tenant, platform=platform, use_alias=use_alias)
        log_action(conn, "register_start", f"Registering on {domain} (platform={platform})", app_id, job_id)

    # Step 2: load profile for name fields
    try:
        profile_data = load_profile()
        personal = profile_data.get("personal", {})
    except Exception as e:
        logger.warning(f"handle_register: could not load profile: {e}")
        personal = {}

    # Fill fields via ElementFinder if available
    if finder:
        first_el = finder.find_element(page, "first_name_field", domain)
        if first_el:
            try:
                first_el.element.fill(personal.get("first_name", ""))
            except Exception as e:
                logger.debug(f"handle_register: first_name fill failed: {e}")

        last_el = finder.find_element(page, "last_name_field", domain)
        if last_el:
            try:
                last_el.element.fill(personal.get("last_name", ""))
            except Exception as e:
                logger.debug(f"handle_register: last_name fill failed: {e}")

        email_el = finder.find_element(page, "email_field", domain)
        if email_el:
            try:
                email_el.element.fill(creds["email"])
            except Exception as e:
                logger.debug(f"handle_register: email fill failed: {e}")

        pw_el = finder.find_element(page, "password_field", domain)
        if pw_el:
            try:
                account_registry.fill_credential(page, pw_el.selector_used, "password", domain)
            except Exception as e:
                logger.debug(f"handle_register: password fill failed: {e}")

        confirm_el = finder.find_element(page, "confirm_password_field", domain)
        if confirm_el:
            try:
                account_registry.fill_credential(page, confirm_el.selector_used, "password", domain)
            except Exception as e:
                logger.debug(f"handle_register: confirm_password fill failed: {e}")

        submit_el = finder.find_element(page, "submit_button", domain)
        if submit_el:
            try:
                submit_el.element.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except PlaywrightTimeoutError:
                    pass
                log_action(conn, "register_submitted", f"Registration submitted on {domain}", app_id, job_id)
                console.print(f"  [cyan]Registration submitted on {domain}[/]")
                return StepResult(
                    result=HandlerResult.SUCCESS,
                    metadata={"domain": domain},
                    message=f"Registration submitted on {domain}"
                )
            except Exception as e:
                logger.debug(f"handle_register: submit click failed: {e}")

    account_registry.mark_failed(domain, "no_submit_button")
    try:
        page.screenshot(path=f"data/logs/debug_register_failed_{domain}.png")
    except Exception:
        pass
    return StepResult(
        result=HandlerResult.FAILED,
        message=f"Could not complete registration form on {domain}"
    )


# ------------------------------------------------------------------
# Post-registration email verification
# ------------------------------------------------------------------

def handle_verify_registration(page, domain: str, settings: dict,
                                conn, app_id: int, job_id: int,
                                account_registry, company_hint: str = None) -> StepResult:
    """Handle post-registration email verification (OTP or magic link).

    Uses the Phase 4 email poller if email_polling is enabled.
    Falls back to manual terminal prompt if manual_otp is enabled.
    """
    from .email_poller import EmailPoller, find_otp_field

    auto_cfg = settings.get("automation", {})

    if auto_cfg.get("email_polling"):
        imap_server = auto_cfg.get("imap_server", "imap.gmail.com")
        imap_port = int(auto_cfg.get("imap_port", 993))
        poll_timeout = int(auto_cfg.get("email_poll_timeout", 120))

        poller = EmailPoller(imap_server=imap_server, imap_port=imap_port)
        try:
            poller.connect()

            # Try OTP first
            code = poller.request_verification(domain, "otp", timeout=poll_timeout, company_hint=company_hint)
            if code:
                otp_field = find_otp_field(page)
                if otp_field:
                    otp_field.fill(code)
                    _click_verify_button(page)
                    account_registry.mark_active(domain)
                    log_action(conn, "registration_verified",
                               f"Verified on {domain} via OTP", app_id, job_id)
                    console.print(f"  [green]Account verified on {domain}[/]")
                    return StepResult(
                        result=HandlerResult.SUCCESS,
                        metadata={"domain": domain},
                        message=f"Account verified on {domain} via OTP"
                    )

            # Try magic link
            link = poller.request_verification(domain, "magic_link", timeout=60)
            if link:
                page.goto(link, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except PlaywrightTimeoutError:
                    pass
                account_registry.mark_active(domain)
                log_action(conn, "registration_verified",
                           f"Verified on {domain} via magic link", app_id, job_id)
                console.print(f"  [green]Account verified on {domain} via magic link[/]")
                return StepResult(
                    result=HandlerResult.SUCCESS,
                    metadata={"domain": domain},
                    message=f"Account verified on {domain} via magic link"
                )
        except Exception as e:
            logger.warning(f"handle_verify_registration: email polling failed: {e}")
        finally:
            try:
                poller.disconnect()
            except Exception:
                pass

    # Manual OTP fallback
    if auto_cfg.get("manual_otp"):
        console.print(f"  [bold yellow]Verification required for {domain}. Browser is open.[/]")
        try:
            code = input(f"  Enter verification code for {domain}: ").strip()
        except EOFError:
            code = ""
        if code:
            otp_field = find_otp_field(page)
            if otp_field:
                otp_field.fill(code)
                _click_verify_button(page)
                account_registry.mark_active(domain)
                log_action(conn, "registration_verified",
                           f"Verified on {domain} via manual OTP", app_id, job_id)
                return StepResult(result=HandlerResult.SUCCESS,
                                  message=f"Account verified on {domain} via manual code")

    account_registry.mark_failed(domain, "verification_timeout")
    try:
        page.screenshot(path=f"data/logs/debug_verify_failed_{domain}.png")
    except Exception:
        pass
    return StepResult(
        result=HandlerResult.FAILED,
        message=f"Verification timed out for {domain}"
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _click_verify_button(page):
    """Click a Verify/Confirm/Submit button after entering an OTP."""
    button_texts = ["Verify", "Confirm", "Submit", "Continue", "Verify Email",
                    "Activate", "Validate", "Next"]
    for text in button_texts:
        try:
            btn = page.get_by_role("button", name=text, exact=False)
            if btn.first.is_visible(timeout=500):
                btn.first.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except PlaywrightTimeoutError:
                    pass
                return
        except Exception:
            continue

    # Fallback: any submit-type button
    for selector in ["button[type='submit']", "input[type='submit']"]:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except PlaywrightTimeoutError:
                    pass
                return
        except Exception:
            continue
