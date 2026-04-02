"""Playwright-driven form extraction and fill helpers."""

import logging

from rich.console import Console

from ..browser_scripts import load_script

console = Console(force_terminal=True)
logger = logging.getLogger(__name__)


_CONTROL_METADATA_JS = load_script("forms/control_metadata.js")
_CHECKABLE_LABEL_METADATA_JS = load_script("forms/checkable_label_metadata.js")
_SET_CHECKED_JS = load_script("forms/set_checked.js")
_SELECT_MENU_BUTTON_OPTION_JS = load_script("forms/select_menu_button_option.js")
_SELECT_AUTOCOMPLETE_OPTION_JS = load_script("forms/select_autocomplete_option.js")


def _looks_machine_label(label: str) -> bool:
    label_lower = (label or "").strip().lower()
    if not label_lower:
        return True
    return (
        label_lower.startswith(("input-", "input_", "select-", "select_", "textarea-", "textarea_", "field-", "field_"))
        or label_lower.startswith("custom_select_")
    )


def _resolved_label(attrs: dict) -> str:
    label = (attrs.get("label") or attrs.get("optionLabel") or "").strip()
    context = (attrs.get("contextLabel") or "").strip()
    placeholder = (attrs.get("placeholder") or "").strip()
    label_lower = label.lower()
    if context and (
        _looks_machine_label(label)
        or label == placeholder
        or "show menu" in label_lower
        or label_lower.startswith("select a ")
        or label_lower.startswith("select an ")
    ):
        return context
    return label


def _first_match(value: str, options: list[str]) -> str | None:
    value_lower = (value or "").strip().lower()
    if not value_lower:
        return None
    for option in options:
        option_lower = option.lower()
        if (
            value_lower == option_lower
            or value_lower in option_lower
            or option_lower in value_lower
        ):
            return option
    return None


def _ensure_option_selected(page, option: dict) -> bool:
    """Select a radio/checkbox option and verify the checked state latched."""
    locator = option["locator"]

    if option.get("label_click"):
        try:
            metadata = locator.evaluate(_CHECKABLE_LABEL_METADATA_JS)
        except Exception:
            metadata = None
        if not (metadata or {}).get("checked"):
            try:
                locator.click(timeout=3000)
                page.wait_for_timeout(100)
            except Exception:
                pass
            try:
                metadata = locator.evaluate(_CHECKABLE_LABEL_METADATA_JS)
            except Exception:
                metadata = None
            linked_id = (metadata or {}).get("linkedId")
            if not (metadata or {}).get("checked") and linked_id:
                try:
                    linked = page.locator(f'[id="{linked_id}"]').first
                    linked.evaluate(_SET_CHECKED_JS, True)
                    page.wait_for_timeout(100)
                except Exception:
                    pass
                try:
                    metadata = locator.evaluate(_CHECKABLE_LABEL_METADATA_JS)
                except Exception:
                    metadata = None
        return bool((metadata or {}).get("checked"))

    try:
        locator.check(timeout=3000, force=True)
    except Exception:
        pass
    try:
        if locator.is_checked():
            return True
    except Exception:
        pass
    try:
        locator.evaluate(_SET_CHECKED_JS, True)
        page.wait_for_timeout(100)
    except Exception:
        return False
    try:
        return locator.is_checked()
    except Exception:
        return True


