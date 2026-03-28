# Phase 4: Email Polling + Verification

## Goal
Automate OTP extraction and magic-link handling via IMAP. Replace the `manual_otp` terminal prompt with an automated poller that reads verification emails. This is also prerequisite infrastructure for Phase 6 (ATS account creation).

## Why This Phase
Jobs get stuck at `needs_login` or fail when an ATS sends a verification code that requires manual copy-paste. The email poller makes these flows fully automated while maintaining the manual prompt as a fallback.

---

## New Files

### `src/automation/email_poller.py`

IMAP-based email verification service. Abstracted behind a clean API so Gmail API can replace IMAP later without changing callers.

```python
import imaplib
import email
import re
import time
from email.header import decode_header

class EmailPoller:
    """IMAP-based email polling for OTP codes and verification links.

    Connects to Gmail (or any IMAP server) using app password.
    Secrets: EMAIL_USER and EMAIL_APP_PASSWORD from .env.
    These are never exposed to LLM or logged.
    """

    # Common OTP patterns across ATS platforms
    OTP_PATTERNS = [
        r'\b(\d{4,8})\b',                          # bare 4-8 digit code
        r'(?:code|pin|otp)[:\s]*(\d{4,8})',         # "code: 123456"
        r'(?:verification|confirm)[:\s]*(\d{4,8})', # "verification: 123456"
    ]

    # Common magic link patterns
    LINK_PATTERNS = [
        r'(https?://\S+(?:verify|confirm|activate|token|auth)\S*)',
        r'(https?://\S+\?(?:code|token|key)=\S+)',
    ]

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

    def disconnect(self):
        """Close IMAP connection."""
        if self._conn:
            self._conn.logout()
            self._conn = None

    def request_verification(self, domain: str, type: str = "otp",
                             timeout: int = 120) -> str | None:
        """High-level API: poll for a verification artifact.

        Args:
            domain: The site that triggered verification (for filtering)
            type: "otp" or "magic_link"
            timeout: Max seconds to wait

        Returns:
            OTP code string, magic link URL, or None if timeout
        """
        if type == "otp":
            return self.poll_for_otp(domain_filter=domain, timeout=timeout)
        elif type == "magic_link":
            return self.poll_for_magic_link(domain_filter=domain, timeout=timeout)
        return None

    def poll_for_otp(self, domain_filter: str = None,
                     subject_filter: str = None,
                     timeout: int = 120) -> str | None:
        """Poll inbox for OTP codes. Returns the code or None.

        Checks every 5 seconds for new emails matching the filter.
        Only considers emails received after poll started (timestamp filtering).
        """
        start_time = time.time()
        poll_start = email.utils.formatdate(localtime=True)

        while time.time() - start_time < timeout:
            self._conn.select("INBOX")

            # Search for recent emails
            criteria = '(SINCE "{}")'.format(
                time.strftime("%d-%b-%Y", time.gmtime(start_time - 60))
            )
            if domain_filter:
                criteria = '(FROM "{}" {})'.format(domain_filter, criteria)

            _, message_ids = self._conn.search(None, criteria)

            for msg_id in reversed(message_ids[0].split()):  # newest first
                _, msg_data = self._conn.fetch(msg_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])

                # Extract body text
                body = self._extract_body(msg)
                if not body:
                    continue

                # Try OTP patterns
                for pattern in self.OTP_PATTERNS:
                    match = re.search(pattern, body, re.IGNORECASE)
                    if match:
                        return match.group(1)

            time.sleep(5)

        return None  # timeout

    def poll_for_magic_link(self, domain_filter: str = None,
                            timeout: int = 120) -> str | None:
        """Poll inbox for verification/magic links. Returns URL or None."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            self._conn.select("INBOX")
            criteria = '(SINCE "{}")'.format(
                time.strftime("%d-%b-%Y", time.gmtime(start_time - 60))
            )
            _, message_ids = self._conn.search(None, criteria)

            for msg_id in reversed(message_ids[0].split()):
                _, msg_data = self._conn.fetch(msg_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                body = self._extract_body(msg)
                if not body:
                    continue

                for pattern in self.LINK_PATTERNS:
                    match = re.search(pattern, body)
                    if match:
                        link = match.group(1)
                        # Validate link domain if filter provided
                        if domain_filter and domain_filter not in link:
                            continue
                        return link

            time.sleep(5)

        return None

    def _extract_body(self, msg) -> str:
        """Extract plain text body from email message."""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    return part.get_payload(decode=True).decode("utf-8", errors="replace")
                elif part.get_content_type() == "text/html":
                    # Strip HTML tags for pattern matching
                    html = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    return re.sub(r'<[^>]+>', ' ', html)
        else:
            return msg.get_payload(decode=True).decode("utf-8", errors="replace")
        return ""
```

---

## Modified Files

### `src/config/models.py`
Add email polling settings to the Automation model:

```python
class Automation(BaseModel):
    # ... existing fields ...
    email_polling: bool = False          # Enable IMAP-based OTP polling
    imap_server: str = "imap.gmail.com"  # IMAP server address
    imap_port: int = 993                 # IMAP server port
    email_poll_timeout: int = 120        # Max seconds to wait for verification email
```

### `config/settings.example.yaml`
Document new settings:

