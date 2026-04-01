"""Profile-driven section fillers for Avature prefill."""

from .field_fill import _is_select2, _standard_select
from .lookup import _find_select_id_by_label
from .select2 import _select2_pick


def fill_profile_sections(page, profile: dict, filled: dict, console):
    """Fill deterministic profile sections before work history."""
    personal = profile.get("personal", {})
    education_list = profile.get("education", [])
    languages_list = profile.get("languages", [])

    degree_map = {
        "bs": "Bachelor",
        "ba": "Bachelor",
        "bachelor": "Bachelor",
        "ms": "Master",
        "master": "Master",
        "phd": "Doctorate",
        "associate": "Associate",
        "high school": "High School",
    }
    if education_list:
        top_edu = education_list[0]
        degree_raw = str(top_edu.get("degree", "")).lower()
        school = top_edu.get("school", "")
        field_of_study = top_edu.get("field_of_study", top_edu.get("field", top_edu.get("major", "")))

        degree_search = next((v for k, v in degree_map.items() if k in degree_raw), "Bachelor")

        for sid in ["2238-1-0"]:
            el = page.query_selector(f'select[id="{sid}"]')
            if el:
                if _standard_select(page, sid, degree_search, "Degree Type"):
                    filled["Degree Type"] = degree_search
                break
        if "Degree Type" not in filled:
            sid = _find_select_id_by_label(page, "Degree Type")
            if sid and _standard_select(page, sid, degree_search, "Degree Type"):
                filled["Degree Type"] = degree_search

        if school:
            for sid in ["2238-2-0"]:
                el = page.query_selector(f'select[id="{sid}"]')
                if el and _is_select2(page, sid):
                    if _select2_pick(page, sid, school, "School"):
                        filled["School"] = school
                    break
            if "School" not in filled:
                sid = _find_select_id_by_label(page, "School", "AutoCompleteField")
                if sid and _is_select2(page, sid) and _select2_pick(page, sid, school, "School"):
                    filled["School"] = school

        if field_of_study:
            for sid in ["2238-3-0"]:
                el = page.query_selector(f'select[id="{sid}"]')
                if el and _is_select2(page, sid):
                    if _select2_pick(page, sid, field_of_study, "Field of Study"):
                        filled["Field of Study"] = field_of_study
                    break
            if "Field of Study" not in filled:
                sid = _find_select_id_by_label(page, "Field of Study", "AutoCompleteField")
                if not sid:
                    sid = _find_select_id_by_label(page, "Major", "AutoCompleteField")
                if sid and _is_select2(page, sid) and _select2_pick(page, sid, field_of_study, "Field of Study"):
                    filled["Field of Study"] = field_of_study

    if not languages_list:
        languages_list = profile.get("personal", {}).get("languages", [])
    if languages_list:
        lang_entry = languages_list[0]
        if isinstance(lang_entry, str):
            import re

            m = re.match(r'^(\w+)\s*(?:\((\w+)\))?', lang_entry)
            lang_name = m.group(1) if m else lang_entry
            level_hint = (m.group(2) or "fluent").capitalize() if m else "Fluent"
            level_map = {"Native": "Fluent", "Fluent": "Fluent",
                         "Advanced": "Advanced", "Intermediate": "Intermediate",
                         "Basic": "Basic", "Beginner": "Basic"}
            written_level = level_map.get(level_hint, "Fluent")
            spoken_level = written_level
        else:
            lang_name = lang_entry.get("language", "English")
            written_level = lang_entry.get("written", "Fluent")
            spoken_level = lang_entry.get("spoken", "Fluent")

        for sid in ["629-1-0"]:
            el = page.query_selector(f'select[id="{sid}"]')
            if el and _is_select2(page, sid):
                if _select2_pick(page, sid, lang_name, "Language"):
                    filled["Language"] = lang_name
                break
        if "Language" not in filled:
            sid = _find_select_id_by_label(page, "Language", "AutoCompleteField")
            if sid and _is_select2(page, sid) and _select2_pick(page, sid, lang_name, "Language"):
                filled["Language"] = lang_name

        for sid in ["629-2-0"]:
            el = page.query_selector(f'select[id="{sid}"]')
            if el:
                if _standard_select(page, sid, written_level, "Written Level"):
                    filled["Written Level"] = written_level
                break
        if "Written Level" not in filled:
            sid = _find_select_id_by_label(page, "Written Level")
            if sid and _standard_select(page, sid, written_level, "Written Level"):
                filled["Written Level"] = written_level

        for sid in ["629-3-0"]:
            el = page.query_selector(f'select[id="{sid}"]')
            if el:
                if _standard_select(page, sid, spoken_level, "Spoken Level"):
                    filled["Spoken Level"] = spoken_level
                break
        if "Spoken Level" not in filled:
            sid = _find_select_id_by_label(page, "Spoken Level")
            if sid and _standard_select(page, sid, spoken_level, "Spoken Level"):
                filled["Spoken Level"] = spoken_level

    country_code = personal.get("country_code", personal.get("country", "United States"))
    for sid in ["6377"]:
        el = page.query_selector(f'select[id="{sid}"]')
        if el and _is_select2(page, sid):
            if _select2_pick(page, sid, country_code, "Country/Territory Code"):
                filled["Country/Territory Code"] = country_code
            break
    if "Country/Territory Code" not in filled:
        sid = _find_select_id_by_label(page, "Country/Territory Code", "AutoCompleteField")
        if sid and _is_select2(page, sid) and _select2_pick(page, sid, country_code, "Country/Territory Code"):
            filled["Country/Territory Code"] = country_code

    country_name = personal.get("country", personal.get("address", {}).get("country", "United States"))
    sid = _find_select_id_by_label(page, "Country/Territory of Residence")
    if sid:
        if _is_select2(page, sid):
            if _select2_pick(page, sid, country_name, "Country of Residence"):
                filled["Country of Residence"] = country_name
        elif _standard_select(page, sid, country_name, "Country of Residence"):
            filled["Country of Residence"] = country_name
    if "Country of Residence" not in filled:
        for label in ["Country/Territory of Res", "Country of Residence", "Country"]:
            sid = _find_select_id_by_label(page, label)
            if sid:
                if _is_select2(page, sid):
                    if _select2_pick(page, sid, country_name, label):
                        filled["Country of Residence"] = country_name
                elif _standard_select(page, sid, country_name, label):
                    filled["Country of Residence"] = country_name
                break

    state = personal.get("state", personal.get("address", {}).get("state", personal.get("location", {}).get("state", "")))
    if state:
        for sid in ["169"]:
            el = page.query_selector(f'select[id="{sid}"]')
            if el:
                if _standard_select(page, sid, state, "State"):
                    filled["State"] = state
                break
        if "State" not in filled:
            sid = _find_select_id_by_label(page, "State")
            if sid and _standard_select(page, sid, state, "State"):
                filled["State"] = state

    gender = profile.get("diversity", {}).get("gender", "")
    pronoun_map = {"male": "He/Him", "he": "He/Him", "female": "She/Her",
                   "she": "She/Her", "non-binary": "They/Them", "they": "They/Them"}
    pronoun_val = pronoun_map.get(gender.lower(), "He/Him") if gender else "He/Him"
    sid = _find_select_id_by_label(page, "Pronouns")
    if sid and _standard_select(page, sid, pronoun_val, "Pronouns"):
        filled["Pronouns"] = pronoun_val

    for label_text in ["Are you 18", "18 years", "legal age"]:
        sid = _find_select_id_by_label(page, label_text)
        if sid:
            if _standard_select(page, sid, "Yes", label_text):
                filled["Are you 18"] = "Yes"
            break

    top_edu = education_list[0] if education_list else {}
    degree_completed = "Yes"
    grad_year = str(top_edu.get("graduation_year", "")).strip()
    if grad_year.isdigit():
        from datetime import datetime

        if int(grad_year) > datetime.now().year:
            degree_completed = "No"

    common_select_answers = [
        (["completed this degree/program", "completed degree/program"], [degree_completed], "Degree Completed"),
        (["cpa license"], ["I do not have or plan to pursue a CPA license", "No"], "CPA License"),
        (["require sponsorship", "sponsorship"], ["No"], "Sponsorship"),
        (["former employee", "alumni"], ["No"], "Former Employee"),
    ]
    for label_variants, answer_variants, field_key in common_select_answers:
        if field_key in filled:
            continue
        matched = False
        for label_text in label_variants:
            sid = _find_select_id_by_label(page, label_text)
            if not sid:
                continue
            for answer in answer_variants:
                if _standard_select(page, sid, answer, field_key, force=True):
                    filled[field_key] = answer
                    matched = True
                    break
            if matched:
                break
