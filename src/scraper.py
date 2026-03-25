"""Job scraper using JobSpy -- searches multiple boards and stores results."""

import time
from datetime import datetime

from rich.console import Console

from .database import (
    get_connection, insert_job, count_jobs_by_status,
    is_scrape_cached, update_scrape_cache
)
from .profile import load_settings

console = Console(force_terminal=True)


def scrape_jobs():
    """Run the job scraper across all configured roles, locations, and sites."""
    from jobspy import scrape_jobs as jobspy_scrape

    settings = load_settings()
    search = settings.get("job_search", {})
    filters = settings.get("filters", {})
    scraping_cfg = settings.get("scraping", {})

    roles = search.get("roles", ["software engineer"])
    locations = search.get("locations", ["United States"])
    sites = search.get("sites", ["indeed"])
    results_per = search.get("results_per_search", 50)
    hours_old = search.get("hours_old", 72)
    job_type = search.get("job_type", "fulltime")
    is_remote = search.get("remote", False)

    cache_enabled = scraping_cfg.get("cache_enabled", True)
    cache_hours = scraping_cfg.get("cache_hours", 12)

    keywords_exclude = [kw.lower() for kw in filters.get("keywords_exclude", [])]
    exclude_companies = [c.lower() for c in filters.get("exclude_companies", [])]
    min_salary = filters.get("min_salary", 0)

    conn = get_connection()
    total_new = 0
    total_skipped = 0
    total_cached = 0

    for role in roles:
        for location in locations:
            # Check scrape cache
            if cache_enabled and is_scrape_cached(conn, role, location, cache_hours):
                console.print(f"\n[dim]Skipping (cached):[/] {role} in {location}")
                total_cached += 1
                continue

            console.print(f"\n[bold blue]Searching:[/] {role} in {location}")

            try:
                jobs_df = jobspy_scrape(
                    site_name=sites,
                    search_term=role,
                    location=location,
                    results_wanted=results_per,
                    hours_old=hours_old,
                    job_type=job_type,
                    is_remote=is_remote,
                    linkedin_fetch_description=True,
                    description_format="markdown",
                    verbose=0,
                )
            except Exception as e:
                console.print(f"  [red]Error scraping {role} in {location}: {e}[/]")
                continue

            if jobs_df is None or jobs_df.empty:
                console.print("  [yellow]No results found.[/]")
                update_scrape_cache(conn, role, location, 0)
                continue

            console.print(f"  Found {len(jobs_df)} listings, processing...")

            new_this_search = 0
            for _, row in jobs_df.iterrows():
                job_data = _row_to_dict(row)
                job_data["search_role"] = role
                job_data["search_location"] = location

                # Apply filters
                if _should_skip(job_data, keywords_exclude, exclude_companies, min_salary):
                    total_skipped += 1
                    continue

                job_id = insert_job(conn, job_data)
                if job_id:
                    total_new += 1
                    new_this_search += 1

            console.print(f"  Added {new_this_search} new jobs")

            # Update cache
            update_scrape_cache(conn, role, location, new_this_search)

            # Rate limiting between searches
            time.sleep(2)

    conn.close()

    console.print(f"\n[bold green]Scraping complete![/]")
    console.print(f"  New jobs added: {total_new}")
    console.print(f"  Filtered out: {total_skipped}")
    if total_cached > 0:
        console.print(f"  Skipped (cached): {total_cached} searches")

    # Show status summary
    conn = get_connection()
    counts = count_jobs_by_status(conn)
    conn.close()
    if counts:
        console.print("\n[bold]Database status:[/]")
        for status, count in sorted(counts.items()):
            console.print(f"  {status}: {count}")


def _row_to_dict(row) -> dict:
    """Convert a JobSpy DataFrame row to a dict for our database."""
    def safe_get(key, default=None):
        try:
            val = row.get(key, default)
            if hasattr(val, 'item'):  # numpy types
                return val.item()
            if val is None or (isinstance(val, float) and str(val) == 'nan'):
                return default
            return val
        except Exception:
            return default

    return {
        "title": safe_get("title", ""),
        "company": safe_get("company_name", safe_get("company", "")),
        "location": safe_get("location", ""),
        "url": safe_get("job_url", safe_get("link", "")),
        "description": safe_get("description", ""),
        "salary_min": safe_get("min_amount"),
        "salary_max": safe_get("max_amount"),
        "job_type": safe_get("job_type", ""),
        "site": safe_get("site", ""),
        "date_posted": str(safe_get("date_posted", "")),
    }


def _should_skip(job_data: dict, keywords_exclude: list, exclude_companies: list,
                 min_salary: float) -> bool:
    """Check if a job should be filtered out."""
    description = (job_data.get("description") or "").lower()
    company = (job_data.get("company") or "").lower()
    title = (job_data.get("title") or "").lower()

    # Excluded keywords in description or title
    for kw in keywords_exclude:
        if kw in description or kw in title:
            return True

    # Excluded companies
    for exc in exclude_companies:
        if exc in company:
            return True

    # Minimum salary filter
    if min_salary > 0:
        salary_max = job_data.get("salary_max")
        if salary_max is not None:
            try:
                if float(salary_max) < min_salary:
                    return True
            except (ValueError, TypeError):
                pass

    return False
