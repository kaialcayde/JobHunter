# JobHunter TODO

## High Priority

- [ ] Fill in work_experience and skills in config/profile.yaml
- [ ] ability to apply to big and small companies
- [ ] confirm resume and cover letter are being tailored per company and role
- [ ] need to find a way t0 get around captcha
- [ ] Option to view jobs we scraped and determine which ones to remove, etc
- [ ] need to have a remove db and applications pipeline
- [ ] move folder stuff to a database?
- [ ] refactor and make everything clean once were done prototyping
- [ ] once this works separate scraping and apply parallelization. apply can be parallelized i think
- [ ] how to fix issue with otp - just skip? Right now just giving up if get otp
- [ ] takes too long for a single application
## Features

- [ ] Add email-based application support (some jobs accept resume via email) (Much later do not do right now)
- [ ] Add job relevance scoring with LLM before tailoring (skip bad matches early) (much later do not do right now)
- [ ] Add interactive mode for reviewing applications before submit and toggle on and off
- [ ] Add a web dashboard for viewing job status and tailored docs
- [ ] think about DB and how that would look like for multiple people (much later do this later)
- [ ] have option to auto make profile based on input docx (Much later do this later)
- [ ] DO MUCH LATER if i make this paid determine where costs are coming from and price baesd on that 

## Improvements

- [ ] Add proxy support for scraping (especially LinkedIn)
- [ ] Add support for multiple resume templates (e.g., one for data roles, one for SWE) (Do this much later)
- [ ] Improve ATS detection with more patterns (Workday, Taleo, iCIMS)
- [ ] Add cover letter tone/style configuration (Do this much later)
- [ ] Add unit tests for tailoring and document generation
- [ ] Add resume targeting - fix prompt to target certain job types (like we want eky words for types of jobs, for example) (Do this much later)
- [ ] for cover letter, need to be able to tweak so it knows my current role and crafts the letter in accordance to what I see is important with the company I am applying to (Do this much later)
- [ ] need to have opus reorganize and make this production ready as code is unorganized (Do this much later)
- [ ] keep user resume template? I don't know might be more complicated, as for example when i refactored mine with claude it made it in that easily readable AI format (Do this much later)

## Done

- [x] Project scaffolding and config files
- [x] SQLite database with job/application tracking
- [x] JobSpy scraper integration
- [x] OpenAI resume/cover letter tailoring with anti-fabrication safeguard
- [x] DOCX/PDF document generation (one-page resume enforcement)
- [x] Playwright browser automation for form filling
- [x] CLI with pipeline, scrape, tailor, apply, status, list commands
- [x] Windows Task Scheduler daily automation
- [x] .gitignore for safe sharing
- [x] Pydantic models for profile.yaml and settings.yaml validation (src/models.py)
- [x] Base resume and cover letter added to templates/
- [x] Profile populated from resume (education, work experience, skills)
- [x] Default resume fallback when tailoring disabled (uses base templates)
- [x] LinkedIn "Share your profile" modal handling (accepts it)
- [x] LinkedIn Easy Apply support with modal-aware selectors
- [x] Scrape cache removed -- always re-scrapes, dedup at job level via url_hash
- [x] Failed job retry (`python -m src retry`) and delete (`python -m src delete-failed`) CLI commands
- [x] API debug logging for form filling (token counts, timing, field details)
- [x] Jobs ordered by date_posted (newest postings first)
- [x] Strict title matching filter (`strict_title_match: true` in settings.yaml filters)
- [x] LinkedIn-specific workarounds organized in src/automation/platforms/linkedin.py
- [x] OpenAI API retry with exponential backoff + proper logging
