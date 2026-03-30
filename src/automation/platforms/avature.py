"""Avature ATS platform-specific DOM pre-fill.

Avature uses two dropdown widget types that the generic extract_form_fields
misses or times out on. This module fills them directly via targeted DOM
interaction BEFORE the vision agent runs.

DOM structure (observed on bloomberg.avature.net, 2026-03-28):

  Standard <select> — class "SelectFormField WizardFieldInputContainer WizardFieldInput"
    Filled via Playwright page.select_option(). Used for: Degree Type, Written
    Level, Spoken Level, State, Pronouns, Is Current Position, Are you 18.

  select2 autocomplete — class contains "AutoCompleteField" +
    "AutocompleteSelectFieldChildHtmlElement" + "select2-hidden-accessible".
    The actual <select> is hidden (tabindex=-1, aria-hidden=true). Interaction
    goes through the sibling <span class="select2-container">:
      1. Click span.select2-selection (the combobox trigger)
      2. Type in the search input that appears (.select2-search__field)
      3. Pick the matching .select2-results__option
    Used for: School, Field of Study, Language, Country/Territory Code, Skills.

  Text inputs:  Standard <input> / <textarea> (handled by generic fill).
  Checkboxes:   Standard input[type="checkbox"] (handled by generic fill).
  Date fields:  input[type="month"] — handled by generic fill.

Field ID patterns (multipleDatasetEntry rows):
  Education: 2238-{col}-{row} → col 1=Degree Type, 2=School, 3=Field of Study,
             4=Graduation Date, 5=Specify Other
  Language:  629-{col}-{row} → col 1=Language, 2=Written Level, 3=Spoken Level
  Work Exp:  172-{col}-{row} → col 1=Company, 2=Title, 3=Is Current, 4=Start, 5=End

Use `python -m src apply-job <id> --debug` to pause after pre-fill and inspect
the browser's live DOM before writing additional selectors here.
"""

import logging

logger = logging.getLogger(__name__)


# ── select2 autocomplete helper ──────────────────────────────────────────────

