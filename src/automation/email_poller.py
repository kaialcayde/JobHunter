"""IMAP-based email polling for OTP codes and verification links.

Connects to Gmail (or any IMAP server) using app password.
Secrets: EMAIL_USER and EMAIL_APP_PASSWORD from .env.
These are never exposed to LLM, browser, DB, or logs.
"""

import imaplib
import email
import email.utils
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

# Common OTP patterns across ATS platforms
OTP_PATTERNS = [
    r'(?:code|pin|otp)[:\s]*(\d{4,8})',         # "code: 123456"
    r'(?:verification|confirm)[:\s]*(\d{4,8})',  # "verification: 123456"
    r'\b(\d{6})\b',                              # bare 6-digit code (most common)
    r'\b(\d{4,8})\b',                            # bare 4-8 digit code (broader)
]

# Common magic link patterns
LINK_PATTERNS = [
    r'(https?://\S+(?:verify|confirm|activate|token|auth)\S*)',
    r'(https?://\S+\?(?:code|token|key)=\S+)',
]


class EmailPoller:
    """IMAP-based email polling for OTP codes and verification links.

    Abstracted behind a clean API so Gmail API can replace IMAP later
    without changing callers.
    """

    def __init__(self, imap_server: str = "imap.gmail.com", imap_port: int = 993):
        self.server = imap_server
        self.port = imap_port
        self._conn = None

    def connect(self):
        """Connect to IMAP server. Reads creds from env, never exposes them."""
        user = os.environ.get("EMAIL_USER")
        password = os.environ.get("EMAIL_APP_PASSWORD")
        if not user or not password:
            raise ValueError("EMAIL_USER and EMAIL_APP_PASSWORD must be set in .env")
        self._conn = imaplib.IMAP4_SSL(self.server, self.port)
        self._conn.login(user, password)
        logger.debug("IMAP connected")

    def disconnect(self):
        """Close IMAP connection."""
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def request_verification(self, domain: str, type: str = "otp",
                             timeout: int = 120,
                             company_hint: str = None) -> str | None:
        """High-level API: poll for a verification artifact.

        Args:
            domain: The site that triggered verification (for filtering sender)
            type: "otp" or "magic_link"
            timeout: Max seconds to wait
            company_hint: Company name — broadens search when ATS emails come from
                          the company domain rather than the ATS platform domain

        Returns:
            OTP code string, magic link URL, or None if timeout
        """
        if type == "otp":
            return self.poll_for_otp(domain_filter=domain, company_hint=company_hint, timeout=timeout)
        elif type == "magic_link":
            return self.poll_for_magic_link(domain_filter=domain, timeout=timeout)
        return None

    def poll_for_otp(self, domain_filter: str = None, company_hint: str = None,
                     timeout: int = 120) -> str | None:
        """Poll inbox for OTP codes. Returns the code or None.

        Checks every 5 seconds for new emails matching the filter.
        Only considers emails received after poll started (timestamp filtering).

        Args:
            domain_filter: ATS domain (e.g. "avature.net") — used for FROM filter.
            company_hint:  Company name (e.g. "bloomberg") — broadens search when the ATS
                           sends verification email from the company's own domain instead of
                           the ATS platform domain (e.g. no-reply@bloomberg.com).
        """
        start_time = time.time()

        # Relevance keywords for broad-fallback body filtering
        relevance_keywords = []
        if domain_filter:
            relevance_keywords.append(domain_filter.split(".")[0].lower())  # e.g. "avature"
        if company_hint:
            relevance_keywords.append(company_hint.lower())  # e.g. "bloomberg"

        while time.time() - start_time < timeout:
            try:
                self._conn.select("INBOX")
                since_date = time.strftime("%d-%b-%Y", time.gmtime(start_time - 60))

                # Try narrow FROM filter first
                if domain_filter:
                    _, mid = self._conn.search(None, f'(FROM "{domain_filter}" SINCE "{since_date}")')
                    ids = mid[0].split()
                    using_broad = False
                else:
                    ids = []
                    using_broad = False

                # If no results with FROM filter, fall back to all recent emails
                # (ATS may send from company domain, e.g. no-reply@bloomberg.com)
                if not ids:
                    _, mid = self._conn.search(None, f'(SINCE "{since_date}")')
                    ids = mid[0].split()
                    using_broad = True

                for msg_id in reversed(ids):  # newest first
                    _, msg_data = self._conn.fetch(msg_id, "(RFC822)")
                    msg = email.message_from_bytes(msg_data[0][1])

                    msg_date = email.utils.parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
                    if msg_date and msg_date.timestamp() < start_time - 60:
                        continue

                    body = self._extract_body(msg)
                    if not body:
                        continue

                    # Broad fallback: require relevance keyword in body to avoid
                    # picking up unrelated OTPs (banking, two-factor, etc.)
                    if using_broad and relevance_keywords:
                        body_lower = body.lower()
                        if not any(kw in body_lower for kw in relevance_keywords):
                            continue

                    for pattern in OTP_PATTERNS:
                        match = re.search(pattern, body, re.IGNORECASE)
                        if match:
                            code = match.group(1)
                            logger.info(f"Email poller: found OTP code ({code[:2]}***)")
                            return code

            except Exception as e:
                logger.warning(f"Email poll cycle error: {e}")

            time.sleep(5)

        logger.info("Email poller: OTP poll timed out")
        return None

    def poll_for_magic_link(self, domain_filter: str = None,
                            timeout: int = 120) -> str | None:
        """Poll inbox for verification/magic links. Returns URL or None."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                self._conn.select("INBOX")
                since_date = time.strftime("%d-%b-%Y", time.gmtime(start_time - 60))
                criteria = f'(SINCE "{since_date}")'
                _, message_ids = self._conn.search(None, criteria)
                ids = message_ids[0].split()

                for msg_id in reversed(ids):
                    _, msg_data = self._conn.fetch(msg_id, "(RFC822)")
                    msg = email.message_from_bytes(msg_data[0][1])

                    msg_date = email.utils.parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
                    if msg_date and msg_date.timestamp() < start_time - 60:
                        continue

                    body = self._extract_body(msg)
                    if not body:
                        continue

                    for pattern in LINK_PATTERNS:
                        match = re.search(pattern, body)
                        if match:
                            link = match.group(1)
                            if domain_filter and domain_filter not in link:
                                continue
                            logger.info(f"Email poller: found magic link")
                            return link

            except Exception as e:
                logger.warning(f"Email poll cycle error: {e}")

            time.sleep(5)

        logger.info("Email poller: magic link poll timed out")
        return None

    def _extract_body(self, msg) -> str:
        """Extract plain text body from email message."""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode("utf-8", errors="replace")
                elif ct == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        html = payload.decode("utf-8", errors="replace")
                        return re.sub(r'<[^>]+>', ' ', html)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode("utf-8", errors="replace")
        return ""


def find_otp_field(page) -> object | None:
    """Find the OTP/verification code input field on the current page.

    Returns a Playwright Locator or None.
    """
    # Try specific OTP-related selectors first
    otp_selectors = [
        'input[autocomplete="one-time-code"]',
        'input[name*="otp" i]',
        'input[name*="code" i]',
        'input[name*="verification" i]',
        'input[id*="otp" i]',
        'input[id*="code" i]',
        'input[id*="verification" i]',
        'input[placeholder*="code" i]',
        'input[placeholder*="verification" i]',
        'input[aria-label*="code" i]',
        'input[aria-label*="verification" i]',
    ]
    for selector in otp_selectors:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=500):
                return loc
        except Exception:
            continue

    # Fallback: find input near OTP-related labels via JS
    result = page.evaluate("""() => {
        const inputs = document.querySelectorAll('input[type="text"], input[type="number"], input[type="tel"]');
        for (const inp of inputs) {
            const label = (inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || '').toLowerCase();
            const parentText = (inp.closest('label, div, fieldset')?.textContent || '').toLowerCase();
            if (['verification', 'code', 'otp', 'confirm', 'one-time'].some(
                k => label.includes(k) || parentText.includes(k)
            )) {
                return true;
            }
        }
        return false;
    }""")

    if result:
        # Use the JS-based fill approach since we can't return a locator from evaluate
        return _OTPFieldProxy(page)
    return None


class _OTPFieldProxy:
    """Proxy that fills OTP fields via JS when no direct selector match exists."""

    def __init__(self, page):
        self._page = page

    def fill(self, code: str):
        self._page.evaluate("""(code) => {
            const inputs = document.querySelectorAll('input[type="text"], input[type="number"], input[type="tel"]');
            for (const inp of inputs) {
                const label = (inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || '').toLowerCase();
                const parentText = (inp.closest('label, div, fieldset')?.textContent || '').toLowerCase();
                if (['verification', 'code', 'otp', 'confirm', 'one-time'].some(
                    k => label.includes(k) || parentText.includes(k)
                )) {
                    inp.focus();
                    inp.value = code;
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    return;
                }
            }
            // Last resort: fill the focused element
            const active = document.activeElement;
            if (active && active.tagName === 'INPUT') {
                active.value = code;
                active.dispatchEvent(new Event('input', {bubbles: true}));
                active.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }""", code)
