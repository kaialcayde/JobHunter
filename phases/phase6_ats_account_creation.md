# Phase 6: ATS Account Creation

## Goal
Auto-register on tenant-specific ATS platforms that require an account before applying. This is generalized — not just Workday, but any ATS that gates applications behind a registration wall. The system detects registration walls, creates accounts using profile data + generated credentials, handles email verification via the Phase 4 poller, and saves sessions for reuse.

## Why This Phase
This is the #1 blocker for jobs currently stuck at `needs_login`. Many ATS platforms (Workday tenants, iCIMS, Greenhouse, SmartRecruiters, Taleo, etc.) require a site-specific account before you can apply. Currently these all get marked `needs_login` and require manual intervention. This phase automates the registration flow.

---

## The Problem

Each Workday tenant (e.g., `google.wd5.myworkdayjobs.com`, `meta.wd1.myworkday.com`) is a separate instance. Creating an account on one does NOT give you access to another. Same pattern with:
- **iCIMS:** `jobs-companyname.icims.com` — each company's iCIMS is a separate tenant
- **Greenhouse:** Some gated instances require registration
- **SmartRecruiters:** `careers.smartrecruiters.com/CompanyName` — sometimes requires account
- **Taleo:** `companyname.taleo.net` — per-tenant accounts
- **ADP:** `workforcenow.adp.com` — per-company portals
- **Lever:** Usually no account needed, but some companies gate it
- **Custom ATS:** Various company-specific portals

The registration flow is remarkably consistent across platforms:
1. Navigate to job → "Create Account" / "Sign Up" / "Register" wall
2. Fill: email + password + name (sometimes phone)
3. Submit registration form
4. Verify email (OTP code or magic link)
5. Account active → redirect to application

---

## New Files

### `src/automation/account_registry.py`

Local encrypted credential store + registration utilities.

```python
import os
import secrets
import sqlite3
from cryptography.fernet import Fernet
from base64 import urlsafe_b64encode
from hashlib import pbkdf2_hmac

class AccountRegistry:
    """Encrypted local credential store for ATS tenant accounts.

    Passwords are encrypted at rest using Fernet with a key derived
    from REGISTRY_KEY in .env. Never exposes raw passwords to LLM,
    logs, or DB in plaintext.
    """

    DB_PATH = "data/account_registry.db"  # gitignored

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS accounts (
        domain TEXT NOT NULL,
        tenant TEXT,                   -- e.g. "google" for google.wd5.myworkdayjobs.com
        email TEXT NOT NULL,           -- plus alias used for this tenant
        password_encrypted BLOB,       -- Fernet-encrypted password
        platform TEXT,                 -- "workday", "icims", "greenhouse", etc.
        status TEXT DEFAULT 'pending', -- pending, active, needs_verify, failed, disabled
        created_at TEXT,
        last_login TEXT,
        last_used TEXT,
        notes TEXT,
        PRIMARY KEY (domain)
    )
    """

    def __init__(self):
        self._fernet = self._init_encryption()
        self._conn = sqlite3.connect(self.DB_PATH)
        self._conn.execute(self.SCHEMA)

    def _init_encryption(self) -> Fernet:
        """Derive Fernet key from REGISTRY_KEY env var."""
        master_key = os.environ.get("REGISTRY_KEY")
        if not master_key:
            raise ValueError("REGISTRY_KEY must be set in .env for account registry")
        # Derive a proper Fernet key from the master key
        key = pbkdf2_hmac("sha256", master_key.encode(), b"jobhunter-registry", 100000)
        return Fernet(urlsafe_b64encode(key[:32]))

    def generate_credentials(self, domain: str, tenant: str = None,
                             platform: str = None) -> dict:
        """Generate and store credentials for a new ATS tenant account.

        Returns: {"email": str, "password": str}
        Password is stored encrypted BEFORE being returned.
        """
        # Generate plus alias email
        base_email = os.environ.get("EMAIL_USER", "")
        user, domain_part = base_email.split("@")
        alias = f"{user}+{platform or 'ats'}-{tenant or domain}@{domain_part}"

        # Generate strong random password
        password = secrets.token_urlsafe(16)

        # Store FIRST, then return (safety: never lose a password)
        encrypted = self._fernet.encrypt(password.encode())
        self._conn.execute(
            "INSERT OR REPLACE INTO accounts (domain, tenant, email, password_encrypted, platform, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', datetime('now'))",
            (domain, tenant, alias, encrypted, platform)
        )
        self._conn.commit()

        return {"email": alias, "password": password}

    def get_credentials(self, domain: str) -> dict | None:
        """Retrieve decrypted credentials for a domain."""
        row = self._conn.execute(
            "SELECT email, password_encrypted, status FROM accounts WHERE domain = ?",
            (domain,)
        ).fetchone()
        if not row:
            return None
        email, encrypted, status = row
        password = self._fernet.decrypt(encrypted).decode()
        return {"email": email, "password": password, "status": status}

    def mark_active(self, domain: str):
        """Mark account as verified and active."""
        self._conn.execute(
            "UPDATE accounts SET status = 'active', last_login = datetime('now') WHERE domain = ?",
            (domain,)
        )
        self._conn.commit()

    def mark_failed(self, domain: str, reason: str = ""):
        """Mark account registration as failed."""
        self._conn.execute(
            "UPDATE accounts SET status = 'failed', notes = ? WHERE domain = ?",
            (reason, domain)
        )
        self._conn.commit()

    def has_account(self, domain: str) -> bool:
        """Check if we already have an account for this domain."""
        row = self._conn.execute(
            "SELECT status FROM accounts WHERE domain = ?", (domain,)
        ).fetchone()
        return row is not None and row[0] in ("active", "pending")

    def fill_credential(self, page, field_locator, credential_type: str, domain: str):
        """Fill a credential into a form field WITHOUT exposing the value.

        This is the secure tool function. Handlers call this instead of
        directly accessing passwords.

        credential_type: "email" or "password"
        """
        creds = self.get_credentials(domain)
        if not creds:
            raise ValueError(f"No credentials for {domain}")
        value = creds[credential_type]
        # Fill directly into page, value never returned to caller context
        page.locator(field_locator).fill(value)
```

