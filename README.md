# JobHunter

JobHunter is a self-sufficient resume applier. The target system is a reliable `scrape -> tailor -> apply` pipeline that uses Playwright-first browser automation, persisted sessions/cookies where possible, and LLM help only when deterministic automation is not enough.

## Startup

The repeated VS Code flow currently used is:

1. `JobHunter: Pipeline (Refresh Profile from profile.yaml)`
2. `JobHunter: Login`
3. `JobHunter: Apply Only`

This is useful when you want to refresh the answer bank and scrape/tailor first, then repeatedly run apply with refreshed site sessions.

## Current Site Status

- LinkedIn is the primary supported board today.
- Indeed login can work, but the automated apply path is still unreliable because of Cloudflare challenges.
- ZipRecruiter is currently treated as unsupported in the automated pipeline.
- Google Careers is currently treated as unsupported in the automated pipeline and is blacklisted in `config/domain_blacklist.txt`.

## What It Does

1. Scrapes jobs from configured boards with JobSpy.
2. Tailors resume and cover letter documents with OpenAI.
3. Applies with Playwright automation using DOM/selectors first and vision fallback second.
4. Tracks job/application state in SQLite.
5. Saves screenshots and logs for debugging failed flows.

## Quick Start

### 1. Install

```bash
cd JobHunter
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure

```bash
copy config\profile.example.yaml config\profile.yaml
copy config\settings.example.yaml config\settings.yaml
```

Edit:

| File | Purpose |
|------|---------|
| `.env` | API keys and optional email/registry secrets |
| `config/profile.yaml` | Personal info, work history, skills, links, preferences |
| `config/settings.yaml` | Search settings, automation settings, caps, supported sites |
| `config/domain_blacklist.txt` | Domains or URL fragments to skip during scraping |

### 3. Add Base Documents

Place your base documents here:

- `templates/base_resume.docx`
- `templates/base_cover_letter.docx`

### 4. Run

```bash
python -m src pipeline
python -m src scrape
python -m src tailor
python -m src apply
python -m src login
python -m src login-sites
python -m src status
python -m src list
```

## VS Code Launch Configs

Primary launch configs in `.vscode/launch.json`:

- `JobHunter: Pipeline (Refresh Profile from profile.yaml)`
- `JobHunter: Login`
- `JobHunter: Apply Only`
- `JobHunter: Full Pipeline (scrape -> tailor -> apply)`
- `JobHunter: Scrape Only`
- `JobHunter: Tailor Only`
- `JobHunter: Login Sites (Retry)`
- `JobHunter: Apply Job (by ID)`
- `JobHunter: Apply Job --debug (pause after DOM fill, each vision round)`

Practical repeated workflow:

1. Run `JobHunter: Pipeline (Refresh Profile from profile.yaml)` to refresh the answer bank and prepare jobs.
2. Run `JobHunter: Login` to refresh LinkedIn and Indeed auth.
3. Run `JobHunter: Apply Only` as needed.

## Login and Session Notes

- `python -m src login` currently targets LinkedIn and Indeed.
- LinkedIn session state is stored in `data/linkedin_auth.json`.
- Other site cookies are stored in `data/site_auth/{domain}.json`.
- Google Careers is not part of the default login flow.
- `python -m src login-sites` refreshes default logins and then walks blocked `needs_login` domains one at a time.

## Domain Blacklist

`config/domain_blacklist.txt` is the plain-text control point for blocked sites.

Rules:

- One entry per line.
- `domain.com` blocks that hostname and subdomains.
- `domain.com/path` blocks URLs containing that fragment.
- Comment out a line with `#` or delete it to re-enable that site.

Examples:

```txt
indeed.com
ziprecruiter.com
google.com/about/careers
```

## Architecture Summary

High-level flow:

1. Scrape inserts `new` jobs into SQLite.
2. Tailor generates resume/cover letter outputs and moves jobs to `tailored`.
3. Apply uses the automation kernel to drive each job through explicit states.

Important architecture rules:

- Playwright first, LLM second.
- DOM-first, vision-last for application filling.
- Reusable browser-side DOM logic lives in `src/automation/browser_scripts/`.
- Load specific JS assets into `page.evaluate(...)`; do not create one giant master JS file.
- Prefer same-name packages for large automation surfaces; keep the public import paths stable while splitting internals into smaller modules.
- Platform-specific page-state handling belongs behind platform hooks in `src/automation/platforms/`.
- Handlers return structured `StepResult` values.
- The kernel owns state transitions.
- Selector cache and element finder reduce hardcoded selector fragility.
- Email polling and account registry support gated ATS flows.

See [notes/ARCHITECTURE.md](C:\Users\kaina\OneDrive\Documents\JobHunter\notes\ARCHITECTURE.md) for the full architecture guide.

## Project Structure

```text
src/
  cli.py                  CLI orchestration and commands
  db.py                   SQLite schema and queries
  utils.py                Shared paths and helpers
  config/
    loader.py             YAML loading, blacklist loading, profile summary
    models.py             Pydantic config validation
  core/
    scraper.py            JobSpy scraping and filtering
    tailoring.py          OpenAI tailoring and answer inference
    document.py           DOCX/PDF generation
  automation/
    applicant.py          Batch orchestration
    browser_scripts/      Browser-side JS assets for page/frame.evaluate()
    kernel.py             Explicit application state machine
    handlers/             Public handler package with setup/navigation/fill/verify modules
    handlers_account/     Public ATS auth/registration package
    results.py            HandlerResult and StepResult
    detection.py          Apply/login/CAPTCHA detection
    page_checks.py        Blocker checks and login recovery
    forms/                Public form package with DOM, Playwright, select, and upload helpers
    element_finder.py     Escalating element lookup
    selector_cache.py     Adaptive selector memory
    selectors.py          Intent bootstrap and selector constants
    vision_agent/         Public vision-agent package with client, loop, action, OTP, and submission modules
    captcha_solver.py     2Captcha integration
    email_poller.py       IMAP OTP handling
    account_registry.py   Encrypted ATS account store
    platforms/
      linkedin/           Public LinkedIn automation package
      avature/            Public Avature automation package
config/
  profile.example.yaml
  settings.example.yaml
  domain_blacklist.txt
notes/
  ARCHITECTURE.md
  phases/
```

## Safety and Operational Notes

- The system is designed to never fabricate resume experience.
- Daily caps exist to reduce account flagging risk.
- Some sites will still require manual intervention.
- Debug screenshots in `data/logs/` are part of the normal debugging workflow.
- Personal config files, runtime data, and application artifacts are gitignored.

## References

- [CLAUDE.md](C:\Users\kaina\OneDrive\Documents\JobHunter\CLAUDE.md)
- [LEARNINGS.md](C:\Users\kaina\OneDrive\Documents\JobHunter\LEARNINGS.md)
- [notes/ARCHITECTURE.md](C:\Users\kaina\OneDrive\Documents\JobHunter\notes\ARCHITECTURE.md)
- [TODO.md](C:\Users\kaina\OneDrive\Documents\JobHunter\TODO.md)
