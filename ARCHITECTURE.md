# Architecture Guide

This document explains how the JobHunter codebase is organized and how the pieces connect.

## Directory Map

```
src/
├── __init__.py                  # Package marker (empty)
├── __main__.py                  # python -m src entry point -> calls cli.main()
├── cli.py                       # CLI argument parsing and pipeline orchestration
├── db.py                        # SQLite database: schema, CRUD, audit log
├── utils.py                     # Shared path constants and helpers
│
├── config/                      # Everything related to user configuration
│   ├── __init__.py              # Re-exports: load_profile, load_settings, Profile, Settings
│   ├── models.py                # Pydantic models that validate profile.yaml & settings.yaml
│   └── loader.py                # Reads YAML files, runs validation, builds profile summaries
│
├── core/                        # Core business logic (no browser dependency)
│   ├── __init__.py
│   ├── scraper.py               # JobSpy integration: parallel scraping, filtering, dedup
│   ├── tailoring.py             # OpenAI calls: resume/cover letter tailoring, form answer inference
│   └── document.py              # DOCX and PDF generation from tailored text
│
└── automation/                  # Browser automation (requires Playwright)
    ├── __init__.py              # Re-exports: apply_to_jobs
    ├── applicant.py             # Batch orchestration: caps, round-robin, parallel browsers
    ├── kernel.py                # Application state machine (single-job lifecycle controller)
    ├── handlers.py              # Stateless handler functions (one per kernel state)
    ├── results.py               # StepResult + HandlerResult types (canonical return values)
    ├── element_finder.py        # 6-level element discovery escalation pipeline
    ├── selector_cache.py        # SQLite-backed adaptive selector memory
    ├── selectors.py             # Centralized selector constants, button texts, CAPTCHA indicators
    ├── page_checks.py           # Page inspection: dead page, listing, access denied, CAPTCHA, login
    ├── detection.py             # Button finding, modal dismissal, CAPTCHA/login detection
    ├── forms.py                 # DOM field extraction, form filling, file uploads, React-Select
    ├── vision_agent.py          # GPT-4o vision-based form filling for external ATS
    ├── captcha_solver.py        # 2Captcha integration: reCAPTCHA v2/Enterprise, hCaptcha, Turnstile
    ├── email_poller.py          # IMAP-based OTP/verification email polling
    └── platforms/               # Platform-specific automation (one module per job board)
        └── linkedin.py          # Easy Apply modal, share profile modal, SDUI flow
```

## Data Flow

```
User Config                     External Services
─────────────                   ─────────────────
profile.yaml ──┐                JobSpy (Indeed, LinkedIn, etc.)
settings.yaml ─┤                    │
.env ──────────┘                    v
       │                    ┌──────────────┐
       v                    │  scraper.py   │──> SQLite (jobs table, status: "new")
┌─────────────┐             └──────────────┘
│ config/     │                     │
│ loader.py   │──> Pydantic ──>     v
│ models.py   │             ┌──────────────┐      ┌──────────────┐
└─────────────┘             │ tailoring.py  │──────│  OpenAI API  │
                            └──────────────┘      └──────────────┘
                                    │
                                    v
                            ┌──────────────┐
                            │ document.py   │──> applications/{Co}/{Pos}/
                            └──────────────┘     resume.docx, .pdf
                                    │            cover_letter.docx, .pdf
                                    v
                            ┌──────────────────────────┐
                            │ applicant.py (batch)      │
                            │   └─ kernel.py (per-job)  │──> Browser (Playwright)
                            │       ├─ handlers.py      │    screenshots, form submission
                            │       ├─ element_finder   │──> SQLite (applications table,
                            │       ├─ email_poller     │    selector_cache table)
                            │       └─ vision_agent     │
                            └──────────────────────────┘
```

## Application Kernel (State Machine)

The `ApplicationKernel` in `kernel.py` controls the lifecycle of a single job application. It owns all state transitions; handlers are stateless workers that return `StepResult` values.

### State Diagram

```
SETUP ──> NAVIGATE ──> ROUTE ──> DETECT_STRATEGY
                                       │
                          ┌────────────┴────────────┐
                          v                         v
                   FILL_SELECTOR              FILL_VISION
                          │                         │
                          └────────────┬────────────┘
                                       v
                                    VERIFY ──> VERIFY_EMAIL
                                       │             │
                                       v             v
                                    CLEANUP ──> COMPLETE
                                    ▲     ▲
        SOLVE_CAPTCHA ──────────────┘     │
        RECOVER_LOGIN ────────────────────┘
```

### Key Design Rules

- **Handlers never advance state.** They return `StepResult(result=HandlerResult.XXX, metadata={...})` and the kernel's transition table decides the next state.
- **KernelContext** is a mutable dataclass threaded through the lifecycle. Handlers read/write it via explicit parameters, not global state.
- **Cleanup is centralized.** All terminal outcomes (success, failure, blocker) route through `_run_cleanup()` which handles DB writes, app directory moves, and debug screenshots.
- **CAPTCHA resume.** When a CAPTCHA is detected mid-flow, the kernel saves `pre_captcha_state` and transitions to `SOLVE_CAPTCHA`. On success, it resumes from the saved state.

### Element Finder Escalation

The `ElementFinder` tries up to 6 levels to locate an element on the page:

