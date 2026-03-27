"""SQLite database layer for tracking jobs and applications."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .utils import DATA_DIR, ensure_dirs

DB_PATH = DATA_DIR / "jobhunter.db"


def get_connection() -> sqlite3.Connection:
    """Get a database connection, creating tables if needed."""
    ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT,
            url TEXT,
            description TEXT,
            salary_min REAL,
            salary_max REAL,
            job_type TEXT,
            site TEXT,
            date_posted TEXT,
            date_scraped TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            url_hash TEXT UNIQUE,
            title_company_hash TEXT,
            search_role TEXT DEFAULT '',
            search_location TEXT DEFAULT '',
            listing_url TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            resume_path TEXT,
            cover_letter_path TEXT,
            submitted_at TEXT,
            form_answers_json TEXT,
            screenshot_path TEXT,
            notes TEXT,
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        );

        CREATE TABLE IF NOT EXISTS application_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER,
            job_id INTEGER,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            FOREIGN KEY (application_id) REFERENCES applications(id),
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        );

        CREATE TABLE IF NOT EXISTS scrape_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            location TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            job_count INTEGER DEFAULT 0,
            UNIQUE(role, location)
        );

        CREATE TABLE IF NOT EXISTS answer_bank (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_label TEXT NOT NULL UNIQUE,
            answer TEXT NOT NULL DEFAULT 'N/A',
            source TEXT DEFAULT 'auto',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_url_hash ON jobs(url_hash);
        CREATE INDEX IF NOT EXISTS idx_answer_bank_label ON answer_bank(question_label);
        CREATE INDEX IF NOT EXISTS idx_applications_job_id ON applications(job_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_search ON jobs(search_role, search_location);
    """)
    conn.commit()

    # Safe column additions for existing databases
    for col, coltype, default in [
        ("search_role", "TEXT", "''"),
        ("search_location", "TEXT", "''"),
        ("listing_url", "TEXT", "''"),
        ("retry_count", "INTEGER", "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coltype} DEFAULT {default}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists


# -- Scrape Cache -----------------------------------------------------

def is_scrape_cached(conn: sqlite3.Connection, role: str, location: str, cache_hours: int) -> bool:
    """Check if a role+location was scraped within cache_hours."""
    row = conn.execute(
        "SELECT scraped_at FROM scrape_cache WHERE role = ? AND location = ?",
        (role.lower(), location.lower())
    ).fetchone()
    if not row:
        return False
    scraped_at = datetime.fromisoformat(row["scraped_at"])
    age_hours = (datetime.now() - scraped_at).total_seconds() / 3600
    return age_hours < cache_hours


def update_scrape_cache(conn: sqlite3.Connection, role: str, location: str, job_count: int):
    """Update the scrape cache for a role+location."""
    conn.execute("""
        INSERT INTO scrape_cache (role, location, scraped_at, job_count)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(role, location) DO UPDATE SET scraped_at = ?, job_count = ?
    """, (role.lower(), location.lower(), datetime.now().isoformat(), job_count,
          datetime.now().isoformat(), job_count))
    conn.commit()


# -- Job CRUD ----------------------------------------------------------

def insert_job(conn: sqlite3.Connection, job_data: dict) -> Optional[int]:
    """Insert a job if it doesn't already exist. Returns job id or None if duplicate."""
    import hashlib
    url_hash = hashlib.md5(job_data.get("url", "").encode()).hexdigest() if job_data.get("url") else None
    title_company = f"{job_data.get('title', '')}|{job_data.get('company', '')}".lower()
    tc_hash = hashlib.md5(title_company.encode()).hexdigest()

    # Check for duplicates
    if url_hash:
        existing = conn.execute("SELECT id FROM jobs WHERE url_hash = ?", (url_hash,)).fetchone()
        if existing:
            return None

    try:
        cursor = conn.execute("""
            INSERT INTO jobs (title, company, location, url, description, salary_min, salary_max,
                              job_type, site, date_posted, date_scraped, status, url_hash, title_company_hash,
                              search_role, search_location, listing_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?)
        """, (
            job_data.get("title"),
            job_data.get("company"),
            job_data.get("location"),
            job_data.get("url"),
            job_data.get("description"),
            job_data.get("salary_min"),
            job_data.get("salary_max"),
            job_data.get("job_type"),
            job_data.get("site"),
            job_data.get("date_posted"),
            datetime.now().isoformat(),
            url_hash,
            tc_hash,
            job_data.get("search_role", ""),
            job_data.get("search_location", ""),
            job_data.get("listing_url", ""),
        ))
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_jobs_by_status(conn: sqlite3.Connection, status: str, limit: int = 100) -> list[dict]:
    """Get jobs with a given status."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = ? ORDER BY date_posted DESC NULLS LAST, date_scraped DESC LIMIT ?",
        (status, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def update_job_status(conn: sqlite3.Connection, job_id: int, status: str):
    """Update a job's status."""
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()


def increment_retry_count(conn: sqlite3.Connection, job_id: int) -> int:
    """Increment retry count for a job. Returns new count."""
    conn.execute("UPDATE jobs SET retry_count = COALESCE(retry_count, 0) + 1 WHERE id = ?", (job_id,))
    conn.commit()
    row = conn.execute("SELECT retry_count FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row["retry_count"] if row else 0


def reset_failed_jobs(conn: sqlite3.Connection, max_retries: int = 2) -> int:
    """Reset failed jobs back to 'tailored' for retry, if under max retry count.

    Returns count of jobs reset.
    """
    cursor = conn.execute(
        "UPDATE jobs SET status = 'tailored' WHERE status IN ('failed', 'failed_captcha') AND COALESCE(retry_count, 0) < ?",
        (max_retries,)
    )
    conn.commit()
    return cursor.rowcount


def delete_failed_jobs(conn: sqlite3.Connection) -> int:
    """Delete all failed and failed_captcha jobs. Returns count deleted."""
    cursor = conn.execute(
        "DELETE FROM jobs WHERE status IN ('failed', 'failed_captcha')"
    )
    conn.commit()
    return cursor.rowcount


def get_failed_jobs_with_details(conn: sqlite3.Connection) -> list[dict]:
    """Get failed jobs with company and title for folder cleanup."""
    rows = conn.execute(
        "SELECT id, title, company, status FROM jobs WHERE status IN ('failed', 'failed_captcha')"
    ).fetchall()
    return [dict(r) for r in rows]


def nuke_database(conn: sqlite3.Connection):
    """Drop all data from all tables. Used by the reset command."""
    for table in ["application_log", "applications", "jobs", "scrape_cache"]:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def get_job_by_id(conn: sqlite3.Connection, job_id: int) -> Optional[dict]:
    """Get a single job by ID."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def count_jobs_by_status(conn: sqlite3.Connection) -> dict:
    """Get counts of jobs per status."""
    rows = conn.execute("SELECT status, COUNT(*) as count FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["count"] for r in rows}


# -- Application CRUD -------------------------------------------------

def insert_application(conn: sqlite3.Connection, job_id: int, resume_path: str = None,
                       cover_letter_path: str = None) -> int:
    """Create an application record."""
    cursor = conn.execute("""
        INSERT INTO applications (job_id, resume_path, cover_letter_path)
        VALUES (?, ?, ?)
    """, (job_id, resume_path, cover_letter_path))
    conn.commit()
    return cursor.lastrowid


def update_application(conn: sqlite3.Connection, app_id: int, **kwargs):
    """Update application fields."""
    allowed = {"resume_path", "cover_letter_path", "submitted_at", "form_answers_json", "screenshot_path", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [app_id]
    conn.execute(f"UPDATE applications SET {set_clause} WHERE id = ?", values)
    conn.commit()


def get_application_by_job(conn: sqlite3.Connection, job_id: int) -> Optional[dict]:
    """Get an application by job ID."""
    row = conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def count_applications_today(conn: sqlite3.Connection) -> int:
    """Count applications submitted today (for daily cap)."""
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) as count FROM applications WHERE submitted_at LIKE ?",
        (f"{today}%",)
    ).fetchone()
    return row["count"] if row else 0


# -- Log ---------------------------------------------------------------

def log_action(conn: sqlite3.Connection, action: str, details: str = None,
               application_id: int = None, job_id: int = None):
    """Write an entry to the application log."""
    conn.execute("""
        INSERT INTO application_log (application_id, job_id, timestamp, action, details)
        VALUES (?, ?, ?, ?, ?)
    """, (application_id, job_id, datetime.now().isoformat(), action, details))
    conn.commit()


# -- Answer Bank -------------------------------------------------------

def get_saved_answers(conn: sqlite3.Connection) -> dict[str, str]:
    """Get all saved answers as {question_label: answer}."""
    rows = conn.execute("SELECT question_label, answer FROM answer_bank").fetchall()
    return {r["question_label"]: r["answer"] for r in rows}


def get_unanswered_questions(conn: sqlite3.Connection) -> list[dict]:
    """Get questions that still have N/A answers."""
    rows = conn.execute(
        "SELECT id, question_label, created_at FROM answer_bank WHERE answer = 'N/A' ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def save_answer(conn: sqlite3.Connection, question_label: str, answer: str, source: str = "auto"):
    """Save or update an answer in the answer bank."""
    now = datetime.now().isoformat()
    conn.execute("""
        INSERT INTO answer_bank (question_label, answer, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(question_label) DO UPDATE SET answer = ?, source = ?, updated_at = ?
    """, (question_label, answer, source, now, now, answer, source, now))
    conn.commit()


def save_answers_batch(conn: sqlite3.Connection, questions: list[str], source: str = "auto"):
    """Save multiple N/A questions to the answer bank (skips existing)."""
    now = datetime.now().isoformat()
    for q in questions:
        conn.execute("""
            INSERT OR IGNORE INTO answer_bank (question_label, answer, source, created_at, updated_at)
            VALUES (?, 'N/A', ?, ?, ?)
        """, (q, source, now, now))
    conn.commit()
