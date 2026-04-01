# JobHunter - Claude Context

## What you are
You are a principal software engineer focused on software architecture, optimization and efficiency, and creating working prototypes.

## What This Project Is
Automated job application system for the owner (Kai Alcayde). Scrapes job listings, tailors resume/cover letter with OpenAI GPT-4o, and submits applications via Playwright browser automation.

Primary goal: build a self-sufficient resume applier that can reliably run the full scrape -> tailor -> apply pipeline with minimal manual intervention. The system should persist sessions/cookies where possible so it can behave like a durable browser-based operator across repeated runs.

## Architecture
- Keep organization as you think it should be. If you need to make a new file in a new folder, do so - do not pack in thousands of line of code into a single file.
- **Python 3.12** project, venv at `venv/`
- **CLI entry point**: `python -m src <command>` (pipeline, scrape, tailor, apply, status, list)
- **Config**: `config/profile.yaml` (personal info), `config/settings.yaml` (search params), `.env` (API keys)
- **Templates**: `templates/base_resume.docx`, `templates/base_cover_letter.docx`
- **Output**: `applications/{Company}/{Position}/` with tailored DOCX/PDF + metadata JSON
- **Database**: SQLite at `data/jobhunter.db`
- **Logs**: `data/logs/pipeline_YYYY-MM-DD.log`
- **Scheduler**: `run_pipeline.bat` via Windows Task Scheduler

## Key Modules
- `src/cli.py` - CLI orchestrator, pipeline flow
- `src/db.py` - SQLite schema (jobs, applications, application_log, scrape_cache, answer_bank, selector_cache tables)
- `src/utils.py` - Path constants, directory helpers, filename sanitization
- `src/config/` - Configuration loading and validation
  - `models.py` - Pydantic models validating profile.yaml and settings.yaml
  - `loader.py` - YAML loading through Pydantic validation, profile summary generation
- `src/core/` - Core business logic
  - `scraper.py` - JobSpy wrapper, multi-board search, dedup, filtering
  - `tailoring.py` - OpenAI integration, answer bank seeding, form answer inference, anti-fabrication safeguard
  - `document.py` - DOCX/PDF generation, one-page resume enforcement
- `src/automation/` - Browser automation
  - `applicant.py` - Batch orchestration: caps, round-robin distribution, parallel browsers
  - `browser_scripts/` - Browser-context JS assets loaded into `page.evaluate(...)` / `frame.evaluate(...)`
  - `kernel.py` - Application state machine (single-job lifecycle: SETUP → NAVIGATE → ROUTE → FILL → VERIFY → CLEANUP)
  - `handlers.py` - Thin public facade for stateless handler functions
  - `handlers_steps/` - Internal setup/navigation/fill/verify handler modules
  - `handlers_account.py` - Thin public facade for ATS auth/registration handlers
  - `auth_flow/` - Internal ATS auth-type detection, login, registration, verification
  - `results.py` - Canonical types: HandlerResult enum + StepResult dataclass
  - `element_finder.py` - 6-level element discovery escalation (cache → heuristic → a11y → text → LLM)
  - `selector_cache.py` - SQLite-backed adaptive selector memory (confidence decay, bootstrap from SELECTOR_INTENTS)
  - `selectors.py` - Centralized selector constants (button texts, modal selectors, CAPTCHA indicators, ATS domains)
  - `page_checks.py` - Page inspection (dead page, listing, access denied, CAPTCHA, login), login recovery, URL utilities
  - `detection.py` - CAPTCHA/login detection, modal dismissal, Apply/Next/Submit button clicking
  - `forms.py` - Thin public facade for unified form extraction/fill API
  - `forms_helpers/` - Internal DOM, Playwright, coordinate, select, and upload helpers
  - `vision_agent.py` - Thin public facade for GPT-4o vision fallback
  - `vision/` - Internal vision client, loop, action, OTP, and submission modules
  - `captcha_solver.py` - 2Captcha integration, reCAPTCHA v2/Enterprise, hCaptcha, Turnstile, Cloudflare auto-challenge
  - `email_poller.py` - IMAP-based OTP/verification email polling (code extraction, magic link detection)
  - `platforms/` - Platform-specific automation (one module per job board with custom quirks)
    - `linkedin.py` - Thin public facade for LinkedIn logic
    - `linkedin_parts/` - LinkedIn modal and apply internals
    - `avature.py` - Thin public facade for Avature support
    - `avature_parts/` - Avature widget-prefill and page-flow internals
    - `greenhouse.py` - (create when needed) reCAPTCHA Enterprise gate

