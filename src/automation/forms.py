"""Compatibility facade for form extraction, filling, and uploads."""

from .forms_helpers import (
    _fill_custom_select,
    _fill_react_select,
    dom_fill_fallback,
    dom_select_fallback,
    extract_fields,
    extract_form_fields,
    extract_form_fields_playwright,
    fill_fields,
    fill_form_fields,
    fill_form_fields_playwright,
    find_input_at_coords,
    handle_file_uploads,
)

__all__ = [
    "extract_fields",
    "fill_fields",
    "find_input_at_coords",
    "dom_fill_fallback",
    "dom_select_fallback",
    "extract_form_fields_playwright",
    "fill_form_fields_playwright",
    "extract_form_fields",
    "fill_form_fields",
    "_fill_react_select",
    "_fill_custom_select",
    "handle_file_uploads",
]
