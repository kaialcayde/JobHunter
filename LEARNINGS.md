# JobHunter Learnings

Hard-won lessons from debugging the automation pipeline. **Reference this before changing automation code.** Update it when a new platform quirk is discovered.

---

## LinkedIn

Platform module: `src/automation/platforms/linkedin.py`

### Two Apply Flows

LinkedIn has **two distinct apply flows** — code must handle both:

1. **Classic Easy Apply Modal** — A `[role="dialog"]` overlay with class `.jobs-easy-apply-modal`. Form fields, Next/Review/Submit buttons all live inside the modal. The main page is visible behind it.

2. **SDUI (Server-Driven UI) Flow** — Navigates to `/apply/?openSDUIApplyFlow=true`. Renders form fields inline on the page (not in a classic modal). URL stays on `linkedin.com`. Detected by URL pattern + presence of visible form fields.

### Modal Fragility

LinkedIn Easy Apply modals are **easily dismissed** by interactions with the page behind them:

- `window.scrollTo()` on the main page can close the modal
- Clicking outside the modal dismisses it
- `document.querySelectorAll` picks up elements from the background page, not just the modal

**Rule:** When a modal is open, always:
- Scope DOM queries to the modal element (use `scope = modal || document`)
- Never scroll the main page (`window.scrollTo`)
- Scope button searches (Next, Submit, Review) inside the modal first

Affected functions:
- `extract_form_fields()` in `forms.py` — field search + lazy-load scroll
- `click_next_button()` in `detection.py` — scrolls page + button search
- `click_submit_button()` in `detection.py` — scrolls page + button search

### Apply Button Detection

The Apply button on LinkedIn job pages **lazy-loads** and may not appear immediately. LinkedIn is a heavy SPA — nav buttons render first, then the job content area loads asynchronously. The code uses `wait_for_selector` with a 5s timeout on reliable selectors (`.jobs-apply-button`, `button[aria-label*="Apply"]`), then falls back to polling with modal dismissal.

If the button is never found, it falls back to `_force_apply_click()` which extracts apply URLs directly from page elements/scripts.

Common failure: buttons list shows only `['Skip to main content', 'Home', 'Jobs', 'Me', 'For Business']` — means the job content area hasn't loaded yet, or a blocking modal/overlay is covering it. A debug screenshot is saved to `data/logs/debug_no_apply_button.png` when this happens.

**Important:** Don't just poll with short waits — use `wait_for_selector` with adequate timeouts. LinkedIn can take 3-5s to render the job content.

### "Share Your Profile?" Modal

LinkedIn shows a "Share your profile?" modal AFTER clicking the Apply button on external jobs. This modal has a "Continue" button that actually triggers the external redirect (opens new tab to ATS). The X button dismisses without applying.

**Critical:** The Apply `<a>` button's `href` often points back to the SAME LinkedIn job page (not the external ATS). So direct navigation via href does nothing. The actual redirect only happens when "Continue" is clicked in the Share Profile modal.

**Flow:** Click Apply -> Share Profile modal appears -> Click Continue -> New tab opens with external ATS

**Implementation:** The Continue button MUST be clicked with Playwright's native `.click()` (not JS `el.click()` inside `page.evaluate()`), because LinkedIn's click handler uses `window.open()` to open the external ATS tab — JS-dispatched clicks don't trigger popup-opening handlers. The `_handle_share_profile_modal` function in `detection.py` handles this with `expect_page` to catch the new tab. The `dismiss_all_linkedin_modals` function in `linkedin.py` is a fallback that uses JS click (works when called from other contexts where native click isn't available).

**Modal Selector Fragility:** LinkedIn's Share Profile modal does NOT always use `[role="dialog"]` or `.artdeco-modal`. Detection must use broad selectors including `.artdeco-modal-overlay`, `div[class*="modal"][class*="overlay"]`, and a fallback that searches for a "Continue" button near share/profile-related text. Also: the caller in `_click_linkedin_apply` must handle both `"new_tab"` AND `True` returns from `_handle_share_profile_modal` — ignoring `True` causes a "nothing happened" false failure.

**`<a>` Tag Default Navigation Destroys Modal:** The Apply button is an `<a>` tag whose `href` points to the same LinkedIn job page. Clicking it fires BOTH the JS handler (shows Share Profile modal) AND the `<a>` default behavior (navigates to href, refreshing the page). The navigation destroys the modal before `_handle_share_profile_modal` can detect it. **Fix:** Before clicking, add a one-time `preventDefault` listener: `el.addEventListener('click', e => e.preventDefault(), {once: true})`. Only do this for same-page LinkedIn hrefs — NOT for `/redir/redirect/` URLs which are actual external redirects.

### LinkedIn Redirect URLs

LinkedIn external apply links sometimes use `linkedin.com/redir/redirect/?url=<encoded_ats_url>`. These look like LinkedIn URLs (contain `linkedin.com`) but are actually redirects to external ATS. The code must treat `/redir/redirect/` hrefs as external — navigate directly via `page.goto()` instead of blocking them as same-page links.

### SDUI Gate Check

