"""Encrypted local credential store for ATS tenant accounts.

Passwords are encrypted at rest using Fernet with a key derived from
REGISTRY_KEY in .env. Credentials are NEVER exposed to LLM, logs, or DB
in plaintext. domain is the primary key -- one account per ATS tenant.
"""

import fnmatch
import logging
import os
import re
import secrets
import sqlite3
from base64 import urlsafe_b64encode
from hashlib import pbkdf2_hmac

from cryptography.fernet import Fernet

from ..utils import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "account_registry.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    domain      TEXT NOT NULL PRIMARY KEY,
    tenant      TEXT,
    email       TEXT NOT NULL,
    password_encrypted BLOB,
    platform    TEXT,
    status      TEXT DEFAULT 'pending',
    created_at  TEXT,
    last_login  TEXT,
    last_used   TEXT,
    notes       TEXT
)
"""


class AccountRegistry:
    """Encrypted local credential store for ATS tenant accounts."""

    def __init__(self):
        self._fernet = self._init_encryption()
        self._conn = sqlite3.connect(str(DB_PATH))
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def _init_encryption(self) -> Fernet:
        """Derive a Fernet key from REGISTRY_KEY env var."""
        master_key = os.environ.get("REGISTRY_KEY")
        if not master_key:
            raise ValueError("REGISTRY_KEY must be set in .env for account registry")
        key = pbkdf2_hmac("sha256", master_key.encode(), b"jobhunter-registry", 100_000)
        return Fernet(urlsafe_b64encode(key[:32]))

    # ------------------------------------------------------------------
    # Credential lifecycle
    # ------------------------------------------------------------------

    def generate_credentials(self, domain: str, tenant: str = None,
                             platform: str = None) -> dict:
        """Generate + store credentials for a new ATS tenant account.

        Password is stored encrypted BEFORE being returned (safety: we
        never lose a password due to a failed form submit).

        Returns: {"email": str, "password": str}
        """
        base_email = os.environ.get("EMAIL_USER", "")
        if "@" in base_email:
            user, domain_part = base_email.split("@", 1)
            tag = f"{platform or 'ats'}-{tenant or domain}"
            alias = f"{user}+{tag}@{domain_part}"
        else:
            alias = base_email

        password = secrets.token_urlsafe(16)
        encrypted = self._fernet.encrypt(password.encode())

        self._conn.execute(
            "INSERT OR REPLACE INTO accounts "
            "(domain, tenant, email, password_encrypted, platform, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', datetime('now'))",
            (domain, tenant, alias, encrypted, platform),
        )
        self._conn.commit()
        logger.debug(f"AccountRegistry: generated credentials for {domain}")
        return {"email": alias, "password": password}

    def get_credentials(self, domain: str) -> dict | None:
        """Return decrypted credentials for domain, or None if not found."""
        row = self._conn.execute(
            "SELECT email, password_encrypted, status FROM accounts WHERE domain = ?",
            (domain,),
        ).fetchone()
        if not row:
            return None
        email, encrypted, status = row
        password = self._fernet.decrypt(encrypted).decode()
        return {"email": email, "password": password, "status": status}

    def has_account(self, domain: str) -> bool:
        """True if we have an active or pending account for domain."""
        row = self._conn.execute(
            "SELECT status FROM accounts WHERE domain = ?", (domain,)
        ).fetchone()
        return row is not None and row[0] in ("active", "pending")

    def mark_active(self, domain: str):
        self._conn.execute(
            "UPDATE accounts SET status = 'active', last_login = datetime('now') "
            "WHERE domain = ?",
            (domain,),
        )
        self._conn.commit()

    def mark_failed(self, domain: str, reason: str = ""):
        self._conn.execute(
            "UPDATE accounts SET status = 'failed', notes = ? WHERE domain = ?",
            (reason, domain),
        )
        self._conn.commit()

    def fill_credential(self, page, selector: str, credential_type: str, domain: str):
        """Fill a credential field directly WITHOUT returning the value to the caller.

        Args:
            page: Playwright sync page
            selector: CSS/text selector for the target field
            credential_type: "email" or "password"
            domain: ATS tenant domain
        """
        creds = self.get_credentials(domain)
        if not creds:
            raise ValueError(f"No credentials found for {domain}")
        value = creds[credential_type]
        page.locator(selector).fill(value)


# ------------------------------------------------------------------
# Domain detection utilities
# ------------------------------------------------------------------

_ATS_PATTERNS: dict[str, list[str]] = {
    "workday":        [r"\.myworkdayjobs\.com$", r"\.wd\d+\.myworkday\.com$"],
    "icims":          [r"\.icims\.com$"],
    "greenhouse":     [r"\.greenhouse\.io$", r"boards\.greenhouse\.io"],
    "smartrecruiters":[r"\.smartrecruiters\.com$"],
    "taleo":          [r"\.taleo\.net$"],
    "adp":            [r"workforcenow\.adp\.com$"],
    "lever":          [r"jobs\.lever\.co$"],
    "jobvite":        [r"\.jobvite\.com$"],
    "ashby":          [r"\.ashbyhq\.com$"],
}


def detect_ats_platform(domain: str) -> str | None:
    """Identify the ATS platform from domain string."""
    for platform, patterns in _ATS_PATTERNS.items():
        if any(re.search(p, domain) for p in patterns):
            return platform
    return None


def extract_tenant(domain: str, platform: str | None) -> str:
    """Extract tenant/company name from ATS domain.

    Examples:
        "google.wd5.myworkdayjobs.com" -> "google"
        "jobs-meta.icims.com"          -> "meta"
    """
    if platform == "workday":
        return domain.split(".")[0]
    if platform == "icims":
        return domain.split(".")[0].replace("jobs-", "").replace("jobs.", "")
    return domain.split(".")[0]


def is_auto_register_allowed(domain: str, settings: dict) -> bool:
    """Check if domain matches any pattern in auto_register_domains allowlist."""
    auto_register = settings.get("automation", {}).get("auto_register", False)
    if not auto_register:
        return False
    patterns = settings.get("automation", {}).get("auto_register_domains", [])
    return any(fnmatch.fnmatch(domain, p) for p in patterns)