def extract_form_fields_playwright(page) -> list[dict]:
    """Extract form fields using Playwright locators (pierces shadow DOM)."""
    fields = []
    seen_ids = set()

    try:
        text_like = page.locator(
            'input:not([type]), '
            'input[type="text"], input[type="search"], input[type="email"], '
            'input[type="tel"], input[type="number"], input[type="url"], input[type="date"], '
            'textarea'
        ).all()
        for loc in text_like:
            try:
                if not loc.is_visible(timeout=300):
                    continue
                attrs = loc.evaluate(_CONTROL_METADATA_JS)
                label_text = _resolved_label(attrs)
                fid = attrs.get("id") or attrs.get("name") or label_text or f"input_{len(fields)}"
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                field_type = "textarea" if loc.evaluate("el => el.tagName") == "TEXTAREA" else (attrs.get("type") or "text")
                field_dict = {
                    "id": fid,
                    "selector": attrs.get("selector", ""),
                    "label": label_text,
                    "contextLabel": attrs.get("contextLabel", ""),
                    "type": field_type,
                    "required": attrs.get("required", False),
                    "value": attrs.get("value", ""),
                    "visible": True,
                    "_locator": loc,
                }
                fields.append(field_dict)
            except Exception:
                continue
    except Exception:
        pass

    try:
        for loc in page.locator("select").all():
            try:
                if not loc.is_visible(timeout=300):
                    continue
                attrs = loc.evaluate(_CONTROL_METADATA_JS)
                label_text = _resolved_label(attrs)
                fid = attrs.get("id") or attrs.get("name") or label_text or f"select_{len(fields)}"
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                options = loc.evaluate("el => Array.from(el.options).map(o => o.text.trim()).filter(Boolean)")
                fields.append({
                    "id": fid,
                    "selector": attrs.get("selector", ""),
                    "label": label_text,
                    "contextLabel": attrs.get("contextLabel", ""),
                    "type": "select",
                    "required": attrs.get("required", False),
                    "options": options,
                    "visible": True,
                    "_locator": loc,
                })
            except Exception:
                continue
    except Exception:
        pass

    try:
        for loc in page.locator('[role="combobox"], button[aria-haspopup]').all():
            try:
                if not loc.is_visible(timeout=300):
                    continue
                attrs = loc.evaluate(_CONTROL_METADATA_JS)
                button_text = (loc.text_content() or "").strip().lower()
                label_text = _resolved_label(attrs)
                ident_text = f"{attrs.get('id', '')} {label_text}".lower()
                if button_text in {"submit", "next", "continue", "review", "apply"}:
                    continue
                if any(term in button_text for term in ("upload", "browse", "cookie", "accept all", "reject all")):
                    continue
                if any(term in ident_text for term in ("cookie", "onetrust", "accept all", "reject all")):
                    continue
                fid = attrs.get("id") or attrs.get("name") or attrs.get("label") or f"custom_select_{len(fields)}"
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                fields.append({
                    "id": fid,
                    "selector": attrs.get("selector", ""),
                    "label": label_text or (loc.text_content() or "").strip(),
                    "contextLabel": attrs.get("contextLabel", ""),
                    "type": "custom_select",
                    "required": attrs.get("required", False),
                    "options": [],
                    "visible": True,
                    "_locator": loc,
                })
            except Exception:
                continue
    except Exception:
        pass

    radio_groups = {}
    try:
        for loc in page.locator('input[type="radio"]').all():
            try:
                attrs = loc.evaluate(_CONTROL_METADATA_JS)
                if not attrs.get("fieldVisible"):
                    continue
                group_id = attrs.get("groupId") or attrs.get("name") or f"radio_{len(radio_groups)}"
                label_text = _resolved_label(attrs)
                group = radio_groups.setdefault(group_id, {
                    "id": group_id,
                    "selector": attrs.get("selector", ""),
                    "label": attrs.get("groupLabel") or label_text or group_id,
                    "type": "radio",
                    "required": attrs.get("required", False),
                    "options": [],
                    "contextLabel": attrs.get("contextLabel", ""),
                    "_option_locators": [],
                })
                option_label = attrs.get("optionLabel") or attrs.get("label") or attrs.get("value")
                if option_label and option_label not in group["options"]:
                    group["options"].append(option_label)
                    group["_option_locators"].append({"label": option_label, "locator": loc})
            except Exception:
                continue
    except Exception:
        pass
    for group in radio_groups.values():
        if not group["options"]:
            continue
        if len(group["options"]) <= 1 and group["label"] == group["options"][0]:
            continue
        if group["id"] in seen_ids:
            continue
        seen_ids.add(group["id"])
        fields.append(group)

    checkbox_groups = {}
    try:
        for loc in page.locator('input[type="checkbox"]').all():
            try:
                attrs = loc.evaluate(_CONTROL_METADATA_JS)
                if not attrs.get("fieldVisible"):
                    continue
                group_id = attrs.get("groupId") or attrs.get("name") or ""
                label_text = _resolved_label(attrs)
                option_label = attrs.get("optionLabel") or label_text or attrs.get("value")
                if group_id and option_label and group_id != option_label:
                    group = checkbox_groups.setdefault(group_id, {
                        "id": group_id,
                        "selector": attrs.get("selector", ""),
                        "label": attrs.get("groupLabel") or label_text or group_id,
                        "type": "checkbox_group",
                        "required": attrs.get("required", False),
                        "options": [],
                        "contextLabel": attrs.get("contextLabel", ""),
                        "_option_locators": [],
                    })
                    if option_label not in group["options"]:
                        group["options"].append(option_label)
                        group["_option_locators"].append({"label": option_label, "locator": loc})
                    continue

                fid = attrs.get("id") or attrs.get("name") or option_label or f"checkbox_{len(fields)}"
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
                fields.append({
                    "id": fid,
                    "selector": attrs.get("selector", ""),
                    "label": option_label or label_text,
                    "contextLabel": attrs.get("contextLabel", ""),
                    "type": "checkbox",
                    "required": attrs.get("required", False),
                    "checked": attrs.get("checked", False),
                    "visible": True,
                    "_locator": loc,
                })
            except Exception:
                continue
    except Exception:
        pass
    for group in checkbox_groups.values():
        if group["id"] in seen_ids:
            continue
        seen_ids.add(group["id"])
        fields.append(group)

    label_groups = {}
    try:
        for loc in page.locator('label[for]').all():
            try:
                if not loc.is_visible(timeout=300):
                    continue
                attrs = loc.evaluate(_CHECKABLE_LABEL_METADATA_JS)
                option_label = (attrs.get("optionLabel") or "").strip()
                if not option_label:
                    continue
                group_type = None
                if attrs.get("radioLike"):
                    group_type = "radio"
                elif attrs.get("checkboxLike"):
                    group_type = "checkbox_group"
                if not group_type:
                    continue
                group_id = attrs.get("groupId") or option_label
                group = label_groups.setdefault(group_id, {
                    "id": group_id,
                    "selector": f'label[for="{attrs.get("linkedId", "")}"]' if attrs.get("linkedId") else "",
                    "label": attrs.get("groupLabel") or group_id,
                    "type": group_type,
                    "required": attrs.get("required", False),
                    "options": [],
                    "contextLabel": attrs.get("groupLabel", ""),
                    "_option_locators": [],
                })
                if option_label not in group["options"]:
                    group["options"].append(option_label)
                    group["_option_locators"].append({
                        "label": option_label,
                        "locator": loc,
                        "label_click": True,
                    })
            except Exception:
                continue
    except Exception:
        pass
    for group in label_groups.values():
        if len(group["options"]) <= 1:
            continue
        if group["id"] in seen_ids:
            continue
        seen_ids.add(group["id"])
        fields.append(group)

    try:
        for loc in page.locator('input[type="file"]').all():
            try:
                attrs = loc.evaluate(_CONTROL_METADATA_JS)
                fid = attrs.get("id") or attrs.get("name") or f"file_{len(fields)}"
                fields.append({
                    "id": fid,
                    "selector": attrs.get("selector", ""),
                    "label": _resolved_label(attrs),
                    "contextLabel": attrs.get("contextLabel", ""),
                    "type": "file",
                    "accept": loc.get_attribute("accept") or "",
                    "_locator": loc,
                })
            except Exception:
                continue
    except Exception:
        pass

    if fields:
        logger.info(f"Playwright extraction found {len(fields)} fields (shadow DOM aware)")
    return fields


