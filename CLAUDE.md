# JobHunter - Claude Context

## What This Project Is
Automated job application system for the owner (Kai Alcayde). Scrapes job listings, tailors resume/cover letter with OpenAI GPT-4o, and submits applications via Playwright browser automation.

## Architecture
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
- `src/db.py` - SQLite schema (jobs, applications, application_log, scrape_cache tables)
- `src/utils.py` - Path constants, directory helpers, filename sanitization
- `src/config/` - Configuration loading and validation
  - `models.py` - Pydantic models validating profile.yaml and settings.yaml
  - `loader.py` - YAML loading through Pydantic validation, profile summary generation
- `src/core/` - Core business logic
  - `scraper.py` - JobSpy wrapper, multi-board search, dedup, filtering
  - `tailoring.py` - OpenAI integration, hardcoded anti-fabrication safeguard in SYSTEM_PROMPT
  - `document.py` - DOCX/PDF generation, one-page resume enforcement
- `src/automation/` - Browser automation
  - `applicant.py` - Application orchestration, round-robin distribution, batch processing
  - `detection.py` - CAPTCHA/login detection, modal dismissal, Apply/Next/Submit button clicking
  - `forms.py` - Form field extraction via DOM inspection, LLM-inferred filling, file uploads

## Important Conventions
- **Never fabricate resume content** - the SYSTEM_PROMPT in tailoring.py is hardcoded and must not be weakened
- **Resume must fit one page** - enforced in both LLM prompt and DOCX formatting (10.5pt, tight margins)
- **Windows environment** - use ASCII characters in Rich output (no unicode arrows/box drawing), console uses `force_terminal=True`
- **Salary values in YAML** - must be plain integers, no commas (e.g., `150000` not `150,000`)
- **Config changes go through Pydantic** - `load_profile()` and `load_settings()` validate via models before returning dicts
- **Personal files are gitignored** - .env, profile.yaml, settings.yaml, templates/, applications/, data/

## User Context
- Kai is a Data Engineer at Intuitive Surgical (current employer - excluded from job search)
- BS Mechanical Engineering from UCLA, Minor in Data Science Engineering
- Looking for: data engineer, data science, software engineer roles (entry to senior, fulltime)
- Target locations: San Francisco, Seattle, New York, Chicago (+ remote)
- Has OpenAI API key configured in .env

## Running
- VS Code launch configs in `.vscode/launch.json` (F5 to run)
- Or: `venv\Scripts\python -m src pipeline`
- Daily automation: Windows Task Scheduler runs `run_pipeline.bat` at 09:00

## References
- See `ARCHITECTURE.md` for the full directory guide, data flow, and import graph
- See `TODO.md` for pending features and improvements
