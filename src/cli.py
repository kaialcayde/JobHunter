"""JobHunter CLI -- main entry point and pipeline orchestrator."""

import os
import sys
import logging
from datetime import datetime
from pathlib import Path

# Fix Windows terminal encoding for Rich
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from rich.console import Console
from rich.table import Table
from rich.box import ASCII

from .db import (
    get_connection, get_jobs_by_status, update_job_status,
    count_jobs_by_status, get_job_by_id, count_applications_today,
    reset_failed_jobs, delete_failed_jobs, get_failed_jobs_with_details,
    nuke_database
)
from .config import load_settings
from .utils import (
    LOGS_DIR, LINKEDIN_AUTH_STATE, APPLICATIONS_DIR, ATTEMPTS_DIR,
    SUCCESS_DIR, FAILED_DIR, DATA_DIR, ensure_dirs, sanitize_filename
)

console = Console(force_terminal=True)


def _round_robin_select(jobs: list[dict], limit: int) -> list[dict]:
    """Select jobs via round-robin across search_role values, up to limit."""
    by_role = {}
    for job in jobs:
        role = job.get("search_role", "unknown")
        by_role.setdefault(role, []).append(job)

    selected = []
    role_queues = {role: list(jobs_list) for role, jobs_list in by_role.items()}
    roles = list(role_queues.keys())
    role_idx = 0

    while len(selected) < limit and any(role_queues.values()):
        role = roles[role_idx % len(roles)]
        role_idx += 1
        queue = role_queues.get(role, [])
        if queue:
            selected.append(queue.pop(0))
        # Remove empty queues
        if not queue and role in role_queues:
            del role_queues[role]
            roles = list(role_queues.keys())
            if not roles:
                break
            role_idx = role_idx % len(roles) if roles else 0

    return selected


def setup_logging():
    """Set up logging to both console and daily log file."""
    ensure_dirs()
    log_file = LOGS_DIR / f"pipeline_{datetime.now().strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(str(log_file), encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("jobhunter")


def cmd_scrape():
    """Run the job scraper."""
    console.print("[bold blue]== Job Scraper ==[/]")
    from .core.scraper import scrape_jobs
    scrape_jobs()


def cmd_tailor():
    """Generate tailored resumes and cover letters for new jobs."""
    console.print("[bold blue]== Resume & Cover Letter Tailoring ==[/]")
    from .core.tailoring import tailor_resume, tailor_cover_letter
    from .core.document import create_resume_docx, create_cover_letter_docx, create_resume_pdf, create_cover_letter_pdf

    settings = load_settings()
    conn = get_connection()

    automation = settings.get("automation", {})
    max_per_day = automation.get("max_applications_per_day", 25)
    max_per_round = automation.get("max_applications_per_round", 0)
    distribution = automation.get("distribution", "round_robin")

    # Use round cap if set, otherwise daily cap
    limit = max_per_round if max_per_round > 0 else max_per_day

    # Get new jobs that need tailoring
    all_new = get_jobs_by_status(conn, "new", limit=500)
    if not all_new:
        console.print("[yellow]No new jobs to tailor.[/]")
        conn.close()
        return

    # Only tailor what we'll actually apply to this round
    if distribution == "round_robin":
        jobs = _round_robin_select(all_new, limit)
    else:
        jobs = all_new[:limit]

    console.print(f"Tailoring documents for {len(jobs)} jobs (of {len(all_new)} new)...\n")

    for i, job in enumerate(jobs):
        company = job.get("company", "Unknown")
        title = job.get("title", "Unknown")
        console.print(f"[bold]({i+1}/{len(jobs)}) {title} at {company}[/]")

        try:
            update_job_status(conn, job["id"], "tailoring")

            # Tailor resume
            console.print("  Tailoring resume...")
            resume_text = tailor_resume(job, settings)
            resume_docx = create_resume_docx(resume_text, company, title)
            resume_pdf = create_resume_pdf(resume_text, company, title)
            console.print(f"  Resume saved: {resume_docx} + {resume_pdf}")

            # Tailor cover letter
            console.print("  Tailoring cover letter...")
            cl_text = tailor_cover_letter(job, settings)
            cl_docx = create_cover_letter_docx(cl_text, company, title)
            cl_pdf = create_cover_letter_pdf(cl_text, company, title)
            console.print(f"  Cover letter saved: {cl_docx} + {cl_pdf}")

            update_job_status(conn, job["id"], "tailored")
            console.print(f"  [green]Done![/]\n")

        except Exception as e:
            console.print(f"  [red]Failed: {e}[/]\n")
            update_job_status(conn, job["id"], "failed")

    conn.close()
    console.print("[bold green]Tailoring complete![/]")


