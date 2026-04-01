"""Email verification helpers for ATS registration flows."""

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..results import HandlerResult, StepResult
from .common import console, logger


def handle_verify_registration(page, domain: str, settings: dict,
                               conn, app_id: int, job_id: int,
                               account_registry, company_hint: str = None) -> StepResult:
    """Handle post-registration email verification (OTP or magic link)."""
    from ...db import log_action
    from ..email_poller import EmailPoller, find_otp_field

    auto_cfg = settings.get("automation", {})

    if auto_cfg.get("email_polling"):
        imap_server = auto_cfg.get("imap_server", "imap.gmail.com")
        imap_port = int(auto_cfg.get("imap_port", 993))
        poll_timeout = int(auto_cfg.get("email_poll_timeout", 120))

        poller = EmailPoller(imap_server=imap_server, imap_port=imap_port)
        try:
            poller.connect()

            code = poller.request_verification(domain, "otp", timeout=poll_timeout, company_hint=company_hint)
            if code:
                otp_field = find_otp_field(page)
                if otp_field:
                    otp_field.fill(code)
                    _click_verify_button(page)
                    account_registry.mark_active(domain)
                    log_action(conn, "registration_verified", f"Verified on {domain} via OTP", app_id, job_id)
                    console.print(f"  [green]Account verified on {domain}[/]")
                    return StepResult(
                        result=HandlerResult.SUCCESS,
                        metadata={"domain": domain},
                        message=f"Account verified on {domain} via OTP",
                    )

            link = poller.request_verification(domain, "magic_link", timeout=60)
            if link:
                page.goto(link, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except PlaywrightTimeoutError:
                    pass
                account_registry.mark_active(domain)
                log_action(conn, "registration_verified", f"Verified on {domain} via magic link", app_id, job_id)
                console.print(f"  [green]Account verified on {domain} via magic link[/]")
                return StepResult(
                    result=HandlerResult.SUCCESS,
                    metadata={"domain": domain},
                    message=f"Account verified on {domain} via magic link",
                )
        except Exception as e:
            logger.warning(f"handle_verify_registration: email polling failed: {e}")
        finally:
            try:
                poller.disconnect()
            except Exception:
                pass

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
                log_action(conn, "registration_verified", f"Verified on {domain} via manual OTP", app_id, job_id)
                return StepResult(
                    result=HandlerResult.SUCCESS,
                    message=f"Account verified on {domain} via manual code",
                )

    account_registry.mark_failed(domain, "verification_timeout")
    try:
        page.screenshot(path=f"data/logs/debug_verify_failed_{domain}.png")
    except Exception:
        pass
    return StepResult(
        result=HandlerResult.FAILED,
        message=f"Verification timed out for {domain}",
    )


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
