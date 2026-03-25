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
python -m src.main pipeline

# Or run steps individually
python -m src.main scrape     # Scrape job listings
python -m src.main tailor     # Generate tailored docs
python -m src.main apply      # Submit applications
python -m src.main status     # View stats
python -m src.main list       # List all jobs
python -m src.main list new   # Filter by status
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
  config/
    profile.yaml           # Your personal info (gitignored)
    settings.yaml          # Search settings (gitignored)
    profile.example.yaml   # Template for new users
    settings.example.yaml  # Template for new users
  templates/
    base_resume.docx       # Your base resume (gitignored)
    base_cover_letter.docx # Your base cover letter (gitignored)
  applications/            # Tailored docs per company/position (gitignored)
    {Company}/
      {Position}/
        resume.docx / .pdf
        cover_letter.docx / .pdf
        application.json
  src/
    main.py                # CLI entry point
    scraper.py             # JobSpy integration
    database.py            # SQLite data layer
    tailoring.py           # OpenAI resume/cover letter engine
    document.py            # DOCX/PDF generation
    applicant.py           # Playwright form filling
    profile.py             # Profile loader
    utils.py               # Shared utilities
  data/
    jobhunter.db           # SQLite database (gitignored)
    logs/                  # Daily pipeline logs (gitignored)
  .env                     # API keys (gitignored)
  run_pipeline.bat         # Batch script for scheduled task
  requirements.txt         # Python dependencies
```

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
- [Rich](https://rich.readthedocs.io/) -- terminal output formatting

## License

Personal use.
