"""Avature-specific page transitions for the generic vision loop."""

from rich.console import Console

from .common import logger

console = Console(force_terminal=True)


def handle_avature_page(page, job, settings, resume_file, cl_file,
                        account_registry, history) -> str:
    """Handle Avature-specific page transitions deterministically."""
    from ...detection import click_next_button
    from ...forms import extract_form_fields, fill_form_fields, handle_file_uploads
    from ....core.tailoring import infer_form_answers

    url = page.url
    console.print(f"  [dim]handle_avature_page: url={url[-60:]}[/]")

    if "/careers/Register" in url:
        console.print("  [dim]Avature Register page detected -- DOM fill + submit[/]")
        try:
            try:
                fields = extract_form_fields(page)
                console.print(f"  [dim]Avature Register: extracted {len(fields) if fields else 0} fields[/]")
            except Exception as fe:
                console.print(f"  [dim]Avature Register: extract_form_fields failed: {fe}[/]")
                fields = []
            if fields:
                import re as _re

                _avature_we_id = _re.compile(r'^172-\d+-\d+$')
                text_fields = [
                    f for f in fields
                    if f.get("type", "").lower() in ("text", "email", "tel", "url", "textarea", "number", "date", "month", "hidden")
                    and not _avature_we_id.match(f.get("id") or "")
                ]
                if text_fields:
                    answers = infer_form_answers(text_fields, job, settings)
                    fill_form_fields(page, text_fields, answers)

            from .. import get_platform_prefill
            from ....config.loader import load_profile

            platform_prefill = get_platform_prefill(url)
            if platform_prefill:
                try:
                    _profile_data = load_profile()
                except Exception:
                    _profile_data = settings
                platform_prefill(page, _profile_data, settings)

            if account_registry:
                from urllib.parse import urlparse as _up

                hostname = _up(url).hostname or ""
                creds = account_registry.get_credentials(hostname)
                if creds:
                    pw = creds.get("password", "")
                    pw_locators = page.locator('input[type="password"]').all()
                    console.print(f"  [dim]Avature: filling {len(pw_locators)} password field(s) with registry pw (len={len(pw)})[/]")
                    for pw_loc in pw_locators:
                        try:
                            if pw_loc.is_visible(timeout=500):
                                pw_loc.fill(pw, timeout=3000)
                                pw_loc.dispatch_event("blur")
                                page.wait_for_timeout(100)
                        except Exception as _pwe:
                            logger.debug(f"Avature password locator fill failed: {_pwe}")
            page.wait_for_timeout(500)

            advanced = False
            for btn_name in ("Save and continue", "Continue", "Next"):
                try:
                    loc = page.get_by_role("button", name=btn_name, exact=False).first
                    loc.wait_for(state="visible", timeout=2000)
                    loc.click(timeout=3000)
                    advanced = True
                    break
                except Exception:
                    continue
            if not advanced:
                advanced = click_next_button(page)
            if advanced:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                new_url = page.url
                if "/careers/Register" in new_url:
                    errors = page.evaluate("""() => {
                        const errs = [];
                        const isRelevant = (node) => {
                            if (!node) return false;
                            const container = node.closest('.datasetfieldSpec, .fieldSpec, [class*="field"], [class*="group"]') || node;
                            const ident = `${container.id || ''} ${node.id || ''} ${node.name || ''}`;
                            if (ident.includes('-sample')) return false;
                            const style = window.getComputedStyle(container);
                            if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
                            const rect = container.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0;
                        };
                        document.querySelectorAll('.fieldSpec--error, [class*="error"], [class*="invalid"]').forEach(el => {
                            const container = el.closest('.fieldSpec, [class*="field"], [class*="group"]') || el;
                            if (!isRelevant(container)) return;
                            const label = container.querySelector('label');
                            const labelText = label ? label.innerText.trim().substring(0, 50) : '';
                            const errMsg = el.innerText?.trim().substring(0, 80) || '';
                            if (labelText || errMsg) {
                                errs.push(labelText + ' -> ' + errMsg);
                            }
                        });
                        document.querySelectorAll('select.select2-hidden-accessible, select.SelectFormField').forEach(sel => {
                            if (!isRelevant(sel)) return;
                            if (!sel.value || sel.value === '0' || sel.value === '') {
                                const container = sel.closest('.fieldSpec, [class*="field"]');
                                const label = container
                                    ? container.querySelector('label')
                                    : document.querySelector('label[for="' + sel.id + '"]');
                                const txt = label ? label.innerText.trim().substring(0, 50) : sel.id;
                                errs.push('EMPTY_SELECT: ' + txt);
                            }
                        });
                        return errs.slice(0, 15);
                    }""")
                    if errors:
                        console.print(f"  [yellow]Avature Register: validation errors: {errors[:5]}[/]")
                    else:
                        console.print("  [yellow]Avature Register: page did NOT advance (no visible errors)[/]")
                    try:
                        import os

                        debug_path = os.path.join("data", "logs", "debug_avature_register_validation.png")
                        os.makedirs(os.path.dirname(debug_path), exist_ok=True)
                        page.screenshot(path=debug_path)
                        console.print(f"  [dim]Screenshot saved: {debug_path}[/]")
                    except Exception:
                        pass

                    error_text = " ".join(str(e) for e in (errors or []))
                    if "existing record" in error_text.lower():
                        console.print("  [cyan]Avature: account already exists -- switching to login[/]")
                        login_clicked = False
                        for link_text in ["Sign In", "Log In", "Login", "Already have an account"]:
                            try:
                                link = page.get_by_role("link", name=link_text, exact=False).first
                                if link.is_visible(timeout=1000):
                                    link.click(timeout=3000)
                                    login_clicked = True
                                    break
                            except Exception:
                                continue
                        if not login_clicked:
                            try:
                                login_clicked = page.evaluate("""() => {
                                    for (const a of document.querySelectorAll('a')) {
                                        const t = (a.textContent || '').toLowerCase();
                                        if (t.includes('sign in') || t.includes('log in') || t.includes('login')) {
                                            a.click();
                                            return true;
                                        }
                                    }
                                    return false;
                                }""")
                            except Exception:
                                pass
                        if login_clicked:
                            try:
                                page.wait_for_load_state("domcontentloaded", timeout=5000)
                            except Exception:
                                pass
                            page.wait_for_timeout(1000)
                            if account_registry:
                                from urllib.parse import urlparse as _up2

                                _host = _up2(page.url).hostname or ""
                                creds = account_registry.get_credentials(_host)
                                if creds:
                                    email = creds.get("email", "")
                                    pw = creds.get("password", "")
                                    console.print(f"  [dim]Avature login: filling email={email[:3]}*** pw=len({len(pw)})[/]")
                                    for sel in ['input[type="email"]', 'input[name*="email" i]', 'input[id*="email" i]',
                                                'input[name*="user" i]', 'input[id*="user" i]']:
                                        try:
                                            loc = page.locator(sel).first
                                            if loc.is_visible(timeout=500):
                                                loc.fill(email, timeout=2000)
                                                break
                                        except Exception:
                                            continue
                                    for pw_loc in page.locator('input[type="password"]').all():
                                        try:
                                            if pw_loc.is_visible(timeout=500):
                                                pw_loc.fill(pw, timeout=2000)
                                                break
                                        except Exception:
                                            continue
                                    page.wait_for_timeout(300)
                                    for btn_name in ("Sign In", "Log In", "Login", "Submit"):
                                        try:
                                            btn = page.get_by_role("button", name=btn_name, exact=False).first
                                            btn.wait_for(state="visible", timeout=1500)
                                            btn.click(timeout=3000)
                                            break
                                        except Exception:
                                            continue
                                    try:
                                        page.wait_for_load_state("networkidle", timeout=8000)
                                    except Exception:
                                        pass
                                    page.wait_for_timeout(1500)
                                    console.print(f"  [dim]Avature login: submitted, now at {page.url[-60:]}[/]")
                                    history.append("Avature: logged in with existing account.")
                                    return "advanced"
                        console.print("  [yellow]Avature: could not switch to login -- falling through[/]")
                    return "none"
                history.append("Avature Register page: filled via DOM and clicked Save and continue.")
                console.print("  [dim]Avature Register: submitted[/]")
                return "advanced"
        except Exception as e:
            logger.debug(f"Avature Register page handler failed: {e}")
        return "none"

    if "/careers/ApplicationMethods" in url:
        console.print("  [dim]Avature ApplicationMethods page detected -- upload resume + Continue[/]")
        try:
            if resume_file:
                handle_file_uploads(page, resume_file, cl_file)
                page.wait_for_timeout(1000)
            advanced = False
            for btn_name in ("Continue", "Save and continue", "Next"):
                try:
                    loc = page.get_by_role("button", name=btn_name, exact=False).first
                    loc.wait_for(state="visible", timeout=2000)
                    loc.click(timeout=3000)
                    advanced = True
                    break
                except Exception:
                    continue
            if not advanced:
                advanced = click_next_button(page)
            if advanced:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                history.append("Avature ApplicationMethods: uploaded resume and clicked Continue.")
                console.print("  [dim]Avature ApplicationMethods: advanced[/]")
                return "advanced"
        except Exception as e:
            logger.debug(f"Avature ApplicationMethods handler failed: {e}")
        return "none"

    if "/careers/ApplicationForm" in url:
        console.print("  [dim]Avature ApplicationForm detected -- answering compliance questions[/]")
        try:
            from ....config.loader import load_profile

            try:
                _profile_data = load_profile()
            except Exception:
                _profile_data = settings
            auth = _profile_data.get("work_authorization", {})
            authorized = auth.get("authorized_us", True)
            requires_sponsorship = auth.get("requires_sponsorship", False)
            address = _profile_data.get("personal", {}).get("address", {})
            country = address.get("country", "United States")

            answered = page.evaluate("""(args) => {
                const [authorized, requiresSponsor, country] = args;
                const groups = {};
                for (const radio of document.querySelectorAll('input[type="radio"]')) {
                    if (!radio.name) continue;
                    if (!groups[radio.name]) groups[radio.name] = [];
                    groups[radio.name].push(radio);
                }

                let filled = 0;
                for (const [name, radios] of Object.entries(groups)) {
                    if (radios.some(r => r.checked)) continue;

                    let questionText = '';
                    const first = radios[0];
                    const fieldset = first.closest('fieldset') || first.closest('.formField') || first.closest('[class*="Field"]');
                    if (fieldset) {
                        const legend = fieldset.querySelector('legend, label, [class*="label"], [class*="question"]');
                        if (legend) questionText = legend.innerText.toLowerCase();
                        else questionText = fieldset.innerText.toLowerCase();
                    }
                    if (!questionText) {
                        let el = first.parentElement;
                        for (let i = 0; i < 3 && el; i++) {
                            questionText = el.innerText.toLowerCase();
                            if (questionText.length > 10) break;
                            el = el.parentElement;
                        }
                    }

                    let answer = null;
                    if (questionText.includes('legally authorized') || questionText.includes('authorized to work')) {
                        answer = authorized ? 'yes' : 'no';
                    } else if (questionText.includes('sponsorship') || questionText.includes('visa')) {
                        answer = requiresSponsor ? 'yes' : 'no';
                    } else if (questionText.includes('previously employed') || questionText.includes('previously work') ||
                               questionText.includes('current or former employee') || questionText.includes('been employed by') ||
                               questionText.includes('relative') || questionText.includes('family member') ||
                               questionText.includes('spouse') || questionText.includes('referral') ||
                               questionText.includes('referred by')) {
                        answer = 'no';
                    } else {
                        answer = 'no';
                    }

                    for (const radio of radios) {
                        const lbl = document.querySelector(`label[for="${radio.id}"]`);
                        const lblText = (lbl ? lbl.innerText : radio.value || radio.nextSibling?.textContent || '').toLowerCase().trim();
                        if (lblText === answer || (answer === 'yes' && lblText.startsWith('yes')) ||
                            (answer === 'no' && lblText.startsWith('no'))) {
                            radio.click();
                            radio.dispatchEvent(new Event('change', {bubbles: true}));
                            filled++;
                            break;
                        }
                    }
                }
                return filled;
            }""", [authorized, requires_sponsorship, country])

            if answered:
                console.print(f"  [dim]Avature ApplicationForm: answered {answered} compliance question(s)[/]")

            diversity = _profile_data.get("diversity", {})
            eeo_filled = page.evaluate("""(diversity) => {
                let filled = 0;
                for (const sel of document.querySelectorAll('select')) {
                    if (sel.value && sel.value !== '0' && sel.value !== '') continue;
                    const container = sel.closest('.formField, .fieldSpec, [class*="Field"]');
                    if (!container) continue;
                    const label = container.querySelector('label, legend, [class*="label"]');
                    const labelText = (label ? label.innerText : '').toLowerCase();

                    let targetText = '';
                    if (labelText.includes('ethnicity') || labelText.includes('race')) {
                        targetText = diversity.ethnicity || 'decline';
                    } else if (labelText.includes('gender') || labelText.includes('sex')) {
                        targetText = diversity.gender || 'decline';
                    }
                    if (!targetText) continue;

                    const target = targetText.toLowerCase();
                    let bestOption = null;
                    for (const opt of sel.options) {
                        const optText = opt.text.toLowerCase();
                        if (optText.includes('decline') || optText.includes('prefer not') ||
                            optText.includes('do not wish') || optText.includes('not to self')) {
                            if (!bestOption || target.includes('decline') || target.includes('prefer not')) {
                                bestOption = opt;
                            }
                        }
                        if (target && optText.includes(target)) {
                            bestOption = opt;
                            break;
                        }
                    }
                    if (!bestOption) {
                        for (const opt of sel.options) {
                            const t = opt.text.toLowerCase();
                            if (t.includes('decline') || t.includes('prefer not') || t.includes('not to self')) {
                                bestOption = opt;
                                break;
                            }
                        }
                    }
                    if (bestOption) {
                        sel.value = bestOption.value;
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        filled++;
                    }
                }
                return filled;
            }""", {
                "ethnicity": diversity.get("ethnicity", ""),
                "gender": diversity.get("gender", ""),
                "veteran_status": diversity.get("veteran_status", ""),
                "disability_status": diversity.get("disability_status", ""),
            })

            if eeo_filled:
                console.print(f"  [dim]Avature ApplicationForm: filled {eeo_filled} EEO question(s)[/]")
            page.wait_for_timeout(500)

            advanced = False
            for btn_name in ("Continue", "Save and continue", "Next", "Submit"):
                try:
                    loc = page.get_by_role("button", name=btn_name, exact=False).first
                    loc.wait_for(state="visible", timeout=1500)
                    loc.click(timeout=3000)
                    advanced = True
                    break
                except Exception:
                    continue
            if not advanced:
                advanced = click_next_button(page)
            if advanced:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
                total_answered = (answered or 0) + (eeo_filled or 0)
                history.append(f"Avature ApplicationForm: answered {total_answered} question(s) (compliance={answered}, EEO={eeo_filled}) and clicked Continue.")
                console.print("  [dim]Avature ApplicationForm: advanced[/]")
                return "advanced"
        except Exception as e:
            logger.debug(f"Avature ApplicationForm handler failed: {e}")
        return "none"

    if "/careers/Finalize" in url or "/careers/Submit" in url:
        console.print("  [dim]Avature Finalize page detected -- clicking Submit[/]")
        try:
            advanced = False
            for btn_name in ("Submit Application", "Submit", "Finalize", "Confirm"):
                try:
                    loc = page.get_by_role("button", name=btn_name, exact=False).first
                    loc.wait_for(state="visible", timeout=2000)
                    loc.click(timeout=3000)
                    advanced = True
                    break
                except Exception:
                    continue
            if not advanced:
                advanced = click_next_button(page)
            if advanced:
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                history.append("Avature Finalize: clicked Submit Application.")
                console.print("  [green]Avature Finalize: submitted[/]")
                return "done"
        except Exception as e:
            logger.debug(f"Avature Finalize handler failed: {e}")
        return "none"

    return "none"
