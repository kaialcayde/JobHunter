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

def handle_detect_auth_type(page, url: str, settings: dict) -> StepResult:
    """Determine if the current page is a login wall or a registration wall.

    Called when NAVIGATE returns REQUIRES_LOGIN. Differentiates:
    - Registration wall: "Create Account", "Sign Up", confirm-password field
    - Login wall: standard sign-in form

    Only attempts registration if auto_register is enabled AND domain is
    on the auto_register_domains allowlist.
    """
    from .account_registry import is_auto_register_allowed
    from .page_checks import get_site_domain, detect_registration_wall

    auto_register = settings.get("automation", {}).get("auto_register", False)
    if not auto_register:
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            message="auto_register is disabled"
        )

    domain = get_site_domain(page.url)

    if not is_auto_register_allowed(domain, settings):
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            message=f"Domain {domain} not in auto_register_domains allowlist"
        )

    if detect_registration_wall(page):
        console.print(f"  [cyan]Registration wall detected on {domain} -- attempting auto-registration[/]")
        return StepResult(
            result=HandlerResult.REQUIRES_REGISTRATION,
            metadata={"domain": domain},
            message=f"Registration wall detected on {domain}"
        )

    return StepResult(
        result=HandlerResult.REQUIRES_LOGIN,
        message=f"Login wall on {domain} (not a registration wall)"
    )


# ------------------------------------------------------------------
# Registration form fill
# ------------------------------------------------------------------

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

    # Step 1: generate and persist credentials BEFORE touching the form
    creds = account_registry.generate_credentials(domain, tenant=tenant, platform=platform)
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
                                account_registry) -> StepResult:
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
            code = poller.request_verification(domain, "otp", timeout=poll_timeout)
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
