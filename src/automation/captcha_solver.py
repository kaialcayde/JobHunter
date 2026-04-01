"""CAPTCHA solving via 2Captcha API.

Supports reCAPTCHA v2, hCaptcha, and Cloudflare Turnstile.
Requires CAPTCHA_API_KEY in .env and automation.captcha_solving: true in settings.yaml.
"""

import logging
import os
import ssl
import time
import urllib.parse
import urllib.request

import certifi
from dotenv import load_dotenv

from .browser_scripts import evaluate_script

load_dotenv()

logger = logging.getLogger(__name__)

TWOCAPTCHA_API = "https://2captcha.com"
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
POLL_INTERVAL = 5  # seconds between solution checks
MAX_WAIT = 120  # max seconds to wait for a solution


def _get_api_key() -> str | None:
    key = os.getenv("CAPTCHA_API_KEY", "").strip()
    return key if key and key != "your-2captcha-api-key-here" else None


def _submit_task(api_key: str, params: dict) -> str | None:
    """Submit a CAPTCHA task to 2Captcha. Returns task ID or None."""
    import json

    params["key"] = api_key
    params["json"] = "1"
    url = f"{TWOCAPTCHA_API}/in.php?{urllib.parse.urlencode(params)}"

    try:
        with urllib.request.urlopen(url, timeout=30, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") == 1:
            return data["request"]
        logger.warning(f"2Captcha submit failed: {data.get('request', 'unknown error')}")
        return None
    except Exception as e:
        logger.error(f"2Captcha submit error: {e}")
        return None


def _poll_result(api_key: str, task_id: str) -> str | None:
    """Poll 2Captcha for solution. Returns token string or None."""
    import json

    url = f"{TWOCAPTCHA_API}/res.php?key={api_key}&action=get&id={task_id}&json=1"
    start = time.time()

    while time.time() - start < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        try:
            with urllib.request.urlopen(url, timeout=30, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode())
            if data.get("status") == 1:
                return data["request"]
            if data.get("request") != "CAPCHA_NOT_READY":
                logger.warning(f"2Captcha error: {data.get('request')}")
                return None
        except Exception as e:
            logger.warning(f"2Captcha poll error: {e}")

    logger.warning("2Captcha timed out")
    return None


def solve_recaptcha_v2(page, sitekey: str, enterprise: bool = False) -> bool:
    """Solve reCAPTCHA v2 (or Enterprise) and inject the token into the page."""
    api_key = _get_api_key()
    if not api_key:
        return False

    page_url = page.url
    variant = "Enterprise" if enterprise else "v2"
    logger.info(f"Solving reCAPTCHA {variant}: sitekey={sitekey[:20]}... url={page_url[:60]}")

    params = {
        "method": "userrecaptcha",
        "googlekey": sitekey,
        "pageurl": page_url,
    }
    if enterprise:
        params["enterprise"] = "1"

    task_id = _submit_task(api_key, params)
    if not task_id:
        return False

    token = _poll_result(api_key, task_id)
    if not token:
        return False

    evaluate_script(page, "captcha/inject_recaptcha_token.js", token)
    page.wait_for_timeout(500)
    logger.info("reCAPTCHA v2 token injected")
    return True


def solve_hcaptcha(page, sitekey: str) -> bool:
    """Solve hCaptcha and inject the token into the page."""
    api_key = _get_api_key()
    if not api_key:
        return False

    page_url = page.url
    logger.info(f"Solving hCaptcha: sitekey={sitekey[:20]}... url={page_url[:60]}")

    task_id = _submit_task(api_key, {
        "method": "hcaptcha",
        "sitekey": sitekey,
        "pageurl": page_url,
    })
    if not task_id:
        return False

    token = _poll_result(api_key, task_id)
    if not token:
        return False

    evaluate_script(page, "captcha/inject_hcaptcha_token.js", token)
    page.wait_for_timeout(500)
    logger.info("hCaptcha token injected")
    return True


def solve_turnstile(page, sitekey: str) -> bool:
    """Solve Cloudflare Turnstile and inject the token."""
    api_key = _get_api_key()
    if not api_key:
        return False

    page_url = page.url
    logger.info(f"Solving Turnstile: sitekey={sitekey[:20]}... url={page_url[:60]}")

    task_id = _submit_task(api_key, {
        "method": "turnstile",
        "sitekey": sitekey,
        "pageurl": page_url,
    })
    if not task_id:
        return False

    token = _poll_result(api_key, task_id)
    if not token:
        return False

    evaluate_script(page, "captcha/inject_turnstile_token.js", token)
    page.wait_for_timeout(500)
    logger.info("Turnstile token injected")
    return True


def _wait_for_cloudflare_auto_challenge(page) -> bool:
    """Wait for Cloudflare's automatic JS challenge to resolve on its own.

    Many Cloudflare challenges (especially on Ashby/ATS sites) are just browser
    verification that completes automatically after a few seconds. Returns True
    if the challenge page cleared.
    """
    from .detection import detect_captcha

    # Check if this looks like a Cloudflare auto-challenge (not a widget)
    is_auto_challenge = evaluate_script(page, "captcha/detect_auto_challenge.js")

    if not is_auto_challenge:
        return False

    logger.info("Cloudflare auto-challenge detected -- waiting for it to resolve")

    # Wait up to 15 seconds for the challenge to clear
    for _ in range(5):
        page.wait_for_timeout(3000)
        if not detect_captcha(page):
            logger.info("Cloudflare challenge resolved automatically")
            return True

    return False


def solve_captcha(page) -> bool:
    """Detect the CAPTCHA type on the page and solve it.

    Returns True if solved, False if unsolvable or no API key.
    """
    from rich.console import Console
    console = Console(force_terminal=True)

    api_key = _get_api_key()
    if not api_key:
        logger.warning("No CAPTCHA_API_KEY set -- cannot solve")
        return False

    # --- reCAPTCHA (v2 or Enterprise) ---
    recaptcha_info = evaluate_script(page, "captcha/get_recaptcha_info.js")
    if recaptcha_info:
        variant = "Enterprise" if recaptcha_info["enterprise"] else "v2"
        console.print(f"  [cyan]Solving reCAPTCHA {variant}...[/]")
        return solve_recaptcha_v2(page, recaptcha_info["sitekey"], recaptcha_info["enterprise"])

    # --- hCaptcha ---
    sitekey = evaluate_script(page, "captcha/get_hcaptcha_sitekey.js")
    if not sitekey:
        sitekey = evaluate_script(page, "captcha/get_hcaptcha_sitekey_from_iframe.js")
    if sitekey:
        console.print(f"  [cyan]Solving hCaptcha...[/]")
        return solve_hcaptcha(page, sitekey)

    # --- Cloudflare Turnstile ---
    sitekey = evaluate_script(page, "captcha/get_turnstile_sitekey.js")
    if sitekey:
        console.print(f"  [cyan]Solving Cloudflare Turnstile...[/]")
        return solve_turnstile(page, sitekey)

    logger.warning("CAPTCHA detected but could not extract sitekey -- unsolvable")
    return False