After clicking Apply, the code checks if we're "stuck on LinkedIn" by calling `detect_easy_apply_modal()`. If the SDUI flow is active (URL contains `/apply`), the classic modal selectors won't match — `detect_easy_apply_modal` must also check URL patterns and visible form fields, or the application gets skipped with "Could not leave LinkedIn."

### Easy Apply Modal aria-label Varies

The Easy Apply dialog's `aria-label` is NOT always "Easy Apply" — LinkedIn often uses "Apply to {Company Name}" instead. The classic `.jobs-easy-apply-modal` CSS class may also be absent. `detect_easy_apply_modal` must check for `aria-label*="Apply to"` AND use a heuristic fallback: any visible `[role="dialog"]` containing "apply to" or "submit application" text with form inputs counts as an active Easy Apply modal.

### LinkedIn-Specific Button Selectors

Easy Apply modal buttons use aria-labels:
- `button[aria-label="Continue to next step"]` — Next
- `button[aria-label="Review your application"]` — Review
- `button[aria-label="Submit application"]` — Submit

These are scoped inside the modal, not the main page.

### Easy Apply Multi-Step Flow

Easy Apply is a **multi-step modal** flow. After clicking the Easy Apply button, a modal opens with form fields. The user must fill fields, click Next/Continue, fill more fields, then click Review, then Submit. The code handles this with a selector-based loop (not vision agent) that:
1. Extracts form fields scoped to the modal
2. Uses LLM to infer answers
3. Fills fields
4. Clicks Next (via `click_next_button` which searches inside the modal)
5. Repeats until no Next button is found (we're at Submit)
6. Clicks Submit

**Critical timing issue:** After clicking the Easy Apply button, the modal takes 1-3 seconds to render. `detect_easy_apply_modal()` was returning False because only 500ms had elapsed. **Fix:** `_click_linkedin_apply` now returns `"easy_apply"` (not `True`), and `_apply_to_single_job` waits up to 3s for the modal to appear when it sees this result, skipping the "stuck on LinkedIn" fallback.

**Return value distinction:**
- `"easy_apply"` — Easy Apply button clicked, modal expected on same page
- `True` — External apply, page navigated or modal handled
- `"new_tab"` — New tab opened for external ATS

### Shadow DOM (`interop-shadowdom`)

As of March 2026, LinkedIn renders Easy Apply modals inside a **shadow DOM** host element: `<div id="interop-outlet" data-testid="interop-shadowdom">`. This means:
- `document.querySelector()` in `page.evaluate()` **cannot** find modal elements
- Playwright's `page.locator()` and `page.get_by_role()` **can** pierce open shadow roots
- `detect_easy_apply_modal()` now iterates over `element.shadowRoot` for all potential host elements
- Form extraction uses `extract_form_fields_playwright()` (Playwright locators) instead of the JS-based `extract_form_fields()` for Easy Apply
- Button clicks (`click_next_button`, `click_submit_button`) try Playwright locators first, then fall back to JS

**The `interop-outlet` overlay also intercepts pointer events**, causing `ElementHandle.click()` to time out with `"<div id="interop-outlet"> intercepts pointer events"`. Using `page.locator().click()` or `force=True` bypasses this.

---

## SmartRecruiters

Platform module: `src/automation/platforms/smartrecruiters.py` (create when needed)

### Apply Button

SmartRecruiters uses **"I'm interested"** instead of "Apply". The button is typically in the top-right of the job listing page. Clicking it may:
- Navigate to a login/signup page on SmartRecruiters
- Open the application form inline
- Open a new tab/popup

### Known Selectors
- Apply button: `button:has-text("I'm interested")`, `.js-btn-apply`
- URL pattern: `jobs.smartrecruiters.com/{company}/...`

---

## Greenhouse

Platform module: `src/automation/platforms/greenhouse.py` (create when needed)

### Apply Button
- Button text: "Apply Now", "Apply for this job"
- URL patterns: `boards.greenhouse.io/{company}/jobs/...` and `job-boards.greenhouse.io/{company}/jobs/...`
- LinkedIn external apply often redirects here via `grnh.se` short links
- Application form is often on the SAME page as the job description (below the fold), NOT a separate page. `_is_listing_page()` must not treat Greenhouse URLs as listing-only pages.

### Form Below the Fold
Greenhouse puts the job description at the top and the application form below. The vision agent's first screenshot sees only the job description + an "Apply" button and gets stuck clicking it. **Fix:** Before vision agent handoff, scroll to the first form input (`scrollIntoView`) so the initial screenshot shows the form fields. Also, `_is_listing_page()` should check for form inputs in the DOM (not just viewport-visible ones) since the form exists but is below the fold.

### reCAPTCHA Enterprise Gate
Greenhouse job boards (`job-boards.greenhouse.io`) use **reCAPTCHA Enterprise** as a bot gate before showing the application form. Key differences from standard reCAPTCHA v2:
- The iframe src contains `/enterprise` (e.g., `recaptcha/enterprise/anchor`)
- `grecaptcha.enterprise` is loaded instead of plain `grecaptcha`
- 2Captcha requires `enterprise=1` parameter to produce a valid token
- After token injection, the page may need a form submit or reload to advance — there's no automatic redirect

The code now detects Enterprise via script src and iframe URL patterns, and passes `enterprise=1` to 2Captcha. A debug screenshot is saved to `data/logs/debug_captcha_unsolved.png` when the token is injected but the page doesn't advance.

### Email Verification Code (OTP) Gate
Some Greenhouse forms require an email verification code after filling out the form and before submitting. The page shows a "verification code" input field. The vision agent has no way to access the applicant's email to get this code and loops for 15 rounds trying to type a code it doesn't have.

**Fix:** Added OTP/verification code detection in the vision agent's main loop. If `manual_otp` is enabled in settings, the first OTP round prompts the user in the terminal to enter the code, then injects it into the verification input via DOM. If no code is entered or `manual_otp` is off, and 2+ consecutive rounds reference OTP keywords, the agent bails with `needs_login` status.

### Form Structure
- Clean, standard HTML forms — selector-based `extract_form_fields` works well
- File uploads use standard `input[type="file"]`
- Custom dropdowns use **React-Select** combobox components, NOT native `<select>` elements

### React-Select Dropdowns (Critical)
Greenhouse uses React-Select (`react-select`) for all dropdown fields (country, sponsorship, "how did you hear", etc.). These appear as `<input role="combobox" class="select__input">` inside `.select__control` containers.

**Key behaviors:**
- `extract_form_fields` detects these as `type: "text"` (not `custom_select`), so `fill_form_fields` must check for `role="combobox"` or `.select__input` class and route to `_fill_react_select()`
- The correct interaction: click the input, type to filter, then click the first visible `[role="option"]`
- **DO NOT** use `Control+a / Backspace` to clear — this breaks React-Select's dropdown state (closes the dropdown, typing no longer filters). Instead, clear via JS: `el.evaluate('e => e.value = ""')` then re-click
- `page.fill()` on a React-Select input just sets filter text without selecting an option — useless
- After typing, `page.query_selector_all('[role="option"]')` returns options from ALL dropdowns on the page (including hidden phone country pickers) — always check `opt.is_visible()` to only click options from the CURRENTLY OPEN dropdown
- Options are not loaded until the dropdown is clicked — `extract_form_fields` gets empty options lists
- When the LLM-inferred answer doesn't match any option (e.g., "Job Board" when options are "LinkedIn", "Recruiter Outreach", etc.), fall back to related terms: "LinkedIn" > "Online" > "Other"
- Already-selected values show in a `.select__single-value` element inside the container — check this before re-selecting to avoid clearing a valid selection
- URL patterns: `boards.greenhouse.io/{company}/jobs/...` and `job-boards.greenhouse.io/{company}/jobs/...`

---

## Lever

Platform module: `src/automation/platforms/lever.py` (create when needed)

### Apply Button
- Button text: "Apply for this job"
- URL pattern: `jobs.lever.co/{company}/...`
- Application form is usually on a separate page

---

## Ashby

Platform module: `src/automation/platforms/ashby.py` (create when needed)

### Apply Button
- Button text: "Apply"
- URL pattern: `jobs.ashbyhq.com/{company}/...`
- Forms are React-based SPAs — vision agent handles these

### Cloudflare Protection
Ashby job pages use Cloudflare challenges (Turnstile or browser verification). These often resolve automatically after a few seconds — the code now waits up to 15 seconds for auto-resolution before attempting sitekey-based solving. The sitekey may not be in standard DOM attributes; check script tags for `turnstile.render()` calls.

### Invisible reCAPTCHA False Positive
Ashby application forms include `script[src*="recaptcha"]`, `.grecaptcha-badge`, and invisible reCAPTCHA elements **even after the challenge has auto-resolved**. These are passive (not blocking the form). `detect_captcha()` must skip script-only and badge-only detections when visible form fields exist (2+ inputs visible = form is loaded and CAPTCHA was already passed).

### Spam Detection on Submit
Ashby's invisible reCAPTCHA can **block form submission** even when the form fields loaded fine. The `detect_captcha()` false-positive logic skips passive reCAPTCHA when form fields are visible, so the CAPTCHA isn't caught before the vision agent submits. Ashby then rejects with "Your application submission was flagged as possible spam." After rejection, the form fields disappear and `detect_captcha()` now returns true — but the submit is already rejected and 2Captcha may return `ERROR_CAPTCHA_UNSOLVABLE`.

**Fix:** The vision agent now checks for CAPTCHA in the "done but form still showing" path (not just "stuck"), catching the spam rejection earlier. However, Ashby's invisible reCAPTCHA may simply be unsolvable via 2Captcha on some pages.

### Vision Agent Type Loop on Ashby
Ashby's React-based forms sometimes reject coordinate-based typing — the vision agent types values but they don't appear (React controlled inputs drop events not originating from real user focus). The agent sees "empty" fields, reports "re-filling", and loops for 10+ rounds without progress. The exact coordinates change slightly each round, so the coord-based repeat detection (`prev_batch_coords == current_coords`) doesn't trigger consistently.

**Fix:** Added "type-loop" detection: if 4+ consecutive rounds have mostly type/click actions with "re-fill"/"appears empty" reasoning, force a DOM-based `click_submit_button()`. If that doesn't work, inject a CRITICAL prompt telling the model to stop filling and click Submit.

### DOM Pre-Fill Before Vision Agent
Ashby's React forms reject the vision agent's coordinate-based `page.keyboard.type()` but work fine with Playwright's `page.fill()` (which dispatches proper input/change events). The applicant now runs `extract_form_fields` + `infer_form_answers` + `fill_form_fields` + `handle_file_uploads` BEFORE the vision agent takes over. This handles text fields reliably, leaving only checkboxes/radio buttons and submit for the vision agent.

### CAPTCHA Solve Wipes Ashby Form
After Ashby's invisible reCAPTCHA is solved, the page reloads and **all form fields are cleared**. The DOM pre-fill must run again after every CAPTCHA solve. There are three CAPTCHA solve locations in the vision agent: (1) "done but form still showing", (2) "stuck", (3) post-click-action check. All three now re-run DOM pre-fill after CAPTCHA is solved.

### Submit Button
- Button text: "Submit Application" (green button at bottom of form)
- The submit button is a standard `<button>` — DOM-based `click_submit_button()` works reliably. Vision agent coordinate clicks often miss it.

---

## Amazon Jobs

Platform module: `src/automation/platforms/amazon.py` (create when needed)

### Login Required
Amazon Jobs (`amazon.jobs`) requires authentication to apply. Clicking Apply redirects to `amazon.jobs/account/signin` which is a login page with email + password fields. The vision agent was wasting 3 rounds trying to interact with the login form before being detected as stuck.

**Fix:** Added `amazon.jobs/account/signin` and `passport.amazon.jobs` to `detect_login_page` URL patterns. Also added generic login detection (`/login`, `/signin` patterns with password field check) and login-keyword detection in the vision agent's stuck handler so it returns `"needs_login"` instead of `False`. Jobs are now marked `needs_login` and can be retried via `python -m src login-sites`.

**URL Note:** The Apply button href navigates to `amazon.jobs/en/jobs/{id}/...` but then a client-side redirect goes to `passport.amazon.jobs/` (not the expected `/account/signin`). Detection must include the `passport.amazon.jobs` subdomain.

### "Already Applied" Detection
After logging in with saved cookies, Amazon shows "You have already applied for this position" if the user previously applied. The vision agent was stuck-retrying 3 times on this page. **Fix:** Vision agent now detects "already applied/submitted" keywords in stuck reasoning and returns `"already_applied"`. The caller marks the job as `applied` and moves to success folder.

### Expired Session Redirects to Careers Landing Page
When Amazon session cookies expire, clicking Apply redirects to the Amazon Jobs careers landing page (`amazon.jobs/en`) instead of the application form. The page has search/filter inputs that make `extract_form_fields` find ~22 "fields" — fooling the system into thinking it's a form. The vision agent then sees "Recommended Jobs", "AI careers", "Find your role" and gets stuck. **Fix:** Batch-mark all Amazon jobs as `needs_login` since they all require active session cookies. Don't waste vision agent rounds on these.

### Multi-Step Form — Vision Agent Stuck on "Continue"
Amazon Jobs uses a multi-step application form (Contact info -> General questions -> Education -> Job-specific questions -> Work Eligibility -> Resume -> ...). Each step has a "Continue" button. The vision agent's coordinate-based click on "Continue" often doesn't work (possibly due to coordinate inaccuracy or the button requiring native click). The repeat-detection code was only trying `click_submit_button()` (which looks for Submit-like buttons), missing the "Continue" button entirely.

**Fix:** Vision agent repeat-detection and type-loop detection now try `click_next_button()` (which matches "continue", "next", "review" text) BEFORE `click_submit_button()`. On success, all repeat counters are reset so the agent treats the new step as fresh. Also reduced `scroll_into_view_if_needed` timeout from 30s to 3s in DOM pre-fill to avoid long hangs on hidden/detached elements.

---

## Avature (avature.net)

Platform module: none (create when needed)

### Domain Pattern
- Some employers host the same Avature flow on a branded domain (for example, `apply.deloitte.com`) while keeping Avature path signatures like `/careers/InviteToApply`.
- `*.avature.net` (e.g. `bloomberg.avature.net`)
- Each employer gets a subdomain tenant — treat each as a distinct account

### Auth Flow
Clicking Apply navigates to the job detail page, then redirects to a login page at `{tenant}.avature.net`. The user has no existing account — auto-registration is required.

### Registration
The login page has a "Create Account" or "Sign Up" link. `handle_detect_auth_type` clicks it to navigate to the registration form, then the standard `handle_register` fills name/email/password and submits.

### Alternate URL Trap
The job's `listing_url` often points to Indeed. If `try_recover_login` falls back to the Indeed URL, it hits a **Cloudflare CAPTCHA** (verified in `debug_no_submit.png` for job #280). The vision agent cannot complete this challenge.

**Fix:** `try_recover_login` now short-circuits for any domain in `auto_register_domains` — skips the alternate URL entirely and returns `REQUIRES_LOGIN` so the kernel routes to `DETECT_AUTH_TYPE → REGISTER`.

### Custom-Hosted Avature Domains
Deloitte redirected `deloitteus.avature.net` to `apply.deloitte.com/en_US/careers/InviteToApply?...`, which is still Avature. Hostname-only checks missed it, so login recovery treated it like a generic site, fell back to the Indeed `listing_url`, and the run died on Indeed's Cloudflare CAPTCHA.

**Fix:** detect Avature by URL path signatures as well as hostname. Reuse that shared detection in login recovery, auto-register allowlisting, platform-prefill routing, and the deterministic Avature handler inside the vision agent.

### Domain Collapse Gotcha
`ATS_DOMAINS` contains `avature.net`, so `get_site_domain("bloomberg.avature.net")` collapses to `avature.net`. But `fnmatch.fnmatch("avature.net", "*.avature.net")` returns False (no prefix before the dot). All places that call `is_auto_register_allowed` or use the domain as an account registry key now use `urlparse(url).hostname` (full hostname) instead of the collapsed domain.

### Account Registry Key
`bloomberg.avature.net` is stored as the full hostname key — not `avature.net`. This prevents credential collisions between different employers using Avature.

### Resume Upload Step (Upload-Only Page)
Avature's first application step is a resume-upload-only page ("Select Your Resume"). It has a "From Device" file chooser button and a "CONTINUE" button — but NO visible text inputs. DOM pre-fill uploads the resume successfully, but the code did not click "Continue" afterward. The vision agent then started on the same upload step, looped for 9 rounds trying to re-upload and click Continue via coordinates — which doesn't work on Avature.

**Fix:** After `handle_file_uploads()` in `handle_fill_vision`, count visible text-type inputs. If fewer than 2 are visible (upload-only step), call `click_next_button()` immediately. The page advances to Personal Information before the vision agent starts.

**Root cause of coordinate click failure:** Avature's Continue button requires a Playwright native click (via `get_by_role` or `page.locator()`). Coordinate-based `page.mouse.click(x, y)` does not trigger the button's JS handler reliably.

### Gmail `+` Aliases Normalized to Base Email
Avature (and many ATS platforms) **normalize Gmail plus-addressing**: `kalcaydecl+avature-bloomberg@gmail.com` is treated as `kalcaydecl@gmail.com` for duplicate checking. When `use_email_aliases: true`, auto-registration attempts fail with "There's an existing record with that email" if the base email already has an account on the site.

**Fix:** Keep `use_email_aliases: false` and manually seed the base email credentials via `python -m src set-account bloomberg.avature.net kalcaydecl@gmail.com Pog1ako1`. When registration detects "existing record", it switches to login and uses the stored credentials automatically.

### Template `-sample` Rows vs Live Dataset Rows
Avature multi-row sections (education, work history) render both hidden/template controls like `6074-1-sample` and live row controls like `6074-1-0`. The template row often keeps the placeholder label text (`"School Select an option"`) while the live row label changes to include the chosen value. Label-based lookup and validation logging will drift back to the template row unless the code explicitly prefers visible non-`-sample` controls.

**Fix:** for Avature label-based control discovery, sort candidates by visible container + non-`-sample` row before exact-label match. Also ignore `-sample` containers when dumping validation errors from the Register page.

### Autocomplete "Other" Path
Some Avature select2/autocomplete fields do NOT contain the applicant's real value in the tenant's option list. Deloitte's work-history `Employer` field explicitly instructs: *If employer is not listed, please select "Other".* When the code forced a partial match, it selected the wrong employer (`Intuit Inc.`) and hid the real root cause.

**Fix:** use strict matching for employer/company autocomplete fields. If no exact-ish option exists, prefer the field's `Other` path and fill the paired `Other *` text input with the real company name instead of inventing a nearby option.

### Configuration
- `selectors.py` `ATS_DOMAINS`: `avature.net` added
- `account_registry.py` `_ATS_PATTERNS`: `"avature": [r"\.avature\.net$"]`
- `settings.yaml` + `settings.example.yaml` `auto_register_domains`: `*.avature.net`

---

## Indeed

Platform module: none (create when needed)

### Cloudflare Bot Gate
- URL pattern: `indeed.com/job/...`
- Indeed uses a full Cloudflare interstitial challenge page ("Additional Verification Required" + "Verify you are human" checkbox).
- The CAPTCHA detection triggers on `scripts-only` (Cloudflare scripts present, no form content) but the Turnstile sitekey extraction fails — the Cloudflare challenge page embeds the widget differently than standard Turnstile integrations.
- `_wait_for_cloudflare_auto_challenge` detects the page and waits 15s, but the challenge requires a manual checkbox click and doesn't auto-resolve.
- Jobs are correctly marked `failed_captcha`. Indeed jobs sourced via LinkedIn external apply will hit this gate.
- **Unresolved:** Would need Cloudflare challenge-page-specific sitekey extraction or native click on the Turnstile checkbox to solve.

---

## ADP

Platform module: `src/automation/platforms/adp.py` (create when needed)

### Apply Button on Listing Page
- URL pattern: `myjobs.adp.com/...`
- ADP listing pages have an "Apply" button that the vision agent clicks but doesn't navigate (likely opens a new tab or uses JS popup handler)
- ADP listing pages also have search/filter form inputs (keyword, location) that make `_is_listing_page()` return False — the code thinks it's a form page when it's actually a listing
- **Fix:** `_is_listing_page()` now excludes search/filter inputs from the form field count. Added `adp.com` to the ATS domain list in `_force_apply_click()`. Vision agent's stuck handler now detects "job listing/description" keywords and tries `_force_apply_click()` before giving up
- **Unresolved:** ADP's Apply button uses a framework-specific handler that neither URL extraction, JS click, nor `window.open` interception can trigger. `_force_apply_click()` fails. ADP jobs may need a platform-specific module with Playwright native click + popup detection

---

## TEKsystems

Platform module: none (create when needed)

### `apply.teksystems.com/v1/s/` Listing Shell
- URL pattern: `apply.teksystems.com/v1/s/...`
- Symptom: external redirect lands on a page titled "Job Application | TEKsystems Careers", but DOM pre-fill finds 0 fields and the only obvious control is a `Filter` button (`#filter-btn-handler`).
- Root cause: this is still a listing/search shell, not the actual application form. `is_listing_page()` was returning `False` because the page lacked the usual listing-keyword text, so the flow skipped listing recovery and handed the shell to the vision agent.
- Fix applied: treat `apply.teksystems.com/v1/s/` with `#filter-btn-handler` and 0 form fields as a listing page. In listing recovery, try `click_apply_button()` before `force_apply_click()` so ATS pages with a visible Apply button get one normal Playwright click before URL-extraction fallback.

---

## Workday

Platform module: `src/automation/platforms/workday.py` (create when needed)

### Apply Button
- Button text: "Apply", "Start application", "Apply Manually"
- URL pattern: `{company}.wd{1-5}.myworkdayjobs.com/...`
- Multi-step form with heavy JS — vision agent is typically needed
- Known for slow page loads and complex form validation

### Create Account Gate
Workday requires creating an account (email + password) before starting the application. Clicking "Apply Manually" on the job page navigates to a "Create Account" form. The `detect_login_page` check runs BEFORE the vision agent starts (at which point the page is still the job listing), so it misses this gate. The vision agent then fills in the account creation form and gets stuck because it can't actually create an account.

**Fix:** Added "create account", "account creation", "sign up" keywords to the vision agent's stuck handler login detection. Also added a DOM-based `detect_login_page()` fallback in the stuck handler so it catches password fields + login phrases regardless of what the vision model reports. Additionally, added a `detect_login_page()` check in `_apply_to_single_job` right after "External ATS detected" — before DOM pre-fill and vision agent — so login/signup pages are caught immediately without wasting any vision rounds.

---

## Broken/Error Pages

### WordPress "Critical Error" False CAPTCHA
Some ATS sites (e.g., Terakeet) are hosted on WordPress and occasionally show "There has been a critical error on this website." This is a server-side PHP crash, NOT a CAPTCHA. However, WordPress still loads reCAPTCHA scripts in the page skeleton, which triggers `detect_captcha()` as a false positive. **Fix:** `_is_access_denied()` now checks for "critical error" and "500 internal server error" phrases, catching these pages before the CAPTCHA check. Debug screenshots saved to `data/logs/debug_captcha_blocked.png` help distinguish true CAPTCHAs from broken pages.

---

## General ATS Rules

### Apply Button Text Varies By Platform

All recognized apply button texts are centralized in the `applyTexts` array in `click_apply_button()` in `detection.py`. When adding a new platform, add its button text there.

### When to Create a Platform Module

Create `src/automation/platforms/{platform}.py` when a platform needs:
- Custom modal/dialog handling (like LinkedIn's Easy Apply)
- Platform-specific login or session management
- Unique form structures that break generic extraction
- Special button selectors or navigation flows

Until then, the generic paths in `detection.py` and `forms.py` handle most platforms.

---

## Vision Agent (External ATS)

### Batch Actions Over Single Actions

The vision agent originally did one action per screenshot (30 API calls for an 8-field form). This caused:
- The model looping on the same fields because it forgot what it already tried
- Fields not being filled (coordinate drift between screenshots)
- Hitting the 30-step limit with nothing submitted

**Fix:** Return ALL actions for visible fields in one API call per screenshot. A typical form now completes in 3-5 rounds instead of 20-30 steps.

### Coordinate Accuracy

Vision models (even gpt-4o) can miss input fields if coordinates target the label instead of the input center. When a batch of actions targets the same coordinates as the previous round, the model is warned to aim more precisely at field centers.

### Model Choice

gpt-4o is preferred over gpt-4o-mini for the vision agent. The batch approach already cuts API calls by ~80%, so cost savings are built in. gpt-4o-mini has weaker spatial reasoning which compounds with batch execution (one bad coordinate cascades).

---

## Gem.com

Platform module: `src/automation/platforms/gem.py` (create when needed)

### False CAPTCHA Detection
Gem.com (`jobs.gem.com`) application forms include a `.g-recaptcha` element and reCAPTCHA scripts, but the CAPTCHA is invisible/passive — the form is fully loaded and fillable. `detect_captcha()` was flagging the `.g-recaptcha` div as blocking because it lacked `data-size="invisible"`, even though the form had 10+ visible inputs.

**Fix:** Revised `.g-recaptcha` detection logic: when form content exists (2+ visible inputs), only flag `.g-recaptcha` as blocking if the element itself is visibly rendered (offsetWidth/Height > 10). Zero-size or hidden `.g-recaptcha` with form content = passive, skip it.

---

## Clicks

Tracking click/navigation failures where clicking a button (Apply, Submit, Next) doesn't produce the expected result. These are distinct from form-filling issues — the button is found and clicked, but the page doesn't transition.

### Paylocity — Invisible reCAPTCHA Blocks Apply

- **URL pattern:** `recruiting.paylocity.com/Recruiting/Jobs/Details/...`
- **Symptom:** Vision agent clicks "Apply" button, page doesn't change. Agent reports "stuck" after 2-3 rounds of retrying the same click.
- **Root cause:** Paylocity uses an **invisible reCAPTCHA** (badge visible in bottom-right corner). Clicking "Apply" triggers a reCAPTCHA challenge behind the scenes. Without solving it, the form never loads.
- **Detection:** Look for the reCAPTCHA badge (`iframe[src*="recaptcha"]` or `.grecaptcha-badge`) on the page even when no visible challenge widget appears. Also check for `script[src*="recaptcha"]` tags.
- **Fix applied:**
  1. `detect_captcha()` now checks for `.grecaptcha-badge` and `script[src*="recaptcha"]` to catch invisible reCAPTCHA.
  2. Vision agent checks for CAPTCHA after executing click actions — if detected, attempts to solve before next round.
  3. Vision agent's "stuck" handler checks for CAPTCHA before retrying or giving up — if a CAPTCHA is gating, solve it first.

### Vision Agent Type-Loop on Address Fields

- **URL pattern:** `recruiting.paylocity.com/Recruiting/Jobs/Details/...`
- **Symptom:** Vision agent types Address Line 1, City, State, Zip Code values, but fields remain empty. Agent loops 15 rounds with "required but not filled" reasoning without triggering type-loop detection.
- **Root cause:** Paylocity uses controlled React inputs that reject coordinate-based `page.keyboard.type()`. DOM pre-fill found only 1 field (most fields may be pre-populated from Paylocity profile, leaving only address fields which may use non-standard input rendering).
- **Type-loop detection gap:** The detection only checked for "re-fill"/"appears empty" keywords in reasoning, but Paylocity's model output uses "not filled"/"required but" phrasing instead.
- **Fix applied:** Extended type-loop keyword matching to include "not filled" and "required but" patterns so the 4-round bypass triggers correctly.
- **Further fix:** Added DOM fallback for `type` action: after coordinate-based typing, verify the field value via `_find_input_at_coords()`. If empty, use `page.fill()` or JS value dispatch with React-compatible events (`nativeSetter.call()` + `input`/`change`/`blur` events). DOM fill is now tried FIRST before coordinate typing.

### Bloomberg/Avature — Alternate URL Leads to Cloudflare CAPTCHA

- **URL pattern:** `bloomberg.avature.net/...`
- **Symptom:** Vision agent interacted with a Cloudflare challenge page (1 action, status=done). "Application submitted via DOM click!" rejected by verify — job marked failed.
- **Root cause:** `try_recover_login` tried the `listing_url` (Indeed) before checking if the ATS domain supports auto-registration. Indeed responded with a Cloudflare interstitial, not an application form.
- **Fix applied:** `try_recover_login` now short-circuits for any domain matching `auto_register_domains` patterns — returns `REQUIRES_LOGIN` immediately so kernel routes to `DETECT_AUTH_TYPE` instead of falling through to the alternate URL.

### Greenhouse — React-Select Dropdowns Stay Empty After Select Action

- **URL pattern:** `job-boards.greenhouse.io/{company}/jobs/...`
- **Symptom:** Vision agent's `select` action clicks dropdown, types option text, but dropdown stays empty. Agent loops trying to re-select the same fields for 15 rounds.
- **Root cause:** Three compounding issues:
  1. `_find_input_at_coords()` hits the `.select__placeholder` div, not the `<input role="combobox">` — so React-Select detection failed
  2. Using `Control+a / Backspace` to clear the input closes React-Select's dropdown — subsequent typing doesn't filter
  3. `page.query_selector('[role="option"]')` returns the first option from ANY dropdown on the page (e.g., hidden phone country picker), not the currently open one
- **Fix applied:**
  1. Added secondary combobox search: walk up from `elementFromPoint` to find `.select` container, then querySelector for `input[role="combobox"]`
  2. Clear React-Select inputs via JS (`el.evaluate('e => e.value = ""')`) instead of keyboard shortcuts
  3. Filter `[role="option"]` results with `opt.is_visible()` to only click options from the active dropdown
  4. Added "already selected" check: if `.select__single-value` in container already shows the desired text, skip the action

---

## Kernel States

Module: `src/automation/kernel.py`

### Common State Transition Patterns

- **Happy path:** SETUP → NAVIGATE → ROUTE → DETECT_STRATEGY → FILL_SELECTOR/FILL_VISION → VERIFY → CLEANUP(applied) → COMPLETE
- **CAPTCHA mid-flow:** Any state → SOLVE_CAPTCHA (saves `pre_captcha_state`) → resume from saved state on success, CLEANUP(failed_captcha) on failure
- **Login wall:** NAVIGATE or ROUTE → RECOVER_LOGIN → retry from NAVIGATE on success, CLEANUP(needs_login) on failure
- **Dead page / access denied:** NAVIGATE → CLEANUP(failed_error) — no retry, page is broken

### When CAPTCHA Resume Works vs Doesn't

CAPTCHA resume works when:
- The CAPTCHA appeared as a gate before the form (e.g., Greenhouse reCAPTCHA Enterprise). After solving, the form loads and filling can proceed from the saved state.
- The CAPTCHA appeared on a Cloudflare interstitial. After solving, the page redirects to the actual content.

CAPTCHA resume does NOT work when:
- The CAPTCHA solve causes a page reload that wipes form state (e.g., Ashby invisible reCAPTCHA). The kernel must re-run DOM pre-fill after resuming.
- The site uses CAPTCHA as a one-time gate but the token expires before the form is submitted. The kernel sees a second CAPTCHA and may exhaust retries.

### Handler Design Rules

- Handlers accept explicit parameters (page, job, settings, etc.), never read global state
- Return `StepResult(result=HandlerResult.XXX, metadata={...})` — never call other handlers
- Only the kernel's transition table decides the next state
- Metadata dict carries forward state updates (new page references, form answers, strategy choice)

---

## Selector Cache

Module: `src/automation/selector_cache.py`

### Confidence Tuning

- **Initial confidence:** 0.8 (seeded from generic patterns via `SELECTOR_INTENTS`, not yet verified on any domain)
- **On success:** Reset to 1.0
- **On failure:** Multiplied by decay factor 0.7 (0.8 → 0.56 → 0.39 → 0.27)
- **Age decay:** Exponential decay after 30 days of non-use
- **Skip threshold:** 0.3 — selectors below this are treated as expired

### Which ATS Platforms Change Selectors Frequently

- **LinkedIn** — Frequent selector changes (shadow DOM migration, class name rotation). Cache hits are valuable but short-lived. The `interop-outlet` shadow DOM wrapper appeared in March 2026 and broke all JS-based selectors.
- **Greenhouse** — Stable selectors. React-Select class names (`.select__control`, `.select__input`) have been consistent. Cache entries have long useful lifetimes.
- **Ashby** — Moderate changes. React component class names are hashed but role attributes (`role="combobox"`, `role="option"`) remain stable.
- **Workday** — Unstable. Heavy JS framework with dynamically generated IDs. Accessibility roles are the most reliable selector strategy.

### Bootstrap Process

On first run, `bootstrap_from_selectors()` seeds the cache with wildcard (`'*'`) entries from `SELECTOR_INTENTS` in `selectors.py`. Domain-specific entries are learned through actual usage via `ElementFinder` — when a selector succeeds on a specific domain, it's cached with confidence 1.0.

---

## Email Polling

Module: `src/automation/email_poller.py`

### OTP Patterns Per ATS

| ATS | Email From | Code Format | Subject Pattern |
|-----|-----------|-------------|-----------------|
| Greenhouse | `no-reply@greenhouse.io` | 6-digit numeric | "Verification code" or "Confirm your email" |
| Workday | varies by company | 6-digit numeric | "Verify your identity" or company-branded |
| iCIMS | `noreply@icims.com` | 6-8 digit numeric | "Verification code" or "Your application" |

### Configuration

Requires in `.env`:
```
EMAIL_USER=your.email@gmail.com
EMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx  # Gmail app password, NOT account password
```

And in `settings.yaml`:
```yaml
automation:
  email_polling: true
  imap_server: imap.gmail.com
  imap_port: 993
  email_poll_timeout: 120
```

### Fallback Chain

1. Email poller attempts IMAP connection and polls for matching emails within timeout
2. If poller fails (no email found, connection error, or disabled), falls back to manual terminal prompt (if `manual_otp: true`)
3. If manual prompt is skipped or times out, job is marked `needs_login`

### Known Limitations

- Gmail requires an App Password (not regular password) with "Less secure app access" or 2FA enabled
- Some ATS platforms send OTP from company-branded domains that don't match the ATS domain — the poller may miss these if domain filtering is too strict
- Magic link emails may use tracking redirects that obscure the actual verification URL
