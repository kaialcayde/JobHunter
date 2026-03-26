"""Shared utilities for JobHunter."""

import os
import re
import shutil
from pathlib import Path

# Project root directory
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
CONFIG_DIR = PROJECT_ROOT / "config"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
APPLICATIONS_DIR = PROJECT_ROOT / "applications"
ATTEMPTS_DIR = APPLICATIONS_DIR / "attempts"
SUCCESS_DIR = APPLICATIONS_DIR / "success"
FAILED_DIR = APPLICATIONS_DIR / "failed"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = DATA_DIR / "logs"
LINKEDIN_AUTH_STATE = DATA_DIR / "linkedin_auth.json"


def ensure_dirs():
    """Create required directories if they don't exist."""
    for d in [CONFIG_DIR, TEMPLATES_DIR, APPLICATIONS_DIR, ATTEMPTS_DIR,
              SUCCESS_DIR, FAILED_DIR, DATA_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str) -> str:
    """Convert a string to a safe directory/file name."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    name = re.sub(r'_+', '_', name)
    return name[:100]  # cap length


def get_application_dir(company: str, position: str) -> Path:
    """Get or create the application directory in attempts/ for a company/position."""
    company_dir = sanitize_filename(company)
    position_dir = sanitize_filename(position)
    path = ATTEMPTS_DIR / company_dir / position_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def move_application_dir(company: str, position: str, destination: str) -> Path:
    """Move an application folder from attempts/ to success/ or failed/.

    Args:
        company: Company name
        position: Position title
        destination: "success" or "failed"

    Returns:
        The new path, or the old path if move failed.
    """
    company_dir = sanitize_filename(company)
    position_dir = sanitize_filename(position)
    src = ATTEMPTS_DIR / company_dir / position_dir

    if destination == "success":
        dest_base = SUCCESS_DIR
    elif destination == "failed":
        dest_base = FAILED_DIR
    else:
        return src

    dest = dest_base / company_dir / position_dir

    if not src.exists():
        # Maybe it's in the old flat structure -- check there too
        old_src = APPLICATIONS_DIR / company_dir / position_dir
        if old_src.exists() and old_src != dest:
            src = old_src
        else:
            return src

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(src), str(dest))

        # Clean up empty company dir in source
        src_company = src.parent
        if src_company.exists() and not any(src_company.iterdir()):
            src_company.rmdir()

        return dest
    except Exception:
        return src
