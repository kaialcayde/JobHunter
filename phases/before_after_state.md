# Before vs After State

## Architecture: Before

```
src/automation/
├── applicant.py          # Batch orchestrator, calls flow.apply_single_job()
├── flow.py               # 425-line MONOLITH: state machine + DB writes + error handling all in one
├── detection.py           # Button clicking with hardcoded selector lists
├── forms.py              # 42KB: extraction + filling + React handling + coord fallback
├── page_checks.py        # Returns mixed bool/string, login recovery
├── vision_agent.py       # GPT-4o form filling, standalone
├── captcha_solver.py     # 2Captcha integration, standalone
├── selectors.py          # Static constants, duplicated between PW and JS variants
└── platforms/
    └── linkedin.py       # LinkedIn Easy Apply, SDUI, Share Profile
```

### Control Flow: Before
```
applicant.py
  └── flow.apply_single_job()      ← EVERYTHING happens here
        ├── setup (local vars)
        ├── page.goto()
        ├── check_page_blockers()   ← returns (bool, str), mixed types
        ├── try_solve_captcha()     ← inline in flow
        ├── try_recover_login()     ← inline in flow
        ├── click_apply_button()    ← iterates hardcoded selectors
        ├── if vision: run_vision_agent()
        │   else: extract_fields() + infer_answers() + fill_fields() loop
        ├── click_submit_button()   ← iterates hardcoded selectors
        ├── DB writes (status, log)  ← mixed into every branch
        └── directory moves          ← mixed into cleanup
```

**Problems:**
- State transitions are implicit (if/elif chains, try/except nesting)
- DB writes scattered across 10+ locations in the function
- Adding a new state (e.g., "registration wall") requires modifying the monolith
- No structured results — functions return mixed types (bool, str, tuple, None)
- Selectors are static — when an ATS updates their UI, selectors break silently
- OTP handling requires manual terminal input (`input()` call)
- Registration walls → all marked `needs_login`, requires manual intervention
- No learning from success/failure on selectors
- LLM used for vision agent and form answers, but no LLM for element discovery

### Data Flow: Before
```
Config (YAML) → Scraper → DB (status=new) → Tailoring → DB (status=tailored)
  → flow.py MONOLITH → DB (status=applied/failed/needs_login)

Selectors: selectors.py (static file) → detection.py (hardcoded iteration)
Sessions: data/site_auth/{domain}.json (manual login only)
OTP: manual terminal prompt (input())
ATS accounts: not handled (all registration walls → needs_login)
```

---

## Architecture: After (Phase 5 Complete)

```
src/automation/
├── applicant.py          # Batch orchestrator, calls kernel.run()
├── kernel.py             # NEW: State machine, transition table, handler dispatch
├── handlers.py           # NEW: 7+ stateless handler functions
├── results.py            # NEW: HandlerResult enum + StepResult dataclass
├── selector_cache.py     # NEW: SQLite-backed adaptive selector memory
├── element_finder.py     # NEW: 6-level escalation pipeline
├── email_poller.py       # NEW: IMAP-based OTP/magic link extraction
├── detection.py          # MODIFIED: Uses element_finder instead of hardcoded selectors
├── forms.py              # MODIFIED: Uses element_finder for field discovery
├── page_checks.py        # MODIFIED: Returns StepResult, registration wall detection
├── vision_agent.py       # UNCHANGED: Called by handle_fill_vision handler
├── captcha_solver.py     # UNCHANGED: Called by handle_solve_captcha handler
├── selectors.py          # MODIFIED: Bootstrap data for selector cache
└── platforms/
    └── linkedin.py       # UNCHANGED
```

### Control Flow: After
```
applicant.py
  └── ApplicationKernel.run()
        ├── State.SETUP         → handle_setup()         → StepResult
        ├── State.NAVIGATE      → handle_navigate()      → StepResult
        ├── State.ROUTE         → handle_route()          → StepResult
        ├── State.DETECT_STRAT  → _detect_strategy()     → StepResult (LLM injection point)
        ├── State.FILL_*        → handle_fill_*()         → StepResult
        ├── State.VERIFY        → handle_verify()         → StepResult
        ├── State.CLEANUP       → handle_cleanup()        → StepResult (ALL DB writes here)
        │
        ├── CAPTCHA_DETECTED    → State.SOLVE_CAPTCHA    → resume pre-captcha state
        ├── REQUIRES_LOGIN      → State.RECOVER_LOGIN    → retry NAVIGATE
        └── REQUIRES_VERIF      → State.VERIFY_EMAIL     → email_poller → retry NAVIGATE
```

