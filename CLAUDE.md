# JobHunter - Claude Context

## What This Project Is
Automated job application system for the owner (Kai Alcayde). Scrapes job listings, tailors resume/cover letter with OpenAI GPT-4o, and submits applications via Playwright browser automation.

## Architecture
- **Python 3.12** project, venv at `venv/`
- **CLI entry point**: `python -m src.main <command>` (pipeline, scrape, tailor, apply, status, list)
- **Config**: `config/profile.yaml` (personal info), `config/settings.yaml` (search params), `.env` (API keys)
- **Templates**: `templates/base_resume.docx`, `templates/base_cover_letter.docx`
- **Output**: `applications/{Company}/{Position}/` with tailored DOCX/PDF + metadata JSON
- **Database**: SQLite at `data/jobhunter.db`
- **Logs**: `data/logs/pipeline_YYYY-MM-DD.log`
- **Scheduler**: `run_pipeline.bat` via Windows Task Scheduler

## Key Modules
- `src/scraper.py` - JobSpy wrapper, multi-board search, dedup, filtering
- `src/tailoring.py` - OpenAI integration, hardcoded anti-fabrication safeguard in SYSTEM_PROMPT
- `src/document.py` - DOCX/PDF generation, one-page resume enforcement
- `src/applicant.py` - Playwright form filling, ATS detection, LLM-driven field inference
- `src/database.py` - SQLite schema (jobs, applications, application_log tables)
- `src/models.py` - Pydantic models validating profile.yaml and settings.yaml
- `src/profile.py` - Config loading through Pydantic validation
- `src/main.py` - CLI orchestrator, pipeline flow

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
- Or: `venv\Scripts\python -m src.main pipeline`
- Daily automation: Windows Task Scheduler runs `run_pipeline.bat` at 09:00

## TODO
See `TODO.md` for pending features and improvements.
