"""OTP resolution helpers for the vision agent."""

from .common import console, logger


def _try_resolve_otp(page, settings: dict) -> str | None:
    """Try to resolve an OTP code via email poller, then manual prompt."""
    from ..email_poller import EmailPoller, find_otp_field
    from ..page_checks import get_site_domain

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
                    return code
                console.print("  [yellow]Got OTP from email but no field found on page[/]")
            else:
                console.print("  [yellow]Email poller timed out[/]")
        except Exception as e:
            logger.warning(f"Email poller failed: {e}")
            console.print(f"  [yellow]Email poller error: {e}[/]")
        finally:
            poller.disconnect()

    if auto_settings.get("manual_otp"):
        console.print("  [bold yellow]OTP/verification code required![/]")
        try:
            user_code = input("  Enter the verification code (or press Enter to skip): ").strip()
        except EOFError:
            user_code = ""
        if user_code:
            otp_field = find_otp_field(page)
            if otp_field:
                otp_field.fill(user_code)
                console.print("  [green]Entered verification code[/]")
                return user_code
        else:
            console.print("  [yellow]No code entered -- skipping[/]")
            return None

    console.print("  [yellow]OTP required but no method available (enable email_polling or manual_otp)[/]")
    return None
