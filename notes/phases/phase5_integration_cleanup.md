# Phase 5: Integration + Cleanup

## Goal
Remove deprecated code, update all documentation, verify end-to-end functionality, and consolidate the refactored architecture. After this phase, the system is stable and ready for Phase 6 (account creation).

## Why This Phase
Phases 1-4 built new infrastructure alongside the old code. Phase 5 cuts the umbilical cord — `flow.py` gets deleted, docs get updated, and the full pipeline is validated.

---

## Deleted Files

### `src/automation/flow.py`
All callers now use `ApplicationKernel.run()`. The compatibility shim from Phase 1 is no longer needed.

**Pre-deletion checklist:**
- [ ] `applicant.py` imports from `kernel.py`, not `flow.py`
- [ ] No other file imports from `flow.py` (grep to confirm)
- [ ] Full pipeline test passes without `flow.py`

---

## Modified Files

### `ARCHITECTURE.md`
Update with new architecture:

- Add kernel state machine diagram
- Add element finder escalation pipeline
- Add email poller service description
- Update import graph (kernel.py, handlers.py, results.py, selector_cache.py, element_finder.py, email_poller.py)
- Remove references to `flow.py`
- Update data flow section with selector cache and email polling

### `CLAUDE.md`
Update conventions:

- Add kernel module descriptions to Key Modules section
- Update File Interaction Map with new modules
- Add convention: "Handler functions return StepResult, never advance workflow state"
- Add convention: "New element intents go in selector_cache bootstrap, not hardcoded in handlers"
- Update Execution Flow with kernel state machine
- Add email poller to config section

### `LEARNINGS.md`
Add new sections:

- **Kernel States**: Common state transition patterns, when CAPTCHA resume works vs doesn't
- **Selector Cache**: Which ATS platforms change selectors frequently, confidence threshold tuning
- **Email Polling**: OTP patterns per ATS (Workday format vs Greenhouse format vs iCIMS format)

### `TODO.md`
- Mark completed: Automation kernel refactor, selector cache, email polling
- Add Phase 6 items: ATS account creation, identity management
- Add future items: Gmail API upgrade, folder restructure consideration, forms.py split

### `config/settings.example.yaml`
Final pass to ensure ALL new settings from Phases 1-4 are documented:

```yaml
automation:
  # ... existing settings ...

  # Element finder (Phase 3)
  element_finder_llm: true        # Allow LLM fallback in element finder (levels 5-6)

  # Email polling (Phase 4)
  email_polling: false
  imap_server: imap.gmail.com
  imap_port: 993
  email_poll_timeout: 120
```

### `.vscode/launch.json`
Verify all existing launch configs still work with the kernel architecture. No new commands were added in Phases 1-4, so existing configs should be fine.

---

## End-to-End Verification Checklist

### 1. Full Pipeline
```bash
python -m src pipeline
```
- [ ] Scrape completes (jobs inserted to DB)
- [ ] Tailor completes (resume/cover letter generated)
- [ ] Apply runs through kernel (state transitions logged)
- [ ] Results match expected: applied/failed/needs_login per job

### 2. LinkedIn Easy Apply
- [ ] Kernel routes to FILL_SELECTOR strategy
- [ ] Multi-step modal form fills correctly
- [ ] Share Profile modal handled
- [ ] Submit succeeds or fails gracefully

### 3. External ATS (Vision Agent)
- [ ] Kernel routes to FILL_VISION strategy
- [ ] Vision agent batch actions execute
- [ ] Form completion in 3-5 rounds
- [ ] Submit and verification work

### 4. CAPTCHA Site
- [ ] Kernel transitions to SOLVE_CAPTCHA
- [ ] 2Captcha API called (if captcha_solving enabled)
- [ ] On success: kernel resumes pre-CAPTCHA state
- [ ] On failure: kernel routes to CLEANUP with failed_captcha status

### 5. Login-Gated Site
- [ ] Kernel transitions to RECOVER_LOGIN
- [ ] Stored cookies tried first
- [ ] Manual login fallback works (if manual_login enabled)
- [ ] On recovery: kernel retries from NAVIGATE
- [ ] On failure: job marked needs_login

### 6. Selector Cache
- [ ] Cache table populated after first run
- [ ] Second run shows cache hits (Level 1) in logs
- [ ] Deliberately break a selector → verify rediscovery at Level 2-4
- [ ] Verify cache updates after rediscovery

### 7. Email Polling (if configured)
- [ ] OTP email detected and code extracted
- [ ] OTP filled into form field
- [ ] Fallback to manual prompt when poller fails
- [ ] Timeout handled gracefully

### 8. Regression
- [ ] No new files in git that should be gitignored
- [ ] `data/` directory contents not committed
- [ ] Debug screenshots saved on failures
- [ ] Console output uses ASCII only (no unicode)

---

## Code Cleanup Tasks

1. **Remove dead imports:** Check all files for imports of deleted `flow.py` functions
2. **Remove compatibility shims:** Any code with comments like "TODO: remove after kernel migration"
3. **Consolidate logging:** Ensure kernel state transitions use consistent log format
4. **Review handler signatures:** All handlers should accept `KernelContext`, not individual params
5. **Check for orphaned constants:** Any selectors.py constants not mapped to an intent in `SELECTOR_INTENTS`

---

## Dependencies
- **Phases 1-4** must all be complete and tested individually

## Estimated Scope
- ~0 lines new code
- ~200 lines deleted (flow.py)
- ~300 lines modified (docs, settings)
- Primary effort: testing and validation