## Important Conventions
- **Never fabricate resume content** - the SYSTEM_PROMPT in tailoring.py is hardcoded and must not be weakened
- **Resume must fit one page** - enforced in both LLM prompt and DOCX formatting (10.5pt, tight margins)
- **Windows environment** - use ASCII characters in Rich output (no unicode arrows/box drawing), console uses `force_terminal=True`
- **Salary values in YAML** - must be plain integers, no commas (e.g., `150000` not `150,000`)
- **Config changes go through Pydantic** - `load_profile()` and `load_settings()` validate via models before returning dicts
- **Personal files are gitignored** - .env, profile.yaml, settings.yaml, templates/, applications/, data/
- **New CLI commands must have a VS Code launch config** - when adding a new `python -m src <command>`, always add a matching entry in `.vscode/launch.json` so the user can run it via the green button (F5)
- **Config edits must mirror to example files** - when editing `config/settings.yaml` or `config/profile.yaml`, always apply the same structural changes (new keys, removed keys, reordering) to `config/settings.example.yaml` or `config/profile.example.yaml` respectively. Example files are checked into git; the real config files are gitignored
- **Read LEARNINGS.md before changing automation code** - contains hard-won platform-specific quirks (LinkedIn modals, ATS button texts, vision agent pitfalls). Reference it before making changes, update it when discovering new platform behavior
- **New platform quirks get their own LEARNINGS.md section** - each job board (LinkedIn, SmartRecruiters, Greenhouse, etc.) has its own section documenting apply button text, selectors, URL patterns, and known gotchas
- **Platform-specific code goes in `src/automation/platforms/`** - when a job board needs custom modal handling, login flows, or unique form structures, create a dedicated module (e.g., `platforms/smartrecruiters.py`). Generic behavior stays in `detection.py` and `forms.py`
- **Reusable browser JS goes in `src/automation/browser_scripts/`** - when DOM logic inside `page.evaluate(...)` or `frame.evaluate(...)` becomes non-trivial or reused, move it into a dedicated `.js` asset under `browser_scripts/`. Keep tiny one-line evaluate calls inline only when they are truly local.
- **Do not create one giant master JS file** - keep browser scripts split by concern (`forms/`, `detection/`, `linkedin/`, `captcha/`, etc.). Each script should expose one function expression compatible with Playwright `evaluate(script, args)`.
- **Pass dynamic values through evaluate args, not string interpolation** - this keeps browser scripts reusable and avoids brittle injected JS strings, especially for tokens, selectors, and text values.
- **Keep automation facade modules thin** - public modules like `forms.py`, `handlers.py`, `handlers_account.py`, `vision_agent.py`, and platform entry modules should stay import-stable facades while implementation moves into responsibility-based subpackages.
- **New ATS button texts go in `src/automation/selectors.py`** - keep apply/next/submit text lists centralized there so both Python logic and browser scripts can share the same canonical button vocabulary. Document new platform-specific button text in LEARNINGS.md
- **Always add debug screenshots on automation failures** - when adding or modifying automation code that can fail (CAPTCHA unsolved, button not found, form not submitted, etc.), save a debug screenshot to `data/logs/` with a descriptive name (e.g., `debug_captcha_unsolved.png`, `debug_no_apply_button.png`). Screenshots are essential for diagnosing headless browser issues
- **Read the "Clicks" section of LEARNINGS.md before changing automation code** - this section logs click/navigation failures where a button is clicked but the page doesn't transition (Apply doesn't open form, Submit doesn't submit, invisible CAPTCHA gates, etc.). Use it as context for any automation change. When a new click failure is discovered, add it with platform, URL pattern, symptom, and root cause
- **Handler functions return StepResult, never advance workflow state** - only the kernel's transition table decides the next state. Handlers are stateless workers
- **New element intents go in selector_cache bootstrap, not hardcoded in handlers** - add new intents to `SELECTOR_INTENTS` in `selectors.py` so the cache can learn them
- **Playwright-first, LLM-second automation** - prefer stable Playwright selectors, DOM extraction, native input filling, stored sessions, and reusable cookies first. Use the LLM or vision agent only when deterministic browser automation is insufficient.
- **DOM-First, Vision-Last philosophy** — DOM pre-fill handles deterministic fields (name, email, phone, address, education, languages, consent checkboxes, file uploads). Vision agent handles only unpredictable free-text fields. When adding ATS support: inspect DOM via `--debug` mode first, write a platform module if custom widgets exist, then vision cleans up the rest. Never rely on vision to brute-force fields that DOM can fill.
- **Platform modules handle non-standard DOM** — when a major ATS uses custom dropdown/widget components that don't match standard patterns (e.g. Avature's click-to-open divs, Workday shadow DOM), create `platforms/<ats>.py` with a `prefill(page, profile, settings)` function. Register it in `platforms/__init__.py:get_platform_prefill()`. Do NOT add ATS-specific hacks to the generic `extract_form_fields`.
- **Platform-owned page flow stays in platform packages** — if an ATS needs deterministic multi-step page handling inside the vision path, expose it from `platforms/__init__.py` as a platform hook. Do NOT embed ATS-specific page-state logic directly inside generic `vision_agent.py`.
- **Debug/inspect mode** — `python -m src apply-job <id> --debug` pauses after DOM pre-fill and after each vision round. Saves per-pause screenshots to `data/logs/`. Use when developing new platform modules — inspect the live browser to understand component structure before writing targeted DOM code.
- **Account registry seeding** — When an ATS account exists but wasn't auto-generated (e.g., manually created on the site or password lost), use `python -m src set-account <domain> <email> <password>` to store the credentials encrypted. The system uses these during the "existing record" → login flow. Keep `use_email_aliases: false` to avoid Avature duplicate-email errors from Gmail + aliases.

