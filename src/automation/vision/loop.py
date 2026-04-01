"""Main vision-agent loop."""

import base64
import json
import time

from .actions import _execute_action, _extract_batch_coords
from .client import (
    _decide_actions,
    _get_vision_client,
    _get_vision_detail,
    _get_vision_model,
    _is_vision_logging,
    _take_screenshot,
)
from .common import MAX_CONSECUTIVE_SCROLLS, MAX_ROUNDS, SYSTEM_PROMPT, console, logger
from .otp import _try_resolve_otp
from .submission import _handle_done_status, _handle_stuck_status, _try_dom_advance


def _run_platform_page_handler(page, job, settings, resume_file, cl_file,
                               account_registry, history):
    """Run platform-owned page handlers until they stop advancing."""
    from urllib.parse import urlparse

    from ..platforms import get_platform_vision_page_handler

    visited: dict[str, int] = {}
    loops = 0
    max_path_visits = 6
    while loops < 10:
        handler = get_platform_vision_page_handler(page.url)
        if not handler:
            break
        path = urlparse(page.url).path
        visit_count = visited.get(path, 0)
        if visit_count >= max_path_visits:
            break
        visited[path] = visit_count + 1
        result = handler(page, job, settings, resume_file, cl_file, account_registry, history)
        loops += 1
        if result == "done":
            return "done"
        if result != "advanced":
            break
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(500)
    return "advanced" if loops > 0 else "none"