def cmd_login():
    """Launch a browser for manual LinkedIn login, then save session cookies."""
    from playwright.sync_api import sync_playwright

    ensure_dirs()
    console.print("[bold blue]== LinkedIn Login ==[/]\n")
    console.print("A browser window will open. Log in to LinkedIn manually.")
    console.print("Handle any 2FA or CAPTCHA prompts in the browser.")
    console.print("When you see your LinkedIn feed, come back here and press Enter.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        # Load existing session if refreshing -- use same UA as apply context
        from .utils import USER_AGENT
        context_kwargs = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": USER_AGENT,
        }
        if LINKEDIN_AUTH_STATE.exists():
            context_kwargs["storage_state"] = str(LINKEDIN_AUTH_STATE)
            console.print("[dim]Loading existing session (refreshing)...[/]")

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        input("Press Enter here after you have logged in...")

        context.storage_state(path=str(LINKEDIN_AUTH_STATE))
        browser.close()

    console.print(f"\n[green]LinkedIn session saved to {LINKEDIN_AUTH_STATE}[/]")
    console.print("Future apply/pipeline runs will use this session automatically.")


def _check_linkedin_auth() -> bool:
    """Check for LinkedIn auth state. Prompt user to log in if missing.

    Returns True if auth is available or user chose to continue without it.
    """
    if LINKEDIN_AUTH_STATE.exists():
        return True

    console.print("[yellow]No LinkedIn login found.[/]")
    console.print("Jobs requiring LinkedIn auth will be skipped without it.")
    console.print("Run 'python -m src login' to authenticate.\n")

    try:
        answer = input("Continue without LinkedIn auth? (y/N): ").strip().lower()
    except EOFError:
        answer = "y"  # non-interactive environment, continue without auth

    if answer != "y":
        cmd_login()

    return True


def cmd_apply():
    """Run the application automation."""
    console.print("[bold blue]== Application Automation ==[/]")
    _check_linkedin_auth()
    from .automation import apply_to_jobs
    apply_to_jobs()


def cmd_apply_job():
    """Apply to a specific job by ID, or the next available job if no ID given."""
    if len(sys.argv) >= 3:
        try:
            job_id = int(sys.argv[2])
        except ValueError:
            console.print(f"[red]Invalid job ID: {sys.argv[2]}[/]")
            return
    else:
        # No ID given -- pick the next available job
        conn = get_connection()
        jobs = get_jobs_by_status(conn, "new", limit=1)
        if not jobs:
            jobs = get_jobs_by_status(conn, "tailored", limit=1)
        conn.close()
        if not jobs:
            console.print("[yellow]No jobs available to apply to (no 'new' or 'tailored' jobs).[/]")
            return
        job_id = jobs[0]["id"]
        console.print(f"[bold]Auto-selected next job: #{job_id} - {jobs[0].get('title', '?')} @ {jobs[0].get('company', '?')}[/]")
    console.print(f"[bold blue]== Test Apply: Job #{job_id} ==[/]")
    _check_linkedin_auth()
    from .automation import apply_to_single_job_by_id
    apply_to_single_job_by_id(job_id)


def cmd_seed_answers():
    """Seed the answer bank from profile.yaml."""
    from .config import load_profile
    from .db import seed_answer_bank_from_profile

    console.print("[bold blue]== Seed Answer Bank ==[/]\n")

    profile = load_profile()
    conn = get_connection()
    count = seed_answer_bank_from_profile(conn, profile)
    conn.close()

    console.print(f"[green]Seeded {count} answer bank entries from profile.yaml.[/]")
    console.print("[dim]Run 'python -m src answers' to review or override any entry.[/]")


