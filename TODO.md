# JobHunter TODO

## High Priority

- [ ] need to remove legacy methods in settings.yaml

- [ ] confirm what secrets are stored and what passwords, etc my agent has access to

- [ ] remove all debug, retry shouldn't need this in production much later on

Run 5 debug cycles of `venv/bin/python -m src apply` (max_applications_per_round is already 1 in settings.yaml). For each cycle:

1. Run apply, wait for it to finish (timeout 5min)
2. Read LEARNINGS.md before diagnosing (required by CLAUDE.md)
3. Check debug screenshots in data/logs/ if the run fails
4. Diagnose the root cause from output + screenshots
5. Fix the code
6. If a job was marked failed/failed_captcha/skipped, reset it to 'new' so the next run has a job to try
7. Update LEARNINGS.md with any new platform quirks discovered after a run. Update after every run

Key lessons from past cycles:
- LinkedIn "Share your profile?" modal: the Apply `<a>` tag's href navigates to the same page, destroying the modal. Must preventDefault before clicking so the JS handler can show the modal. Also the modal doesn't always use `[role="dialog"]` — use broad selectors.
- LinkedIn redirect URLs (`linkedin.com/redir/redirect/?url=...`): treat as external, not internal LinkedIn URLs.
- Ashby/Greenhouse invisible reCAPTCHA: detect_captcha false positives when form is already loaded. Skip passive CAPTCHA indicators (badge, scripts) when 2+ visible form inputs exist.
- Greenhouse form below fold: scroll to first form input before vision agent handoff, otherwise vision agent sees job description and clicks "Apply" repeatedly.
- Vision agent "done" but not submitted: try DOM-based click_submit_button() before falling back to vision coordinate clicks for the final submit.

- [ ] Fill in work_experience and skills in config/profile.yaml
- [ ] ability to apply to big and small companies
- [ ] confirm resume and cover letter are being tailored per company and role
- [ ] need to find a way t0 get around captcha
- [ ] Option to view jobs we scraped and determine which ones to remove, etc
- [ ] need to have a remove db and applications pipeline
- [ ] move folder stuff to a database?
- [ ] once this works separate scraping and apply parallelization. apply can be parallelized i think
- [ ] Replace terminal OTP/verification prompts with browser popup or find a way to automate (e.g. OpenClaw integration)
- [ ] takes too long for a single application
- [ ] captcha enterprise greenhouse canceled for now not working

## Phase 6: ATS Account Creation

- [ ] Auto-create accounts on ATS platforms (Workday, Greenhouse, iCIMS) before applying
- [ ] Identity management: store credentials per ATS domain in encrypted local store
- [ ] Handle email verification during account creation (email_poller integration)
- [ ] Detect "create account" gates and route to account creation flow before application

## Features

- [ ] Add email-based application support (some jobs accept resume via email) (Much later do not do right now)
- [ ] Add job relevance scoring with LLM before tailoring (skip bad matches early) (much later do not do right now)
- [ ] Add interactive mode for reviewing applications before submit and toggle on and off
- [ ] Add a web dashboard for viewing job status and tailored docs
- [ ] think about DB and how that would look like for multiple people (much later do this later)
- [ ] have option to auto make profile based on input docx (Much later do this later)
- [ ] DO MUCH LATER if i make this paid determine where costs are coming from and price baesd on that
- [ ] have imap to be able to paste in otp
    email timestamp filtering, HTML body parsing fallback, multiple OTP patterns, timeout handling, and retry logic. That's what makes it stable enough to run unattended. Polling

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

## Future (Post Phase 6)

- [ ] Gmail API upgrade (replace IMAP polling with Gmail API for better reliability and OAuth)
- [ ] Split forms.py into smaller modules (extraction, filling, react-select, file upload)
- [ ] ElementFinder levels 5-6: text LLM and vision LLM fallback for element discovery

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
- [x] How to fix issue with otp - added manual_otp setting, prompts in terminal for verification codes
- [x] Automation kernel refactor (Phases 1-4): kernel.py state machine, handlers.py, results.py, element_finder.py, selector_cache.py, email_poller.py
- [x] Remove flow.py — all callers migrated to ApplicationKernel.run()
