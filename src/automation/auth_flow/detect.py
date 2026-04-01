"""Auth-type detection helpers."""

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..browser_scripts import evaluate_script
from ..results import HandlerResult, StepResult
from .common import console


def handle_detect_auth_type(page, url: str, settings: dict, account_registry=None) -> StepResult:
    """Determine if the current page is a login wall or a registration wall."""
    from urllib.parse import urlparse

    from ..account_registry import is_auto_register_allowed
    from ..page_checks import detect_registration_wall, get_site_domain

    auto_register = settings.get("automation", {}).get("auto_register", False)
    if not auto_register:
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            message="auto_register is disabled",
        )

    hostname = urlparse(page.url).hostname or ""
    domain = get_site_domain(page.url)

    if not (
        is_auto_register_allowed(page.url, settings)
        or is_auto_register_allowed(url, settings)
    ):
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            message=f"Domain {hostname} not in auto_register_domains allowlist",
        )

    if account_registry is not None and account_registry.has_account(hostname):
        console.print(f"  [cyan]Existing account found for {domain} -- attempting registry login[/]")
        return StepResult(
            result=HandlerResult.REQUIRES_EXISTING_LOGIN,
            metadata={"domain": hostname},
            message=f"Existing account found for {domain} -- attempting login",
        )

    register_link_texts = [
        "Create Profile", "Create Account", "Create an Account",
        "Sign Up", "Register", "New User", "Join", "Get Started",
    ]
    for text in register_link_texts:
        for role in ("link", "button"):
            try:
                el = page.get_by_role(role, name=text, exact=False).first
                if el.is_visible(timeout=500):
                    el.click()
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except PlaywrightTimeoutError:
                        pass
                    if _is_application_form(page):
                        console.print(f"  [cyan]Navigated to application form on {domain} -- handing off to fill agent[/]")
                        return StepResult(
                            result=HandlerResult.SUCCESS,
                            metadata={"domain": hostname},
                            message=f"Navigated to application form on {domain}",
                        )
                    if detect_registration_wall(page):
                        console.print(f"  [cyan]Navigated to registration page on {domain}[/]")
                        return StepResult(
                            result=HandlerResult.REQUIRES_REGISTRATION,
                            metadata={"domain": hostname},
                            message=f"Navigated to registration page on {domain}",
                        )
            except Exception:
                continue

    if detect_registration_wall(page):
        console.print(f"  [cyan]Registration wall detected on {domain} -- attempting auto-registration[/]")
        return StepResult(
            result=HandlerResult.REQUIRES_REGISTRATION,
            metadata={"domain": hostname},
            message=f"Registration wall detected on {domain}",
        )

    return StepResult(
        result=HandlerResult.REQUIRES_LOGIN,
        message=f"Login wall on {domain} (not a registration wall)",
    )


def _is_application_form(page) -> bool:
    """Detect if the page is an application form rather than a registration form."""
    try:
        return evaluate_script(page, "auth/is_application_form.js")
    except Exception:
        return False