## User Context
- Kai is a Data Engineer at Intuitive Surgical (current employer - excluded from job search)
- BS Mechanical Engineering from UCLA, Minor in Data Science Engineering
- Looking for: data engineer, data science, software engineer roles (entry to senior, fulltime)
- Target locations: San Francisco, Seattle, New York, Chicago (+ remote)
- Has OpenAI API key configured in .env

## Execution Flow

### Pipeline: `python -m src pipeline`
1. **Scrape** — Pull jobs from Indeed/LinkedIn/ZipRecruiter/Google via JobSpy. Insert with status `new`.
2. **Seed Answers** — Populate answer bank from profile.yaml (name, email, work auth, salary, etc.). Source=`profile` entries auto-refresh; source=`user` entries are never overwritten.
3. **Tailor** — For each `new` job: generate tailored resume + cover letter via OpenAI. Status: `new` → `tailored`. Skips jobs on `domain_blocklist`.
4. **Apply** — For each `tailored` job: open browser, navigate to ATS, fill form, submit. Status: `tailored` → `applied` / `failed` / `needs_login`.
5. **Login recovery** (manual) — `python -m src login-sites` opens browser for manual login on sites that blocked. Saves cookies, resets jobs for retry.

### Job Status Flow
```
new → tailoring → tailored → applying → applied (success)
                                      → failed (generic failure)
                                      → failed_captcha (CAPTCHA unsolvable)
                                      → failed_listing (stuck on listing page)
                                      → failed_error (server error / access denied)
                                      → needs_login (login wall)
                                      → skipped (no URL / other)
```

### Kernel State Machine
Each job application runs through the `ApplicationKernel` state machine:
```
SETUP → NAVIGATE → ROUTE → DETECT_STRATEGY → FILL_SELECTOR or FILL_VISION → VERIFY → CLEANUP → COMPLETE
```
Side states: `SOLVE_CAPTCHA` (with pre-CAPTCHA state resume), `RECOVER_LOGIN`, `VERIFY_EMAIL`.

### Two Apply Strategies
- **LinkedIn Easy Apply** (FILL_SELECTOR) — Selector-based: extract form fields from modal DOM (shadow DOM via Playwright locators), LLM infers answers, fill via Playwright, click Next/Submit in multi-step loop.
- **External ATS** (FILL_VISION) (Greenhouse, Workday, Ashby, etc.) — Vision agent: DOM pre-fill first (Playwright `fill()` for React compatibility), then screenshot → GPT-4o returns batch actions → execute all → repeat 3-5 rounds.

## File Interaction Map

### Config → Core → Automation
```
config/profile.yaml ──→ src/config/loader.py ──→ src/core/tailoring.py (profile summary for LLM)
config/settings.yaml ─→ src/config/loader.py ──→ all modules (settings dict)
.env ─────────────────→ src/core/tailoring.py (OPENAI_API_KEY)
                       → src/automation/vision_agent.py (OPENAI_API_KEY)
                       → src/automation/captcha_solver.py (CAPTCHA_API_KEY)
                       → src/automation/email_poller.py (EMAIL_USER, EMAIL_APP_PASSWORD)
```

### CLI → Core → Automation
```
src/cli.py
 ├─ cmd_scrape() ──→ src/core/scraper.py ──→ src/db.py (insert jobs)
 ├─ cmd_tailor() ──→ src/core/tailoring.py ──→ src/core/document.py (DOCX/PDF)
 └─ cmd_apply() ───→ src/automation/applicant.py (batch orchestration)
                      → src/automation/kernel.py (single-job state machine)
                        ├─ handlers.py (stateless workers per state)
                        ├─ element_finder.py (6-level element discovery)
                        ├─ selector_cache.py (adaptive selector memory)
                        ├─ page_checks.py (CAPTCHA/login/dead page detection, login recovery)
                        ├─ detection.py (button clicking, modal dismiss)
                        ├─ forms.py (field extraction + filling, unified API)
                        ├─ vision_agent.py (external ATS via GPT-4o screenshots)
                        ├─ captcha_solver.py (2Captcha API)
                        ├─ email_poller.py (IMAP OTP/verification polling)
                        └─ platforms/linkedin.py (Easy Apply modal, share profile, SDUI)
```

### Data Flow
```
Scraper → jobs table (status=new)
Tailoring → applications/attempts/ dirs + jobs table (status=tailored)
Automation → applications/success/ or failed/ dirs + applications table + answer_bank table
Element Finder → selector_cache table (domain + intent → selector, confidence)
Cookies → data/linkedin_auth.json + data/site_auth/{domain}.json
```

## Running
- VS Code launch configs in `.vscode/launch.json` (F5 to run)
- Or: `venv\Scripts\python -m src pipeline`
- Daily automation: Windows Task Scheduler runs `run_pipeline.bat` at 09:00

## References
- See `ARCHITECTURE.md` for the full directory guide, data flow, and import graph
- See `TODO.md` for pending features and improvements
- See `LEARNINGS.md` for platform-specific automation quirks and debugging lessons
