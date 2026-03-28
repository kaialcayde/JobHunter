"""SQLite-backed adaptive selector memory with confidence decay.

Selectors that work get reinforced. Selectors that fail decay.
Below confidence threshold, the cache yields to rediscovery.
"""

import logging
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)

TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS selector_cache (
    domain TEXT NOT NULL,
    intent TEXT NOT NULL,
    selector_value TEXT NOT NULL,
    selector_type TEXT NOT NULL,
    last_success TEXT,
    confidence REAL DEFAULT 0.8,
    failure_count INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (domain, intent)
)
"""

CONFIDENCE_THRESHOLD = 0.3
DECAY_FACTOR = 0.7
AGE_DECAY_DAYS = 30


class SelectorCache:
    """Adaptive selector memory with confidence decay.

    Selectors that work get reinforced. Selectors that fail decay.
    Below confidence threshold, the cache yields to rediscovery.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_selector(self, domain: str, intent: str) -> tuple[str, str, float] | None:
        """Returns (selector_value, selector_type, confidence) or None.

        Checks domain-specific entry first, then falls back to wildcard ('*').
        Applies age decay if the entry hasn't been used recently.
        """
        for d in [domain, "*"]:
            row = self.conn.execute(
                "SELECT selector_value, selector_type, confidence, last_success "
                "FROM selector_cache WHERE domain = ? AND intent = ?",
                (d, intent)
            ).fetchone()
            if not row:
                continue

            confidence = row["confidence"]

            # Apply age decay if older than AGE_DECAY_DAYS
            if row["last_success"]:
                last = datetime.fromisoformat(row["last_success"])
                age_days = (datetime.now() - last).total_seconds() / 86400
                if age_days > AGE_DECAY_DAYS:
                    decay_periods = age_days / AGE_DECAY_DAYS
                    confidence *= 0.5 ** decay_periods
                    self.conn.execute(
                        "UPDATE selector_cache SET confidence = ?, updated_at = ? "
                        "WHERE domain = ? AND intent = ?",
                        (confidence, datetime.now().isoformat(), d, intent)
                    )
                    self.conn.commit()

            if confidence < CONFIDENCE_THRESHOLD:
                continue

            return (row["selector_value"], row["selector_type"], confidence)

        return None

    def record_success(self, domain: str, intent: str, value: str, selector_type: str):
        """Upsert selector with confidence reset to 1.0."""
        now = datetime.now().isoformat()
        self.conn.execute("""
            INSERT INTO selector_cache
                (domain, intent, selector_value, selector_type,
                 last_success, confidence, failure_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1.0, 0, ?, ?)
            ON CONFLICT(domain, intent) DO UPDATE SET
                selector_value = ?, selector_type = ?, last_success = ?,
                confidence = 1.0, failure_count = 0, updated_at = ?
        """, (domain, intent, value, selector_type, now, now, now,
              value, selector_type, now, now))
        self.conn.commit()

    def record_failure(self, domain: str, intent: str):
        """Multiply confidence by DECAY_FACTOR, increment failure_count."""
        self.conn.execute("""
            UPDATE selector_cache
            SET confidence = confidence * ?,
                failure_count = failure_count + 1,
                updated_at = ?
            WHERE domain = ? AND intent = ?
        """, (DECAY_FACTOR, datetime.now().isoformat(), domain, intent))
        self.conn.commit()

    def bootstrap_from_selectors(self):
        """Seed cache from selectors.py constants on first run.

        Only seeds wildcard ('*') entries. Domain-specific entries are
        learned through actual usage. Initial confidence is 0.8 (not 1.0,
        since these are generic patterns, not domain-verified).
        """
        from .selectors import SELECTOR_INTENTS

        now = datetime.now().isoformat()
        seeded = 0
        for intent, data in SELECTOR_INTENTS.items():
            # Check if any wildcard entry exists for this intent
            existing = self.conn.execute(
                "SELECT 1 FROM selector_cache WHERE domain = '*' AND intent = ?",
                (intent,)
            ).fetchone()
            if existing:
                continue

            # Seed the first (highest priority) PW selector
            pw_selectors = data.get("pw_selectors", [])
            if pw_selectors:
                self.conn.execute("""
                    INSERT OR IGNORE INTO selector_cache
                        (domain, intent, selector_value, selector_type,
                         confidence, created_at, updated_at)
                    VALUES ('*', ?, ?, 'css', 0.8, ?, ?)
                """, (intent, pw_selectors[0], now, now))
                seeded += 1

        self.conn.commit()
        if seeded:
            logger.info(f"Selector cache bootstrapped with {seeded} entries")
        return seeded

    def export_sanitized(self) -> list[dict]:
        """Export cache without sensitive data (for git-safe sharing)."""
        rows = self.conn.execute(
            "SELECT domain, intent, selector_value, selector_type, "
            "confidence, failure_count FROM selector_cache"
        ).fetchall()
        return [dict(r) for r in rows]