def cmd_pipeline():
    """Run the full pipeline: seed answers -> scrape -> tailor -> apply."""
    logger = setup_logging()
    logger.info("Starting JobHunter pipeline")

    refresh_profile = "--refresh_profile" in sys.argv or "--refresh-profile" in sys.argv

    settings = load_settings()
    max_per_day = settings.get("automation", {}).get("max_applications_per_day", 25)

    # Check daily cap before starting
    conn = get_connection()
    applied_today = count_applications_today(conn)
    conn.close()

    if applied_today >= max_per_day:
        console.print(f"[yellow]Daily cap already reached ({applied_today}/{max_per_day}). Nothing to do.[/]")
        logger.info(f"Daily cap reached ({applied_today}/{max_per_day}). Exiting.")
        return

    console.print(f"[bold]Daily progress: {applied_today}/{max_per_day} applications[/]\n")

    # Step 1: Seed answer bank from profile
    console.print("\n[bold magenta]=== Step 1/4: Seeding Answer Bank ===[/]")
    try:
        if refresh_profile:
            console.print("[yellow]--refresh_profile: forcing re-seed from profile.yaml[/]")
        cmd_seed_answers()
    except Exception as e:
        console.print(f"[red]Answer bank seeding failed: {e}[/]")
        logger.error(f"Answer bank seeding failed: {e}")

    # Step 2: Scrape
    console.print("\n[bold magenta]=== Step 2/4: Scraping Jobs ===[/]")
    try:
        cmd_scrape()
    except Exception as e:
        console.print(f"[red]Scraping failed: {e}[/]")
        logger.error(f"Scraping failed: {e}")

    # Step 3: Tailor
    if settings.get("tailoring", {}).get("enabled", True):
        console.print("\n[bold magenta]=== Step 3/4: Tailoring Documents ===[/]")
        try:
            cmd_tailor()
        except Exception as e:
            console.print(f"[red]Tailoring failed: {e}[/]")
            logger.error(f"Tailoring failed: {e}")
    else:
        console.print("\n[bold magenta]=== Step 3/4: Tailoring (skipped -- disabled in settings) ===[/]")
        # Promote "new" jobs straight to "tailored" so apply step picks them up
        conn = get_connection()
        new_jobs = get_jobs_by_status(conn, "new", limit=500)
        promoted = 0
        for job in new_jobs:
            update_job_status(conn, job["id"], "tailored")
            promoted += 1
        conn.close()
        if promoted:
            console.print(f"  Promoted {promoted} jobs to ready-to-apply (no tailoring)")

    # Step 4: Apply
    console.print("\n[bold magenta]=== Step 4/4: Submitting Applications ===[/]")
    _check_linkedin_auth()
    try:
        from .automation import apply_to_jobs
        apply_to_jobs()
    except Exception as e:
        console.print(f"[red]Application failed: {e}[/]")
        logger.error(f"Application failed: {e}")

    # Summary
    conn = get_connection()
    counts = count_jobs_by_status(conn)
    applied_now = count_applications_today(conn)
    conn.close()

    console.print("\n[bold magenta]=== Pipeline Complete ===[/]")
    console.print(f"Applications submitted today: {applied_now}")
    for status, count in sorted(counts.items()):
        console.print(f"  {status}: {count}")
    logger.info(f"Pipeline complete. Applied today: {applied_now}. Status: {counts}")


def cmd_status():
    """Show application status summary."""
    conn = get_connection()
    counts = count_jobs_by_status(conn)
    applied_today = count_applications_today(conn)
    conn.close()

    console.print("\n[bold blue]== JobHunter Status ==[/]\n")

    table = Table(title="Jobs by Status", box=ASCII)
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")
    total = 0
    for status, count in sorted(counts.items()):
        table.add_row(status, str(count))
        total += count
    table.add_row("-----", "-----", style="dim")
    table.add_row("TOTAL", str(total), style="bold")
    console.print(table)

    settings = load_settings()
    max_per_day = settings.get("automation", {}).get("max_applications_per_day", 25)
    console.print(f"\nApplications today: {applied_today}/{max_per_day}")


