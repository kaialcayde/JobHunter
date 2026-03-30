"""Application orchestration -- batch processing, round-robin distribution, browser lifecycle.

This module is the entry point for the apply command. It handles:
- Daily/round caps and distribution strategy
- Browser launch and context management
- Batch job processing (sequential or parallel by site)

The actual single-job application logic lives in kernel.py (ApplicationKernel).
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console

from ..db import (
    get_connection, get_jobs_by_status, update_job_status,
    count_applications_today, log_action, increment_retry_count
)
from ..config import load_settings
from ..utils import LINKEDIN_AUTH_STATE, USER_AGENT

from .kernel import ApplicationKernel

logger = logging.getLogger(__name__)

console = Console(force_terminal=True)


def _get_round_robin_jobs(conn, remaining: int, max_per_role: int, max_per_location: int, status: str = "tailored") -> list[dict]:
    """Get jobs distributed evenly across roles and locations via round-robin."""
    all_jobs = get_jobs_by_status(conn, status, limit=500)
    if not all_jobs:
        return []

    # Group by role and location
    by_role = {}
    by_location = {}
    for job in all_jobs:
        role = job.get("search_role", "unknown")
        loc = job.get("search_location", "unknown")
        by_role.setdefault(role, []).append(job)
        by_location.setdefault(loc, []).append(job)

    selected = []
    selected_ids = set()
    role_counts = {}
    location_counts = {}

    # Round-robin: cycle through roles, then within each role cycle locations
    roles = list(by_role.keys())
    if not roles:
        return all_jobs[:remaining]

    role_idx = 0
    stale_rounds = 0

    while len(selected) < remaining and stale_rounds < len(roles):
        role = roles[role_idx % len(roles)]
        role_idx += 1

        # Check per-role cap
        if max_per_role > 0 and role_counts.get(role, 0) >= max_per_role:
            stale_rounds += 1
            continue

        # Find next unselected job for this role
        found = False
        for job in by_role.get(role, []):
            if job["id"] in selected_ids:
                continue
            loc = job.get("search_location", "unknown")

            # Check per-location cap
            if max_per_location > 0 and location_counts.get(loc, 0) >= max_per_location:
                continue

            selected.append(job)
            selected_ids.add(job["id"])
            role_counts[role] = role_counts.get(role, 0) + 1
            location_counts[loc] = location_counts.get(loc, 0) + 1
            found = True
            stale_rounds = 0
            break

        if not found:
            stale_rounds += 1

    return selected


def apply_to_jobs():
    """Main application loop -- process jobs that have been tailored and apply."""
    settings = load_settings()
    automation = settings.get("automation", {})
    max_per_day = automation.get("max_applications_per_day", 25)
    max_per_round = automation.get("max_applications_per_round", 0)  # 0 = no round cap
    max_per_role = automation.get("max_per_role", 0)
    max_per_location = automation.get("max_per_location", 0)
    distribution = automation.get("distribution", "round_robin")
    take_screenshot = automation.get("screenshot_before_submit", True)

    conn = get_connection()

    # Check daily cap
    applied_today = count_applications_today(conn)
    if applied_today >= max_per_day:
        console.print(f"[yellow]Daily cap reached ({applied_today}/{max_per_day}). Stopping.[/]")
        conn.close()
        return

    remaining = max_per_day - applied_today

    # Apply per-round cap (overrides daily remaining if lower)
    if max_per_round > 0 and max_per_round < remaining:
        console.print(f"[dim]Per-round cap: {max_per_round} (daily remaining was {remaining})[/]")
        remaining = max_per_round

    # When tailoring is disabled, apply directly to "new" jobs using base templates
    tailoring_enabled = settings.get("tailoring", {}).get("enabled", True)
    job_status = "tailored" if tailoring_enabled else "new"

    # Get jobs based on distribution strategy
    if distribution == "round_robin":
        jobs = _get_round_robin_jobs(conn, remaining, max_per_role, max_per_location, status=job_status)
    else:
        jobs = get_jobs_by_status(conn, job_status, limit=remaining)

    if not jobs:
        console.print(f"[yellow]No {job_status} jobs ready for application.[/]")
        conn.close()
        return

    # Show distribution breakdown
    role_breakdown = {}
    loc_breakdown = {}
    for j in jobs:
        r = j.get("search_role", "?")
        l = j.get("search_location", "?")
        role_breakdown[r] = role_breakdown.get(r, 0) + 1
        loc_breakdown[l] = loc_breakdown.get(l, 0) + 1

    console.print(f"\n[bold blue]Applying to {len(jobs)} jobs (daily cap: {remaining} remaining)[/]")
    console.print(f"  By role: {role_breakdown}")
    console.print(f"  By location: {loc_breakdown}")

    parallel_per_site = automation.get("parallel_browsers_per_site", 1)

    try:
        if parallel_per_site <= 1:
            # Sequential mode -- single browser, all jobs
            _run_application_batch(jobs, settings, take_screenshot, label="")
        else:
            # Parallel mode -- group jobs by site, one browser per site
            by_site = {}
            for job in jobs:
                site = job.get("site", "unknown") or "unknown"
                by_site.setdefault(site, []).append(job)

            # Cap total concurrent browsers
            max_concurrent = min(len(by_site), parallel_per_site, 4)
            console.print(f"[bold]Running up to {max_concurrent} parallel browsers ({len(by_site)} sites)[/]")
            for site, site_jobs in by_site.items():
                console.print(f"  [dim]{site}: {len(site_jobs)} jobs[/]")

            with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
                futures = {}
                for site, site_jobs in by_site.items():
                    label = f"[{site}]"
                    futures[executor.submit(
                        _run_application_batch, site_jobs, settings, take_screenshot, label
                    )] = site

                for future in as_completed(futures):
                    site = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        console.print(f"  [red]{site} worker crashed: {e}[/]")

    except ImportError:
        console.print("[red]Playwright not installed. Run: pip install playwright && playwright install chromium[/]")

    conn.close()
    console.print("\n[bold green]Application round complete![/]")


def apply_to_single_job_by_id(job_id: int, debug: bool = False):
    """Apply to a specific job by database ID. Used for testing/debugging."""
    settings = load_settings()
    if debug:
        settings.setdefault("automation", {})["debug_mode"] = True
    take_screenshot = settings.get("automation", {}).get("screenshot_before_submit", True)

    conn = get_connection()
    from ..db import get_job_by_id
    job = get_job_by_id(conn, job_id)
    if not job:
        console.print(f"[red]Job ID {job_id} not found in database.[/]")
        conn.close()
        return

    console.print(f"\n[bold blue]Applying to job #{job_id}:[/]")
    console.print(f"  Title: {job.get('title', '?')}")
    console.print(f"  Company: {job.get('company', '?')}")
    console.print(f"  URL: {job.get('url', '?')[:80]}")
    console.print(f"  Status: {job.get('status', '?')}")

    # Force status to 'tailored' so the application logic proceeds
    current_status = job.get("status", "")
    if current_status not in ("tailored", "new"):
        console.print(f"  [yellow]Resetting status from '{current_status}' to 'tailored'[/]")
        update_job_status(conn, job_id, "tailored")
        job["status"] = "tailored"

    conn.close()

    try:
        _run_application_batch([job], settings, take_screenshot, label=f"[test-{job_id}]")
    except ImportError:
        console.print("[red]Playwright not installed. Run: pip install playwright && playwright install chromium[/]")

    console.print(f"\n[bold]Done. Check job #{job_id} status with: python -m src list[/]")


def _run_application_batch(jobs: list[dict], settings: dict,
                           take_screenshot: bool, label: str = ""):
    """Process a batch of applications in a single browser instance.

    Each call launches its own Playwright browser, making it safe to run
    multiple batches in parallel threads.
    """
    from playwright.sync_api import sync_playwright

    auto = settings.get("automation", {})
    headless = auto.get("headless", True)
    # Force visible browser only for manual_login (needs browser interaction)
    if auto.get("manual_login"):
        headless = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        context_kwargs = {
            "viewport": {"width": 1280, "height": 2400},
            "user_agent": USER_AGENT,
        }
        if LINKEDIN_AUTH_STATE.exists():
            context_kwargs["storage_state"] = str(LINKEDIN_AUTH_STATE)
            console.print(f"  [dim]{label}Loaded LinkedIn auth state[/]")

        context = browser.new_context(**context_kwargs)

        conn = get_connection()
        for i, job in enumerate(jobs):
            console.print(f"\n[bold]{label}({i+1}/{len(jobs)}) [Job #{job['id']}] {job['title']} at {job['company']}[/]")
            try:
                ApplicationKernel().run(context, job, settings, take_screenshot)
            except Exception as e:
                logger.exception(f"Unhandled error applying to job #{job['id']}")
                console.print(f"  [red]{label}Failed: {e}[/]")
                increment_retry_count(conn, job["id"])
                update_job_status(conn, job["id"], "failed")
                log_action(conn, "apply_failed", str(e), job_id=job["id"])

        # Re-save auth state to capture refreshed cookies
        try:
            context.storage_state(path=str(LINKEDIN_AUTH_STATE))
        except Exception as e:
            logger.debug(f"Failed to save LinkedIn auth state: {e}")

        conn.close()
        browser.close()