def run_vision_agent(page, job: dict, settings: dict,
                     resume_file=None, cl_file=None,
                     initial_history: list = None,
                     account_registry=None) -> bool:
    """Run the vision-based browser agent to complete a job application."""
    from ...config import get_profile_summary, load_profile
    from ...db import get_connection as get_db_conn, get_saved_answers

    profile = load_profile()
    profile_summary = get_profile_summary(profile)

    db_conn = get_db_conn()
    saved_answers = get_saved_answers(db_conn)
    db_conn.close()
    answered = {q: a for q, a in saved_answers.items() if a != "N/A"}
    answer_bank_text = ""
    if answered:
        lines = [f'  - "{q}": "{a}"' for q, a in answered.items()]
        answer_bank_text = "\n\n## Pre-Answered Questions (use these exact answers when you see matching fields)\n" + "\n".join(lines)

    company = job.get("company", "Unknown")
    position = job.get("title", "Unknown")

    system_prompt = SYSTEM_PROMPT.format(
        company=company,
        position=position,
        profile_summary=profile_summary,
    ) + answer_bank_text

    client = _get_vision_client(settings)
    model = _get_vision_model(settings)
    detail = _get_vision_detail(settings)
    vision_logging = _is_vision_logging(settings)

    history = list(initial_history) if initial_history else []
    resume_already_uploaded = bool(initial_history)
    prev_batch_coords = set()
    repeat_count = 0
    type_loop_rounds = 0
    single_action_repeats = 0
    prev_single_action_key = None
    otp_round_count = 0
    consecutive_scrolls = 0
    round_start_url = page.url

    console.print(f"  [magenta]Vision agent active (model: {model}, detail: {detail})[/]")

    platform_result = _run_platform_page_handler(page, job, settings, resume_file, cl_file, account_registry, history)
    if platform_result == "done":
        return True
    round_start_url = page.url

    for round_num in range(MAX_ROUNDS):
        try:
            current_url = page.url
            if current_url != round_start_url:
                from urllib.parse import urlparse

                prev_path = urlparse(round_start_url).path
                curr_path = urlparse(current_url).path
                if prev_path != curr_path:
                    if resume_already_uploaded:
                        resume_already_uploaded = False
                        console.print("  [dim]New page detected -- re-enabling resume upload[/]")
                    platform_result = _run_platform_page_handler(page, job, settings, resume_file, cl_file, account_registry, history)
                    if platform_result == "done":
                        return True
                    if platform_result == "advanced":
                        repeat_count = 0
                        prev_batch_coords = None
                        resume_already_uploaded = False
                        round_start_url = page.url
                        single_action_repeats = 0
                        prev_single_action_key = None
                        continue
                round_start_url = current_url

            screenshot_b64 = _take_screenshot(page)
            try:
                import pathlib

                dbg_dir = pathlib.Path("data/logs")
                dbg_dir.mkdir(parents=True, exist_ok=True)
                round_shot = dbg_dir / f"vision_round_{round_num+1}.png"
                round_shot.write_bytes(base64.b64decode(screenshot_b64))
                if settings.get("automation", {}).get("debug_mode"):
                    console.print(f"\n  [bold yellow]DEBUG: Vision round {round_num+1} screenshot saved: {round_shot}[/]")
                    console.print("  [bold yellow]  Inspect the browser, then press Enter to send screenshot to GPT-4o...[/]")
                    try:
                        input()
                    except EOFError:
                        pass
            except Exception:
                pass

            for attempt in range(3):
                try:
                    response = _decide_actions(client, model, screenshot_b64, system_prompt, history, detail=detail)
                    break
                except Exception as api_err:
                    if "429" in str(api_err) and attempt < 2:
                        wait = (attempt + 1) * 1.0
                        logger.warning(f"Vision round {round_num+1}: rate limited, retrying in {wait}s")
                        time.sleep(wait)
                    else:
                        raise

            status = response.get("status", "continue")
            actions = response.get("actions", [])
            overall_reasoning = response.get("reasoning", "")

            if vision_logging:
                logger.info(f"Vision round {round_num+1}: status={status}, {len(actions)} actions - {overall_reasoning}")
                console.print(f"  [dim]  Round {round_num+1}: {len(actions)} actions, status={status}[/]")

            if status == "done":
                result = _handle_done_status(page, settings, history, job, resume_file, cl_file)
                if result == "submitted":
                    return True
                if result == "captcha_failed":
                    return False
                if result == "needs_verification":
                    return "needs_verification"
                continue

            if status == "stuck":
                result, page = _handle_stuck_status(
                    page, settings, history, overall_reasoning, round_num, job, resume_file, cl_file
                )
                if result == "continue":
                    continue
                if result in ("needs_login", "already_applied", "needs_verification"):
                    return result
                return False

            if not actions:
                history.append("Round returned no actions. If form is complete, click Submit. If stuck, report stuck.")
                continue

            otp_keywords = ["verification code", "verify code", "otp", "one-time", "confirmation code", "security code"]
            action_texts = " ".join(a.get("reasoning", "") for a in actions).lower()
            if any(kw in action_texts for kw in otp_keywords):
                otp_round_count += 1
                if otp_round_count == 1:
                    otp_code = _try_resolve_otp(page, settings)
                    if otp_code:
                        otp_round_count = 0
                        history.append("Verification code was entered automatically. Now click Submit/Continue to proceed.")
                        continue
                    if otp_code is None:
                        return "needs_login"
                elif otp_round_count >= 2:
                    console.print("  [yellow]Vision agent: OTP/verification code required -- cannot proceed[/]")
                    return "needs_login"
            else:
                otp_round_count = 0

            current_coords = _extract_batch_coords(actions)
            if current_coords and current_coords == prev_batch_coords:
                repeat_count += 1
                if repeat_count >= 2:
                    if vision_logging:
                        console.print(f"  [yellow]  Round {round_num+1}: fields targeted 3x -- attempting DOM next/submit[/]")
                    advance = _try_dom_advance(page, settings, history, "repeat bypass")
                    if advance == "advanced":
                        repeat_count = 0
                        prev_batch_coords = None
                        single_action_repeats = 0
                        prev_single_action_key = None
                        continue
                    if advance == "submitted":
                        return True
                    history.append(
                        "CRITICAL: You have targeted the same fields 3 times. The fields ARE filled — "
                        "you cannot see the values due to rendering. STOP trying to fill fields. "
                        "Scroll down and click the Submit/Apply/Continue/Next button NOW."
                    )
                else:
                    history.append(
                        "WARNING: You targeted the same fields as last round but they are still empty. "
                        "Your coordinates may be off — try clicking more precisely at the CENTER of each input field."
                    )
                if vision_logging:
                    console.print(f"  [yellow]  Round {round_num+1}: same fields targeted again, warning model[/]")
            else:
                repeat_count = 0
            prev_batch_coords = current_coords

            type_actions = sum(1 for a in actions if a.get("action") in ("type", "click"))
            if type_actions >= len(actions) * 0.5 and any(
                "re-fill" in r or "refill" in r or "appears empty" in r
                or "appears incorrect" in r or "not filled" in r or "required but" in r
                for a in actions for r in [a.get("reasoning", "").lower()]
            ):
                type_loop_rounds += 1
            else:
                type_loop_rounds = 0

            if type_loop_rounds >= 4:
                if vision_logging:
                    console.print(f"  [yellow]  Round {round_num+1}: type-loop detected ({type_loop_rounds} rounds) -- forcing DOM next/submit[/]")
                advance = _try_dom_advance(page, settings, history, "type-loop bypass")
                if advance == "advanced":
                    type_loop_rounds = 0
                    repeat_count = 0
                    prev_batch_coords = None
                    single_action_repeats = 0
                    prev_single_action_key = None
                    continue
                if advance == "submitted":
                    return True
                history.append(
                    "CRITICAL: You have been refilling the same fields for many rounds. "
                    "STOP filling fields and click Submit/Continue/Next NOW. If Submit does not work, report 'stuck'."
                )

            if len(actions) == 1:
                a = actions[0]
                action_key = (a.get("action", ""), a.get("reasoning", "")[:40])
                if action_key == prev_single_action_key:
                    single_action_repeats += 1
                else:
                    single_action_repeats = 1
                    prev_single_action_key = action_key
                if single_action_repeats >= 3:
                    if vision_logging:
                        console.print(f"  [yellow]  Round {round_num+1}: single action repeated {single_action_repeats}x -- skipping[/]")
                    history.append(
                        f"CRITICAL: You have tried '{a.get('reasoning', '')[:60]}' for {single_action_repeats} rounds "
                        "but it is not working. SKIP this element entirely."
                    )
                    continue
            else:
                single_action_repeats = 0
                prev_single_action_key = None

            round_results = []
            has_clicks = False

            for i, action in enumerate(actions):
                act_type = action.get("action", "unknown")
                reasoning = action.get("reasoning", "")

                if act_type == "scroll":
                    consecutive_scrolls += 1
                    if consecutive_scrolls > MAX_CONSECUTIVE_SCROLLS:
                        round_results.append("Scroll skipped (too many consecutive scrolls)")
                        continue
                else:
                    consecutive_scrolls = 0

                if act_type == "upload_resume" and resume_already_uploaded:
                    skip_msg = f"Skipped re-upload at round {round_num+1}: resume was already uploaded before vision agent started"
                    round_results.append(skip_msg)
                    if vision_logging:
                        console.print(f"  [dim]    {i+1}. upload_resume: BLOCKED (already uploaded)[/]")
                    continue

                if act_type == "upload_resume":
                    resume_already_uploaded = True

                if vision_logging:
                    console.print(f"  [dim]    {i+1}. {act_type}: {reasoning[:70]}[/]")

                url_before = page.url
                try:
                    result = _execute_action(page, action, resume_file, cl_file)
                    round_results.append(result)
                except Exception as e:
                    logger.debug(f"Action execution error: {e}", exc_info=True)
                    round_results.append(f"Error executing {act_type}: {str(e)[:60]}")
                url_after = page.url
                if url_before != url_after:
                    console.print(f"  [red]  !! Action {i+1} ({act_type}: {reasoning[:50]}) NAVIGATED: {url_before[-60:]} -> {url_after[-60:]}[/]")
                    logger.warning(f"Action {i+1} ({act_type}: {reasoning}) navigated from {url_before} to {url_after}")
                if act_type == "click":
                    has_clicks = True

            summary = f"Round {round_num+1}: executed {len(round_results)} actions: " + "; ".join(round_results)
            history.append(summary)
            if vision_logging:
                logger.info(summary)

            if has_clicks:
                from ..detection import detect_captcha, try_solve_captcha
                from .submission import _dom_refill_after_captcha

                if detect_captcha(page):
                    console.print("  [cyan]CAPTCHA detected after click -- attempting solve[/]")
                    if try_solve_captcha(page, settings):
                        console.print("  [green]CAPTCHA solved![/]")
                        _dom_refill_after_captcha(page, job, settings, resume_file, cl_file)
                        history.append("CAPTCHA was blocking after click. Solved and form re-filled via DOM. Proceed with the form.")
                        page.wait_for_timeout(2000)
                    else:
                        history.append("CAPTCHA detected after click but could not solve.")

            time.sleep(1.0 if has_clicks else 0.5)

        except json.JSONDecodeError as e:
            logger.warning(f"Vision round {round_num+1}: invalid JSON from model: {e}")
            history.append("Error: model returned invalid JSON — try again with valid JSON")
            continue
        except Exception as e:
            logger.exception(f"Vision round {round_num+1} error")
            history.append(f"Error: {str(e)[:80]}")
            continue

    console.print(f"  [yellow]Vision agent: hit round limit ({MAX_ROUNDS})[/]")
    return False
