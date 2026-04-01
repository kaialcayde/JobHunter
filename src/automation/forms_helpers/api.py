"""Public entry points for form extraction and filling."""

from .dom_backend import extract_form_fields, fill_form_fields
from .playwright_backend import extract_form_fields_playwright, fill_form_fields_playwright


def extract_fields(page, *, use_playwright: bool = False) -> list[dict]:
    """Unified form field extraction."""
    if use_playwright:
        fields = extract_form_fields_playwright(page)
        if fields:
            return fields
    return extract_form_fields(page)


def fill_fields(page, fields: list[dict], answers: dict, *, use_playwright: bool = False):
    """Unified form filling."""
    if use_playwright and any(f.get("_locator") for f in fields):
        fill_form_fields_playwright(page, fields, answers)
    else:
        fill_form_fields(page, fields, answers)