def _select2_pick(page, select_id: str, search_text: str,
                  label_hint: str = "") -> bool:
    """Fill a select2 autocomplete widget by its hidden <select> element ID.

    Key insight: select2 appends a DETACHED dropdown to <body>. It links back
    to the originating widget via `data-select2-id` on the container. But the
    simplest way to scope is: after clicking our trigger, the LAST
    `.select2-dropdown` in the DOM is ours. We also verify by checking that
    `.select2-container--open` is set on our container.

    Previous bug: the result dump was picking up `.select2-selection__choice`
    elements (already-selected tags from a Skills multi-select), not actual
    dropdown results. Fixed by strictly querying inside `.select2-results`.

    Returns True if an option was selected.
    """
    try:
        from rich.console import Console
        _c = Console(force_terminal=True)

        # Check if the select element even exists
        select_el = page.query_selector(f'select[id="{select_id}"]')
        if not select_el:
            _c.print(f"    [red]select2: select[id={select_id!r}] NOT FOUND[/]")
            return False
        is_s2 = "select2-hidden-accessible" in (select_el.get_attribute("class") or "")
        _c.print(f"    [dim]select2: found select[id={select_id!r}], is_select2={is_s2}[/]")

        # Find the select2 container (sibling or child of parent)
        container = page.locator(
            f'select[id="{select_id}"] + span.select2-container, '
            f'select[id="{select_id}"] ~ span.select2-container'
        ).first
        if not container.is_visible(timeout=500):
            _c.print(f"    [red]select2: container not visible for #{select_id}[/]")
            parent = page.locator(f'select[id="{select_id}"]').locator('..')
            alt_container = parent.locator('span.select2-container').first
            if alt_container.is_visible(timeout=500):
                container = alt_container
                _c.print(f"    [dim]select2: found container via parent[/]")
            else:
                return False

        # Search term candidates: full, then shorter prefixes
        candidates = [search_text]
        if len(search_text) > 8:
            candidates.append(search_text[:12])
        if len(search_text) > 4:
            candidates.append(search_text[:6])
        # Also try without commas or parenthetical
        base = search_text.split(",")[0].split("(")[0].strip()
        if base != search_text and base not in candidates:
            candidates.insert(1, base)

        for attempt, query in enumerate(candidates):
            # Close any previously open dropdown
            page.keyboard.press("Escape")
            page.wait_for_timeout(100)

            # Scroll the widget into view before clicking — select2 won't open
            # if the trigger is below the viewport
            trigger = container.locator('.select2-selection').first
            trigger.scroll_into_view_if_needed()
            page.wait_for_timeout(100)
            trigger.click()
            page.wait_for_timeout(300)

            # Verify dropdown actually opened — container should have --open class
            is_open = page.evaluate("""(selId) => {
                const sel = document.getElementById(selId);
                if (!sel) return false;
                const cont = sel.closest('.fieldSpec, [class*="field"]')
                           || sel.parentElement;
                const s2 = cont ? cont.querySelector('.select2-container') : null;
                return s2 ? s2.classList.contains('select2-container--open') : false;
            }""", select_id)
            _c.print(f"    [dim]select2: trigger clicked for #{select_id}, open={is_open}, typing {query!r} (attempt {attempt+1})[/]")

            if not is_open:
                # Debug: dump the container's actual HTML structure
                debug_html = page.evaluate("""(selId) => {
                    const sel = document.getElementById(selId);
                    if (!sel) return 'SELECT NOT FOUND';
                    const parent = sel.parentElement;
                    if (!parent) return 'NO PARENT';
                    return {
                        parentTag: parent.tagName,
                        parentClass: parent.className.substring(0, 120),
                        selectClass: sel.className.substring(0, 120),
                        siblings: Array.from(parent.children).map(c =>
                            c.tagName + '.' + (c.className || '').substring(0, 60)
                        ).slice(0, 5),
                        inViewport: (() => {
                            const r = sel.getBoundingClientRect();
                            return r.top >= 0 && r.bottom <= window.innerHeight;
                        })()
                    };
                }""", select_id)
                _c.print(f"    [yellow]select2: #{select_id} not opening. DOM: {debug_html}[/]")

                # Try multiple strategies to open the dropdown
                opened = False
                # Strategy 1: click arrow/caret
                arrow = container.locator('.select2-selection__arrow, .select2-selection__rendered').first
                try:
                    if arrow.is_visible(timeout=200):
                        arrow.click()
                        page.wait_for_timeout(300)
                        opened = container.evaluate('el => el.classList.contains("select2-container--open")')
                except Exception:
                    pass
                # Strategy 2: JS trigger — open select2 programmatically
                if not opened:
                    try:
                        page.evaluate("""(selId) => {
                            const el = document.getElementById(selId);
                            if (el && typeof jQuery !== 'undefined') {
                                jQuery('#' + selId).select2('open');
                            }
                        }""", select_id)
                        page.wait_for_timeout(300)
                        opened = True
                        _c.print(f"    [dim]select2: opened #{select_id} via jQuery.select2('open')[/]")
                    except Exception:
                        pass

            # Find the search input — could be inside the dropdown (appended to body)
            # or inside the container (for multi-select tag inputs)
            search = page.locator('.select2-search__field:visible').first
            if not search.is_visible(timeout=500):
                # Some select2 configs put the search input inside the container
                search = container.locator('input.select2-search__field').first
            if search.is_visible(timeout=300):
                search.fill("")
                # Use press_sequentially (type with key events) — critical for select2
                # which fires AJAX on keyup, not on input/change
                search.press_sequentially(query, delay=60)
                _c.print(f"    [dim]select2: typed {query!r} into search field[/]")
            else:
                _c.print(f"    [yellow]select2: no visible search field for #{select_id}[/]")
                page.keyboard.press("Escape")
                continue

            # Poll for AJAX results inside .select2-results (NOT .select2-selection)
            # The dropdown is appended to <body>, so we query globally
            results_found = False
            for poll in range(12):  # up to 2.4s
                page.wait_for_timeout(200)
                result_info = page.evaluate("""() => {
                    // Find the active dropdown (last one appended to body)
                    const dropdowns = document.querySelectorAll('.select2-dropdown');
                    const dd = dropdowns[dropdowns.length - 1];
                    if (!dd) return {dropdown: false, count: 0, loading: false, items: []};
                    const results = dd.querySelector('.select2-results');
                    if (!results) return {dropdown: true, count: 0, loading: false, items: []};
                    // Only count items inside .select2-results (NOT .select2-selection__choice)
                    const opts = results.querySelectorAll('.select2-results__option');
                    const loading = !!results.querySelector('.loading-results, [aria-busy="true"]');
                    const noResults = !!results.querySelector('.select2-results__message');
                    const items = Array.from(opts).slice(0, 8).map(o => ({
                        text: o.innerText.trim().substring(0, 80),
                        cls: o.className.substring(0, 80),
                        role: o.getAttribute('role'),
                        selectable: !o.classList.contains('select2-results__option--disabled')
                                    && o.getAttribute('aria-disabled') !== 'true'
                    }));
                    // Filter out "Searching..." / "Loading..." messages (incl unicode ellipsis)
                    const skipTexts = ['searching...', 'searching\u2026',
                                       'loading...', 'loading\u2026',
                                       'loading more results...', 'loading more results\u2026',
                                       'please enter'];
                    const real = items.filter(i =>
                        i.text && !skipTexts.includes(i.text.toLowerCase())
                    );
                    return {
                        dropdown: true, count: real.length,
                        loading: loading, noResults: noResults,
                        items: real
                    };
                }""")
                if result_info["count"] > 0:
                    results_found = True
                    _c.print(f"    [dim]select2: {result_info['count']} AJAX results for {query!r}:[/]")
                    for o in result_info["items"][:5]:
                        _c.print(f"      [dim]{o['text']!r} selectable={o['selectable']}[/]")
                    break
                if not result_info["dropdown"]:
                    _c.print(f"    [yellow]select2: no dropdown in DOM after typing[/]")
                    break
                if result_info.get("noResults") and not result_info.get("loading"):
                    # Double-check: "No results" vs "Searching..." (still loading)
                    raw_text = page.evaluate("""() => {
                        const dds = document.querySelectorAll('.select2-dropdown');
                        const dd = dds[dds.length - 1];
                        if (!dd) return '';
                        const msg = dd.querySelector('.select2-results__option');
                        return msg ? msg.innerText.trim().toLowerCase() : '';
                    }""")
                    if "search" in raw_text:
                        continue  # still searching, keep polling
                    _c.print(f"    [yellow]select2: 'no results' for {query!r}[/]")
                    break

            if not results_found:
                _c.print(f"    [yellow]select2: no results for {query!r}, trying next candidate[/]")
                page.keyboard.press("Escape")
                page.wait_for_timeout(150)
                continue

            # Click the best result
            if _select2_click_result(page, search_text, select_id, _c):
                return True

            page.keyboard.press("Escape")
            page.wait_for_timeout(150)

        _c.print(f"    [red]select2: exhausted all attempts for {search_text!r} on #{select_id}[/]")
        return False

    except Exception as e:
        logger.debug(f"select2_pick failed for #{select_id} ({label_hint}): {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _select2_click_result(page, search_text: str, select_id: str, _c) -> bool:
    """Click the best matching result in the open select2 dropdown.

    Priority: exact text match > contains search_text > contains keyword > highlighted.
    Scopes all queries to `.select2-results` inside the LAST `.select2-dropdown`
    to avoid picking up `.select2-selection__choice` tags from other widgets.
    """
    base = '.select2-dropdown:last-of-type .select2-results .select2-results__option'
    skip_lower = {"searching...", "searching\u2026", "no results found",
                  "loading more results...", "loading more results\u2026",
                  "loading...", "loading\u2026", "please enter", ""}

    # Gather all visible result options with their text
    all_options = page.evaluate("""(base) => {
        const dds = document.querySelectorAll('.select2-dropdown');
        const dd = dds[dds.length - 1];
        if (!dd) return [];
        const results = dd.querySelector('.select2-results');
        if (!results) return [];
        const opts = results.querySelectorAll('.select2-results__option');
        return Array.from(opts).map((o, i) => ({
            index: i,
            text: o.innerText.trim(),
            disabled: o.classList.contains('select2-results__option--disabled')
                      || o.getAttribute('aria-disabled') === 'true'
        }));
    }""", base)

    # Filter out status messages and disabled items
    candidates = [o for o in all_options
                  if o["text"].lower() not in skip_lower and not o["disabled"]]
    if not candidates:
        return False

    search_lower = search_text.lower()

    # 1. Exact match
    for c in candidates:
        if c["text"].lower() == search_lower:
            return _click_option_by_index(page, c["index"], c["text"], select_id, _c)

    # 2. Build keyword variants to check containment
    keywords = [search_text]
    # "University of California, Los Angeles" → "University of California"
    if "," in search_text:
        keywords.append(search_text.split(",")[0].strip())
    # "Mechanical Engineering, Minor in ..." → "Mechanical Engineering"
    keywords.append(search_text.split(",")[0].strip())
    # Dedupe while preserving order
    seen = set()
    keywords = [k for k in keywords if k and k.lower() not in seen and not seen.add(k.lower())]

    # 3. For each keyword, find the best match
    # Extract distinctive secondary words from search_text
    # "University of California, Los Angeles" → secondary = "los angeles"
    secondary_parts = []
    if "," in search_text:
        secondary_parts.append(search_text.split(",", 1)[1].strip().lower())
    if "-" in search_text:
        secondary_parts.append(search_text.split("-", 1)[1].strip().lower())

    for kw in keywords:
        kw_lower = kw.lower()
        matches = [c for c in candidates if kw_lower in c["text"].lower()]
        if matches:
            def _score(c):
                t = c["text"].lower()
                # Highest priority: secondary keyword match (e.g. "Los Angeles" in result)
                has_secondary = 1
                for sp in secondary_parts:
                    if sp and sp in t:
                        has_secondary = 0
                        break
                # Then: starts with keyword
                starts = 0 if t.startswith(kw_lower) else 1
                # Then: shorter text is more specific
                return (has_secondary, starts, len(c["text"]))
            matches.sort(key=_score)
            best = matches[0]
            return _click_option_by_index(page, best["index"], best["text"], select_id, _c)

    # 4. Fallback: click first selectable option
    if candidates:
        best = candidates[0]
        return _click_option_by_index(page, best["index"], best["text"], select_id, _c)

    return False


def _click_option_by_index(page, index: int, text: str, select_id: str, _c) -> bool:
    """Click a select2 result option by its index in the dropdown."""
    try:
        base = '.select2-dropdown:last-of-type .select2-results .select2-results__option'
        opt = page.locator(base).nth(index)
        if opt.is_visible(timeout=200):
            opt.click()
            page.wait_for_timeout(200)
            _c.print(f"    [green]select2: selected {text!r} for #{select_id}[/]")
            return True
    except Exception as e:
        _c.print(f"    [red]select2: click failed for index {index}: {e}[/]")
    return False


def _standard_select(page, select_id: str, label_text: str,
                     label_hint: str = "", force: bool = False) -> bool:
    """Fill a standard <select> element by ID using Playwright select_option.

    Tries label match first, then value match.
    force=True skips the already-filled check (use for work experience rows where
    Avature's resume parser may have pre-filled incorrect values).
    Returns True if successful.
    """
    try:
        el = page.query_selector(f'select[id="{select_id}"]')
        if not el:
            logger.debug(f"standard_select: #{select_id} not found ({label_hint})")
            return False
        # Check if it already has a non-empty value (skip unless force=True)
        current = el.evaluate('e => e.value')
        if not force and current and current != "" and current != "0":
            logger.debug(f"standard_select: #{select_id} already has value {current!r}")
            return False
        # Try exact label match first
        try:
            page.select_option(f'select[id="{select_id}"]', label=label_text, timeout=1000)
            logger.debug(f"standard_select: label match {label_text!r} on #{select_id}")
            return True
        except Exception:
            pass
        # Try value match (e.g. "CA" as option value for California)
        try:
            page.select_option(f'select[id="{select_id}"]', value=label_text, timeout=1000)
            logger.debug(f"standard_select: value match {label_text!r} on #{select_id}")
            return True
        except Exception:
            pass
        # JS smart match: prefer startsWith, then includes, pick shortest match
        matched = page.evaluate("""(args) => {
            const [selId, text] = args;
            const sel = document.getElementById(selId);
            if (!sel) return false;
            const lc = text.toLowerCase();
            // Priority 1: option text starts with search text
            for (const opt of sel.options) {
                if (opt.text.toLowerCase().startsWith(lc)) {
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
            }
            // Priority 2: option value equals search text (case-insensitive)
            for (const opt of sel.options) {
                if (opt.value.toLowerCase() === lc) {
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
            }
            // Priority 3: contains, but pick shortest match to avoid false positives
            let best = null;
            for (const opt of sel.options) {
                if (opt.text.toLowerCase().includes(lc)) {
                    if (!best || opt.text.length < best.text.length) {
                        best = opt;
                    }
                }
            }
            if (best) {
                sel.value = best.value;
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            return false;
        }""", [select_id, label_text])
        if matched:
            logger.debug(f"standard_select: JS match for {label_text!r} on #{select_id}")
        return matched
    except Exception as e:
        logger.debug(f"standard_select failed for #{select_id} ({label_hint}): {e}")
        return False


def _find_select_id_by_label(page, label_text: str, field_class: str = "") -> str | None:
    """Find a <select> element ID by its associated label text.

    Avature labels use a for="{id}" attribute or are siblings in a fieldSpec container.
    """
    try:
        result = page.evaluate("""(args) => {
            const [labelText, fieldClass] = args;
            const labels = document.querySelectorAll('label');
            for (const label of labels) {
                if (label.innerText.trim().toLowerCase().includes(labelText.toLowerCase())) {
                    // Try label[for] → select#id
                    if (label.htmlFor) {
                        const el = document.getElementById(label.htmlFor);
                        if (el && el.tagName === 'SELECT') return el.id;
                    }
                    // Try sibling/descendant select in same container
                    const container = label.closest(
                        '.fieldSpec, [class*="field"], [class*="group"]'
                    );
                    if (container) {
                        const sel = container.querySelector(
                            fieldClass ? `select.${fieldClass}` : 'select'
                        );
                        if (sel && sel.id) return sel.id;
                    }
                }
            }
            return null;
        }""", [label_text, field_class])
        return result
    except Exception:
        return None


def _is_select2(page, select_id: str) -> bool:
    """Check if a <select> element is wrapped by select2."""
    try:
        return page.evaluate("""(id) => {
            const el = document.getElementById(id);
            return el && el.classList.contains('select2-hidden-accessible');
        }""", select_id)
    except Exception:
        return False


# ── Date field helper ──────────────────────────────────────────────────────

def _fill_date_field(page, input_id: str, date_str: str,
                     label_hint: str, filled: dict,
                     force: bool = False) -> bool:
    """Fill a date/month input field on Avature.

    Avature date fields may be type="month" (YYYY-MM) or type="date" (YYYY-MM-DD)
    or even plain text inputs accepting various formats.

    Accepts date_str in common formats:
      "2023-06", "June 2023", "06/2023", "2023-06-01", "Present"
    force=True overwrites even if already filled (use for work experience rows
    where Avature's resume parser may have pre-filled incorrect dates).
    """
    import re
    el = page.query_selector(f'input[id="{input_id}"]')
    if not el or not el.is_visible():
        # Try label-based fallback
        return False
    try:
        current = el.evaluate('e => e.value')
        if not force and current and current.strip():
            return False  # already filled

        input_type = el.get_attribute("type") or "text"

        # Normalize date_str to YYYY-MM format
        normalized = _normalize_date(date_str)
        if not normalized:
            return False

        if input_type == "month":
            # type="month" expects YYYY-MM
            el.fill(normalized)
            el.dispatch_event("change")
        elif input_type == "date":
            # type="date" expects YYYY-MM-DD
            el.fill(normalized + "-01")
            el.dispatch_event("change")
        else:
            # Plain text — try YYYY-MM first, then original string
            el.fill(normalized)
            el.dispatch_event("change")

        filled[label_hint] = normalized
        logger.debug(f"date_field: filled #{input_id} with {normalized!r}")
        return True
    except Exception as e:
        logger.debug(f"date_field: failed for #{input_id} ({label_hint}): {e}")
        return False


def _normalize_date(date_str: str) -> str | None:
    """Convert various date formats to YYYY-MM.

    Handles: "2023-06", "2023-06-15", "June 2023", "Jun 2023",
             "06/2023", "6/2023", "2023/06"
    Returns None for "Present", "Current", empty strings.
    """
    import re
    if not date_str or date_str.lower() in ("present", "current", "now"):
        return None

    # Already YYYY-MM or YYYY-MM-DD
    m = re.match(r'^(\d{4})-(\d{1,2})', date_str)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"

    # MM/YYYY or M/YYYY
    m = re.match(r'^(\d{1,2})/(\d{4})$', date_str)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"

    # YYYY/MM
    m = re.match(r'^(\d{4})/(\d{1,2})$', date_str)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"

    # "Month YYYY" (e.g., "June 2023", "Jun 2023")
    month_names = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        "january": "01", "february": "02", "march": "03", "april": "04",
        "june": "06", "july": "07", "august": "08", "september": "09",
        "october": "10", "november": "11", "december": "12",
    }
    m = re.match(r'^([A-Za-z]+)\s+(\d{4})$', date_str.strip())
    if m:
        month_key = m.group(1).lower()
        if month_key in month_names:
            return f"{m.group(2)}-{month_names[month_key]}"

    # Just a year — default to January
    m = re.match(r'^(\d{4})$', date_str.strip())
    if m:
        return f"{m.group(1)}-01"

    return None


