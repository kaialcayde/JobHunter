"""Document generation — creates DOCX and PDF versions of tailored resumes and cover letters."""

import json
import re
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH

from .utils import get_application_dir


def _add_formatted_text(doc: Document, text: str):
    """Parse LLM-generated text and add it to a DOCX document with basic formatting.

    Handles markdown-style headings (##), bold (**text**), and bullet points (- or *).
    """
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            doc.add_paragraph("")
            continue

        # Headings
        if stripped.startswith("### "):
            p = doc.add_heading(stripped[4:], level=3)
        elif stripped.startswith("## "):
            p = doc.add_heading(stripped[3:], level=2)
        elif stripped.startswith("# "):
            p = doc.add_heading(stripped[2:], level=1)
        # Bullet points
        elif stripped.startswith("- ") or stripped.startswith("* "):
            content = stripped[2:]
            p = doc.add_paragraph(style="List Bullet")
            _add_inline_formatting(p, content)
        else:
            p = doc.add_paragraph()
            _add_inline_formatting(p, stripped)


def _add_inline_formatting(paragraph, text: str):
    """Handle **bold** inline formatting within a paragraph."""
    parts = re.split(r'(\*\*.*?\*\*)', text)
    for part in parts:
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        else:
            paragraph.add_run(part)


def create_resume_docx(tailored_text: str, company: str, position: str) -> Path:
    """Create a DOCX resume from tailored text.

    Returns the path to the created file.
    """
    app_dir = get_application_dir(company, position)
    output_path = app_dir / "resume.docx"

    doc = Document()

    # Set default font — 10.5pt for one-page fit
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Calibri"
    font.size = Pt(10.5)

    # Tighten paragraph spacing for one-page resume
    paragraph_format = style.paragraph_format
    paragraph_format.space_before = Pt(0)
    paragraph_format.space_after = Pt(2)
    paragraph_format.line_spacing = 1.0

    # Narrow margins to maximize space
    for section in doc.sections:
        section.top_margin = Inches(0.4)
        section.bottom_margin = Inches(0.4)
        section.left_margin = Inches(0.5)
        section.right_margin = Inches(0.5)

    _add_formatted_text(doc, tailored_text)
    doc.save(str(output_path))
    return output_path


def create_cover_letter_docx(tailored_text: str, company: str, position: str) -> Path:
    """Create a DOCX cover letter from tailored text.

    Returns the path to the created file.
    """
    app_dir = get_application_dir(company, position)
    output_path = app_dir / "cover_letter.docx"

    doc = Document()

    style = doc.styles["Normal"]
    font = style.font
    font.name = "Calibri"
    font.size = Pt(11)

    for section in doc.sections:
        section.top_margin = Inches(1.0)
        section.bottom_margin = Inches(1.0)
        section.left_margin = Inches(1.0)
        section.right_margin = Inches(1.0)

    _add_formatted_text(doc, tailored_text)
    doc.save(str(output_path))
    return output_path


def convert_to_pdf(docx_path: Path) -> Path:
    """Convert a DOCX file to PDF. Returns the PDF path."""
    pdf_path = docx_path.with_suffix(".pdf")
    try:
        from docx2pdf import convert
        convert(str(docx_path), str(pdf_path))
    except Exception as e:
        # Fallback: if docx2pdf fails (needs Word installed on Windows),
        # we'll note it but continue — the DOCX is still valid
        print(f"  Warning: PDF conversion failed ({e}). DOCX file is still available.")
        return None
    return pdf_path


def save_application_metadata(company: str, position: str, job: dict, form_answers: dict = None):
    """Save application metadata as JSON alongside the documents."""
    app_dir = get_application_dir(company, position)
    metadata = {
        "job_title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "job_url": job.get("url"),
        "site": job.get("site"),
        "applied_at": datetime.now().isoformat(),
        "form_answers": form_answers,
    }
    meta_path = app_dir / "application.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    return meta_path