### Data Flow: After
```
Config (YAML) → Scraper → DB (status=new) → Tailoring → DB (status=tailored)
  → Kernel (explicit state machine) → DB (status=applied/failed/needs_login)

Selectors: selector_cache (SQLite, adaptive) ← bootstrapped from selectors.py
  → element_finder (6-level escalation: cache → heuristic → ARIA → text → LLM text → LLM vision)
  → discovered selectors cached for future use

Sessions: data/site_auth/{domain}.json (manual + automatic post-login save)
OTP: email_poller (IMAP) → fallback to manual terminal prompt
ATS accounts: (Phase 6) account_registry.db (encrypted) → auto-register on allowlisted domains
```

---

## Architecture: After (Phase 6 Complete)

```
src/automation/
├── (everything from Phase 5)
├── account_registry.py   # NEW: Encrypted credential store + registration utilities
└── handlers_account.py   # NEW: detect_auth_type, register, verify_registration handlers
```

### Control Flow: After Phase 6
```
applicant.py
  └── ApplicationKernel.run()
        ├── ... (same as Phase 5) ...
        │
        ├── REQUIRES_LOGIN      → State.DETECT_AUTH_TYPE
        │   ├── login wall + has credentials → RECOVER_LOGIN → retry NAVIGATE
        │   ├── login wall + no credentials  → CLEANUP (needs_login)
        │   └── registration wall + domain allowed → REGISTER
        │       → VERIFY_REGISTRATION (email_poller) → NAVIGATE (retry with new session)
        │
        └── REQUIRES_REGISTRATION → State.REGISTER → ...
```

---

## Side-by-Side Comparison

| Aspect | Before | After |
|--------|--------|-------|
| **State machine** | Implicit (if/elif in 425-line function) | Explicit transition table in kernel.py |
| **Handler results** | Mixed types (bool, str, tuple, None) | `StepResult` dataclass with `HandlerResult` enum |
| **DB writes** | Scattered across 10+ locations | All in kernel CLEANUP state |
| **Selectors** | Static constants in selectors.py | Adaptive cache with confidence decay |
| **Element discovery** | Hardcoded list iteration | 6-level escalation (4 free + 2 LLM fallback) |
| **LLM for elements** | Never (only vision agent) | Last resort (levels 5-6), cached after discovery |
| **LLM cost** | ~$0.01-0.05 per job (vision agent) | Same for known ATS; +$0.001 per new selector discovery |
| **CAPTCHA handling** | Inline in flow.py | Kernel state: pause → solve → resume |
| **Login recovery** | Inline in flow.py, mixed returns | Kernel state: detect → recover → retry |
| **OTP handling** | Manual terminal prompt | Email poller (IMAP) → manual fallback |
| **Registration walls** | All → `needs_login` | Auto-register on allowlisted domains |
| **Credentials** | Not managed | Encrypted local registry, secure fill |
| **Selector learning** | None | Success → reinforce, failure → decay → rediscover |
| **Debug screenshots** | Some handlers, inconsistent | Every non-SUCCESS result, kernel-enforced |
| **State history** | Log file only | application_log table, full state trace |
| **Testability** | Mock entire flow.py function | Mock individual handlers, test kernel transitions |

---

## File Count

| | Before | After (Phase 5) | After (Phase 6) |
|---|--------|-----------------|-----------------|
| Files in `src/automation/` | 10 | 15 (+5 new, -1 deleted) | 17 (+2 new) |
| Lines of code (est.) | ~3500 | ~4200 | ~4850 |
| Net new lines | — | ~700 | ~1350 |

---

## What Stays the Same

These modules are battle-tested and unchanged:
- `captcha_solver.py` — 2Captcha integration works, just called from a handler now
- `vision_agent.py` — GPT-4o batch actions work, called from handle_fill_vision
- `platforms/linkedin.py` — LinkedIn quirks unchanged, called from handlers
- `forms.py` — Field extraction/filling unchanged (element_finder is for buttons/UI elements, forms.py is for form fields)
- Core business logic: `scraper.py`, `tailoring.py`, `document.py` — completely untouched

## What Gets Deleted
- `flow.py` — The monolith. Replaced by kernel + handlers. Deleted in Phase 5.

## New Dependencies
- `cryptography` (Phase 6 only, for Fernet encryption of passwords)
- No other new packages — IMAP is stdlib (`imaplib`), SQLite is stdlib
