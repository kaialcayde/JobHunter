# Phase 3: Selector Cache + Element Finder

## Goal
Replace static selector constants with adaptive memory that learns from success/failure. Formalize the element-finding escalation chain so Playwright deterministic actions are ALWAYS tried first, and LLM is the absolute last resort.

## Why This Phase
The current `selectors.py` has hardcoded constants duplicated between Playwright and JS variants. When an ATS updates their UI, selectors break silently. The selector cache remembers what worked, forgets what didn't, and only escalates to expensive LLM calls when all deterministic methods fail.

## LLM Minimization Strategy

The escalation pipeline is explicitly designed to minimize LLM usage:

```
Level 1: Selector cache          — FREE (SQLite lookup, ~1ms)
Level 2: Heuristic selectors     — FREE (hardcoded patterns, ~5ms)
Level 3: Accessibility roles     — FREE (Playwright get_by_role, ~10ms)
Level 4: Visible text scan       — FREE (JS evaluation, ~20ms)
Level 5: Text LLM               — CHEAP (DOM snippet to haiku-class model, ~500ms, ~100 tokens)
Level 6: Vision LLM             — EXPENSIVE (screenshot to GPT-4o, ~2s, ~1000+ tokens)
```

**Levels 1-4 are always tried first.** Level 5-6 only fire when deterministic methods all fail. Once an LLM-discovered selector succeeds, it's cached at Level 1 for all future uses on that domain. So the LLM cost is paid ONCE per domain+intent, then it's free forever.

**Expected LLM usage per job application:**
- Known ATS (Workday, Greenhouse, LinkedIn): 0 LLM calls for element finding (cache hits)
- First encounter with new ATS: 1-3 LLM calls (then cached)
- Broken/changed selectors: 1 LLM call per broken selector (then re-cached)

---

## New Files

### `src/automation/selector_cache.py`

SQLite-backed adaptive selector memory.

```python
class SelectorCache:
    """Adaptive selector memory with confidence decay.

    Selectors that work get reinforced. Selectors that fail decay.
    Below confidence threshold, the cache yields to rediscovery.
    """

    TABLE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS selector_cache (
        domain TEXT NOT NULL,
        intent TEXT NOT NULL,        -- e.g. "apply_button", "next_button", "submit_button"
        selector_value TEXT NOT NULL, -- CSS selector, text content, or aria query
        selector_type TEXT NOT NULL,  -- "css", "text", "role", "xpath"
        last_success TEXT,
        confidence REAL DEFAULT 0.8,
        failure_count INTEGER DEFAULT 0,
        created_at TEXT,
        updated_at TEXT,
        PRIMARY KEY (domain, intent)
    )
    """

    CONFIDENCE_THRESHOLD = 0.3    # below this, skip cache and rediscover
    DECAY_FACTOR = 0.7            # multiply confidence on each failure
    AGE_DECAY_DAYS = 30           # halve confidence after this many days

    def get_selector(self, domain: str, intent: str) -> tuple[str, str, float] | None:
        """Returns (selector_value, selector_type, confidence) or None."""
        # Apply age decay if older than AGE_DECAY_DAYS
        # Return None if confidence < CONFIDENCE_THRESHOLD

    def record_success(self, domain: str, intent: str, value: str, type: str):
        """Reset confidence to 1.0, update timestamp."""

    def record_failure(self, domain: str, intent: str):
        """Multiply confidence by DECAY_FACTOR, increment failure_count."""

    def bootstrap_from_selectors(self):
        """Seed cache from selectors.py constants on first run."""
        # Maps APPLY_BUTTON_PW_SELECTORS -> intent "apply_button"
        # Maps NEXT_BUTTON_PW_SELECTORS -> intent "next_button"
        # etc. Initial confidence = 0.8 (not 1.0, since these are generic)

    def export_sanitized(self) -> list[dict]:
        """Export cache without sensitive data (for git-safe sharing)."""
```

### `src/automation/element_finder.py`

The 6-level escalation pipeline. Each level returns on first success.

