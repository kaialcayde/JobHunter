# JobHunter

Automated job application system that scrapes listings, tailors your resume and cover letter with AI, and submits applications.

## What It Does

1. **Scrapes** job listings from Indeed, LinkedIn, Glassdoor, ZipRecruiter, and Google using [JobSpy](https://github.com/speedyapply/JobSpy)
2. **Tailors** your resume and cover letter for each job using OpenAI GPT-4o -- never fabricates experience
3. **Applies** automatically via browser automation (Playwright) -- fills forms, uploads docs, submits
4. **Tracks** everything in a SQLite database with screenshots as proof
5. **Runs daily** via Windows Task Scheduler -- hands-free job applications

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

Copy the example configs and fill in your details:

```bash
copy config\profile.example.yaml config\profile.yaml
copy config\settings.example.yaml config\settings.yaml
```

Edit these files:

| File | What to fill in |
|------|----------------|
| `.env` | Your OpenAI API key: `OPENAI_API_KEY=sk-...` |
| `config/profile.yaml` | Name, email, phone, education, skills, links, salary preferences |
| `config/settings.yaml` | Job roles, locations, sites, filters, daily application cap |

### 3. Add Your Resume and Cover Letter

Export your resume and cover letter from Google Docs as `.docx` files and place them at:

- `templates/base_resume.docx`
- `templates/base_cover_letter.docx`

### 4. Run

```bash
# Full pipeline: scrape -> tailor -> apply
python -m src pipeline

# Or run steps individually
python -m src scrape     # Scrape job listings
python -m src tailor     # Generate tailored docs
python -m src apply      # Submit applications
python -m src status     # View stats
python -m src list       # List all jobs
python -m src list new   # Filter by status
```

### 5. Daily Automation (Optional)

Set up a Windows scheduled task to run the pipeline daily at 9 AM:

```
schtasks /create /tn "JobHunter Daily Pipeline" /tr "C:\path\to\JobHunter\run_pipeline.bat" /sc daily /st 09:00
```

Manage the task:
- Disable: `schtasks /change /tn "JobHunter Daily Pipeline" /disable`
- Delete: `schtasks /delete /tn "JobHunter Daily Pipeline" /f`

## Project Structure

```
JobHunter/
├── src/                         # Application source code
│   ├── __init__.py              # Package marker
│   ├── __main__.py              # Entry point (python -m src)
│   ├── cli.py                   # CLI commands & pipeline orchestration
│   ├── db.py                    # SQLite database layer (jobs, applications, logs)
│   ├── utils.py                 # Path constants, directory helpers
│   ├── config/                  # Configuration loading & validation
│   │   ├── __init__.py          # Re-exports (load_profile, load_settings, etc.)
│   │   ├── models.py            # Pydantic models for profile.yaml & settings.yaml
│   │   └── loader.py            # YAML loading, validation, profile summary
│   ├── core/                    # Core business logic
│   │   ├── __init__.py
│   │   ├── scraper.py           # JobSpy multi-board scraping, dedup, filtering
│   │   ├── tailoring.py         # OpenAI resume/cover letter tailoring (anti-fabrication)
│   │   └── document.py          # DOCX/PDF generation, one-page resume enforcement
│   └── automation/              # Browser automation
│       ├── __init__.py          # Re-exports apply_to_jobs
│       ├── applicant.py         # Application orchestration, round-robin, batching
│       ├── detection.py         # CAPTCHA/login detection, button clicking (Apply/Next/Submit)
│       └── forms.py             # Form field extraction, LLM-inferred filling, file uploads
├── config/                      # User configuration
│   ├── profile.example.yaml     # Template -- personal info
│   ├── settings.example.yaml    # Template -- search params & automation settings
│   ├── profile.yaml             # Your personal info (gitignored)
│   └── settings.yaml            # Your search settings (gitignored)
├── templates/                   # Resume & cover letter templates (gitignored)
│   ├── base_resume.docx         # Your base resume
│   └── base_cover_letter.docx   # Your base cover letter
├── applications/                # Generated output per job (gitignored)
│   └── {Company}/{Position}/
│       ├── resume.docx / .pdf
│       ├── cover_letter.docx / .pdf
│       ├── application.json
│       ├── pre_submit_screenshot.png
│       └── confirmation_screenshot.png
├── data/                        # Runtime data (gitignored)
│   ├── jobhunter.db             # SQLite database
│   └── logs/                    # Daily pipeline logs
├── .env                         # API keys (gitignored)
├── run_pipeline.bat             # Batch script for Windows Task Scheduler
├── requirements.txt             # Python dependencies
├── CLAUDE.md                    # AI assistant context
└── TODO.md                      # Planned features & improvements
```

## How It Works

### Pipeline Flow

```
scrape ──> tailor ──> apply
  │           │          │
  │           │          ├── Navigate to job URL
  │           │          ├── Detect CAPTCHA / login walls
  │           │          ├── Click Apply button
  │           │          ├── Extract form fields (DOM inspection)
  │           │          ├── Infer answers (LLM + profile data)
  │           │          ├── Fill form & upload documents
  │           │          ├── Screenshot before submit
  │           │          ├── Submit application
  │           │          └── Screenshot confirmation
  │           │
  │           ├── Load base resume/cover letter from templates/
  │           ├── Call OpenAI to tailor for each job
  │           └── Generate DOCX + PDF to applications/{Company}/{Position}/
  │
  ├── Search across role × location × site combos (parallel)
  ├── Filter by keywords, company exclusions, min salary
  ├── Deduplicate by URL hash
  └── Insert new jobs into SQLite with status "new"
```

### Job Status Lifecycle

```
new ──> tailoring ──> tailored ──> applying ──> applied
 │         │                          │
 └─────────┴──────────────────────────┴──> failed
                                      └──> failed_captcha
                                      └──> skipped (login wall / no URL)
```

### Module Responsibilities

| Package | Purpose |
|---------|---------|
| `src/config/` | Load and validate YAML configs via Pydantic. All config access goes through `load_profile()` / `load_settings()`. |
| `src/core/` | Business logic with no browser dependency. Scraping, AI tailoring, and document generation. |
| `src/automation/` | Playwright browser automation. Split into detection (what's on the page), forms (extracting and filling fields), and applicant (orchestrating the full apply flow). |
| `src/db.py` | Single SQLite connection with WAL mode. Tracks jobs, applications, audit log, and scrape cache. |
| `src/cli.py` | Parses CLI commands, wires together core + automation, handles logging and progress output. |

## Safety

- **No fabrication**: The LLM is hardcoded to never invent skills, experience, or credentials. It only reorders and rewords what is in your actual resume.
- **Daily cap**: Default 25 applications/day (configurable in settings.yaml) to avoid account flags.
- **Screenshots**: Saves screenshots before submission and after confirmation as an audit trail.
- **Deduplication**: Never applies to the same job twice (tracked in database).
- **Privacy**: All data stays local. Only outbound calls are to OpenAI (for tailoring) and job application websites.

## Dependencies

- Python >= 3.10
- [JobSpy](https://github.com/speedyapply/JobSpy) -- multi-board job scraping
- [OpenAI Python SDK](https://github.com/openai/openai-python) -- LLM API
- [Playwright](https://playwright.dev/python/) -- browser automation
- [python-docx](https://python-docx.readthedocs.io/) -- DOCX generation
- [fpdf2](https://py-pdf.github.io/fpdf2/) -- PDF generation
- [Rich](https://rich.readthedocs.io/) -- terminal output formatting
- [Pydantic](https://docs.pydantic.dev/) -- config validation

## License

Personal use.