# ── Public API ──────────────────────────────────────────────────────────────

def prefill(page, profile: dict, settings: dict) -> dict:
    """Fill Avature-specific custom widgets that generic form extraction misses.

    Called in handle_fill_vision AFTER generic extract/fill_form_fields and
    BEFORE run_vision_agent. Fills select2 autocomplete widgets and standard
    selects that the generic code timed out on.

    Returns a dict of {field_label: value_filled} for logging.
    """
    from rich.console import Console
    console = Console(force_terminal=True)
    filled = {}

    personal = profile.get("personal", {})
    education_list = profile.get("education", [])
    languages_list = profile.get("languages", [])

    # ── Education section (multipleDatasetEntry_2238) ────────────────────────
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
        field_of_study = top_edu.get("field_of_study",
                                     top_edu.get("field",
                                     top_edu.get("major", "")))

        degree_search = next(
            (v for k, v in degree_map.items() if k in degree_raw),
            "Bachelor"
        )

        # Degree Type — standard <select> (id=2238-1-0)
        # Try known ID first, then label-based fallback
        for sid in ["2238-1-0"]:
            el = page.query_selector(f'select[id="{sid}"]')
            if el:
                if _standard_select(page, sid, degree_search, "Degree Type"):
                    filled["Degree Type"] = degree_search
                break
        if "Degree Type" not in filled:
            sid = _find_select_id_by_label(page, "Degree Type")
            if sid:
                if _standard_select(page, sid, degree_search, "Degree Type"):
                    filled["Degree Type"] = degree_search

        # School — select2 autocomplete (id=2238-2-0)
        if school:
            for sid in ["2238-2-0"]:
                el = page.query_selector(f'select[id="{sid}"]')
                if el and _is_select2(page, sid):
                    if _select2_pick(page, sid, school, "School"):
                        filled["School"] = school
                    break
            if "School" not in filled:
                sid = _find_select_id_by_label(page, "School", "AutoCompleteField")
                if sid and _is_select2(page, sid):
                    if _select2_pick(page, sid, school, "School"):
                        filled["School"] = school

        # Field of Study — select2 autocomplete (id=2238-3-0)
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
                if sid and _is_select2(page, sid):
                    if _select2_pick(page, sid, field_of_study, "Field of Study"):
                        filled["Field of Study"] = field_of_study

    # ── Language section (multipleDatasetEntry_629) ──────────────────────────
    # Languages can come from profile in multiple formats:
    #   - personal.languages: ["English (native)", "Tagalog (fluent)"]
    #   - top-level languages: [{"language": "English", "written": "Fluent", ...}]
    if not languages_list:
        languages_list = profile.get("personal", {}).get("languages", [])
    if languages_list:
        lang_entry = languages_list[0]
        if isinstance(lang_entry, str):
            # Parse "English (native)" or "English" format
            import re
            m = re.match(r'^(\w+)\s*(?:\((\w+)\))?', lang_entry)
            lang_name = m.group(1) if m else lang_entry
            level_hint = (m.group(2) or "fluent").capitalize() if m else "Fluent"
            # Map common level words to Avature's dropdown labels
            level_map = {"Native": "Fluent", "Fluent": "Fluent",
                         "Advanced": "Advanced", "Intermediate": "Intermediate",
                         "Basic": "Basic", "Beginner": "Basic"}
            written_level = level_map.get(level_hint, "Fluent")
            spoken_level = written_level
        else:
            lang_name = lang_entry.get("language", "English")
            written_level = lang_entry.get("written", "Fluent")
            spoken_level = lang_entry.get("spoken", "Fluent")

        # Language name — select2 autocomplete (id=629-1-0)
        for sid in ["629-1-0"]:
            el = page.query_selector(f'select[id="{sid}"]')
            if el and _is_select2(page, sid):
                if _select2_pick(page, sid, lang_name, "Language"):
                    filled["Language"] = lang_name
                break
        if "Language" not in filled:
            sid = _find_select_id_by_label(page, "Language", "AutoCompleteField")
            if sid and _is_select2(page, sid):
                if _select2_pick(page, sid, lang_name, "Language"):
                    filled["Language"] = lang_name

        # Written Level — standard <select> (id=629-2-0)
        for sid in ["629-2-0"]:
            el = page.query_selector(f'select[id="{sid}"]')
            if el:
                if _standard_select(page, sid, written_level, "Written Level"):
                    filled["Written Level"] = written_level
                break
        if "Written Level" not in filled:
            sid = _find_select_id_by_label(page, "Written Level")
            if sid:
                if _standard_select(page, sid, written_level, "Written Level"):
                    filled["Written Level"] = written_level

        # Spoken Level — standard <select> (id=629-3-0)
        for sid in ["629-3-0"]:
            el = page.query_selector(f'select[id="{sid}"]')
            if el:
                if _standard_select(page, sid, spoken_level, "Spoken Level"):
                    filled["Spoken Level"] = spoken_level
                break
        if "Spoken Level" not in filled:
            sid = _find_select_id_by_label(page, "Spoken Level")
            if sid:
                if _standard_select(page, sid, spoken_level, "Spoken Level"):
                    filled["Spoken Level"] = spoken_level

    # ── Country/Territory Code — select2 autocomplete (id=6377) ──────────────
    country_code = personal.get("country_code",
                                personal.get("country", "United States"))
    for sid in ["6377"]:
        el = page.query_selector(f'select[id="{sid}"]')
        if el and _is_select2(page, sid):
            if _select2_pick(page, sid, country_code, "Country/Territory Code"):
                filled["Country/Territory Code"] = country_code
            break
    if "Country/Territory Code" not in filled:
        sid = _find_select_id_by_label(page, "Country/Territory Code", "AutoCompleteField")
        if sid and _is_select2(page, sid):
            if _select2_pick(page, sid, country_code, "Country/Territory Code"):
                filled["Country/Territory Code"] = country_code

    # ── Country/Territory of Residence — standard <select> (separate from Code)
    country_name = personal.get("country",
                                personal.get("address", {}).get("country", "United States"))
    sid = _find_select_id_by_label(page, "Country/Territory of Residence")
    if sid:
        if _is_select2(page, sid):
            if _select2_pick(page, sid, country_name, "Country of Residence"):
                filled["Country of Residence"] = country_name
        else:
            if _standard_select(page, sid, country_name, "Country of Residence"):
                filled["Country of Residence"] = country_name
    # Also try "Country" as a shorter label match
    if "Country of Residence" not in filled:
        for label in ["Country/Territory of Res", "Country of Residence", "Country"]:
            sid = _find_select_id_by_label(page, label)
            if sid:
                if _is_select2(page, sid):
                    if _select2_pick(page, sid, country_name, label):
                        filled["Country of Residence"] = country_name
                else:
                    if _standard_select(page, sid, country_name, label):
                        filled["Country of Residence"] = country_name
                break

    # ── State — standard <select> (id=169) ───────────────────────────────────
    state = personal.get("state",
                        personal.get("address", {}).get("state",
                        personal.get("location", {}).get("state", "")))
    if state:
        for sid in ["169"]:
            el = page.query_selector(f'select[id="{sid}"]')
            if el:
                if _standard_select(page, sid, state, "State"):
                    filled["State"] = state
                break
        if "State" not in filled:
            sid = _find_select_id_by_label(page, "State")
            if sid:
                if _standard_select(page, sid, state, "State"):
                    filled["State"] = state

    # ── Pronouns — standard <select>, label-based lookup ─────────────────────
    gender = profile.get("diversity", {}).get("gender", "")
    pronoun_map = {"male": "He/Him", "he": "He/Him", "female": "She/Her",
                   "she": "She/Her", "non-binary": "They/Them", "they": "They/Them"}
    pronoun_val = pronoun_map.get(gender.lower(), "He/Him") if gender else "He/Him"
    sid = _find_select_id_by_label(page, "Pronouns")
    if sid:
        if _standard_select(page, sid, pronoun_val, "Pronouns"):
            filled["Pronouns"] = pronoun_val

    # ── "Are you 18" — standard <select>, label-based lookup ──────────────
    for label_text in ["Are you 18", "18 years", "legal age"]:
        sid = _find_select_id_by_label(page, label_text)
        if sid:
            if _standard_select(page, sid, "Yes", label_text):
                filled["Are you 18"] = "Yes"
            break

    # ── Work Experience section (multipleDatasetEntry_172) ──────────────────
    # Avature's resume parser pre-populates rows for real work experience.
    # Strategy: match each existing row by position title to find the correct company/dates.
    # Unmatched rows are skipped (do not fill with fabricated data).
    work_exp_list = profile.get("work_experience", [])

    # Detect how many rows exist in the form
    _max_rows = 0
    for _r in range(20):
        if page.query_selector(f'input[id="172-1-{_r}"]'):
            _max_rows = _r + 1
        else:
            break

    if _max_rows == 0 and work_exp_list:
        # No rows yet — create row 0 for the first work experience
        pass

    console.print(f"  [dim]Avature WE: {_max_rows} rows, {len(work_exp_list)} work_exp entries[/]")
    for row in range(max(_max_rows, len(work_exp_list))):
        company_el = page.query_selector(f'input[id="172-1-{row}"]')
        title_el = page.query_selector(f'input[id="172-2-{row}"]')
        if not company_el or not title_el:
            break

        # Read the existing title to determine if this is a work exp or project row
        try:
            existing_title = (title_el.evaluate('e => e.value') or "").strip().lower()
        except Exception:
            existing_title = ""
        try:
            existing_company = (company_el.evaluate('e => e.value') or "").strip()
        except Exception:
            existing_company = ""
        console.print(f"  [dim]  Row {row}: title={existing_title!r}, company={existing_company!r}[/]")

        # Find the matching work experience entry by title substring
        matching_job = None
        if work_exp_list:
            for we_entry in work_exp_list:
                we_title_lower = we_entry.get("title", "").lower()
                if we_title_lower and (we_title_lower in existing_title or existing_title in we_title_lower):
                    matching_job = we_entry
                    break

        if matching_job:
            console.print(f"  [dim]  Row {row}: MATCHED → {matching_job.get('title', '?')} at {matching_job.get('company', '?')}[/]")
            company = matching_job.get("company", "")
            start_date = matching_job.get("start_date", "")
            end_date = matching_job.get("end_date", "")
            is_current = end_date.lower() in ("present", "current", "") if end_date else True

            # Company Name — always overwrite (resume parser may have put profile name here)
            try:
                company_el.fill(company)
                filled[f"Company Name {row}"] = company
            except Exception:
                pass

            # Is Current Position — force=True because resume parser may have pre-set wrong value
            current_val = "Yes" if is_current else "No"
            el = page.query_selector(f'select[id="172-3-{row}"]')
            if el:
                if _standard_select(page, f"172-3-{row}", current_val, "Is Current Position", force=True):
                    filled[f"Is Current Position {row}"] = current_val

            # Start Date — force=True to overwrite resume parser's pre-filled (possibly wrong) dates
            if start_date:
                _fill_date_field(page, f"172-4-{row}", start_date, f"Start Date {row}", filled, force=True)

            # End Date (only if not current position) — force=True for same reason
            if end_date and not is_current:
                _fill_date_field(page, f"172-5-{row}", end_date, f"End Date {row}", filled, force=True)
        else:
            # Unmatched row — skip (do not fill with fabricated data)
            console.print(f"  [dim]  Row {row}: UNMATCHED — skipping (no matching work experience)[/]")

    # ── Phone number cleanup (Avature requires digits only) ─────────────────
    # Always overwrite phone fields with digits-only and dispatch blur to clear
    # stale validation errors (initial generic fill may have used "626-283-1122").
    phone = personal.get("phone", "")
    if phone:
        import re
        digits_only = re.sub(r'[^\d]', '', phone)
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

    # ── Privacy / consent checkboxes ─────────────────────────────────────────
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

    # ── Generic select2 sweep ────────────────────────────────────────────────
    # Catch any remaining unfilled select2 widgets by label matching
    _sweep_remaining_select2(page, profile, filled)

    if filled:
        console.print(f"  [dim]Avature prefill: filled {len(filled)} custom widgets[/]")
        logger.info(f"Avature prefill filled: {list(filled.keys())}")
    else:
        console.print("  [dim]Avature prefill: no custom widgets matched (may need debug inspection)[/]")

    return filled


def _sweep_remaining_select2(page, profile: dict, filled: dict):
    """Sweep all remaining unfilled select2 widgets and try to fill from profile."""
    try:
        unfilled = page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('select.select2-hidden-accessible').forEach(sel => {
                if (sel.value && sel.value !== '') return;  // already filled
                // Find label
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
            # Try common profile mappings
            if "country" in label and "Country/Territory Code" not in filled:
                p = profile.get("personal", {})
                country = p.get("country_code", p.get("country", "United States"))
                if _select2_pick(page, sid, country, item["label"]):
                    filled[f"select2:{item['label'][:30]}"] = country
    except Exception as e:
        logger.debug(f"select2 sweep failed: {e}")
