# Architecture Guide

This document reflects the current JobHunter architecture after the kernel/selector-cache/email-polling/account-registry phases. It uses `CLAUDE.md`, `LEARNINGS.md`, `notes/phases/*`, and the current codebase as the source of truth.

## Project Goal

JobHunter is meant to become a self-sufficient resume applier that can reliably run:

1. `scrape`
2. `tailor`
3. `apply`

Core operating principle:

- Playwright-first, LLM-second
- DOM-first, vision-last
- Persist sessions/cookies where possible
- Use explicit state transitions instead of monolithic orchestration

## Current System Shape

Compared to the old `flow.py` monolith described in `notes/phases/before_after_state.md`, the code now uses:

- `ApplicationKernel` for explicit workflow control
- `StepResult` / `HandlerResult` structured returns
- `ElementFinder` + `SelectorCache` for adaptive element discovery
- `EmailPoller` for OTP/verification handling
- `AccountRegistry` + account handlers for ATS registration/login flows
- platform modules for site-specific logic

## Directory Map

```text
src/
  __main__.py                  python -m src entry point
  cli.py                       CLI commands and orchestration
  db.py                        SQLite schema, status queries, audit logging
  utils.py                     Shared paths and helper utilities

  config/
    __init__.py
    loader.py                  YAML loading, blacklist loading, profile summaries
    models.py                  Pydantic models for profile/settings

  core/
    __init__.py
    scraper.py                 JobSpy scraping, filtering, dedup, blacklist skip
    tailoring.py               OpenAI tailoring and answer inference
    document.py                DOCX/PDF generation

  automation/
    __init__.py
    applicant.py               Batch orchestration across jobs/sites
    kernel.py                  Explicit single-job state machine
    handlers.py                Stateless workflow handlers
    handlers_account.py        Auth wall classification and ATS account flows
    results.py                 HandlerResult enum + StepResult dataclass
    detection.py               Apply button, CAPTCHA, modal, login detection
    page_checks.py             Blocker checks and login recovery
    forms.py                   DOM field extraction/filling, uploads, React-Select
    element_finder.py          Escalating element discovery
    selector_cache.py          SQLite-backed adaptive selector memory
    selectors.py               Intent bootstrap and selector constants
    vision_agent.py            GPT-4o vision fallback for external ATS
    captcha_solver.py          2Captcha integration
    email_poller.py            IMAP verification polling
    account_registry.py        Encrypted ATS credential store
    platforms/
      __init__.py
      linkedin.py              LinkedIn Easy Apply / SDUI / Share Profile logic
      avature.py               Avature-specific support
```

## Config and Runtime Files

```text
config/
  profile.yaml
  settings.yaml
  profile.example.yaml
  settings.example.yaml
  domain_blacklist.txt

data/
  jobhunter.db
  linkedin_auth.json
  site_auth/{domain}.json
  logs/
  account_registry.db
```

## Pipeline Overview

### 1. Scrape

`src/core/scraper.py`

- Reads `job_search` settings
- Scrapes configured boards via JobSpy
- Filters by company, salary, title, keywords
- Skips URLs matching `config/domain_blacklist.txt`
- Inserts jobs into SQLite with status `new`

### 2. Tailor

`src/core/tailoring.py` + `src/core/document.py`

- Uses profile/settings + OpenAI to generate tailored resume/cover letter text
- Preserves anti-fabrication constraints
- Generates DOCX/PDF outputs
- Updates job status to `tailored`

### 3. Apply

`src/automation/applicant.py` + `src/automation/kernel.py`

- Selects which jobs to apply to based on caps/distribution
- Launches browser contexts
- Runs each job through the explicit kernel lifecycle
- Writes final statuses and artifacts

## State Machine

The current system replaced implicit flow control with an explicit kernel.

Primary states:

```text
SETUP -> NAVIGATE -> ROUTE -> DETECT_STRATEGY
                                  |-> FILL_SELECTOR
                                  |-> FILL_VISION
FILL_* -> VERIFY -> CLEANUP -> COMPLETE
```

Cross-cutting states:

```text
SOLVE_CAPTCHA
RECOVER_LOGIN
VERIFY_EMAIL
DETECT_AUTH_TYPE
LOGIN_REGISTRY
REGISTER
```

Key rules:

- Handlers never advance workflow state directly.
- Handlers return `StepResult`.
- The kernel transition table decides what happens next.
- Cleanup centralizes terminal status handling and logging.

## Handler Model

The phase docs originally described the move from mixed returns to structured results. That is now the active architecture:

- `HandlerResult` is the canonical outcome type.
- `StepResult` carries the result plus metadata.
- `KernelContext` carries mutable job/application state across the run.

This is the foundation that enables:

- CAPTCHA pause/resume
- login recovery and retry
- registration flows
- verification flows
- strategy switching

## Strategy Selection

The system supports two main apply strategies:

### Selector / DOM Strategy

Used primarily for LinkedIn Easy Apply and deterministic flows.

- Extract fields from the DOM
- Infer answers using profile + answer bank
- Fill with Playwright-native interactions
- Advance through Next/Review/Submit buttons

