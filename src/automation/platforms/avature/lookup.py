"""Field and label lookup helpers for Avature."""

from ...browser_scripts import evaluate_script


def _normalize_label_text(value: str) -> str:
    """Normalize Avature label text for fuzzy matching."""
    import re

    text = (value or "").lower()
    text = text.replace("*", " ")
    text = text.replace("select an option", " ")
    return re.sub(r"\s+", " ", text).strip()


def _parse_dataset_field_id(value: str | None) -> tuple[str, str, str] | None:
    """Parse Avature dataset ids like 6076-11-0 into (dataset, col, row)."""
    import re

    if not value:
        return None
    match = re.match(r"^(\d+)-(\d+)-([^-]+)$", value)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _find_control_id_by_label(
    page,
    label_text: str,
    tag_name: str,
    field_class: str = "",
    input_types: tuple[str, ...] = (),
    dataset_id: str | None = None,
    row_id: str | None = None,
) -> str | None:
    """Find the best-matching control id for a visible Avature label."""
    try:
        candidates = evaluate_script(
            page,
            "platforms/avature/find_control_id_by_label.js",
            [label_text, tag_name, field_class, list(input_types), dataset_id or "", row_id or ""],
        )
        if not candidates:
            return None

        candidates.sort(key=lambda item: (
            0 if item.get("containerVisible") else 1,
            0 if not item.get("sampleRow") else 1,
            0 if item.get("viaFor") else 1,
            0 if item.get("classMatch") else 1,
            0 if item.get("exactLabel") else 1,
            0 if not item.get("hiddenType") else 1,
            len(item.get("label", "")),
            item.get("id", ""),
        ))
        return candidates[0]["id"]
    except Exception:
        return None


def _find_select_id_by_label(page, label_text: str, field_class: str = "") -> str | None:
    """Find the best select id for a label, preferring live non-sample rows."""
    return _find_control_id_by_label(page, label_text, "SELECT", field_class=field_class)


def _find_input_id_by_label(
    page,
    label_text: str,
    input_types: tuple[str, ...] = ("text",),
    dataset_id: str | None = None,
    row_id: str | None = None,
) -> str | None:
    """Find the best input id by label, optionally scoped to a dataset row."""
    return _find_control_id_by_label(
        page,
        label_text,
        "INPUT",
        input_types=input_types,
        dataset_id=dataset_id,
        row_id=row_id,
    )
