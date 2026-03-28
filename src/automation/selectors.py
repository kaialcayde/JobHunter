"""Centralized selector constants for browser automation.

All button texts, Playwright selectors, URL patterns, and detection phrases
live here. When a new ATS platform uses non-standard text, add it here
and document it in LEARNINGS.md.

NOTE: JS-embedded selectors (inside page.evaluate() strings) stay inline
in their respective modules since they run in browser context. Those
modules reference constants here via comments for discoverability.
"""

# ── Apply Button ────────────────────────────────────────────────────

# Text-based matching for Apply buttons across ATS platforms (lowercased)
APPLY_BUTTON_TEXTS = [
    "apply now", "apply", "apply for this job", "apply for this position",
    "i'm interested", "im interested", "submit application", "start application",
    "begin application", "apply at company name",
]

# Playwright selectors for Apply buttons (non-LinkedIn)
APPLY_BUTTON_PW_SELECTORS = [
    'button:has-text("Apply Now")', 'button:has-text("Apply")',
    "button:has-text(\"I'm interested\")", 'button:has-text("Start application")',
    '[data-testid*="apply"]', '.apply-button', '#apply-button',
    '.js-btn-apply', '[data-testid*="interest"]',
]

# Selectors used in force_apply_click URL extraction fallback
FORCE_APPLY_SELECTORS = [
    'a:has-text("Apply Now")',
    'button:has-text("Apply Now")',
    'a:has-text("Apply")',
    'button:has-text("Apply")',
    '[data-testid*="apply"]',
    '.apply-button',
    '#apply-button',
]


# ── Next / Continue Button ──────────────────────────────────────────

# Playwright selectors for Next/Continue (LinkedIn Easy Apply aria-labels first)
NEXT_BUTTON_PW_SELECTORS = [
    'button[aria-label="Continue to next step"]',
    'button[aria-label="Next"]',
    'button[aria-label="Review your application"]',
    'button[aria-label="Review"]',
    'button[data-automation-id="bottom-navigation-next-button"]',
]

# Text-based matching for Next buttons (Playwright get_by_role + JS fallback)
NEXT_BUTTON_TEXTS = ["Next", "Continue", "Review"]

# JS-side selectors (used inside page.evaluate, duplicated for browser context)
# Keep in sync with NEXT_BUTTON_PW_SELECTORS
NEXT_BUTTON_JS_SELECTORS = [
    'button[aria-label="Continue to next step"]',
    'button[aria-label="Next"]',
    'button[aria-label="Review your application"]',
    'button[aria-label="Review"]',
    'button[data-automation-id="bottom-navigation-next-button"]',
    '[data-testid*="next"]',
]

NEXT_BUTTON_JS_TEXTS = ["next", "continue", "review"]


# ── Submit Button ───────────────────────────────────────────────────

# Playwright selectors for Submit (LinkedIn Easy Apply aria-labels first)
SUBMIT_BUTTON_PW_SELECTORS = [
    'button[aria-label="Submit application"]',
    'button[aria-label="Submit"]',
    '#submit_app', '#submit-application',
    'button[data-automation-id="submit"]',
    'input[type="submit"]', 'button[type="submit"]',
]

# Text-based matching for Submit buttons (Playwright get_by_role)
SUBMIT_BUTTON_TEXTS = [
    "Submit application", "Submit", "Send application",
    "Apply", "Complete", "Done",
]

# JS-side selectors (used inside page.evaluate, duplicated for browser context)
SUBMIT_BUTTON_JS_SELECTORS = [
    'button[aria-label="Submit application"]', 'button[aria-label="Submit"]',
    '#submit_app', '#submit-application',
    'button[data-automation-id="submit"]',
    '.posting-btn-submit', 'button.postings-btn',
    '.iCIMS_Button', 'button.btn-submit',
    '[data-testid*="submit"]', '[data-testid*="apply"]',
    'input[type="submit"]', 'button[type="submit"]',
]

SUBMIT_BUTTON_JS_TEXTS = [
    "submit application", "submit", "send application",
    "apply", "complete", "finish", "done",
]


# ── Modal Dismiss ───────────────────────────────────────────────────

# Generic modal dismiss selectors (JS-side, used in dismiss_modals)
MODAL_DISMISS_SELECTORS = [
    'button[aria-label="Dismiss"]', 'button[aria-label="Close"]',
    '[data-test-modal-close-btn]', '.modal__dismiss',
    'button[class*="close"]', 'button[class*="dismiss"]',
    '[aria-label="close"]',
]

MODAL_DISMISS_TEXTS = ["dismiss", "not now", "no thanks", "skip"]


# ── CAPTCHA Detection ───────────────────────────────────────────────

# Challenge widget selectors — block if visible (JS-side, in detect_captcha)
CAPTCHA_CHALLENGE_SELECTORS = [
    'iframe[src*="hcaptcha"]',
    '#captcha',
    'iframe[src*="challenges.cloudflare.com"]',
    '[class*="cf-turnstile"]', '#challenge-running', '#challenge-form',
]

# Passive CAPTCHA indicators — only block when no form content visible
CAPTCHA_PASSIVE_SELECTORS = [
    '.grecaptcha-badge',
    'iframe[src*="recaptcha"]',
    'iframe[title*="reCAPTCHA"]',
    '[class*="captcha"]',
]

# Domains where reCAPTCHA scripts are always loaded but not blocking
CAPTCHA_KNOWN_PASSIVE_DOMAINS = ["ashbyhq.com", "gem.com"]

# Body text phrases indicating active CAPTCHA challenge
CAPTCHA_BODY_PHRASES = [
    "verify you are human", "additional verification required",
    "please verify you're not a robot", "checking your browser",
]


