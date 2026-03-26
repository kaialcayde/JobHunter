# JobHunter TODO

## High Priority

- [ ] Fill in work_experience and skills in config/profile.yaml
- [ ] parallelize job scraping and searching and application? Seems like it's pretty serial right now
- [ ] ability to apply to big and small companies
- [ ] confirm resume and cover letter are being tailored per company / role
- [ ] ability to turn resume and cover letter tailoring on and off
- [ ] confirm that with cached searches if new items come up like in 
    Skipping (cached): software engineer in New York, NY
    Skipping (cached): software engineer in Chicago, IL
    All searches cached. Nothing to scrape.
    That we continue to search new instances of thsoe jobs and the city and specific search itself isn't cached, just the results

    Note: 
    Caching is fine — it only caches when a role+location combo was last scraped (timestamp), not the results themselves. After cache_hours (12h default) expires, it re-scrapes that same role+location and gets fresh results. Individual jobs are deduped by URL hash, so new postings get added while duplicates are skipped.
    Good — caching confirmed working correctly: The cache only tracks "when did we last run this search?" (role+location+timestamp). It does NOT cache the job results. Every re-scrape after cache expiry fetches fresh listings from the job boards, and insert_job deduplicates by URL hash so only genuinely new postings get added.

    - [ ] need to confirm that I can tweak cache_hours
- [ ] need to find a way t oget around captcha
- [ ] still tailoring cover letters etc when tailor sett o false
    (1/5) Software Engineer III at Abbott
      Tailoring resume...
    2026-03-25 17:16:30,600 [INFO] HTTP Request: POST https://api.openai.com/v1/chat/completions "HTTP/1.1 200 OK"
      Resume saved: C:\Users\kaina\OneDrive\Documents\JobHunter\applications\Abbott\Software_Engineer_III\resume.docx + 
    C:\Users\kaina\OneDrive\Documents\JobHunter\applications\Abbott\Software_Engineer_III\resume.pdf
      Tailoring cover letter...
Now let me also check the job dedup logic:
- [ ] failed jobs just stay failed. Retry?
- [ ] add debugging print logs to api (when processing a form so many api calls
- [ ] Option to view jobs we scraped and determine which ones to remove, etc
- [ ] confirm parallelization works correctly
- [ ] need to underestand processing form better. Found 2 form field then going to next page, etc leading to so many api chat requests

## Features

- [ ] Add LinkedIn Easy Apply support (requires LinkedIn session cookies)
- [ ] Add email-based application support (some jobs accept resume via email)
- [ ] Add job relevance scoring with LLM before tailoring (skip bad matches early)
- [ ] Add interactive mode for reviewing applications before submit
- [ ] Add a web dashboard for viewing job status and tailored docs
- [ ] Feature for restirciting X per search? Right now the logic is unclear - I don't know if it searches all 25 only for data engineer.
- [ ] think about DB and how that would look like for multiple people
- [ ] think about new and old listings and which ones to prioritize applying to when have a upper limit (newest ones first?
- [ ] have option to auto make profile based on input resume

## Improvements

- [ ] Add retry logic for failed OpenAI API calls
= [ ] Add further granularity (stuff like SaaS Configuration Specialist is being applied to so we can make stricter rules)
- [ ] Add proxy support for scraping (especially LinkedIn)
- [ ] Add support for multiple resume templates (e.g., one for data roles, one for SWE)
- [ ] Improve ATS detection with more patterns (Workday, Taleo, iCIMS)
- [ ] Add cover letter tone/style configuration
- [ ] Add unit tests for tailoring and document generation
- [ ] Add resume targeting - fix prompt to target certain job types (like we want eky words for types of jobs, for example)
- [ ] for cover letter, need to be able to tweak so it knows my current role and crafts the letter in accordance to what I see is important with the company I am applying to
- [ ] need to have opus reorganize and make this production ready as code is unorganized
- [ ] keep user resume template? I don't know might be more complicated, as for example when i refactored mine with claude it made it in that easily readable AI format

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
