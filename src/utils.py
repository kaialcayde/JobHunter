"""Shared utilities for JobHunter."""

import os
import re
from pathlib import Path

# Project root directory
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
CONFIG_DIR = PROJECT_ROOT / "config"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
APPLICATIONS_DIR = PROJECT_ROOT / "applications"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = DATA_DIR / "logs"
LINKEDIN_AUTH_STATE = DATA_DIR / "linkedin_auth.json"


def ensure_dirs():
    """Create required directories if they don't exist."""
    for d in [CONFIG_DIR, TEMPLATES_DIR, APPLICATIONS_DIR, DATA_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str) -> str:
    """Convert a string to a safe directory/file name."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    name = re.sub(r'_+', '_', name)
    return name[:100]  # cap length


def get_application_dir(company: str, position: str) -> Path:
    """Get or create the application directory for a company/position."""
    company_dir = sanitize_filename(company)
    position_dir = sanitize_filename(position)
    path = APPLICATIONS_DIR / company_dir / position_dir
    path.mkdir(parents=True, exist_ok=True)
    return path
