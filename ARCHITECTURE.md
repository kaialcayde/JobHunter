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
    ├── applicant.py             # Orchestrates the full apply flow: batching, parallelism, retries
    ├── detection.py             # Page analysis: CAPTCHA, login walls, modal dismissal, button finding
    └── forms.py                 # DOM field extraction, form filling, file upload handling
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
                            ┌──────────────┐
                            │ applicant.py  │──> Browser (Playwright)
                            │ detection.py  │    screenshots, form submission
                            │ forms.py      │──> SQLite (applications table)
                            └──────────────┘
```

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

- Split into three files by responsibility:
  - **`detection.py`** -- Reads the page: is there a CAPTCHA? A login wall? Where's the Apply button?
  - **`forms.py`** -- Interacts with forms: extract fields from DOM, fill them, upload files
  - **`applicant.py`** -- Orchestrates: which jobs to apply to, in what order, how many browsers
- `applicant.py` is the only file that imports from both `detection.py` and `forms.py`
- Each function takes a Playwright `page` object -- no global browser state

### `db.py` -- Database

- Single module, not a package -- the schema is simple enough
- WAL journal mode for concurrent read/write safety
- Safe column migration via ALTER TABLE with error suppression
- All queries return `dict` (via `sqlite3.Row`) for easy access

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
├── core.tailoring (infer_form_answers)
├── core.document  (save_application_metadata)
├── automation.detection
└── automation.forms

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
