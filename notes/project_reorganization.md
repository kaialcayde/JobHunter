# Project Reorganization Plan

## Why this needs a folder split

The intended architecture in `CLAUDE.md` is already directionally right, but the actual hotspots are still concentrated in a few large modules:

- `src/automation/vision_agent.py`
- `src/automation/platforms/avature.py`
- `src/automation/forms.py`
- `src/automation/handlers.py`
- `src/cli.py`

That shape creates three recurring problems:

1. Platform logic, generic automation logic, and browser-side JS are mixed together.
2. Large files hide duplicate helpers and dead patterns.
3. Inline `page.evaluate("""...""")` blocks make browser logic hard to reuse and review.

## Recommended target layout

```text
src/
  cli/
    __init__.py
    main.py
    commands/
      scrape.py
      tailor.py
      apply.py
      login.py
      jobs.py
      accounts.py

  automation/
    workflow/
      applicant.py
      kernel.py
      results.py

    handlers/
      setup.py
      navigation.py
      routing.py
      fill.py
      verify.py
      cleanup.py
      account.py

    forms/
      __init__.py
      dom.py
      extract.py
      fill.py
      uploads.py

    detection/
      blockers.py
      buttons.py
      page_checks.py
      element_finder.py
      selector_cache.py
      selectors.py

    vision/
      __init__.py
      prompts.py
      client.py
      actions.py
      submit.py
      otp.py
      loop.py
      avature_flow.py

    auth/
      account_registry.py
      captcha_solver.py
      email_poller.py

    platforms/
      linkedin/
        __init__.py
        apply.py
        modals.py
        diagnostics.py
      avature/
        __init__.py
        prefill.py
        select2.py
        fields.py
        registration.py

    browser_scripts/
      __init__.py
      loader.py
      forms/
      detection/
      platforms/
```

## Rules for the split

- Keep Python modules focused on one job. A file should usually stay under 250-350 lines unless it is mostly prompt text or static data.
- Do not create one giant `master.js`. Use `browser_scripts/` as the single access point, but keep the JS assets split by intent.
- Each `.js` asset should export one function expression that can be passed directly to `page.evaluate(script, args)`.
- Pass dynamic data through the `args` parameter, not string interpolation, so JS assets stay reusable and safer.
- Keep tiny one-line browser expressions inline. Move longer or reused DOM logic into `browser_scripts/`.
- During migration, preserve current public imports and use compatibility wrappers so the kernel and CLI do not churn all at once.

## First migration order

1. `forms.py`
   Move coordinate helpers, extraction, filling, and uploads into separate modules first. This file already contains four distinct concerns.
2. `vision_agent.py`
   Split prompts, OpenAI client calls, action execution, OTP handling, submit checks, and Avature-specific loop handling.
3. `handlers.py`
   Break by state-handler responsibility so `kernel.py` keeps importing a stable surface from `automation.handlers`.
4. `platforms/avature.py`
   Separate select2 helpers, field lookup, registration flow, and profile prefill.
5. `cli.py`
   Convert to a thin command router with one command module per action.

## Browser JS pattern

Recommended pattern:

```python
from .browser_scripts import load_script

find_input_js = load_script("forms/find_input_at_coords.js")
candidate = page.evaluate(find_input_js, {"x": x, "y": y})
```

Why this is better than a single `find_elements.js` catch-all:

- Browser scripts stay reviewable.
- Reuse is explicit.
- Platform-specific DOM code can live under `browser_scripts/platforms/`.
- Python keeps ownership of orchestration while JS owns only DOM inspection/manipulation.

## What should move to browser_scripts next

- `forms.extract_form_fields`
- `detection.detect_captcha`
- `detection.click_next_button`
- `detection.click_submit_button`
- Avature select2 DOM probes
- LinkedIn modal diagnostics

## What should stay in Python

- Kernel transitions
- database writes
- OpenAI calls
- answer inference
- retry policy
- file path handling
- cross-page orchestration
