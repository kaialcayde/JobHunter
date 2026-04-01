"""Public Avature automation package."""

from .field_fill import (
    _fill_date_field,
    _fill_text_field,
    _get_current_work_experience,
    _get_input_value,
    _get_select2_rendered_text,
    _is_select2,
    _normalize_date,
    _standard_select,
)
from .lookup import (
    _find_control_id_by_label,
    _find_input_id_by_label,
    _find_select_id_by_label,
    _normalize_label_text,
    _parse_dataset_field_id,
)
from .prefill import prefill
from .select2 import (
    _click_option_by_index,
    _select2_click_result,
    _select2_pick,
)
from .vision import handle_avature_page
from .work_history import _sweep_remaining_select2

__all__ = [
    "prefill",
    "handle_avature_page",
    "_select2_pick",
    "_select2_click_result",
    "_click_option_by_index",
    "_standard_select",
    "_normalize_label_text",
    "_parse_dataset_field_id",
    "_find_control_id_by_label",
    "_find_select_id_by_label",
    "_find_input_id_by_label",
    "_get_current_work_experience",
    "_fill_text_field",
    "_get_input_value",
    "_get_select2_rendered_text",
    "_is_select2",
    "_fill_date_field",
    "_normalize_date",
    "_sweep_remaining_select2",
]
