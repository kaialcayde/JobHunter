"""Job scraper using JobSpy -- searches multiple boards and stores results."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from rich.console import Console

from ..db import (
    get_connection, insert_job, count_jobs_by_status,
    update_scrape_cache
)
from ..config import load_settings
from ..config.loader import load_domain_blacklist, is_blacklisted_url

console = Console(force_terminal=True)


def scrape_jobs():
    """Run the job scraper across all configured roles, locations, and sites.

    Parallelizes scraping across role+location combos, then inserts results serially.
    """
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

    max_workers = scraping_cfg.get("max_workers", 1)

    keywords_exclude = [kw.lower() for kw in filters.get("keywords_exclude", [])]
    exclude_companies = [c.lower() for c in filters.get("exclude_companies", [])]
    min_salary = filters.get("min_salary", 0)
    strict_title_match = filters.get("strict_title_match", False)
    domain_blacklist = load_domain_blacklist()

    conn = get_connection()

    # Build list of all role+location searches
    # Job-level dedup via url_hash UNIQUE handles duplicates, so always re-scrape
    searches = [(role, loc) for role in roles for loc in locations]

    if not searches:
        console.print("[yellow]No role+location combos configured. Nothing to scrape.[/]")
        conn.close()
        return

    console.print(f"\n[bold blue]Running {len(searches)} searches...[/]\n")

    # Parallel scraping -- each search runs in its own thread
    results = []
    max_workers = min(max_workers, len(searches))

    def _do_scrape(role, location):
        """Scrape a single role+location combo. Returns (role, location, dataframe_or_none, error)."""
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
            return (role, location, jobs_df, None)
        except Exception as e:
            return (role, location, None, str(e))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_do_scrape, role, loc): (role, loc) for role, loc in searches}
        for future in as_completed(futures):
            results.append(future.result())

    # Process results serially (SQLite writes)
    total_new = 0
    total_skipped = 0

    for role, location, jobs_df, error in results:
        if error:
            console.print(f"  [red]{role} in {location}: {error}[/]")
            continue

        if jobs_df is None or jobs_df.empty:
            console.print(f"  [yellow]{role} in {location}: no results[/]")
            update_scrape_cache(conn, role, location, 0)
            continue

        new_this_search = 0
        for _, row in jobs_df.iterrows():
            job_data = _row_to_dict(row)
            job_data["search_role"] = role
            job_data["search_location"] = location

            if is_blacklisted_url(job_data.get("url", ""), domain_blacklist) or \
               is_blacklisted_url(job_data.get("listing_url", ""), domain_blacklist):
                total_skipped += 1
                continue

            if _should_skip(job_data, keywords_exclude, exclude_companies, min_salary,
                           roles=roles, strict_title_match=strict_title_match):
                total_skipped += 1
                continue

            job_id = insert_job(conn, job_data)
            if job_id:
                total_new += 1
                new_this_search += 1

        console.print(f"  [green]{role} in {location}: +{new_this_search} new jobs[/]")
        update_scrape_cache(conn, role, location, new_this_search)

    conn.close()

    console.print(f"\n[bold green]Scraping complete![/]")
    console.print(f"  New jobs added: {total_new}")
    console.print(f"  Filtered out: {total_skipped}")


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

    # Prefer direct apply URL (company's ATS) over listing URL (LinkedIn/Indeed page)
    direct_url = safe_get("job_url_direct")
    listing_url = safe_get("job_url", safe_get("link", ""))
    url = direct_url if direct_url else listing_url

    return {
        "title": safe_get("title", ""),
        "company": safe_get("company_name", safe_get("company", "")),
        "location": safe_get("location", ""),
        "url": url,
        "listing_url": listing_url,  # keep original for reference
        "description": safe_get("description", ""),
        "salary_min": safe_get("min_amount"),
        "salary_max": safe_get("max_amount"),
        "job_type": safe_get("job_type", ""),
        "site": safe_get("site", ""),
        "date_posted": str(safe_get("date_posted", "")),
    }


# Keywords that indicate a title is related to common search roles.
# Maps role fragments to accepted title keywords.
_ROLE_KEYWORDS = {
    "software": ["software", "developer", "engineer", "swe", "backend", "frontend",
                  "fullstack", "full-stack", "full stack", "devops", "sre", "platform"],
    "data engineer": ["data", "engineer", "etl", "pipeline", "analytics", "warehouse",
                      "database", "dbt", "airflow", "spark"],
    "data scien": ["data", "scientist", "machine learning", "ml", "ai", "analytics",
                   "research", "nlp", "deep learning"],
}


def _title_matches_roles(title: str, roles: list[str]) -> bool:
    """Check if a job title is relevant to at least one search role."""
    title_lower = title.lower()
    for role in roles:
        role_lower = role.lower()
        # Direct substring match (e.g., "software engineer" in "Senior Software Engineer")
        if role_lower in title_lower:
            return True
        # Check keyword mappings
        for role_fragment, keywords in _ROLE_KEYWORDS.items():
            if role_fragment in role_lower:
                if any(kw in title_lower for kw in keywords):
                    return True
    return False


def _should_skip(job_data: dict, keywords_exclude: list, exclude_companies: list,
                 min_salary: float, roles: list[str] = None,
                 strict_title_match: bool = False) -> bool:
    """Check if a job should be filtered out."""
    description = (job_data.get("description") or "").lower()
    company = (job_data.get("company") or "").lower()
    title = (job_data.get("title") or "").lower()

    for kw in keywords_exclude:
        if kw in description or kw in title:
            return True

    for exc in exclude_companies:
        if exc in company:
            return True

    if min_salary > 0:
        salary_max = job_data.get("salary_max")
        if salary_max is not None:
            try:
                if float(salary_max) < min_salary:
                    return True
            except (ValueError, TypeError):
                pass

    if strict_title_match and roles and title:
        if not _title_matches_roles(title, roles):
            return True

    return False