def cmd_list(status_filter: str = None):
    """List jobs, optionally filtered by status."""
    conn = get_connection()

    if status_filter:
        jobs = get_jobs_by_status(conn, status_filter, limit=50)
        console.print(f"\n[bold blue]Jobs with status '{status_filter}':[/]\n")
    else:
        # Show all recent jobs
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY date_scraped DESC LIMIT 50"
        ).fetchall()
        jobs = [dict(r) for r in rows]
        console.print("\n[bold blue]Recent jobs (last 50):[/]\n")

    conn.close()

    if not jobs:
        console.print("[yellow]No jobs found.[/]")
        return

    table = Table(box=ASCII)
    table.add_column("ID", style="dim", width=5)
    table.add_column("Title", width=30)
    table.add_column("Company", width=20)
    table.add_column("Location", width=20)
    table.add_column("Status", width=12)
    table.add_column("Site", width=10)

    for job in jobs:
        status_style = {
            "new": "white",
            "tailored": "cyan",
            "applied": "green",
            "failed": "red",
            "skipped": "yellow",
            "failed_captcha": "red",
        }.get(job["status"], "white")
        table.add_row(
            str(job["id"]),
            (job["title"] or "")[:30],
            (job["company"] or "")[:20],
            (job["location"] or "")[:20],
            f"[{status_style}]{job['status']}[/]",
            job.get("site", ""),
        )

    console.print(table)


def cmd_retry():
    """Reset failed jobs back to ready-for-apply so they can be retried."""
    conn = get_connection()
    count = reset_failed_jobs(conn)
    conn.close()
    if count:
        console.print(f"[green]Reset {count} failed jobs back to ready-for-apply.[/]")
    else:
        console.print("[yellow]No failed jobs to retry.[/]")


def cmd_delete_failed():
    """Delete all failed jobs from the database."""
    conn = get_connection()
    failed = count_jobs_by_status(conn)
    failed_count = failed.get("failed", 0) + failed.get("failed_captcha", 0)
    if not failed_count:
        console.print("[yellow]No failed jobs to delete.[/]")
        conn.close()
        return
    count = delete_failed_jobs(conn)
    conn.close()
    console.print(f"[green]Deleted {count} failed jobs.[/]")


def cmd_reset():
    """Full reset: delete applications folder and database."""
    import shutil

    console.print("[bold red]== Full Reset ==[/]\n")
    console.print("This will DELETE:")
    console.print(f"  - All application folders in {APPLICATIONS_DIR}")
    console.print(f"  - The database at {DATA_DIR / 'jobhunter.db'}")
    console.print()

    try:
        answer = input("Are you sure? Type 'yes' to confirm: ").strip().lower()
    except EOFError:
        answer = ""

    if answer != "yes":
        console.print("[yellow]Reset cancelled.[/]")
        return

    # Delete applications folder
    deleted_apps = 0
    if APPLICATIONS_DIR.exists():
        for item in APPLICATIONS_DIR.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
                deleted_apps += 1
        console.print(f"  Deleted {deleted_apps} application folders")

    # Nuke database contents
    conn = get_connection()
    nuke_database(conn)
    conn.close()
    console.print("  Database cleared")

    console.print("\n[green]Reset complete. Fresh start![/]")


def cmd_view():
    """View successful applications -- list applied jobs and open their folders."""
    import subprocess

    conn = get_connection()
    applied_jobs = get_jobs_by_status(conn, "applied", limit=100)
    conn.close()

    if not applied_jobs:
        console.print("[yellow]No successful applications yet.[/]")
        return

    console.print(f"\n[bold green]== {len(applied_jobs)} Successful Applications ==[/]\n")

    table = Table(box=ASCII)
    table.add_column("#", style="dim", width=4)
    table.add_column("Company", width=20)
    table.add_column("Position", width=30)
    table.add_column("Folder", width=40)

    for i, job in enumerate(applied_jobs, 1):
        company = sanitize_filename(job.get("company", "Unknown"))
        title = sanitize_filename(job.get("title", "Unknown"))
        # Check success/ first, then attempts/, then old flat path
        folder = SUCCESS_DIR / company / title
        if not folder.exists():
            folder = ATTEMPTS_DIR / company / title
        if not folder.exists():
            folder = APPLICATIONS_DIR / company / title
        exists = "[green]exists[/]" if folder.exists() else "[red]missing[/]"
        table.add_row(
            str(i),
            job.get("company", "?")[:20],
            job.get("title", "?")[:30],
            exists,
        )

    console.print(table)

    console.print(f"\nSuccess folder: {SUCCESS_DIR}")
    try:
        answer = input("\nOpen success folder? (y/N): ").strip().lower()
    except EOFError:
        answer = ""

    if answer == "y":
        target = SUCCESS_DIR if SUCCESS_DIR.exists() else APPLICATIONS_DIR
        if sys.platform == "darwin":
            subprocess.run(["open", str(target)])
        elif sys.platform == "win32":
            subprocess.run(["explorer", str(target)])
        else:
            subprocess.run(["xdg-open", str(target)])