# ── Login Detection ─────────────────────────────────────────────────

# Site-specific login URLs (definitive — no password field needed)
LOGIN_SITE_PATTERNS = [
    "linkedin.com/signup", "linkedin.com/login", "linkedin.com/checkpoint",
    "linkedin.com/uas/login", "indeed.com/account/login", "indeed.com/auth",
    "glassdoor.com/member/auth", "amazon.jobs/account/signin",
    "passport.amazon.jobs",
]

# Generic URL patterns (require password field to confirm)
LOGIN_GENERIC_URL_PATTERNS = ["/login", "/signin", "/sign-in", "/auth/"]

# Body text phrases indicating login page (require password field)
LOGIN_BODY_PHRASES = [
    "sign in to continue", "sign in to see who you already know",
    "join linkedin", "join now", "log in to indeed", "create an account",
    "log in using your", "log in to your account", "sign in to your account",
    "enter your password",
]


# ── Access Denied / Error Detection ─────────────────────────────────

ACCESS_DENIED_PHRASES = [
    "access denied", "access to this page has been denied",
    "403 forbidden", "you don't have permission",
    "request blocked", "this page is not available",
    "there has been a critical error", "500 internal server error",
    "this site is experiencing technical difficulties",
]


# ── Listing Page Detection ──────────────────────────────────────────

# Signals that a page is a job listing (not an application form)
LISTING_SIGNALS = [
    "job description", "responsibilities", "qualifications",
    "about the role", "what you'll do", "requirements",
    "benefits", "life at", "about us",
]

# URL patterns that indicate a form page (not listing-only)
LISTING_EXCEPTION_PATTERNS = [
    "boards.greenhouse.io", "job-boards.greenhouse.io", "grnh.se",
    "/application", "/apply",
]


# ── ATS Domains ─────────────────────────────────────────────────────

# Known ATS domains (used for URL collapsing and apply URL extraction)
ATS_DOMAINS = [
    "myworkdayjobs.com", "workday.com", "greenhouse.io", "lever.co",
    "icims.com", "smartrecruiters.com", "ashbyhq.com", "taleo.net",
    "jobvite.com", "adp.com", "ultipro.com",
]


# ── LinkedIn-Specific ───────────────────────────────────────────────

# LinkedIn modal container selectors
LINKEDIN_MODAL_SELECTORS = (
    '[role="dialog"], .artdeco-modal, .artdeco-modal-overlay, '
    '[data-test-modal], div[class*="modal"][class*="overlay"], '
    '.share-profile-modal'
)

# LinkedIn Apply button selectors (ordered by reliability)
LINKEDIN_APPLY_SELECTORS = [
    # LinkedIn-specific classes (most reliable) — match both <button> and <a>
    '.jobs-apply-button',
    '.jobs-s-apply button',
    '.jobs-s-apply a',
    'button.jobs-apply-button',
    'a.jobs-apply-button',
    # Aria labels — both button and anchor
    'button[aria-label*="Apply"]',
    'a[aria-label*="Apply"]',
    'button[aria-label*="apply"]',
    'a[aria-label*="apply"]',
    # Data attributes
    '[data-tracking-control-name*="apply"]',
    # Text-based fallback — both button and anchor
    'button:has-text("Easy Apply")',
    'button:has-text("Apply")',
    'a:has-text("Easy Apply")',
    'a:has-text("Apply")',
]

# LinkedIn Apply button area wait selectors (broad, for lazy-load detection)
LINKEDIN_APPLY_WAIT_SELECTORS = (
    '.jobs-apply-button, .jobs-s-apply, [data-tracking-control-name*="apply"]'
)

# LinkedIn Easy Apply modal detection selectors
LINKEDIN_EASY_APPLY_SELECTORS = [
    '.jobs-easy-apply-modal', '.jobs-easy-apply-content',
    '[role="dialog"][aria-label*="Easy Apply"]',
    '[role="dialog"][aria-label*="Apply to"]',
    '[role="dialog"] .jobs-easy-apply-form-element',
]

# LinkedIn shadow DOM host selectors (for Easy Apply inside shadow root)
LINKEDIN_SHADOW_HOST_SELECTORS = (
    '#interop-outlet, [data-testid="interop-shadowdom"], '
    '[class*="interop"], [id*="shadow"]'
)

# LinkedIn modal scope selectors (for scoping button/field searches to modal)
LINKEDIN_MODAL_SCOPE_SELECTORS = (
    '.jobs-easy-apply-modal, .jobs-easy-apply-content, '
    '[role="dialog"], .artdeco-modal'
)

# LinkedIn overlay dismiss selectors
LINKEDIN_OVERLAY_SELECTORS = (
    '.artdeco-modal-overlay, .artdeco-modal-overlay--is-top-layer'
)

# Continue button selectors for Share Profile modal (Playwright)
SHARE_PROFILE_CONTINUE_SELECTORS = [
    'button:has-text("Continue")',
    '[role="dialog"] button:has-text("Continue")',
    '.artdeco-modal button:has-text("Continue")',
    '[role="dialog"] a:has-text("Continue")',
    'a:has-text("Continue")',
]


# ── Intent Mappings (for SelectorCache bootstrap) ──────────────────

SELECTOR_INTENTS = {
    "apply_button": {
        "pw_selectors": APPLY_BUTTON_PW_SELECTORS,
        "texts": APPLY_BUTTON_TEXTS,
    },
    "next_button": {
        "pw_selectors": NEXT_BUTTON_PW_SELECTORS,
        "texts": NEXT_BUTTON_TEXTS,
    },
    "submit_button": {
        "pw_selectors": SUBMIT_BUTTON_PW_SELECTORS,
        "texts": SUBMIT_BUTTON_TEXTS,
    },
}
