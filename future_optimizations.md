## Part B: Answer Bank Architecture

### The Problem

Three separate systems answer form questions:
1. **Answer bank** (`answer_bank` table) — populated retroactively when LLM encounters questions
2. **Profile direct mapping** (`_direct_map_profile_fields`) — hardcoded label→profile field mapping at runtime
3. **LLM inference** (`infer_form_answers`) — calls OpenAI for remaining fields

The answer bank is underutilized: it only has entries for questions the LLM has already seen and returned "N/A" for. Common questions (work authorization, salary, start date) get re-inferred by the LLM every application.

### The Fix: Seed answer bank from profile.yaml

Add a **seed step** that runs at the start of `cmd_pipeline()` (or as a standalone `python -m src seed-answers` command):

1. Read `profile.yaml`
2. Generate answer bank entries for known question patterns:

| Question Pattern | Source Field | Example Answer |
|---|---|---|
| "first name", "given name" | personal.first_name | "Kai" |
| "last name", "surname" | personal.last_name | "Alcayde" |
| "email", "e-mail" | personal.email | "kai@..." |
| "phone", "mobile" | personal.phone | "555-123-4567" |
| "street", "address line 1" | personal.address.street | "123 Main St" |
| "city" | personal.address.city | "San Francisco" |
| "state", "province" | personal.address.state | "CA" |
| "zip", "postal code" | personal.address.zip_code | "94102" |
| "country" | personal.address.country | "United States" |
| "linkedin" | links.linkedin | "https://linkedin.com/in/..." |
| "github" | links.github | "https://github.com/..." |
| "authorized to work" | work_authorization.authorized_us | "Yes" |
| "require sponsorship" | work_authorization.requires_sponsorship | "No" |
| "desired salary", "salary expectation" | preferences.desired_salary_min/max | "150000" |
| "willing to relocate" | preferences.willing_to_relocate | "Yes" |
| "remote preference" | preferences.remote_preference | "any" |
| "start date", "earliest start" | preferences.start_date | "Immediately" |
| "school", "university" | education[0].school | "UCLA" |
| "degree" | education[0].degree | "BS Mechanical Engineering" |
| "gender" | diversity.gender | "Prefer not to answer" |
| "ethnicity", "race" | diversity.ethnicity | "Decline to self-identify" |
| "veteran" | diversity.veteran_status | "I am not a veteran" |
| "disability" | diversity.disability_status | "Prefer not to answer" |

3. Insert with `source='profile'`. These entries auto-refresh when profile changes.
4. User-provided answers (`source='user'`) are never overwritten.

### Replace `_direct_map_profile_fields` with answer bank lookup

After seeding, `infer_form_answers` simplifies to:
1. Check answer bank (which now includes profile-seeded entries)
2. Call LLM only for genuinely new questions
3. Save N/A questions to answer bank for user to fill via `python -m src answers`

Delete `_direct_map_profile_fields` — the answer bank is now the single source of truth.

### Preferences stay in `profile.yaml`

Salary, relocation, remote preference, start date describe the candidate, not the system. Keep them in `profile.yaml` under `preferences`. The seed step reads them into the answer bank. When the user changes their salary expectations, they edit `profile.yaml` and the next pipeline run re-seeds.

### Add a `--refresh_profile` flag

`python -m src pipeline --refresh_profile` forces a fresh seed from profile, overwriting `source='profile'` entries. Useful after editing profile.yaml. Make sure there is a pipeline we can use in launch.json

---

## Part C: Pipeline & Execution Optimizations

### Current Flow
```
scrape → tailor → apply → [manual] login-sites → [manual] retry
```

### C1: Skip tailoring for known-bad domains

**Problem:** Tailoring costs $0.03-0.10/job via OpenAI. Jobs on domains that always fail (Indeed + Cloudflare, ADP + broken Apply button) get tailored then immediately fail.

**Fix:** Add a `domain_blocklist` in settings.yaml:
```yaml
filters:
  domain_blocklist:
    - "indeed.com"      # Cloudflare bot gate
    - "myjobs.adp.com"  # Apply button unclickable
```

During `cmd_tailor()`, check each job's URL against the blocklist. Skip tailoring and set status to `skipped_domain`. This saves money and time.

### C2: Site success rate tracking

**Problem:** No data on which ATS domains succeed vs. fail.

**Fix:** Add aggregate query: `SELECT site_domain, status, COUNT(*) FROM jobs GROUP BY site_domain, status`. Display in `cmd_status()` output. Use this data to inform the domain blocklist.

Could also add a `site_stats` table for historical tracking, but the simpler query-based approach works for a solo project.

### C3: Quick page classify before vision agent

**Problem:** Vision agent burns 3-15 rounds ($0.06-0.50) on login pages, listing pages, and error pages.

**Fix:** Add `quick_page_classify(page)` in `page_checks.py`:
```python
def quick_page_classify(page) -> str:
    """Returns 'form', 'listing', 'login', 'error', or 'unknown'."""
```

Call it at the top of `run_vision_agent()`. Bail immediately for non-form pages. The existing `_is_listing_page`, `detect_login_page`, `_is_access_denied` already do this work — just consolidate into one call.

### C4: Auto-chain login-sites after apply

**Problem:** After `apply` finishes, needs_login jobs sit until the user manually runs `login-sites`.

**Fix:** At the end of `cmd_apply()` (or `cmd_pipeline()`), check if there are `needs_login` jobs. If yes AND `manual_login` is enabled, prompt: "X jobs need login. Open browser now? [y/N]". If yes, run the `login-sites` flow inline, then retry those jobs.

If `manual_login` is disabled, just print a reminder: "X jobs need login. Run `python -m src login-sites` to proceed."

### C5: Failed folder cleanup

**Problem:** `delete_failed_jobs` cleans DB but leaves orphan `applications/failed/` folders.

**Fix:** In `cmd_remove_failed()`, after deleting DB records, also remove the corresponding `applications/failed/{company}/{position}/` directories.

### C6: Smarter failure statuses for retries

**Current statuses:** `failed`, `failed_captcha`, `needs_login`

**Add:**
- `failed_listing` — stuck on listing page, Apply button unresponsive. Won't work on retry.
- `failed_error` — server error, access denied. Might work after cooldown.

`reset_failed_jobs` only resets `failed` and `failed_error`, NOT `failed_listing` or `failed_captcha`. This avoids wasting retries on predictably hopeless jobs.

### C7: Parallel tailoring

**Problem:** Tailoring is sequential — one OpenAI call per job.

**Fix:** Use `ThreadPoolExecutor(max_workers=5)` in `cmd_tailor()` to parallelize. OpenAI calls are I/O bound and independent per job. 5 concurrent calls cuts tailoring time by ~80%.

---

