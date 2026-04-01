"""Kernel navigation and routing steps."""

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..detection import click_apply_button, dismiss_modals
from ..page_checks import check_page_blockers, force_apply_click, is_dead_page
from ..platforms.linkedin import handle_linkedin_post_apply
from ..results import HandlerResult, StepResult
from .common import console


def handle_navigate(page, url: str, listing_url: str, settings: dict, conn, app_id: int, job_id: int, verbose: bool) -> StepResult:
    """Load the page and check for initial blockers."""
    if verbose:
        console.print(f"  [dim]Loading: {url[:80]}[/]")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=2000)
    except PlaywrightTimeoutError:
        pass
    if verbose:
        console.print(f"  [dim]Page loaded: {page.url[:80]}[/]")

    block = check_page_blockers(page, url, listing_url, settings, conn, app_id, job_id, verbose)
    if block is not None:
        return block

    return StepResult(result=HandlerResult.SUCCESS)


def handle_route(page, url: str, listing_url: str, settings: dict, conn,
                 app_id: int, job_id: int, verbose: bool, finder=None) -> StepResult:
    """Dismiss modals, click Apply, and handle tab switching / LinkedIn flows."""
    dismiss_modals(page)
    if verbose:
        console.print("  [dim]Looking for Apply button...[/]")
    apply_result = click_apply_button(page, finder=finder)

    if not apply_result:
        dismiss_modals(page)
        page.wait_for_timeout(500)
        apply_result = click_apply_button(page, finder=finder)

    if not apply_result:
        console.print("  [dim]Apply button not found -- trying URL extraction...[/]")
        if force_apply_click(page):
            apply_result = True
            if len(page.context.pages) > 1:
                latest = page.context.pages[-1]
                if latest != page and latest.url != "about:blank":
                    apply_result = "new_tab"

    if verbose:
        console.print(f"  [dim]Apply button result: {apply_result}[/]")

    if apply_result == "new_tab" and len(page.context.pages) > 1:
        old_page = page
        page = page.context.pages[-1]
        page.wait_for_load_state("domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except PlaywrightTimeoutError:
            pass
        old_page.close()
        console.print(f"  [dim]Now on: {page.url[:80]}[/]")

    block = check_page_blockers(page, url, listing_url, settings, conn, app_id, job_id, verbose)
    if block is not None:
        block.metadata["page"] = page
        return block

    if is_dead_page(page):
        if listing_url and listing_url != url and "linkedin.com" not in listing_url.lower():
            console.print(f"  [dim]LinkedIn dead page -- trying direct URL: {listing_url[:60]}[/]")
            page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=2000)
            except PlaywrightTimeoutError:
                pass
        else:
            console.print("  [yellow]Landed on empty/dead LinkedIn page -- job may be expired[/]")
            from ..db import log_action

            log_action(conn, "apply_failed", f"Dead page after apply: {page.url[:80]}", app_id, job_id)
            return StepResult(
                result=HandlerResult.FAILED_DEAD_PAGE,
                message="Dead LinkedIn page",
                metadata={"page": page},
            )

    linkedin_result = handle_linkedin_post_apply(page, apply_result, listing_url)
    if linkedin_result == "failed":
        from ..db import log_action

        log_action(conn, "apply_failed",
                   f"Stuck on LinkedIn, no Easy Apply modal. "
                   f"apply_result={apply_result}, listing_url={listing_url}",
                   app_id, job_id)
        return StepResult(
            result=HandlerResult.FAILED,
            message="Stuck on LinkedIn listing page -- no Easy Apply modal",
            metadata={"page": page},
        )

    is_easy_apply_flow = apply_result == "easy_apply" or linkedin_result == "easy_apply"

    return StepResult(
        result=HandlerResult.SUCCESS,
        metadata={
            "page": page,
            "apply_result": apply_result,
            "linkedin_result": linkedin_result,
            "is_easy_apply_flow": is_easy_apply_flow,
        },
    )
