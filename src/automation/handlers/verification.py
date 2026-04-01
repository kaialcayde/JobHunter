"""Kernel verification and cleanup steps."""

import json
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ...core.document import save_application_metadata
from ...db import increment_retry_count, log_action, update_application, update_job_status
from ..email_poller import EmailPoller, find_otp_field
from ..page_checks import get_site_domain
from ..results import HandlerResult, StepResult
from ..vision_agent import verify_submission
from ...utils import move_application_dir
from .common import console, logger


def handle_verify(page, settings: dict, app_dir: Path, use_vision: bool,
                  conn, job_id: int, app_id: int) -> StepResult:
    """Verify submission result with vision check if enabled."""
    try:
        page.wait_for_load_state("networkidle", timeout=2000)
    except PlaywrightTimeoutError:
        pass
    confirm_path = app_dir / "confirmation_screenshot.png"
    page.screenshot(path=str(confirm_path), full_page=True)

    if use_vision:
        console.print("  [dim]Verifying submission with vision...[/]")
        actually_submitted = verify_submission(page, settings)
        if not actually_submitted:
            console.print("  [yellow]Vision check: NOT actually submitted -- marking as failed[/]")
            logger.warning(f"Vision verification rejected submission for job #{job_id}")
            increment_retry_count(conn, job_id)
            update_job_status(conn, job_id, "failed")
            log_action(conn, "false_submission", "Vision verification rejected confirmation", app_id, job_id)
            return StepResult(
                result=HandlerResult.FAILED,
                message="Vision verification rejected confirmation",
            )

    return StepResult(result=HandlerResult.SUCCESS)


def handle_cleanup(submitted: bool, conn, job: dict, app_id: int, app_dir: Path,
                   form_answers_all: dict, url: str) -> StepResult:
    """Update DB status, move application directories, save metadata."""
    company = job.get("company", "Unknown")
    position = job.get("title", "Unknown")
    job_id = job["id"]

    if submitted:
        update_job_status(conn, job_id, "applied")
        answers_json = json.dumps(form_answers_all) if form_answers_all else None
        update_application(
            conn,
            app_id,
            submitted_at=datetime.now().isoformat(),
            form_answers_json=answers_json,
        )
        log_action(conn, "applied", f"Submitted to {company}", app_id, job_id)
        if form_answers_all:
            log_action(conn, "form_answers", answers_json, app_id, job_id)
            console.print(f"  [dim]Stored {len(form_answers_all)} form answers in DB[/]")
        save_application_metadata(company, position, job, form_answers_all)
        final_dir = move_application_dir(company, position, "success")
        console.print("  [green]Successfully applied! (verified)[/]")
        console.print(f"  [dim]{final_dir}[/]")
        return StepResult(result=HandlerResult.SUCCESS, metadata={"final_dir": final_dir})

    increment_retry_count(conn, job_id)
    update_job_status(conn, job_id, "failed")
    log_action(conn, "apply_failed", f"Could not complete application at {url}", app_id, job_id)
    final_dir = move_application_dir(company, position, "failed")
    console.print("  [red]Application failed[/]")
    console.print(f"  [dim]Debug: {final_dir / 'debug_no_submit.png'}[/]")
    return StepResult(result=HandlerResult.FAILED, metadata={"final_dir": final_dir})


def handle_verification(page, settings: dict, conn, app_id: int, job_id: int) -> StepResult:
    """Handle email verification when detected during navigation or form filling."""
    auto_settings = settings.get("automation", {})
    domain = get_site_domain(page.url)

    if auto_settings.get("email_polling"):
        console.print(f"  [cyan]Polling email for verification code from {domain}...[/]")
        poller = EmailPoller(
            imap_server=auto_settings.get("imap_server", "imap.gmail.com"),
            imap_port=auto_settings.get("imap_port", 993),
        )
        try:
            poller.connect()
            code = poller.request_verification(
                domain=domain,
                type="otp",
                timeout=auto_settings.get("email_poll_timeout", 120),
            )
            if code:
                otp_field = find_otp_field(page)
                if otp_field:
                    otp_field.fill(code)
                    console.print(f"  [green]OTP filled from email: {code[:2]}***[/]")
                    log_action(conn, "otp_filled", f"Email poller filled OTP for {domain}", app_id, job_id)
                    return StepResult(result=HandlerResult.SUCCESS, message="OTP filled from email")
                console.print("  [yellow]Got OTP from email but no field found on page[/]")
            else:
                console.print("  [yellow]Email poller timed out -- no verification email received[/]")
        except Exception as e:
            logger.warning(f"Email poller failed: {e}")
            console.print(f"  [yellow]Email poller error: {e}[/]")
        finally:
            poller.disconnect()

    if auto_settings.get("manual_otp"):
        console.print(f"  [bold yellow]OTP/verification code required for {domain}![/]")
        try:
            code = input("  Enter the verification code (or press Enter to skip): ").strip()
        except EOFError:
            code = ""
        if code:
            otp_field = find_otp_field(page)
            if otp_field:
                otp_field.fill(code)
                console.print("  [green]OTP filled manually[/]")
                log_action(conn, "otp_filled", f"Manual OTP for {domain}", app_id, job_id)
                return StepResult(result=HandlerResult.SUCCESS, message="OTP filled manually")
            console.print("  [yellow]No OTP field found on page[/]")

    console.print(f"  [yellow]Verification required for {domain} -- no OTP method succeeded[/]")
    log_action(conn, "verification_failed", f"No OTP method for {domain}", app_id, job_id)
    return StepResult(
        result=HandlerResult.REQUIRES_LOGIN,
        message=f"Verification required for {domain}, no OTP method available",
    )
