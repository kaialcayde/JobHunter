"""Field and label lookup helpers for Avature."""


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
        candidates = page.evaluate(r"""(args) => {
            const [labelText, tagName, fieldClass, inputTypes, datasetId, rowId] = args;
            const normalize = (value) => (value || '')
                .toLowerCase()
                .replace(/\*/g, ' ')
                .replace(/select an option/gi, ' ')
                .replace(/\s+/g, ' ')
                .trim();
            const wanted = normalize(labelText);
            const wantedTokens = wanted.split(' ').filter(token => token.length >= 3);
            const wantedTypes = new Set((inputTypes || []).map(t => String(t).toLowerCase()));
            const wantedTag = String(tagName || '').toUpperCase();
            const seen = new Set();
            const matches = [];

            const isVisible = (node) => {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                if (!style || style.display === 'none' || style.visibility === 'hidden') {
                    return false;
                }
                const rect = node.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };

            const datasetMatches = (el) => {
                if (!datasetId) return true;
                const ident = el.id || el.name || '';
                if (!ident.startsWith(`${datasetId}-`)) return false;
                if (!rowId) return true;
                return ident.endsWith(`-${rowId}`);
            };

            const controlMatches = (el) => {
                if (!el || !el.id || el.tagName !== wantedTag) return false;
                if (fieldClass && !(el.className || '').includes(fieldClass)) return false;
                if (wantedTypes.size) {
                    const kind = String(el.type || '').toLowerCase();
                    if (!wantedTypes.has(kind)) return false;
                }
                return datasetMatches(el);
            };

            const push = (el, label, container, viaFor) => {
                if (!controlMatches(el)) return;
                if (seen.has(el.id)) return;
                seen.add(el.id);
                matches.push({
                    id: el.id,
                    label: label ? (label.innerText || '') : '',
                    exactLabel: normalize(label ? label.innerText : '') === wanted,
                    viaFor: !!viaFor,
                    containerVisible: isVisible(container) || isVisible(label),
                    sampleRow: /-sample$/i.test(el.id) || /-sample$/i.test(el.name || ''),
                    hiddenType: String(el.type || '').toLowerCase() === 'hidden',
                    classMatch: !fieldClass || (el.className || '').includes(fieldClass),
                });
            };

            for (const label of document.querySelectorAll('label')) {
                const labelNorm = normalize(label.innerText || '');
                const tokenMatch = wantedTokens.length >= 2
                    && wantedTokens.every(token => labelNorm.includes(token));
                if (!labelNorm || (!labelNorm.includes(wanted) && !wanted.includes(labelNorm) && !tokenMatch)) {
                    continue;
                }
                const container = label.closest(
                    '.datasetfieldSpec, .fieldSpec, [class*="field"], [class*="group"]'
                ) || label.parentElement;

                if (label.htmlFor) {
                    push(document.getElementById(label.htmlFor), label, container, true);
                }

                const scope = container || label.parentElement || document;
                scope.querySelectorAll(wantedTag.toLowerCase()).forEach((el) => {
                    push(el, label, container, false);
                });
            }

            return matches;
        }""", [label_text, tag_name, field_class, list(input_types), dataset_id or "", row_id or ""])
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