### Vision Strategy

Used for external ATS flows that resist deterministic selectors.

- DOM prefill still happens first
- Vision agent handles the unpredictable remainder
- The agent works in batch rounds rather than one action at a time

The guiding rule from `CLAUDE.md` and `LEARNINGS.md` is still:

- deterministic Playwright actions first
- vision only when needed

## Selector Cache and Element Finder

This came from phases 3 and later learnings. The current design is:

1. selector cache
2. heuristic selectors
3. accessibility roles
4. visible text scan
5. LLM text fallback
6. LLM vision fallback

Why this matters:

- reduces repeated breakage from static selectors
- keeps LLM cost lower
- lets successful discoveries become reusable cache entries

`selectors.py` is now bootstrap/config data, not the whole discovery system.

## Login, Sessions, and Cookies

Current persisted auth model:

- LinkedIn storage state in `data/linkedin_auth.json`
- Other cookie-based site auth in `data/site_auth/{domain}.json`
- `python -m src login` refreshes LinkedIn + Indeed
- `python -m src login-sites` refreshes default sessions and then blocked `needs_login` domains sequentially

Known realities:

- LinkedIn is the strongest supported manual-auth path
- Indeed can still hit Cloudflare challenge pages
- Google Careers rejects automated-browser sign-in and is currently blacklisted
- ZipRecruiter is currently treated as unsupported in the automated pipeline

Long-term architectural direction:

- move toward more durable auth/session models for supported boards
- avoid assuming all major sites are equally automatable

## Email Verification

The phase 4 design exists in code now:

- `email_poller.py` uses IMAP
- settings control server, port, timeout, enablement
- fallback remains manual OTP prompt where needed

This supports:

- OTP codes
- some magic-link style verification flows
- account-creation verification

## ATS Account Creation

Phase 6 concepts are represented in code now:

- `account_registry.py` stores ATS credentials encrypted
- `handlers_account.py` supports:
  - auth wall classification
  - registry login
  - registration flows on allowlisted domains

Relevant config:

- `automation.auto_register`
- `automation.auto_register_domains`
- `REGISTRY_KEY` in `.env`

This is especially important for tenant-based ATS platforms like:

- Workday
- iCIMS
- Greenhouse
- SmartRecruiters
- Taleo
- Avature

## Blacklist Layer

Scraping now honors `config/domain_blacklist.txt`.

Match rules:

- `domain.com` matches hostname and subdomains
- `domain.com/path` matches URL fragments

Current known blacklist use:

- Google Careers is blocked there because automated login/apply is not currently reliable

This layer exists to stop unstable sources from entering the pipeline in the first place.

## Database Responsibilities

`src/db.py` is still the central database module.

Main tables used by the current architecture:

- `jobs`
- `applications`
- `application_log`
- `answer_bank`
- `selector_cache`
- `scrape_cache`

Operational role:

- source of truth for job status
- audit trail for workflow transitions
- selector learning persistence
- answer reuse and seeding

## Launch Config Workflows

Current VS Code launch configs live in `.vscode/launch.json`.

Most commonly used workflow:

1. `JobHunter: Pipeline (Refresh Profile from profile.yaml)`
2. `JobHunter: Login`
3. `JobHunter: Apply Only`

Other important launch configs:

- `JobHunter: Full Pipeline (scrape -> tailor -> apply)`
- `JobHunter: Scrape Only`
- `JobHunter: Tailor Only`
- `JobHunter: Login Sites (Retry)`
- `JobHunter: Apply Job (by ID)`
- `JobHunter: Apply Job --debug (pause after DOM fill, each vision round)`

Why this matters:

- docs should reflect the actual operator workflow, not just the theoretical pipeline
- login refresh and repeated apply runs are part of the real system operation today

## Lessons Baked Into Architecture

The current architecture directly reflects `LEARNINGS.md`:

- LinkedIn requires a dedicated platform module
- Share Profile modal handling must use native Playwright clicks
- shadow DOM support matters for LinkedIn
- React-Select and controlled inputs require DOM-aware filling
- CAPTCHA/login detection must happen before wasting vision rounds
- external ATS pages often need DOM prefill before vision
- some sites are not worth treating as stable first-class automated targets yet

## Design Rules

These remain the governing implementation rules:

- Never weaken anti-fabrication resume safeguards
- Keep Playwright deterministic paths first
- Put site-specific quirks in platform modules
- Add debug screenshots on automation failures
- Add new selector intents in centralized bootstrap/config, not ad hoc handler code
- Mirror config structure changes to example files
- Read `LEARNINGS.md` before changing automation

## References

- `CLAUDE.md`
- `LEARNINGS.md`
- `notes/phases/before_after_state.md`
- `notes/phases/phase1_structured_results.md`
- `notes/phases/phase2_automation_kernel.md`
- `notes/phases/phase3_selector_cache.md`
- `notes/phases/phase4_email_polling.md`
- `notes/phases/phase5_integration_cleanup.md`
- `notes/phases/phase6_ats_account_creation.md`