def cmd_view_failed():
    """View failed applications -- list failed jobs and open their folders."""
    import subprocess

    conn = get_connection()
    failed_jobs = get_failed_jobs_with_details(conn)
    conn.close()

    if not failed_jobs:
        console.print("[yellow]No failed applications.[/]")
        return

    console.print(f"\n[bold red]== {len(failed_jobs)} Failed Applications ==[/]\n")

    table = Table(box=ASCII)
    table.add_column("#", style="dim", width=4)
    table.add_column("Company", width=20)
    table.add_column("Position", width=30)
    table.add_column("Status", width=15)
    table.add_column("Folder", width=10)

    for i, job in enumerate(failed_jobs, 1):
        company = sanitize_filename(job.get("company", "Unknown"))
        title = sanitize_filename(job.get("title", "Unknown"))
        failed_folder = FAILED_DIR / company / title
        attempts_folder = ATTEMPTS_DIR / company / title
        old_folder = APPLICATIONS_DIR / company / title
        if failed_folder.exists():
            exists = "[dim]failed/[/]"
        elif attempts_folder.exists():
            exists = "[yellow]attempts/[/]"
        elif old_folder.exists():
            exists = "[yellow]apps/[/]"
        else:
            exists = "[red]none[/]"
        table.add_row(
            str(i),
            job.get("company", "?")[:20],
            job.get("title", "?")[:30],
            f"[red]{job.get('status', '?')}[/]",
            exists,
        )

    console.print(table)

    # Offer to open folder
    target = FAILED_DIR if FAILED_DIR.exists() else APPLICATIONS_DIR
    console.print(f"\nFailed folder: {target}")
    try:
        answer = input("\nOpen folder? (y/N): ").strip().lower()
    except EOFError:
        answer = ""

    if answer == "y":
        if sys.platform == "darwin":
            subprocess.run(["open", str(target)])
        elif sys.platform == "win32":
            subprocess.run(["explorer", str(target)])
        else:
            subprocess.run(["xdg-open", str(target)])


def cmd_remove_failed():
    """Remove failed jobs from DB and move their application folders to _failed/."""
    import shutil

    conn = get_connection()
    failed_jobs = get_failed_jobs_with_details(conn)

    if not failed_jobs:
        console.print("[yellow]No failed jobs to remove.[/]")
        conn.close()
        return

    console.print(f"[bold blue]Moving {len(failed_jobs)} failed jobs to {FAILED_DIR}[/]\n")

    moved = 0
    for job in failed_jobs:
        company = sanitize_filename(job.get("company", "Unknown"))
        title = sanitize_filename(job.get("title", "Unknown"))

        # Check attempts/ first, then old flat path, then failed/
        src_dir = ATTEMPTS_DIR / company / title
        if not src_dir.exists():
            src_dir = APPLICATIONS_DIR / company / title

        if src_dir.exists():
            dest_dir = FAILED_DIR / company / title
            dest_dir.parent.mkdir(parents=True, exist_ok=True)
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            shutil.move(str(src_dir), str(dest_dir))
            moved += 1
            console.print(f"  [dim]Moved: {company}/{title}[/]")

            # Clean up empty company folder
            company_dir = src_dir.parent
            if company_dir.exists() and not any(company_dir.iterdir()):
                company_dir.rmdir()
        else:
            # Already in failed/ — just delete it
            failed_path = FAILED_DIR / company / title
            if failed_path.exists():
                shutil.rmtree(failed_path)
                moved += 1
                console.print(f"  [dim]Deleted: {company}/{title} (already in failed/)[/]")

                # Clean up empty company folder
                company_dir = failed_path.parent
                if company_dir.exists() and not any(company_dir.iterdir()):
                    company_dir.rmdir()

    # Delete from database
    count = delete_failed_jobs(conn)
    conn.close()

    console.print(f"\n[green]Removed {count} failed jobs from DB, moved {moved} folders to _failed/[/]")