```python
@dataclass
class ElementResult:
    element: Locator | ElementHandle | None
    selector_used: str
    selector_type: str   # "cache", "heuristic", "role", "text", "llm_text", "llm_vision"
    confidence: float
    method_level: int    # 1-6, which level found it

class ElementFinder:
    """Find page elements using escalating strategies.

    Deterministic methods (levels 1-4) are always tried first.
    LLM methods (levels 5-6) are last resort and results get cached.
    """

    def __init__(self, cache: SelectorCache, settings: dict):
        self.cache = cache
        self.settings = settings
        self.llm_enabled = settings.get("automation", {}).get("element_finder_llm", True)

    async def find_element(self, page, intent: str, domain: str = None,
                           hints: list[str] = None) -> ElementResult | None:
        """Find an element matching the intent using escalation.

        Args:
            page: Playwright page
            intent: What we're looking for ("apply_button", "next_button", etc.)
            domain: Site domain for cache lookup
            hints: Optional text hints (button text, aria labels)
        """
        domain = domain or self._extract_domain(page.url)

        for level, finder in enumerate([
            self._find_from_cache,      # Level 1
            self._find_by_heuristic,    # Level 2
            self._find_by_role,         # Level 3
            self._find_by_text,         # Level 4
            self._find_by_llm_text,     # Level 5
            self._find_by_llm_vision,   # Level 6
        ], start=1):
            result = await finder(page, intent, domain, hints)
            if result:
                # Cache successful discovery for future use
                self.cache.record_success(domain, intent, result.selector_used, result.selector_type)
                result.method_level = level
                return result

            # Record cache miss/failure if applicable
            if level == 1:
                self.cache.record_failure(domain, intent)

        return None  # All levels exhausted

    async def _find_from_cache(self, page, intent, domain, hints) -> ElementResult | None:
        """Level 1: Look up cached selector for this domain+intent."""
        cached = self.cache.get_selector(domain, intent)
        if not cached:
            return None
        value, type_, confidence = cached
        # Try the cached selector
        element = await self._try_selector(page, value, type_)
        if element:
            return ElementResult(element, value, "cache", confidence, 1)
        return None

    async def _find_by_heuristic(self, page, intent, domain, hints) -> ElementResult | None:
        """Level 2: Hardcoded patterns (button[type=submit], common class names)."""
        # Uses HEURISTIC_MAP: intent -> list of (selector, type) tuples
        # These are the "obvious" selectors that work across most sites

    async def _find_by_role(self, page, intent, domain, hints) -> ElementResult | None:
        """Level 3: Playwright accessibility role queries."""
        # page.get_by_role("button", name="Apply")
        # page.get_by_role("button", name="Next")
        # page.get_by_role("button", name="Submit")
        # Uses ROLE_MAP: intent -> (role, name_pattern)

    async def _find_by_text(self, page, intent, domain, hints) -> ElementResult | None:
        """Level 4: Scan visible text content via JS evaluation."""
        # Single page.evaluate() call that finds clickable elements
        # matching text patterns for the given intent

    async def _find_by_llm_text(self, page, intent, domain, hints) -> ElementResult | None:
        """Level 5: Send DOM snippet to cheap LLM, ask for selector.

        Only fires if self.llm_enabled is True.
        Uses haiku-class model for cost efficiency (~100 tokens).
        """
        if not self.llm_enabled:
            return None
        # Extract simplified DOM (tag names, ids, classes, text content)
        # Send to LLM: "Given this DOM, what CSS selector targets the {intent}?"
        # Parse response, try the selector
        # Cost: ~$0.0001 per call

    async def _find_by_llm_vision(self, page, intent, domain, hints) -> ElementResult | None:
        """Level 6: Screenshot + vision LLM to find element coordinates.

        Last resort. Only fires if self.llm_enabled is True.
        Uses GPT-4o-mini with low detail for cost efficiency.
        """
        if not self.llm_enabled:
            return None
        # Take screenshot, send to vision model
        # "Click on the {intent} button. Return coordinates."
        # Use elementFromPoint to find the actual element
        # Build a selector from the found element for caching
        # Cost: ~$0.001 per call
```

---

## Modified Files

### `src/db.py`
Add `selector_cache` table to `_create_tables()`:

```python
def _create_tables(conn):
    # ... existing tables ...
    conn.execute(SelectorCache.TABLE_SCHEMA)
```

### `src/automation/detection.py`
Rewrite button-clicking functions to use `ElementFinder`:

