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

    # Inject the token into the page
    page.evaluate(f"""() => {{
        const token = '{token}';

        // Fill all recaptcha response textareas
        document.querySelectorAll('[id*="g-recaptcha-response"]').forEach(ta => {{
            ta.style.display = '';
            ta.value = token;
        }});

        // Try multiple callback invocation strategies
        try {{
            // 1. data-callback on .g-recaptcha or any recaptcha widget
            const widgets = document.querySelectorAll('.g-recaptcha, [data-callback]');
            for (const el of widgets) {{
                const cbName = el.getAttribute('data-callback');
                if (cbName && typeof window[cbName] === 'function') {{
                    window[cbName](token);
                    return;
                }}
            }}

            // 2. grecaptcha enterprise callback (used by Greenhouse and others)
            if (typeof grecaptcha !== 'undefined') {{
                // Try enterprise API first
                if (grecaptcha.enterprise && grecaptcha.enterprise.execute) {{
                    try {{ grecaptcha.enterprise.execute(); }} catch(e) {{}}
                }}
                // Walk registered widget callbacks via internal state
                if (grecaptcha.render && grecaptcha.getResponse) {{
                    // Some sites register callbacks via grecaptcha.render() opts
                    // Try widget IDs 0-5
                    for (let i = 0; i < 5; i++) {{
                        try {{
                            const resp = document.querySelector('#g-recaptcha-response-' + i);
                            if (resp) {{ resp.value = token; }}
                        }} catch(e) {{}}
                    }}
                }}
            }}

            // 3. Search for callbacks in common variable names
            const cbNames = ['onCaptchaSuccess', 'captchaCallback', 'recaptchaCallback',
                             'onRecaptchaSuccess', 'handleCaptcha', 'verifyCaptcha'];
            for (const name of cbNames) {{
                if (typeof window[name] === 'function') {{
                    window[name](token);
                    return;
                }}
            }}

            // 4. Dispatch a custom event some frameworks listen for
            window.dispatchEvent(new CustomEvent('captcha-solved', {{ detail: token }}));
        }} catch(e) {{}}
    }}""")
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

    # Inject Turnstile response
    page.evaluate(f"""() => {{
        const input = document.querySelector('[name="cf-turnstile-response"]');
        if (input) input.value = '{token}';
        // Try callback
        if (typeof turnstile !== 'undefined') {{
            try {{ turnstile.execute(); }} catch(e) {{}}
        }}
    }}""")
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
    is_auto_challenge = page.evaluate("""() => {
        const body = (document.body?.textContent || '').toLowerCase().slice(0, 2000);
        return body.includes('checking your browser') ||
               body.includes('verify you are human') ||
               !!document.querySelector('#challenge-running, #challenge-form, #cf-challenge-running');
    }""")

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
    recaptcha_info = page.evaluate("""() => {
        let sitekey = null;
        let enterprise = false;

        // Check .g-recaptcha widget
        const el = document.querySelector('.g-recaptcha');
        if (el) sitekey = el.getAttribute('data-sitekey');

        // Check iframe src
        if (!sitekey) {
            const iframe = document.querySelector('iframe[src*="recaptcha"]');
            if (iframe) {
                const match = iframe.src.match(/[?&]k=([^&]+)/);
                if (match) sitekey = match[1];
                // Enterprise iframes use recaptcha/enterprise
                if (iframe.src.includes('/enterprise')) enterprise = true;
            }
        }

        // Detect Enterprise: check for grecaptcha.enterprise or enterprise scripts
        if (typeof grecaptcha !== 'undefined' && grecaptcha.enterprise) enterprise = true;
        const scripts = document.querySelectorAll('script[src*="recaptcha"]');
        for (const s of scripts) {
            if (s.src.includes('enterprise')) { enterprise = true; break; }
        }

        return sitekey ? { sitekey, enterprise } : null;
    }""")
    if recaptcha_info:
        variant = "Enterprise" if recaptcha_info["enterprise"] else "v2"
        console.print(f"  [cyan]Solving reCAPTCHA {variant}...[/]")
        return solve_recaptcha_v2(page, recaptcha_info["sitekey"], recaptcha_info["enterprise"])

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
        // 1. Standard data-sitekey attribute on widget div
        const el = document.querySelector('[class*="cf-turnstile"]');
        if (el) {
            const key = el.getAttribute('data-sitekey');
            if (key) return key;
        }

        // 2. iframe src parameter
        const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
        if (iframe) {
            const match = iframe.src.match(/sitekey=([^&]+)/);
            if (match) return match[1];
        }

        // 3. Check script tags for turnstile.render() calls with sitekey
        const scripts = document.querySelectorAll('script');
        for (const script of scripts) {
            const text = script.textContent || '';
            // Match turnstile.render({sitekey: '...'}) or sitekey: "..."
            const m = text.match(/turnstile[.]render\\s*\\([^)]*sitekey\\s*:\\s*['"]([^'"]+)['"]/);
            if (m) return m[1];
            // Match data-sitekey="..." in inline HTML
            const m2 = text.match(/data-sitekey\\s*=\\s*['"]([^'"]+)['"]/);
            if (m2) return m2[1];
        }

        // 4. Check any element with data-sitekey (some sites put it on non-standard elements)
        const anyKey = document.querySelector('[data-sitekey]');
        if (anyKey) return anyKey.getAttribute('data-sitekey');

        return null;
    }""")
    if sitekey:
        console.print(f"  [cyan]Solving Cloudflare Turnstile...[/]")
        return solve_turnstile(page, sitekey)

    logger.warning("CAPTCHA detected but could not extract sitekey -- unsolvable")
    return False