def cmd_answers():
    """Review and fill in unanswered form questions from the answer bank."""
    from .db import get_unanswered_questions, get_saved_answers, save_answer

    conn = get_connection()
    unanswered = get_unanswered_questions(conn)
    all_answers = get_saved_answers(conn)

    answered_count = sum(1 for v in all_answers.values() if v != "N/A")
    na_count = len(unanswered)

    console.print(f"\n[bold blue]== Answer Bank ==[/]\n")
    console.print(f"Total saved: {len(all_answers)} ({answered_count} answered, {na_count} need your input)\n")

    if not unanswered:
        console.print("[green]All questions have been answered! Nothing to do.[/]")

        # Show all saved answers for reference
        if all_answers:
            table = Table(title="Saved Answers", box=ASCII)
            table.add_column("Question", width=40)
            table.add_column("Answer", width=40)
            for q, a in sorted(all_answers.items()):
                table.add_row(q[:40], a[:40])
            console.print(table)

        conn.close()
        return

    console.print(f"[yellow]{na_count} questions need your answer.[/]")
    console.print("Type your answer, or press Enter to skip. Type 'q' to quit.\n")

    filled = 0
    for item in unanswered:
        label = item["question_label"]
        console.print(f"  [bold]{label}[/]")
        try:
            answer = input("  > ").strip()
        except EOFError:
            break

        if answer.lower() == "q":
            break
        if answer:
            save_answer(conn, label, answer, source="user")
            filled += 1
            console.print(f"  [green]Saved![/]\n")
        else:
            console.print(f"  [dim]Skipped[/]\n")

    conn.close()
    console.print(f"\n[bold green]Done! Filled {filled} answers.[/]")
    if na_count - filled > 0:
        console.print(f"[dim]{na_count - filled} questions still need answers. Run 'python -m src answers' again.[/]")


