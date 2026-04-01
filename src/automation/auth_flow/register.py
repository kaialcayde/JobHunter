"""ATS registration flow helpers."""

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..results import HandlerResult, StepResult
from .common import console, logger


def handle_register(page, domain: str, settings: dict, finder,
                    account_registry, conn, app_id: int, job_id: int) -> StepResult:
    """Fill and submit an ATS registration form."""
    from ...config.loader import load_profile
    from ...db import log_action
    from ..account_registry import detect_ats_platform, extract_tenant

    platform = detect_ats_platform(page.url) or detect_ats_platform(domain)
    tenant = extract_tenant(domain, platform)

    existing = account_registry.get_credentials(domain)
    if existing:
        creds = existing
        logger.debug(f"handle_register: reusing existing credentials for {domain}")
        log_action(conn, "register_start", f"Registering on {domain} (reusing credentials)", app_id, job_id)
    else:
        use_alias = settings.get("automation", {}).get("use_email_aliases", False)
        creds = account_registry.generate_credentials(domain, tenant=tenant, platform=platform, use_alias=use_alias)
        log_action(conn, "register_start", f"Registering on {domain} (platform={platform})", app_id, job_id)

    try:
        profile_data = load_profile()
        personal = profile_data.get("personal", {})
    except Exception as e:
        logger.warning(f"handle_register: could not load profile: {e}")
        personal = {}

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
                    message=f"Registration submitted on {domain}",
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
        message=f"Could not complete registration form on {domain}",
    )
