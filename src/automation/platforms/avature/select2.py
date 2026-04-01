"""select2 helpers for Avature widgets."""

from .common import logger


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


def _select2_click_result(page, search_text: str, select_id: str, _c,
                          strict_match: bool = False) -> bool:
    """Click the best matching result in the open select2 dropdown."""
    base = '.select2-dropdown:last-of-type .select2-results .select2-results__option'
    skip_lower = {"searching...", "searching\u2026", "no results found",
                  "loading more results...", "loading more results\u2026",
                  "loading...", "loading\u2026", "please enter", ""}

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

    candidates = [o for o in all_options if o["text"].lower() not in skip_lower and not o["disabled"]]
    if not candidates:
        return False

    search_lower = search_text.lower()

    def _normalize_option_text(value: str) -> str:
        import re

        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    search_normalized = _normalize_option_text(search_text)

    for c in candidates:
        if c["text"].lower() == search_lower:
            return _click_option_by_index(page, c["index"], c["text"], select_id, _c)

    strong_matches = []
    for c in candidates:
        normalized = _normalize_option_text(c["text"])
        if not normalized or not search_normalized:
            continue
        if normalized == search_normalized or search_normalized in normalized or normalized in search_normalized:
            strong_matches.append((0 if normalized == search_normalized else 1, len(c["text"]), c))
    if strong_matches:
        strong_matches.sort(key=lambda item: (item[0], item[1]))
        best = strong_matches[0][2]
        return _click_option_by_index(page, best["index"], best["text"], select_id, _c)

    if strict_match:
        return False

    keywords = [search_text]
    if "," in search_text:
        keywords.append(search_text.split(",")[0].strip())
    keywords.append(search_text.split(",")[0].strip())
    seen = set()
    keywords = [k for k in keywords if k and k.lower() not in seen and not seen.add(k.lower())]

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
                has_secondary = 1
                for sp in secondary_parts:
                    if sp and sp in t:
                        has_secondary = 0
                        break
                starts = 0 if t.startswith(kw_lower) else 1
                return (has_secondary, starts, len(c["text"]))
            matches.sort(key=_score)
            best = matches[0]
            return _click_option_by_index(page, best["index"], best["text"], select_id, _c)

    if candidates:
        best = candidates[0]
        return _click_option_by_index(page, best["index"], best["text"], select_id, _c)

    return False