def cmd_login_sites():
    """Open a visible browser to log in to sites that blocked applications one at a time, save cookies, then retry."""
    from playwright.sync_api import sync_playwright
    from .db import get_jobs_by_status, update_job_status
    from .automation.page_checks import get_site_domain as _get_site_domain, get_site_auth_path as _get_site_auth_path
    from .automation.applicant import _run_application_batch

    conn = get_connection()
    needs_login = get_jobs_by_status(conn, "needs_login")

    if not needs_login:
        console.print("[yellow]No jobs pending login. All clear![/]")
        conn.close()
        return

    # Group jobs by site domain
    from collections import defaultdict
    sites = defaultdict(list)
    for j in needs_login:
        url = j.get("url", "") or j.get("listing_url", "")
        domain = _get_site_domain(url) if url else "unknown"
        sites[domain].append(j)

    console.print(f"[bold blue]{len(needs_login)} jobs need login across {len(sites)} site(s)[/]\n")
    for domain, jobs in sites.items():
        console.print(f"  [bold]{domain}[/] -- {len(jobs)} job(s)")
        for j in jobs:
            console.print(f"    {j['title']} at {j['company']}")

    console.print(f"\n[yellow]You will log in to each site one at a time.[/]")
    console.print("[yellow]After logging in, come back here and press Enter.[/]\n")

    from .utils import USER_AGENT, SITE_AUTH_DIR
    SITE_AUTH_DIR.mkdir(parents=True, exist_ok=True)

    logged_in_domains = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        for domain, jobs in sites.items():
            # Pick a representative URL for this domain
            sample_url = jobs[0].get("url", "") or jobs[0].get("listing_url", "")
            site_auth = _get_site_auth_path(sample_url)

            console.print(f"\n[bold blue]== Log in to {domain} ==[/]")
            console.print(f"  Opening: {sample_url[:80]}")

            # Load existing cookies if any
            context_kwargs = {
                "viewport": {"width": 1280, "height": 900},
                "user_agent": USER_AGENT,
            }
            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            # Load existing cookies if any
            if site_auth.exists():
                try:
                    import json
                    cookies = json.loads(site_auth.read_text())
                    if isinstance(cookies, list):
                        context.add_cookies(cookies)
                    console.print("  [dim]Loading existing cookies (refreshing)...[/]")
                except Exception:
                    pass
            page.goto(sample_url, wait_until="domcontentloaded", timeout=30000)

            input(f"  Press Enter here after you have logged in to {domain}...")

            # Save cookies for this domain
            import json
            state = context.storage_state()
            site_auth.write_text(json.dumps(state.get("cookies", []), indent=2))
            console.print(f"  [green]Cookies saved for {domain} -> {site_auth}[/]")
            logged_in_domains.add(domain)

            context.close()

        browser.close()

    # Retry jobs for domains we logged into
    if logged_in_domains:
        retry_jobs = [j for j in needs_login
                      if _get_site_domain(j.get("url", "") or j.get("listing_url", "")) in logged_in_domains]

        if retry_jobs:
            console.print(f"\n[bold blue]Retrying {len(retry_jobs)} jobs with new cookies...[/]")
            # Reset status to new so they get picked up
            for j in retry_jobs:
                update_job_status(conn, j["id"], "new")
            console.print("[green]Jobs reset to 'new' -- run 'python -m src apply' to retry.[/]")

    conn.close()


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        console.print("[bold]JobHunter[/] - Automated Job Application System\n")
        console.print("Usage: python -m src <command>\n")
        console.print("Commands:")
        console.print("  [bold]scrape[/]     Scrape job listings from configured boards")
        console.print("  [bold]tailor[/]     Generate tailored resume & cover letter for new jobs")
        console.print("  [bold]apply[/]      Submit applications for tailored jobs")
        console.print("  [bold]pipeline[/]   Run full pipeline: scrape -> tailor -> apply")
        console.print("  [bold]status[/]     Show application status summary")
        console.print("  [bold]list[/]       List jobs (optional: list <status>)")
        console.print("  [bold]login[/]      Save LinkedIn session for authenticated applications")
        console.print("  [bold]retry[/]      Reset failed jobs back to ready-for-apply")
        console.print("  [bold]delete-failed[/] Delete all failed jobs from the database")
        console.print("  [bold]view[/]       View successful applications and open folder")
        console.print("  [bold]view-failed[/] View failed applications and open folder")
        console.print("  [bold]reset[/]      Full reset: delete all applications and database")
        console.print("  [bold]remove-failed[/] Move failed apps to _failed/ folder and clean DB")
        console.print("  [bold]login-sites[/]  Log in to sites that blocked apps, then retry those jobs")
        console.print("  [bold]answers[/]     Review and fill in unanswered form questions")
        console.print("  [bold]seed-answers[/] Seed answer bank from profile.yaml")
        console.print("  [bold]apply-job[/]   Apply to a specific job by ID (testing/debugging)")
        console.print("\nFlags:")
        console.print("  [bold]--refresh_profile[/]  Force re-seed answer bank from profile (use with pipeline)")
        return

    command = sys.argv[1].lower()

    if command == "scrape":
        cmd_scrape()
    elif command == "tailor":
        cmd_tailor()
    elif command == "apply":
        cmd_apply()
    elif command == "pipeline":
        cmd_pipeline()
    elif command == "status":
        cmd_status()
    elif command == "list":
        status = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_list(status)
    elif command == "login":
        cmd_login()
    elif command == "retry":
        cmd_retry()
    elif command in ("delete-failed", "delete_failed"):
        cmd_delete_failed()
    elif command == "view":
        cmd_view()
    elif command in ("view-failed", "view_failed"):
        cmd_view_failed()
    elif command == "reset":
        cmd_reset()
    elif command in ("remove-failed", "remove_failed"):
        cmd_remove_failed()
    elif command in ("login-sites", "login_sites"):
        cmd_login_sites()
    elif command == "answers":
        cmd_answers()
    elif command in ("seed-answers", "seed_answers"):
        cmd_seed_answers()
    elif command in ("apply-job", "apply_job"):
        cmd_apply_job()
    else:
        console.print(f"[red]Unknown command: {command}[/]")
        console.print("Run 'python -m src' for usage info.")


if __name__ == "__main__":
    main()
