"""6-level escalation pipeline for finding page elements.

Deterministic methods (levels 1-4) are always tried first.
LLM methods (levels 5-6) are last resort and results get cached.

Level 1: Selector cache          -- FREE (SQLite lookup, ~1ms)
Level 2: Heuristic selectors     -- FREE (hardcoded patterns, ~5ms)
Level 3: Accessibility roles     -- FREE (Playwright get_by_role, ~10ms)
Level 4: Visible text scan       -- FREE (JS evaluation, ~20ms)
Level 5: Text LLM               -- CHEAP (DOM snippet to haiku-class model)
Level 6: Vision LLM             -- EXPENSIVE (screenshot to GPT-4o)
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

from .browser_scripts import evaluate_script
from .selector_cache import SelectorCache

logger = logging.getLogger(__name__)


# ── Heuristic Map (Level 2) ─────────────────────────────────────────
# Specific CSS/attribute selectors that work across most sites.
# Ordered by reliability within each intent.

HEURISTIC_MAP: dict[str, list[str]] = {
    "apply_button": [
        '[data-testid*="apply"]',
        ".apply-button",
        "#apply-button",
        ".js-btn-apply",
        '[data-testid*="interest"]',
        'button:has-text("Apply Now")',
        'button:has-text("Apply")',
        'a:has-text("Apply Now")',
        'a:has-text("Apply")',
        "button:has-text(\"I'm interested\")",
        'button:has-text("Start application")',
    ],
    "next_button": [
        'button[aria-label="Continue to next step"]',
        'button[aria-label="Next"]',
        'button[aria-label="Review your application"]',
        'button[aria-label="Review"]',
        'button[data-automation-id="bottom-navigation-next-button"]',
        '[data-testid*="next"]',
        'button:has-text("Next")',
        'button:has-text("Continue")',
    ],
    "submit_button": [
        'button[aria-label="Submit application"]',
        'button[aria-label="Submit"]',
        "#submit_app",
        "#submit-application",
        'button[data-automation-id="submit"]',
        ".posting-btn-submit",
        "button.postings-btn",
        ".iCIMS_Button",
        "button.btn-submit",
        '[data-testid*="submit"]',
        '[data-testid*="apply"]',
        'input[type="submit"]',
        'button[type="submit"]',
        'button:has-text("Submit")',
        'button:has-text("Submit application")',
    ],
    "email_field": [
        'input[type="email"]',
        'input[name*="email" i]',
        'input[autocomplete="email"]',
    ],
    "password_field": [
        'input[type="password"]',
        'input[name*="password" i]',
    ],
    "file_upload": [
        'input[type="file"]',
        '[data-testid*="upload"]',
    ],
}


# ── Role Map (Level 3) ──────────────────────────────────────────────
# Playwright accessibility role queries: (role, name_pattern)

ROLE_MAP: dict[str, list[tuple[str, str]]] = {
    "apply_button": [
        ("button", "Apply"),
        ("link", "Apply"),
        ("button", "Apply Now"),
        ("link", "Apply Now"),
    ],
    "next_button": [
        ("button", "Next"),
        ("button", "Continue"),
        ("button", "Review"),
        ("button", "Continue to next step"),
        ("button", "Review your application"),
    ],
    "submit_button": [
        ("button", "Submit application"),
        ("button", "Submit"),
        ("button", "Send application"),
        ("button", "Apply"),
        ("button", "Complete"),
        ("button", "Done"),
    ],
}


# ── Text Patterns (Level 4) ─────────────────────────────────────────
# Broad text-content matching via JS evaluation.

TEXT_PATTERNS: dict[str, list[str]] = {
    "apply_button": [
        "apply now", "apply", "apply for this job",
        "apply for this position", "i'm interested",
        "submit application", "start application",
    ],
    "next_button": ["next", "continue", "review"],
    "submit_button": [
        "submit application", "submit", "send application",
        "apply", "complete", "finish", "done",
    ],
}


@dataclass
class ElementResult:
    """Result from the element-finding pipeline."""
    element: Any          # Playwright Locator
    selector_used: str    # The selector/pattern that matched
    selector_type: str    # "cache", "heuristic", "role", "text", "llm_text", "llm_vision"
    confidence: float     # 0.0-1.0
    method_level: int     # 1-6, which level found it


class ElementFinder:
    """Find page elements using escalating strategies.

    Deterministic methods (levels 1-4) are always tried first.
    LLM methods (levels 5-6) are last resort and results get cached.
    """

    def __init__(self, cache: SelectorCache, settings: dict):
        self.cache = cache
        self.settings = settings
        self.llm_enabled = settings.get("automation", {}).get("element_finder_llm", False)

    def find_element(self, page, intent: str, domain: str = None,
                     hints: list[str] | None = None) -> ElementResult | None:
        """Find an element matching the intent using escalation.

        Args:
            page: Playwright page (sync API)
            intent: What we're looking for ("apply_button", "next_button", etc.)
            domain: Site domain for cache lookup (auto-extracted if None)
            hints: Optional text hints (button text, aria labels)

        Returns:
            ElementResult with the found element, or None if all levels failed.
        """
        if domain is None:
            domain = self._extract_domain(page.url)

        finders = [
            (1, self._find_from_cache),
            (2, self._find_by_heuristic),
            (3, self._find_by_role),
            (4, self._find_by_text),
            (5, self._find_by_llm_text),
            (6, self._find_by_llm_vision),
        ]

        cache_tried = False
        for level, finder in finders:
            result = finder(page, intent, domain, hints)
            if result:
                result.method_level = level
                # Cache successful discovery for future use (skip if already from cache)
                if level > 1:
                    self.cache.record_success(
                        domain, intent, result.selector_used, result.selector_type
                    )
                logger.debug(
                    f"ElementFinder: {intent} on {domain} found at level {level} "
                    f"({result.selector_type}: {result.selector_used[:60]})"
                )
                return result

            # Record cache miss on level 1 failure
            if level == 1:
                cache_tried = True

        # All levels exhausted -- record failure if cache had an entry
        if cache_tried:
            self.cache.record_failure(domain, intent)

        logger.debug(f"ElementFinder: {intent} on {domain} not found (all levels exhausted)")
        return None

    # --- Level 1: Selector Cache ---

    def _find_from_cache(self, page, intent: str, domain: str,
                         hints: list[str] | None) -> ElementResult | None:
        """Level 1: Look up cached selector for this domain+intent."""
        cached = self.cache.get_selector(domain, intent)
        if not cached:
            return None
        value, type_, confidence = cached
        element = self._try_selector(page, value)
        if element:
            return ElementResult(element, value, "cache", confidence, 1)
        return None

    # --- Level 2: Heuristic Selectors ---

    def _find_by_heuristic(self, page, intent: str, domain: str,
                           hints: list[str] | None) -> ElementResult | None:
        """Level 2: Hardcoded patterns (aria-labels, data-testids, classes)."""
        selectors = HEURISTIC_MAP.get(intent, [])
        for selector in selectors:
            element = self._try_selector(page, selector)
            if element:
                return ElementResult(element, selector, "heuristic", 0.9, 2)
        return None

    # --- Level 3: Accessibility Roles ---

    def _find_by_role(self, page, intent: str, domain: str,
                      hints: list[str] | None) -> ElementResult | None:
        """Level 3: Playwright accessibility role queries."""
        roles = ROLE_MAP.get(intent, [])
        for role, name in roles:
            try:
                loc = page.get_by_role(role, name=name, exact=False).first
                if loc.is_visible(timeout=300):
                    selector = f'role={role}[name="{name}"]'
                    return ElementResult(loc, selector, "role", 0.85, 3)
            except Exception:
                continue
        return None

    # --- Level 4: Visible Text Scan ---

    def _find_by_text(self, page, intent: str, domain: str,
                      hints: list[str] | None) -> ElementResult | None:
        """Level 4: Scan visible text content via JS evaluation.

        Searches within modal scope when a dialog is present (LinkedIn Easy Apply).
        Returns a Playwright locator built from the matched element's attributes.
        """
        patterns = TEXT_PATTERNS.get(intent, [])
        if not patterns:
            return None

        match_info = evaluate_script(page, "element_finder/find_by_text.js", patterns)

        if not match_info:
            return None

        selector = match_info["selector"]
        element = self._try_selector(page, selector)
        if element:
            return ElementResult(element, selector, "text", 0.8, 4)
        return None

    # --- Level 5: Text LLM (stub) ---

    def _find_by_llm_text(self, page, intent: str, domain: str,
                          hints: list[str] | None) -> ElementResult | None:
        """Level 5: Send DOM snippet to cheap LLM, ask for selector.

        Only fires if element_finder_llm is enabled in settings.
        Uses haiku-class model for cost efficiency (~100 tokens).
        """
        if not self.llm_enabled:
            return None
        # Future: extract simplified DOM, send to LLM, parse selector
        logger.debug(f"ElementFinder level 5 (LLM text) not yet implemented for {intent}")
        return None

    # --- Level 6: Vision LLM (stub) ---

    def _find_by_llm_vision(self, page, intent: str, domain: str,
                            hints: list[str] | None) -> ElementResult | None:
        """Level 6: Screenshot + vision LLM to find element coordinates.

        Last resort. Only fires if element_finder_llm is enabled.
        """
        if not self.llm_enabled:
            return None
        # Future: screenshot -> vision model -> coordinates -> elementFromPoint -> selector
        logger.debug(f"ElementFinder level 6 (LLM vision) not yet implemented for {intent}")
        return None

    # --- Helpers ---

    def _try_selector(self, page, selector: str) -> Any | None:
        """Try a Playwright selector, return the Locator if visible."""
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=500):
                return loc
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract the main domain from a URL."""
        from .page_checks import get_site_domain
        return get_site_domain(url)
