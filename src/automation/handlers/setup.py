"""Kernel setup step."""

from ...db import insert_application, log_action, update_job_status
from ..results import HandlerResult, StepResult
from ...utils import TEMPLATES_DIR, get_application_dir
from .common import console


def handle_setup(job: dict, settings: dict, conn) -> StepResult:
    """Resolve document paths and insert application record."""
    job_id = job["id"]
    url = job.get("url", "")
    listing_url = job.get("listing_url", "")

    if url and "linkedin.com" in url.lower() and listing_url and "linkedin.com" not in listing_url.lower():
        url, listing_url = listing_url, url
    elif not url and listing_url:
        url = listing_url
        listing_url = ""

    if not url:
        console.print("  [yellow]No application URL -- skipping[/]")
        update_job_status(conn, job_id, "skipped")
        return StepResult(
            result=HandlerResult.FAILED,
            message="No application URL",
            metadata={"job_id": job_id},
        )

    update_job_status(conn, job_id, "applying")
    company = job.get("company", "Unknown")
    position = job.get("title", "Unknown")

    app_dir = get_application_dir(company, position)
    resume_pdf = app_dir / "resume.pdf"
    resume_docx = app_dir / "resume.docx"
    cl_pdf = app_dir / "cover_letter.pdf"
    cl_docx = app_dir / "cover_letter.docx"

    resume_file = resume_pdf if resume_pdf.exists() else resume_docx if resume_docx.exists() else None
    cl_file = cl_pdf if cl_pdf.exists() else cl_docx if cl_docx.exists() else None

    if resume_file is None:
        base_resume = TEMPLATES_DIR / "base_resume.docx"
        if base_resume.exists():
            resume_file = base_resume
            console.print("  [dim]Using base resume template (no tailored version)[/]")
    if cl_file is None:
        base_cl = TEMPLATES_DIR / "base_cover_letter.docx"
        if base_cl.exists():
            cl_file = base_cl
            console.print("  [dim]Using base cover letter template (no tailored version)[/]")

    app_id = insert_application(
        conn,
        job_id,
        str(resume_file) if resume_file else None,
        str(cl_file) if cl_file else None,
    )
    log_action(conn, "apply_started", f"URL: {url}", app_id, job_id)

    return StepResult(
        result=HandlerResult.SUCCESS,
        metadata={
            "url": url,
            "listing_url": listing_url,
            "app_dir": app_dir,
            "resume_file": resume_file,
            "cl_file": cl_file,
            "app_id": app_id,
            "company": company,
            "position": position,
            "job_id": job_id,
        },
    )