### `src/automation/handlers_account.py`

Registration-specific handlers for the kernel.

```python
async def handle_detect_auth_type(ctx: KernelContext) -> StepResult:
    """Determine if page is a login wall or registration wall.

    Login wall: has email + password fields, "Sign In" text, no "Create Account"
    Registration wall: has "Create Account", "Sign Up", "Register" links/buttons,
                       or email + password + confirm password fields

    This is GENERIC — works across all ATS platforms.
    """
    page = ctx.page
    auth_info = await page.evaluate("""() => {
        const text = document.body.innerText.toLowerCase();
        const inputs = document.querySelectorAll('input:not([type="hidden"])');
        const hasPassword = !!document.querySelector('input[type="password"]');
        const hasConfirmPassword = document.querySelectorAll('input[type="password"]').length >= 2;

        // Registration signals
        const registerTexts = ['create account', 'sign up', 'register', 'new user',
                               'create your account', 'join now', 'get started'];
        const hasRegisterSignal = registerTexts.some(t => text.includes(t));

        // Login signals
        const loginTexts = ['sign in', 'log in', 'welcome back', 'returning user'];
        const hasLoginSignal = loginTexts.some(t => text.includes(t));

        return {
            hasPassword,
            hasConfirmPassword,
            hasRegisterSignal,
            hasLoginSignal,
            inputCount: inputs.length,
        };
    }""")

    if auth_info["hasRegisterSignal"] or auth_info["hasConfirmPassword"]:
        # Check domain allowlist
        domain = extract_domain(ctx.page.url)
        if not is_auto_register_allowed(domain, ctx.settings):
            return StepResult(
                result=HandlerResult.REQUIRES_LOGIN,
                message=f"Registration wall detected on {domain} but domain not in auto_register allowlist"
            )
        return StepResult(
            result=HandlerResult.REQUIRES_REGISTRATION,
            metadata={"auth_type": "registration", "domain": domain}
        )
    elif auth_info["hasLoginSignal"] and auth_info["hasPassword"]:
        return StepResult(
            result=HandlerResult.REQUIRES_LOGIN,
            metadata={"auth_type": "login"}
        )
    else:
        return StepResult(result=HandlerResult.FAILED, message="Unrecognized auth page")


async def handle_register(ctx: KernelContext) -> StepResult:
    """Fill and submit a registration form on an ATS tenant.

    Generic approach:
    1. Generate credentials (stored encrypted BEFORE form submission)
    2. Fill name fields from profile
    3. Fill email (plus alias)
    4. Fill password + confirm password
    5. Submit registration form
    """
    domain = extract_domain(ctx.page.url)
    registry = ctx.account_registry

    # Step 1: Generate and store credentials FIRST
    platform = detect_ats_platform(domain)
    tenant = extract_tenant(domain, platform)
    creds = registry.generate_credentials(domain, tenant=tenant, platform=platform)

    # Step 2: Fill registration form using element finder
    finder = ctx.element_finder
    profile = ctx.settings.get("profile", {}).get("personal", {})

    # Fill name
    first_name_field = await finder.find_element(ctx.page, "first_name_field", domain)
    if first_name_field:
        await first_name_field.element.fill(profile.get("first_name", ""))

    last_name_field = await finder.find_element(ctx.page, "last_name_field", domain)
    if last_name_field:
        await last_name_field.element.fill(profile.get("last_name", ""))

    # Fill email via secure tool (never exposes value to handler context beyond this)
    email_field = await finder.find_element(ctx.page, "email_field", domain)
    if email_field:
        await email_field.element.fill(creds["email"])

    # Fill password via secure function
    password_field = await finder.find_element(ctx.page, "password_field", domain)
    if password_field:
        registry.fill_credential(ctx.page, password_field.selector_used, "password", domain)

    # Fill confirm password (same value)
    confirm_field = await finder.find_element(ctx.page, "confirm_password_field", domain)
    if confirm_field:
        registry.fill_credential(ctx.page, confirm_field.selector_used, "password", domain)

    # Submit registration
    submit = await finder.find_element(ctx.page, "submit_button", domain)
    if submit:
        await submit.element.click()
        await ctx.page.wait_for_load_state("networkidle")
        return StepResult(result=HandlerResult.SUCCESS, message=f"Registration submitted for {domain}")

    return StepResult(result=HandlerResult.FAILED, message="Could not find registration submit button")


async def handle_verify_registration(ctx: KernelContext) -> StepResult:
    """Handle post-registration email verification.

    Uses Phase 4 email poller to extract OTP or magic link.
    Falls back to manual prompt if poller unavailable.
    """
    domain = extract_domain(ctx.page.url)
    settings = ctx.settings.get("automation", {})

    if settings.get("email_polling"):
        from src.automation.email_poller import EmailPoller
        poller = EmailPoller()
        try:
            poller.connect()
            # Try OTP first
            code = poller.request_verification(domain, "otp", timeout=settings.get("email_poll_timeout", 120))
            if code:
                otp_field = await find_otp_field(ctx.page)
                if otp_field:
                    await otp_field.fill(code)
                    await click_verify_button(ctx.page)
                    ctx.account_registry.mark_active(domain)
                    return StepResult(result=HandlerResult.SUCCESS, message=f"Account verified for {domain}")

            # Try magic link
            link = poller.request_verification(domain, "magic_link", timeout=60)
            if link:
                await ctx.page.goto(link)
                await ctx.page.wait_for_load_state("networkidle")
                ctx.account_registry.mark_active(domain)
                return StepResult(result=HandlerResult.SUCCESS, message=f"Account verified via link for {domain}")
        finally:
            poller.disconnect()

    # Fallback: manual
    if settings.get("manual_verification"):
        code = input(f"Enter verification code for {domain}: ").strip()
        if code:
            otp_field = await find_otp_field(ctx.page)
            if otp_field:
                await otp_field.fill(code)
                await click_verify_button(ctx.page)
                ctx.account_registry.mark_active(domain)
                return StepResult(result=HandlerResult.SUCCESS)

    ctx.account_registry.mark_failed(domain, "verification_timeout")
    return StepResult(result=HandlerResult.FAILED, message="Verification timed out")
```