```yaml
automation:
  # ... existing settings ...

  # Email polling for automated OTP/verification handling
  email_polling: false          # Enable IMAP-based email polling
  imap_server: imap.gmail.com   # IMAP server (Gmail default)
  imap_port: 993                # IMAP port (SSL default)
  email_poll_timeout: 120       # Seconds to wait for verification email
```

### `.env` (example in README/comments, actual file is gitignored)
```
# Email polling (Phase 4)
EMAIL_USER=kalcaydecl@gmail.com
EMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx  # Gmail App Password (NOT account password)
```

### `src/automation/handlers.py`
Integration with the kernel's verification flow:

```python
async def handle_verification(ctx: KernelContext) -> StepResult:
    """Handle email verification when detected during navigation or registration.

    Fallback chain:
    1. Email poller (if email_polling enabled)
    2. Manual terminal prompt (if manual_otp enabled)
    3. Mark as needs_login
    """
    settings = ctx.settings.get("automation", {})

    # Try email poller first
    if settings.get("email_polling"):
        domain = extract_domain(ctx.page.url)
        poller = EmailPoller(
            imap_server=settings.get("imap_server", "imap.gmail.com"),
            imap_port=settings.get("imap_port", 993),
        )
        try:
            poller.connect()
            code = poller.request_verification(
                domain=domain,
                type="otp",
                timeout=settings.get("email_poll_timeout", 120),
            )
            if code:
                # Fill OTP field on page
                otp_field = await find_otp_field(ctx.page)
                if otp_field:
                    await otp_field.fill(code)
                    return StepResult(result=HandlerResult.SUCCESS, message=f"OTP filled: {code[:2]}***")
        except Exception as e:
            logger.warning(f"Email poller failed: {e}")
        finally:
            poller.disconnect()

    # Fallback: manual terminal prompt
    if settings.get("manual_otp"):
        code = input(f"Enter OTP code for {extract_domain(ctx.page.url)}: ").strip()
        if code:
            otp_field = await find_otp_field(ctx.page)
            if otp_field:
                await otp_field.fill(code)
                return StepResult(result=HandlerResult.SUCCESS, message="OTP filled manually")

    # No OTP method available
    return StepResult(result=HandlerResult.REQUIRES_LOGIN, message="Verification required, no OTP method available")
```

### `src/automation/kernel.py`
Add verification states to the kernel:

```python
# New states
VERIFY_EMAIL = "verify_email"
ENTER_OTP = "enter_otp"

# New transitions (added to existing table)
(State.NAVIGATE, HandlerResult.REQUIRES_VERIFICATION): State.VERIFY_EMAIL,
(State.VERIFY_EMAIL, HandlerResult.SUCCESS): State.NAVIGATE,  # retry navigation after verification
(State.VERIFY_EMAIL, HandlerResult.FAILED): State.CLEANUP,
```

---

## Secrets Handling

| Secret | Where Stored | Who Accesses | Never Sent To |
|--------|-------------|-------------|---------------|
| `EMAIL_USER` | `.env` | `email_poller.py` only | LLM, browser, DB |
| `EMAIL_APP_PASSWORD` | `.env` | `email_poller.py` only | LLM, browser, DB, logs |
| OTP codes | In-memory only | Handler fills field, then discards | DB, logs (masked) |
| Magic links | In-memory only | Handler navigates, then discards | DB, logs |

**Gmail App Password setup:**
1. Enable 2FA on Google account
2. Go to Google Account → Security → App Passwords
3. Generate a new app password for "Mail"
4. Add to `.env` as `EMAIL_APP_PASSWORD`

This is NOT the Google account password. It's a 16-character app-specific password that only has IMAP access. It can be revoked at any time without affecting the main account.

---

## Plus Alias Support

Gmail plus aliases (`kalcaydecl+workday@gmail.com`) all deliver to the base inbox. The poller doesn't need special handling — it searches the inbox by domain/timestamp regardless of which alias received the email.

For Phase 6 (account creation), different aliases per ATS tenant will help filter verification emails:
- `kalcaydecl+workday-google@gmail.com` → Workday registration for Google
- `kalcaydecl+icims-meta@gmail.com` → iCIMS registration for Meta

But the poller works the same either way — it's filtering by sender domain and timestamp, not recipient alias.

---

## Testing Strategy

1. **Unit test email parsing:** Feed sample email bodies through `_extract_body()` and OTP/link patterns. Test with:
   - Workday OTP emails
   - Greenhouse verification links
   - iCIMS confirmation codes
   - HTML-only emails (no plain text part)
2. **Integration test:** Send a real test email to `kalcaydecl@gmail.com`, verify poller picks it up within timeout.
3. **Fallback test:** Disable email_polling, verify manual_otp prompt still works.
4. **Kernel test:** Mock a handler returning `REQUIRES_VERIFICATION`, verify kernel routes to `VERIFY_EMAIL` state.

---

## Dependencies
- **Phase 2** (kernel states for verification routing)
- Gmail account with 2FA + App Password set up
- `.env` with EMAIL_USER and EMAIL_APP_PASSWORD

## Estimated Scope
- ~200 lines new code (email_poller.py)
- ~50 lines modified (models.py, settings.example.yaml)
- ~80 lines modified (handlers.py, kernel.py — verification integration)
