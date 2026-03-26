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


def solve_recaptcha_v2(page, sitekey: str) -> bool:
    """Solve reCAPTCHA v2 and inject the token into the page."""
    api_key = _get_api_key()
    if not api_key:
        return False

    page_url = page.url
    logger.info(f"Solving reCAPTCHA v2: sitekey={sitekey[:20]}... url={page_url[:60]}")

    task_id = _submit_task(api_key, {
        "method": "userrecaptcha",
        "googlekey": sitekey,
        "pageurl": page_url,
    })
    if not task_id:
        return False

    token = _poll_result(api_key, task_id)
    if not token:
        return False

    # Inject the token into the page
    page.evaluate(f"""() => {{
        // Fill all recaptcha response textareas
        document.querySelectorAll('[id*="g-recaptcha-response"]').forEach(ta => {{
            ta.style.display = '';
            ta.value = '{token}';
        }});
        // Try the standard grecaptcha callback
        try {{
            if (typeof grecaptcha !== 'undefined' && grecaptcha.getResponse) {{
                // Trigger any registered callback via the data-callback attribute
                const el = document.querySelector('.g-recaptcha');
                if (el) {{
                    const cbName = el.getAttribute('data-callback');
                    if (cbName && typeof window[cbName] === 'function') {{
                        window[cbName]('{token}');
                    }}
                }}
            }}
        }} catch(e) {{}}
    }}""")
    page.wait_for_timeout(1000)
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

    # Inject hCaptcha response
    page.evaluate(f"""() => {{
        // Set the response textarea
        const textareas = document.querySelectorAll('[name="h-captcha-response"], [name="g-recaptcha-response"]');
        textareas.forEach(ta => {{ ta.value = '{token}'; }});
        // Try hcaptcha callback
        if (typeof hcaptcha !== 'undefined') {{
            try {{ hcaptcha.execute(); }} catch(e) {{}}
        }}
    }}""")
    page.wait_for_timeout(1000)
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

    # Inject Turnstile response
    page.evaluate(f"""() => {{
        const input = document.querySelector('[name="cf-turnstile-response"]');
        if (input) input.value = '{token}';
        // Try callback
        if (typeof turnstile !== 'undefined') {{
            try {{ turnstile.execute(); }} catch(e) {{}}
        }}
    }}""")
    page.wait_for_timeout(1000)
    logger.info("Turnstile token injected")
    return True


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

    # --- reCAPTCHA v2 ---
    sitekey = page.evaluate("""() => {
        const el = document.querySelector('.g-recaptcha');
        return el ? el.getAttribute('data-sitekey') : null;
    }""")
    if not sitekey:
        # Try iframe src
        sitekey = page.evaluate("""() => {
            const iframe = document.querySelector('iframe[src*="recaptcha"]');
            if (!iframe) return null;
            const match = iframe.src.match(/[?&]k=([^&]+)/);
            return match ? match[1] : null;
        }""")
    if sitekey:
        console.print(f"  [cyan]Solving reCAPTCHA v2...[/]")
        return solve_recaptcha_v2(page, sitekey)

    # --- hCaptcha ---
    sitekey = page.evaluate("""() => {
        const el = document.querySelector('[data-hcaptcha-sitekey]') || document.querySelector('.h-captcha');
        return el ? (el.getAttribute('data-hcaptcha-sitekey') || el.getAttribute('data-sitekey')) : null;
    }""")
    if not sitekey:
        sitekey = page.evaluate("""() => {
            const iframe = document.querySelector('iframe[src*="hcaptcha"]');
            if (!iframe) return null;
            const match = iframe.src.match(/sitekey=([^&]+)/);
            return match ? match[1] : null;
        }""")
    if sitekey:
        console.print(f"  [cyan]Solving hCaptcha...[/]")
        return solve_hcaptcha(page, sitekey)

    # --- Cloudflare Turnstile ---
    sitekey = page.evaluate("""() => {
        const el = document.querySelector('[class*="cf-turnstile"]');
        return el ? el.getAttribute('data-sitekey') : null;
    }""")
    if not sitekey:
        sitekey = page.evaluate("""() => {
            const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (!iframe) return null;
            const match = iframe.src.match(/sitekey=([^&]+)/);
            return match ? match[1] : null;
        }""")
    if sitekey:
        console.print(f"  [cyan]Solving Cloudflare Turnstile...[/]")
        return solve_turnstile(page, sitekey)

    logger.warning("CAPTCHA detected but could not extract sitekey -- unsolvable")
    return False