1. **Selector cache** — SQLite lookup by domain + intent (1ms)
2. **Heuristic selectors** — Hardcoded CSS/attribute patterns (5ms)
3. **Accessibility roles** — Playwright `get_by_role()` API (10ms)
4. **Visible text scan** — JS `document.evaluate()` XPath (20ms)
5. **Text LLM** — DOM snippet → selector (future)
6. **Vision LLM** — Screenshot → coordinates (future)

On success at any level, the result is cached in `selector_cache` for future use. Confidence decays over time and after failures; selectors below 0.3 are skipped.

### Email Poller

The `EmailPoller` connects via IMAP to watch for OTP codes and magic links during application flows:

- Polls inbox for emails from the ATS domain within a configurable timeout
- Extracts 6-8 digit codes or verification URLs via regex
- Fallback chain: email poller → manual terminal prompt → fail with `needs_login`
- Requires `EMAIL_USER` + `EMAIL_APP_PASSWORD` in `.env` and `email_polling: true` in settings

## Package Design Principles

### `config/` -- Configuration

- All config access goes through `load_profile()` and `load_settings()`
- Pydantic models validate every field before it reaches business logic
- `loader.py` handles file I/O; `models.py` handles validation rules
- The `__init__.py` re-exports everything so callers use `from src.config import load_settings`

### `core/` -- Business Logic

- No dependency on Playwright or browser state
- Each module handles one concern: scraping, tailoring, or document generation
- `tailoring.py` contains the hardcoded SYSTEM_PROMPT anti-fabrication safeguard -- this must never be weakened or made configurable
- `document.py` enforces one-page resume via tight margins and font sizing

### `automation/` -- Browser Automation

Split by responsibility:

- **`applicant.py`** -- Batch orchestration: which jobs, in what order, how many browsers
- **`kernel.py`** -- Single-job state machine: owns all workflow transitions
- **`handlers.py`** -- Stateless workers: one function per kernel state, returns `StepResult`
- **`results.py`** -- Canonical types: `HandlerResult` enum + `StepResult` dataclass
- **`element_finder.py`** -- Smart element discovery with 6-level escalation
- **`selector_cache.py`** -- SQLite-backed adaptive memory for selectors (confidence decay, bootstrap from `SELECTOR_INTENTS`)
- **`selectors.py`** -- Centralized constants: button texts, CAPTCHA indicators, ATS domains
- **`detection.py`** -- Reads the page: CAPTCHA? Login wall? Where's the Apply button?
- **`forms.py`** -- Interacts with forms: extract fields, fill them, upload files
- **`vision_agent.py`** -- GPT-4o fallback for external ATS that resist selector-based filling
- **`email_poller.py`** -- IMAP polling for OTP codes and verification links
- **`captcha_solver.py`** -- 2Captcha API integration for solving CAPTCHAs
- **`platforms/`** -- Platform-specific modules (LinkedIn, etc.) for custom quirks

Each function takes a Playwright `page` object -- no global browser state.

### `db.py` -- Database

- Single module, not a package -- the schema is simple enough
- WAL journal mode for concurrent read/write safety
- Safe column migration via ALTER TABLE with error suppression
- All queries return `dict` (via `sqlite3.Row`) for easy access
- Tables: `jobs`, `applications`, `application_log`, `scrape_cache`, `answer_bank`, `selector_cache`

### `cli.py` -- CLI

- Lazy imports (`from .core.scraper import scrape_jobs` inside functions) to keep startup fast
- Pipeline orchestration: scrape -> tailor -> apply with error isolation per step
- Round-robin job selection for even distribution across search roles

## Key Conventions

| Convention | Why |
|-----------|-----|
| ASCII-only Rich output | Windows terminal compatibility |
| `force_terminal=True` on Console | Ensure color output in all environments |
| Lazy imports for heavy deps | Playwright, JobSpy, OpenAI only loaded when needed |
| PDF uses built-in Helvetica | No font files needed, works everywhere |
| Config via Pydantic then `.model_dump()` | Modules work with plain dicts for simplicity |
| Daily + per-round application caps | Prevent account flagging on job sites |
| Handlers return StepResult | Kernel controls transitions, handlers stay stateless |
| Element finder escalation | Fast cache hits first, expensive LLM only when needed |

## Import Graph

```
cli.py
├── db
├── config (load_settings)
├── utils
├── core.scraper      (lazy)
├── core.tailoring     (lazy)
├── core.document      (lazy)
└── automation         (lazy)

automation.applicant
├── db
├── config
├── utils
└── automation.kernel

automation.kernel
├── automation.handlers
├── automation.results
├── automation.element_finder
├── automation.selector_cache
├── automation.page_checks
└── automation.detection

automation.handlers
├── automation.results
├── automation.detection
├── automation.forms
├── automation.vision_agent
├── automation.page_checks
├── automation.email_poller
├── core.tailoring (infer_form_answers)
├── core.document  (save_application_metadata)
└── db

automation.element_finder
├── automation.selector_cache
└── automation.selectors (HEURISTIC_MAP, ROLE_MAP, TEXT_PATTERNS)

automation.selector_cache
├── automation.selectors (SELECTOR_INTENTS)
└── db

core.scraper
├── db
└── config

core.tailoring
├── config
└── utils

core.document
└── utils

config.loader
├── config.models
└── utils

db
└── utils
```
