"""Internal helpers for the public forms facade."""

from .api import extract_fields, fill_fields
from .coordinates import find_input_at_coords, dom_fill_fallback, dom_select_fallback
from .dom_backend import extract_form_fields, fill_form_fields
from .playwright_backend import extract_form_fields_playwright, fill_form_fields_playwright
from .selects import _fill_custom_select, _fill_react_select
from .uploads import handle_file_uploads

__all__ = [
    "extract_fields",
    "fill_fields",
    "find_input_at_coords",
    "dom_fill_fallback",
    "dom_select_fallback",
    "extract_form_fields",
    "fill_form_fields",
    "extract_form_fields_playwright",
    "fill_form_fields_playwright",
    "_fill_custom_select",
    "_fill_react_select",
    "handle_file_uploads",
]