def _select2_pick(page, select_id: str, search_text: str,
                  label_hint: str = "", strict_match: bool = False,
                  allow_custom_value: bool = True) -> bool:
    """Fill a select2 autocomplete widget by its hidden <select> element ID."""
    try:
        from rich.console import Console

        _c = Console(force_terminal=True)

        select_el = page.query_selector(f'select[id="{select_id}"]')
        if not select_el:
            _c.print(f"    [red]select2: select[id={select_id!r}] NOT FOUND[/]")
            return False
        is_s2 = "select2-hidden-accessible" in (select_el.get_attribute("class") or "")
        _c.print(f"    [dim]select2: found select[id={select_id!r}], is_select2={is_s2}[/]")

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
                _c.print("    [dim]select2: found container via parent[/]")
            else:
                return False

        candidates = [search_text]
        base = search_text.split(",")[0].split("(")[0].strip()
        if base != search_text and base not in candidates:
            candidates.append(base)
        words = [w for w in base.split() if w]
        if strict_match:
            if len(words) >= 2:
                first_two = " ".join(words[:2])
                if first_two not in candidates:
                    candidates.append(first_two)
            if words and len(words[0]) >= 5 and words[0] not in candidates:
                candidates.append(words[0])
        else:
            if len(search_text) > 8:
                candidates.append(search_text[:12])
            if len(search_text) > 4:
                candidates.append(search_text[:6])

        for attempt, query in enumerate(candidates):
            page.keyboard.press("Escape")
            page.wait_for_timeout(100)

            trigger = container.locator('.select2-selection').first
            trigger.scroll_into_view_if_needed()
            page.wait_for_timeout(100)
            trigger.click()
            page.wait_for_timeout(300)

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

                opened = False
                arrow = container.locator('.select2-selection__arrow, .select2-selection__rendered').first
                try:
                    if arrow.is_visible(timeout=200):
                        arrow.click()
                        page.wait_for_timeout(300)
                        opened = container.evaluate('el => el.classList.contains("select2-container--open")')
                except Exception:
                    pass
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

            search = page.locator('.select2-search__field:visible').first
            if not search.is_visible(timeout=500):
                search = container.locator('input.select2-search__field').first
            if search.is_visible(timeout=300):
                search.fill("")
                search.press_sequentially(query, delay=60)
                _c.print(f"    [dim]select2: typed {query!r} into search field[/]")
            else:
                _c.print(f"    [yellow]select2: no visible search field for #{select_id}[/]")
                page.keyboard.press("Escape")
                continue

            results_found = False
            for poll in range(12):
                page.wait_for_timeout(200)
                result_info = page.evaluate("""() => {
                    const dropdowns = document.querySelectorAll('.select2-dropdown');
                    const dd = dropdowns[dropdowns.length - 1];
                    if (!dd) return {dropdown: false, count: 0, loading: false, items: []};
                    const results = dd.querySelector('.select2-results');
                    if (!results) return {dropdown: true, count: 0, loading: false, items: []};
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
                    const skipTexts = ['searching...', 'searching\\u2026',
                                       'loading...', 'loading\\u2026',
                                       'loading more results...', 'loading more results\\u2026',
                                       'please enter', 'no results found'];
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
                    _c.print("    [yellow]select2: no dropdown in DOM after typing[/]")
                    break
                if result_info.get("noResults") and not result_info.get("loading"):
                    raw_text = page.evaluate("""() => {
                        const dds = document.querySelectorAll('.select2-dropdown');
                        const dd = dds[dds.length - 1];
                        if (!dd) return '';
                        const msg = dd.querySelector('.select2-results__option');
                        return msg ? msg.innerText.trim().toLowerCase() : '';
                    }""")
                    if "search" in raw_text:
                        continue
                    _c.print(f"    [yellow]select2: 'no results' for {query!r}[/]")
                    break

            if not results_found:
                _c.print(f"    [yellow]select2: no results for {query!r}, trying next candidate[/]")
                page.keyboard.press("Escape")
                page.wait_for_timeout(150)
                continue

            if _select2_click_result(page, search_text, select_id, _c, strict_match=strict_match):
                return True

            page.keyboard.press("Escape")
            page.wait_for_timeout(150)

        if strict_match and allow_custom_value:
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(100)
                trigger = container.locator('.select2-selection').first
                trigger.scroll_into_view_if_needed()
                page.wait_for_timeout(100)
                trigger.click()
                page.wait_for_timeout(300)
                search = page.locator('.select2-search__field:visible').first
                if search.is_visible(timeout=300):
                    search.fill("")
                    search.press_sequentially(search_text, delay=60)
                    page.wait_for_timeout(150)
                    search.press("Enter")
                    page.wait_for_timeout(300)
                    created = page.evaluate("""(args) => {
                        const [selId, expected] = args;
                        const normalize = (value) => (value || '')
                            .toLowerCase()
                            .replace(/[^a-z0-9]+/g, ' ')
                            .trim();
                        const rendered = document.getElementById(`select2-${selId}-container`);
                        const renderedText = rendered ? rendered.innerText.trim() : '';
                        const sel = document.getElementById(selId);
                        return {
                            value: sel ? (sel.value || '') : '',
                            text: renderedText,
                            matched: normalize(renderedText).includes(normalize(expected)),
                        };
                    }""", [select_id, search_text])
                    if created.get("value") or created.get("matched"):
                        _c.print(f"    [green]select2: accepted typed value {search_text!r} for #{select_id}[/]")
                        return True

                    injected = page.evaluate("""(args) => {
                        const [selId, text] = args;
                        const normalize = (value) => (value || '')
                            .toLowerCase()
                            .replace(/[^a-z0-9]+/g, ' ')
                            .trim();
                        const sel = document.getElementById(selId);
                        if (!sel) return null;
                        const opt = new Option(text, text, true, true);
                        sel.add(opt);
                        sel.value = text;
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        const rendered = document.getElementById(`select2-${selId}-container`);
                        if (rendered) {
                            rendered.innerHTML = text;
                            rendered.setAttribute('title', text);
                        }
                        const renderedText = rendered ? rendered.innerText.trim() : '';
                        return {
                            value: sel.value || '',
                            text: renderedText,
                            matched: normalize(renderedText).includes(normalize(text)),
                        };
                    }""", [select_id, search_text])
                    if injected and (injected.get("value") or injected.get("matched")):
                        _c.print(f"    [green]select2: injected custom value {search_text!r} for #{select_id}[/]")
                        return True
            except Exception:
                pass

        _c.print(f"    [red]select2: exhausted all attempts for {search_text!r} on #{select_id}[/]")
        return False
    except Exception as e:
        logger.debug(f"select2_pick failed for #{select_id} ({label_hint}): {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False
