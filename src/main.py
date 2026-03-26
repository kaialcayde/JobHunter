"""JobHunter CLI - main entry point and pipeline orchestrator."""

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

from .database import (
    get_connection, get_jobs_by_status, update_job_status,
    count_jobs_by_status, get_job_by_id, count_applications_today
)
from .profile import load_settings
from .utils import LOGS_DIR, ensure_dirs

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
    from .scraper import scrape_jobs
    scrape_jobs()


def cmd_tailor():
    """Generate tailored resumes and cover letters for new jobs."""
    console.print("[bold blue]== Resume & Cover Letter Tailoring ==[/]")
    from .tailoring import tailor_resume, tailor_cover_letter
    from .document import create_resume_docx, create_cover_letter_docx, create_resume_pdf, create_cover_letter_pdf

    settings = load_settings()
    conn = get_connection()

    automation = settings.get("automation", {})
    max_per_day = automation.get("max_applications_per_day", 25)
    distribution = automation.get("distribution", "round_robin")

    # Get new jobs that need tailoring
    all_new = get_jobs_by_status(conn, "new", limit=500)
    if not all_new:
        console.print("[yellow]No new jobs to tailor.[/]")
        conn.close()
        return

    # Apply round-robin to tailoring too so we don't tailor 50 of one role
    if distribution == "round_robin":
        jobs = _round_robin_select(all_new, max_per_day)
    else:
        jobs = all_new[:max_per_day]

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


def cmd_apply():
    """Run the application automation."""
    console.print("[bold blue]== Application Automation ==[/]")
    from .applicant import apply_to_jobs
    apply_to_jobs()


def cmd_pipeline():
    """Run the full pipeline: scrape -> tailor -> apply."""
    logger = setup_logging()
    logger.info("Starting JobHunter pipeline")

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

    # Step 1: Scrape
    console.print("\n[bold magenta]=== Step 1/3: Scraping Jobs ===[/]")
    try:
        cmd_scrape()
    except Exception as e:
        console.print(f"[red]Scraping failed: {e}[/]")
        logger.error(f"Scraping failed: {e}")

    # Step 2: Tailor
    if settings.get("tailoring", {}).get("enabled", True):
        console.print("\n[bold magenta]=== Step 2/3: Tailoring Documents ===[/]")
        try:
            cmd_tailor()
        except Exception as e:
            console.print(f"[red]Tailoring failed: {e}[/]")
            logger.error(f"Tailoring failed: {e}")
    else:
        console.print("\n[bold magenta]=== Step 2/3: Tailoring (skipped — disabled in settings) ===[/]")

    # Step 3: Apply
    console.print("\n[bold magenta]=== Step 3/3: Submitting Applications ===[/]")
    try:
        cmd_apply()
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


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        console.print("[bold]JobHunter[/] - Automated Job Application System\n")
        console.print("Usage: python -m src.main <command>\n")
        console.print("Commands:")
        console.print("  [bold]scrape[/]     Scrape job listings from configured boards")
        console.print("  [bold]tailor[/]     Generate tailored resume & cover letter for new jobs")
        console.print("  [bold]apply[/]      Submit applications for tailored jobs")
        console.print("  [bold]pipeline[/]   Run full pipeline: scrape -> tailor -> apply")
        console.print("  [bold]status[/]     Show application status summary")
        console.print("  [bold]list[/]       List jobs (optional: list <status>)")
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
    else:
        console.print(f"[red]Unknown command: {command}[/]")
        console.print("Run 'python -m src.main' for usage info.")


if __name__ == "__main__":
    main()