---

## Modified Files

### `src/automation/kernel.py`
Add registration states to the state machine:

```python
# New states
DETECT_AUTH_TYPE = "detect_auth_type"
REGISTER = "register"
VERIFY_REGISTRATION = "verify_registration"

# New transitions
(State.NAVIGATE, HandlerResult.REQUIRES_LOGIN): State.DETECT_AUTH_TYPE,  # was: RECOVER_LOGIN
(State.DETECT_AUTH_TYPE, HandlerResult.REQUIRES_LOGIN): State.RECOVER_LOGIN,  # it's a login wall, use existing flow
(State.DETECT_AUTH_TYPE, HandlerResult.REQUIRES_REGISTRATION): State.REGISTER,
(State.REGISTER, HandlerResult.SUCCESS): State.VERIFY_REGISTRATION,
(State.REGISTER, HandlerResult.FAILED): State.CLEANUP,
(State.VERIFY_REGISTRATION, HandlerResult.SUCCESS): State.NAVIGATE,  # retry job URL with new session
(State.VERIFY_REGISTRATION, HandlerResult.FAILED): State.CLEANUP,
```

Updated state machine:
```
NAVIGATE → auth wall detected → DETECT_AUTH_TYPE
  ├── login wall (has credentials) → RECOVER_LOGIN → NAVIGATE (retry)
  ├── login wall (no credentials) → CLEANUP (needs_login)
  └── registration wall (domain allowed) → REGISTER → VERIFY_REGISTRATION → NAVIGATE (retry)
      └── registration wall (domain NOT allowed) → CLEANUP (needs_login)
```

### `src/automation/page_checks.py`
Add `detect_registration_wall()` helper:

```python
async def detect_registration_wall(page) -> bool:
    """Check if page is a registration/signup wall (not a login wall)."""
    # Checks for: confirm password field, "Create Account" text, registration form signals
```

### `src/config/models.py`
Add account creation settings:

```python
class Automation(BaseModel):
    # ... existing fields ...
    auto_register: bool = False                      # Enable auto-registration on ATS tenants
    auto_register_domains: list[str] = [             # Domain patterns allowed for auto-registration
        "*.myworkdayjobs.com",
        "*.wd*.myworkday.com",
        "*.icims.com",
        "*.greenhouse.io",
        "*.smartrecruiters.com",
        "*.taleo.net",
    ]
```

### `config/settings.example.yaml`
```yaml
automation:
  # ... existing settings ...

  # ATS Account Creation (Phase 6)
  auto_register: false              # Auto-create accounts on ATS tenant platforms
  auto_register_domains:            # Domain patterns allowed for auto-registration
    - "*.myworkdayjobs.com"
    - "*.wd*.myworkday.com"
    - "*.icims.com"
    - "*.greenhouse.io"
    - "*.smartrecruiters.com"
    - "*.taleo.net"
```

### `.env` (new entry)
```
# Account registry encryption (Phase 6)
REGISTRY_KEY=your-secure-master-key-here  # Used to encrypt stored passwords
```

### `.gitignore`
Ensure these are present:
```
data/account_registry.db
```

---

## Domain Detection Utilities

```python
# Platform detection from URL
def detect_ats_platform(domain: str) -> str | None:
    """Identify ATS platform from domain."""
    patterns = {
        "workday": [r"\.myworkdayjobs\.com$", r"\.wd\d+\.myworkday\.com$"],
        "icims": [r"\.icims\.com$"],
        "greenhouse": [r"\.greenhouse\.io$", r"boards\.greenhouse\.io"],
        "smartrecruiters": [r"\.smartrecruiters\.com$"],
        "taleo": [r"\.taleo\.net$"],
        "adp": [r"workforcenow\.adp\.com$"],
        "lever": [r"jobs\.lever\.co$"],
    }
    for platform, regexes in patterns.items():
        if any(re.search(r, domain) for r in regexes):
            return platform
    return None

def extract_tenant(domain: str, platform: str) -> str:
    """Extract tenant/company name from ATS domain."""
    # "google.wd5.myworkdayjobs.com" -> "google"
    # "jobs-meta.icims.com" -> "meta"
    if platform == "workday":
        return domain.split(".")[0]
    elif platform == "icims":
        return domain.split(".")[0].replace("jobs-", "")
    # etc.
    return domain.split(".")[0]

def is_auto_register_allowed(domain: str, settings: dict) -> bool:
    """Check if domain matches auto_register_domains allowlist."""
    allowed = settings.get("automation", {}).get("auto_register_domains", [])
    return any(fnmatch.fnmatch(domain, pattern) for pattern in allowed)
```

---

## Safety Rules (Non-Negotiable)

1. **Password stored BEFORE form submission.** `generate_credentials()` writes to DB first, returns second. If the form submission fails, we still have the credential.
2. **Credentials never sent to LLM.** `fill_credential()` fills fields directly via Playwright. The password value doesn't appear in handler code, logs, or LLM context.
3. **Credentials never committed to git.** `data/account_registry.db` is in `.gitignore`. Only the schema and code are versioned.
4. **Domain allowlist enforced.** Only domains matching `auto_register_domains` patterns get auto-registration. Unknown domains → `needs_login` (same as today).
5. **Encrypted at rest.** Passwords in the registry DB are Fernet-encrypted using a key derived from `REGISTRY_KEY` in `.env`.
6. **No fabrication.** Registration forms are filled with real profile data (name, email) + generated password. Never fabricated work history, education, etc.

---

## Testing Strategy

1. **Unit test account_registry.py:** Generate, store, retrieve, encrypt/decrypt cycle. Verify passwords never appear in DB plaintext.
2. **Unit test auth type detection:** Mock pages with login vs registration signals. Verify correct classification.
3. **Integration test — Workday tenant:**
   - Find a Workday job stuck at `needs_login`
   - Run through kernel with `auto_register: true`
   - Verify: credentials generated → form filled → registration submitted → OTP received → account verified → session saved → job applied
4. **Integration test — iCIMS tenant:** Same flow, different platform.
5. **Allowlist test:** Try auto-register on domain NOT in allowlist → verify it falls back to `needs_login`.
6. **Recovery test:** If registration fails mid-way, verify credentials are still in registry (password stored before submission).

---

## Dependencies
- **Phase 2** (kernel state machine)
- **Phase 3** (element finder for registration form fields)
- **Phase 4** (email poller for OTP verification)
- `cryptography` package (for Fernet encryption)
- `REGISTRY_KEY` in `.env`

## Estimated Scope
- ~300 lines new code (account_registry.py)
- ~250 lines new code (handlers_account.py)
- ~100 lines modified (kernel.py, page_checks.py, models.py, settings.example.yaml)
- New dependency: `cryptography` (pip install)