def fill_form_fields_playwright(page, fields: list[dict], answers: dict):
    """Fill form fields using Playwright locators (pierces shadow DOM)."""
    for field in fields:
        fid = field["id"]
        if fid not in answers or field["type"] == "file":
            continue

        value = str(answers[fid])
        if not value or value == "N/A":
            continue

        loc = field.get("_locator")
        try:
            if field["type"] == "select":
                try:
                    loc.select_option(label=value, timeout=3000)
                except Exception:
                    matched = _first_match(value, field.get("options", []) or [])
                    if not matched:
                        raise
                    loc.select_option(label=matched, timeout=3000)
                console.print(f"  [dim]  Filled '{field.get('label', fid)}' = '{value[:30]}'[/]")
            elif field["type"] == "custom_select":
                loc.click(timeout=3000)
                page.wait_for_timeout(300)
                selected_via_menu = False
                for _ in range(10):
                    try:
                        menu_result = loc.evaluate(_SELECT_MENU_BUTTON_OPTION_JS, value)
                    except Exception:
                        menu_result = None
                    status = (menu_result or {}).get("status")
                    if status == "selected":
                        selected_via_menu = True
                        break
                    if status != "loading":
                        break
                    page.wait_for_timeout(250)
                if selected_via_menu:
                    console.print(f"  [dim]  Filled '{field.get('label', fid)}' = '{value[:30]}'[/]")
                    continue
                panel_id = loc.get_attribute("aria-controls") or loc.get_attribute("aria-owns") or ""
                try:
                    page.get_by_role("option", name=value, exact=False).first.click(timeout=2000)
                except Exception:
                    if panel_id:
                        try:
                            panel = page.locator(f'[id="{panel_id}"]').first
                            panel.get_by_text(value, exact=False).first.click(timeout=1500)
                            console.print(f"  [dim]  Filled '{field.get('label', fid)}' = '{value[:30]}'[/]")
                            continue
                        except Exception:
                            pass
                    try:
                        search = page.locator('input:focus, textarea:focus').first
                        if search.is_visible(timeout=500):
                            search.fill(value, timeout=1500)
                        else:
                            raise RuntimeError("no visible custom-select search input")
                    except Exception:
                        try:
                            if (loc.get_attribute("aria-expanded") or "").lower() != "true":
                                loc.press("ArrowDown", timeout=1000)
                                page.wait_for_timeout(200)
                        except Exception:
                            pass
                        page.keyboard.type(value, delay=30)
                    page.wait_for_timeout(300)
                    try:
                        page.get_by_role("option", name=value, exact=False).first.click(timeout=1500)
                    except Exception:
                        if panel_id:
                            try:
                                panel = page.locator(f'[id="{panel_id}"]').first
                                panel.get_by_text(value, exact=False).first.click(timeout=1500)
                                console.print(f"  [dim]  Filled '{field.get('label', fid)}' = '{value[:30]}'[/]")
                                continue
                            except Exception:
                                pass
                        page.keyboard.press("ArrowDown")
                        page.keyboard.press("Enter")
                console.print(f"  [dim]  Filled '{field.get('label', fid)}' = '{value[:30]}'[/]")
            elif field["type"] in ("text", "search", "email", "tel", "number", "url", "date", "textarea"):
                loc.fill(value, timeout=3000)
                label_lower = (field.get("label", "") or "").lower()
                if any(term in label_lower for term in ("street", "address")):
                    for _ in range(8):
                        try:
                            autocomplete_result = loc.evaluate(_SELECT_AUTOCOMPLETE_OPTION_JS, value)
                        except Exception:
                            autocomplete_result = None
                        status = (autocomplete_result or {}).get("status")
                        if status == "selected":
                            page.wait_for_timeout(500)
                            break
                        if status != "loading":
                            break
                        page.wait_for_timeout(250)
                console.print(f"  [dim]  Filled '{field.get('label', fid)}' = '{value[:30]}'[/]")
            elif field["type"] == "radio":
                option_value = _first_match(value, field.get("options", []) or []) or value
                for option in field.get("_option_locators", []):
                    if option_value.lower() in option["label"].lower() or option["label"].lower() in option_value.lower():
                        if _ensure_option_selected(page, option):
                            console.print(f"  [dim]  Selected '{option['label']}' for '{field.get('label', fid)}'[/]")
                            break
            elif field["type"] == "checkbox_group":
                desired = [
                    token.strip() for token in value.replace(";", ",").split(",") if token.strip()
                ]
                if not desired:
                    desired = [value]
                matched_any = False
                for option in field.get("_option_locators", []):
                    should_check = any(
                        token.lower() in option["label"].lower() or option["label"].lower() in token.lower()
                        for token in desired
                    )
                    if should_check:
                        matched_any = _ensure_option_selected(page, option) or matched_any
                if matched_any:
                    console.print(f"  [dim]  Checked '{field.get('label', fid)}' = '{value[:30]}'[/]")
            elif field["type"] == "checkbox":
                should_check = value.lower() in ("true", "yes", "1", "checked", "agree")
                is_checked = loc.is_checked()
                if should_check != is_checked:
                    try:
                        loc.check(timeout=3000, force=True)
                    except Exception:
                        loc.evaluate(_SET_CHECKED_JS, should_check)
        except Exception as e:
            logger.warning(f"Failed to fill '{field.get('label', fid)}': {e}")
            console.print(f"  [dim]  Failed to fill '{field.get('label', fid)}': {str(e)[:40]}[/]")
