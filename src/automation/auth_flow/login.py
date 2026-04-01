"""Registry-backed login flow."""

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ...db import log_action
from ..results import HandlerResult, StepResult
from .common import console, logger


def handle_login_registry(page, domain: str, settings: dict, finder,
                          account_registry, conn, app_id: int, job_id: int) -> StepResult:
    """Log in to an ATS tenant portal using stored registry credentials."""
    log_action(conn, "login_registry_start", f"Attempting registry login on {domain}", app_id, job_id)

    creds = account_registry.get_credentials(domain)
    if not creds:
        logger.warning(f"handle_login_registry: no credentials for {domain}")
        return StepResult(
            result=HandlerResult.FAILED,
            metadata={"domain": domain},
            message=f"No credentials found for {domain}",
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

    from ..detection import detect_login_page

    if not detect_login_page(page):
        account_registry.mark_active(domain)
        log_action(conn, "login_registry_success", f"Registry login succeeded on {domain}", app_id, job_id)
        console.print(f"  [green]Registry login succeeded on {domain}[/]")
        return StepResult(
            result=HandlerResult.SUCCESS,
            metadata={"domain": domain},
            message=f"Registry login succeeded on {domain}",
        )

    console.print(f"  [yellow]Registry login failed on {domain} -- will attempt re-registration[/]")
    log_action(conn, "login_registry_failed", f"Registry login failed on {domain}", app_id, job_id)
    try:
        page.screenshot(path=f"data/logs/debug_login_registry_failed_{domain}.png")
    except Exception:
        pass
    return StepResult(
        result=HandlerResult.FAILED,
        metadata={"domain": domain},
        message=f"Registry login failed on {domain}",
    )
