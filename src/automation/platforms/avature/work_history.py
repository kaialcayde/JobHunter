"""Work-history and cleanup fillers for Avature prefill."""

from .common import logger
from .field_fill import (
    _fill_date_field,
    _fill_text_field,
    _get_current_work_experience,
    _get_input_value,
    _get_select2_rendered_text,
    _is_select2,
    _standard_select,
)
from .lookup import _find_control_id_by_label, _find_input_id_by_label
from .select2 import _select2_pick


def _sweep_remaining_select2(page, profile: dict, filled: dict):
    """Sweep all remaining unfilled select2 widgets and try to fill from profile."""
    try:
        unfilled = page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('select.select2-hidden-accessible').forEach(sel => {
                if (sel.value && sel.value !== '') return;
                const container = sel.closest('.fieldSpec, [class*="field"], [class*="group"]');
                const label = container
                    ? container.querySelector('label')
                    : document.querySelector(`label[for="${sel.id}"]`);
                results.push({
                    id: sel.id,
                    label: label ? label.innerText.trim() : '',
                    name: sel.name
                });
            });
            return results;
        }""")
        for item in unfilled:
            label = item["label"].lower()
            sid = item["id"]
            if not sid or sid in [k.split(":")[-1] for k in filled]:
                continue
            if "country" in label and "Country/Territory Code" not in filled:
                p = profile.get("personal", {})
                country = p.get("country_code", p.get("country", "United States"))
                if _select2_pick(page, sid, country, item["label"]):
                    filled[f"select2:{item['label'][:30]}"] = country
    except Exception as e:
        logger.debug(f"select2 sweep failed: {e}")


def fill_work_history(page, profile: dict, filled: dict, console):
    """Fill work-history, phone, consent, and remaining select2 widgets."""
    work_exp_list = profile.get("work_experience", [])

    _max_rows = 0
    for _r in range(20):
        if page.query_selector(f'input[id="172-1-{_r}"]'):
            _max_rows = _r + 1
        else:
            break

    console.print(f"  [dim]Avature WE: {_max_rows} rows, {len(work_exp_list)} work_exp entries[/]")
    for row in range(max(_max_rows, len(work_exp_list))):
        company_el = page.query_selector(f'input[id="172-1-{row}"]')
        title_el = page.query_selector(f'input[id="172-2-{row}"]')
        if not company_el or not title_el:
            break

        try:
            existing_title = (title_el.evaluate('e => e.value') or "").strip().lower()
        except Exception:
            existing_title = ""
        try:
            existing_company = (company_el.evaluate('e => e.value') or "").strip()
        except Exception:
            existing_company = ""
        console.print(f"  [dim]  Row {row}: title={existing_title!r}, company={existing_company!r}[/]")

        matching_job = None
        for we_entry in work_exp_list:
            we_title_lower = we_entry.get("title", "").lower()
            if we_title_lower and (we_title_lower in existing_title or existing_title in we_title_lower):
                matching_job = we_entry
                break

        if matching_job:
            console.print(f"  [dim]  Row {row}: MATCHED -> {matching_job.get('title', '?')} at {matching_job.get('company', '?')}[/]")
            company = matching_job.get("company", "")
            start_date = matching_job.get("start_date", "")
            end_date = matching_job.get("end_date", "")
            is_current = end_date.lower() in ("present", "current", "") if end_date else True

            try:
                company_el.fill(company)
                filled[f"Company Name {row}"] = company
            except Exception:
                pass

            el = page.query_selector(f'select[id="172-3-{row}"]')
            current_val = "Yes" if is_current else "No"
            if el and _standard_select(page, f"172-3-{row}", current_val, "Is Current Position", force=True):
                filled[f"Is Current Position {row}"] = current_val

            if start_date:
                _fill_date_field(page, f"172-4-{row}", start_date, f"Start Date {row}", filled, force=True)

            if end_date and not is_current:
                _fill_date_field(page, f"172-5-{row}", end_date, f"End Date {row}", filled, force=True)
        else:
            console.print(f"  [dim]  Row {row}: UNMATCHED -- skipping (no matching work experience)[/]")

    current_work = _get_current_work_experience(work_exp_list)
    title_id = _find_input_id_by_label(page, "Job Title", input_types=("text",))
    from .lookup import _parse_dataset_field_id

    parsed_title = _parse_dataset_field_id(title_id)
    if current_work and parsed_title:
        dataset_id, _, row_id = parsed_title
        title = current_work.get("title", "")
        company = current_work.get("company", "")
        start_date = current_work.get("start_date", "")
        end_date = current_work.get("end_date", "")
        is_current = str(end_date or "").strip().lower() in ("present", "current", "")

        _fill_text_field(page, title_id, title, "Current Job Title", filled, force=True)

        current_job_select = _find_control_id_by_label(page, "Current Job", "SELECT", dataset_id=dataset_id, row_id=row_id)
        if current_job_select:
            current_val = "Yes" if is_current else "No"
            if _standard_select(page, current_job_select, current_val, "Current Job", force=True):
                filled["Current Job"] = current_val

        employer_id = _find_control_id_by_label(
            page,
            "Employer",
            "SELECT",
            field_class="AutoCompleteField",
            dataset_id=dataset_id,
            row_id=row_id,
        )
        if employer_id and company and _is_select2(page, employer_id):
            other_id = _find_input_id_by_label(
                page,
                "Other",
                input_types=("text", "hidden"),
                dataset_id=dataset_id,
                row_id=row_id,
            )
            existing_other = _get_input_value(page, other_id) if other_id else ""
            rendered_employer = _get_select2_rendered_text(page, employer_id)
            rendered_norm = rendered_employer.strip().lower()

            if existing_other.strip():
                filled["Current Employer"] = existing_other.strip()
            elif rendered_norm and rendered_norm not in ("select an option", "please type"):
                filled["Current Employer"] = rendered_employer.strip()
            elif _select2_pick(page, employer_id, company, "Employer", strict_match=True, allow_custom_value=False):
                filled["Current Employer"] = company
            elif _select2_pick(page, employer_id, "Other", "Employer Other", strict_match=True):
                if other_id and _fill_text_field(
                    page,
                    other_id,
                    company,
                    "Current Employer Other",
                    filled,
                    force=True,
                    allow_hidden=True,
                ):
                    filled["Current Employer"] = company

        start_id = _find_input_id_by_label(
            page,
            "Start Date",
            input_types=("month", "date", "text"),
            dataset_id=dataset_id,
            row_id=row_id,
        )
        if start_id and start_date:
            _fill_date_field(page, start_id, start_date, "Current Job Start Date", filled, force=True)

        if not is_current and end_date:
            end_id = _find_input_id_by_label(
                page,
                "End Date",
                input_types=("month", "date", "text"),
                dataset_id=dataset_id,
                row_id=row_id,
            )
            if end_id:
                _fill_date_field(page, end_id, end_date, "Current Job End Date", filled, force=True)

    personal = profile.get("personal", {})
    phone = personal.get("phone", "")
    if phone:
        import re

        digits_only = re.sub(r"[^\d]", "", phone)
        try:
            phone_inputs = page.query_selector_all(
                'input[type="tel"], input[name*="phone" i], input[id*="phone" i]'
            )
            for pi in phone_inputs:
                try:
                    if pi.is_visible():
                        pi.fill(digits_only)
                        pi.dispatch_event("input")
                        pi.dispatch_event("change")
                        pi.dispatch_event("blur")
                        filled["Phone (digits)"] = digits_only
                except Exception:
                    continue
        except Exception:
            pass

    consent_keywords = ["consent", "privacy", "agree", "terms", "acknowledge"]
    try:
        checkboxes = page.query_selector_all('input[type="checkbox"]')
        for cb in checkboxes:
            try:
                if cb.is_checked() or not cb.is_visible():
                    continue
                label_text = page.evaluate("""cb => {
                    const label = cb.labels?.[0] || cb.closest('label') ||
                                  document.querySelector(`label[for="${cb.id}"]`);
                    return label ? label.innerText.toLowerCase() : '';
                }""", cb)
                if any(kw in label_text for kw in consent_keywords):
                    cb.check()
                    filled[f"consent:{label_text[:40]}"] = True
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"avature consent checkbox fill failed: {e}")

    _sweep_remaining_select2(page, profile, filled)