```python
# Before (current):
async def click_apply_button(page, settings):
    for selector in APPLY_BUTTON_PW_SELECTORS:
        try:
            btn = page.locator(selector)
            if await btn.count() > 0:
                await btn.first.click()
                return True
        except:
            continue
    # ... 50 more lines of fallback logic ...

# After:
async def click_apply_button(page, settings, finder: ElementFinder):
    result = await finder.find_element(page, "apply_button")
    if result and result.element:
        await result.element.click()
        return True
    return False
```

Similarly for `click_next_button()`, `click_submit_button()`.

### `src/automation/selectors.py`
Constants remain for backward compatibility and bootstrap data. Add intent mappings:

```python
SELECTOR_INTENTS = {
    "apply_button": {
        "pw_selectors": APPLY_BUTTON_PW_SELECTORS,
        "js_selectors": APPLY_BUTTON_JS_SELECTORS,
        "texts": APPLY_BUTTON_TEXTS,
    },
    "next_button": {
        "pw_selectors": NEXT_BUTTON_PW_SELECTORS,
        "texts": NEXT_BUTTON_TEXTS,
    },
    # ... etc
}
```

### `src/automation/handlers.py`
Handlers that click buttons receive an `ElementFinder` instance via `KernelContext` (Phase 2). The kernel creates the finder with the shared cache.

---

## Confidence Mechanics Examples

**Scenario: Greenhouse updates their Apply button class**
1. First job on greenhouse.io: cache returns `.btn-apply` with confidence 0.8
2. Selector fails (class was renamed): confidence drops to 0.56 (0.8 * 0.7)
3. Heuristic `button[type="submit"]` also fails: try Level 3
4. `page.get_by_role("button", name="Apply")` succeeds
5. Cache updated: `("greenhouse.io", "apply_button", "role:button[name=Apply]", "role", 1.0)`
6. Next Greenhouse job: cache hit at Level 1, confidence 1.0

**Scenario: First time on unknown ATS**
1. No cache entry for `unknownats.com`
2. Heuristic `button:has-text("Apply")` succeeds at Level 2
3. Cache seeded: `("unknownats.com", "apply_button", "button:has-text('Apply')", "css", 1.0)`
4. All future jobs on this domain: cache hit

**Scenario: Stale selector (not used in 30 days)**
1. `oldats.com` has cached selector, last used 45 days ago
2. On next access, confidence halved: 1.0 → 0.5
3. Selector tried: still works → confidence reset to 1.0
4. If it failed: confidence would be 0.35 (0.5 * 0.7), still above threshold
5. Second failure: 0.245, below 0.3 → cache skipped, full rediscovery

---

## Heuristic Map (Level 2)

```python
HEURISTIC_MAP = {
    "apply_button": [
        ("button:has-text('Apply')", "css"),
        ("a:has-text('Apply')", "css"),
        ("button[type='submit']:visible", "css"),
        ("[data-testid*='apply']", "css"),
    ],
    "next_button": [
        ("button:has-text('Next')", "css"),
        ("button:has-text('Continue')", "css"),
        ("button[aria-label*='next' i]", "css"),
    ],
    "submit_button": [
        ("button:has-text('Submit')", "css"),
        ("button[type='submit']", "css"),
        ("button:has-text('Send application')", "css"),
    ],
    "email_field": [
        ("input[type='email']", "css"),
        ("input[name*='email' i]", "css"),
        ("input[autocomplete='email']", "css"),
    ],
    "password_field": [
        ("input[type='password']", "css"),
        ("input[name*='password' i]", "css"),
    ],
    "file_upload": [
        ("input[type='file']", "css"),
        ("[data-testid*='upload']", "css"),
    ],
}
```

---

## Testing Strategy

1. **Unit test selector_cache.py:** Insert, query, decay, bootstrap. Pure SQLite, no browser needed.
2. **Unit test element_finder.py:** Mock page that responds to specific selectors. Verify escalation order (level 1 tried first, level 6 last).
3. **Integration test:** Run apply pipeline on 5 jobs. Verify:
   - Cache gets populated after first run
   - Second run has more Level 1 hits (cache hits)
   - LLM calls decrease over time
4. **Regression test:** Verify detection.py button-click functions still work identically.

---

## Dependencies
- **Phase 2** (kernel provides `KernelContext` with finder instance)
- Can be partially developed in parallel with Phase 2 (cache + finder are independent modules)

## Estimated Scope
- ~250 lines new code (selector_cache.py)
- ~300 lines new code (element_finder.py)
- ~150 lines modified (detection.py, selectors.py, db.py, handlers.py)
