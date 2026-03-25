"""Load and manage user profile from config/profile.yaml."""

import yaml
from pathlib import Path
from .utils import CONFIG_DIR
from .models import Profile, Settings


def load_profile_raw() -> dict:
    """Load raw YAML dict from config/profile.yaml."""
    profile_path = CONFIG_DIR / "profile.yaml"
    if not profile_path.exists():
        raise FileNotFoundError(
            f"Profile not found at {profile_path}. "
            "Copy config/profile.example.yaml to config/profile.yaml and fill in your details."
        )
    with open(profile_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings_raw() -> dict:
    """Load raw YAML dict from config/settings.yaml."""
    settings_path = CONFIG_DIR / "settings.yaml"
    if not settings_path.exists():
        raise FileNotFoundError(
            f"Settings not found at {settings_path}. "
            "Copy config/settings.example.yaml to config/settings.yaml and customize."
        )
    with open(settings_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_profile() -> dict:
    """Load and validate profile through Pydantic model. Returns dict for backward compat."""
    raw = load_profile_raw()
    profile = Profile(**raw)
    return profile.model_dump()


def load_profile_model() -> Profile:
    """Load and validate profile, returning the Pydantic model directly."""
    raw = load_profile_raw()
    return Profile(**raw)


def load_settings() -> dict:
    """Load and validate settings through Pydantic model. Returns dict for backward compat."""
    raw = load_settings_raw()
    settings = Settings(**raw)
    return settings.model_dump()


def load_settings_model() -> Settings:
    """Load and validate settings, returning the Pydantic model directly."""
    raw = load_settings_raw()
    return Settings(**raw)


def get_profile_summary(profile: dict) -> str:
    """Create a text summary of the profile for LLM prompts."""
    personal = profile.get("personal", {})
    lines = []

    name = f"{personal.get('first_name', '')} {personal.get('last_name', '')}".strip()
    if name:
        lines.append(f"Name: {name}")
    if personal.get("email"):
        lines.append(f"Email: {personal['email']}")
    if personal.get("phone"):
        lines.append(f"Phone: {personal['phone']}")

    addr = personal.get("address", {})
    addr_parts = [addr.get("city", ""), addr.get("state", ""), addr.get("country", "")]
    addr_str = ", ".join(p for p in addr_parts if p)
    if addr_str:
        lines.append(f"Location: {addr_str}")

    # Education
    for edu in profile.get("education", []):
        if edu.get("degree"):
            edu_str = f"Education: {edu['degree']} in {edu.get('field', 'N/A')}"
            if edu.get("minor"):
                edu_str += f", Minor in {edu['minor']}"
            edu_str += f" from {edu.get('school', 'N/A')} ({edu.get('graduation_year', 'N/A')})"
            lines.append(edu_str)

    # Work experience
    for exp in profile.get("work_experience", []):
        if exp.get("title"):
            lines.append(f"Experience: {exp['title']} at {exp.get('company', 'N/A')} ({exp.get('start_date', '?')} - {exp.get('end_date', 'present')})")

    # Skills
    skills = profile.get("skills", {})
    all_skills = skills.get("languages", []) + skills.get("frameworks", []) + skills.get("tools", [])
    if all_skills:
        lines.append(f"Skills: {', '.join(all_skills)}")

    # Links
    links = profile.get("links", {})
    for key in ["linkedin", "github", "portfolio"]:
        if links.get(key):
            lines.append(f"{key.title()}: {links[key]}")

    # Work authorization
    auth = profile.get("work_authorization", {})
    if auth.get("authorized_us"):
        lines.append("Work Authorization: Authorized to work in the US")
    if auth.get("requires_sponsorship") is False:
        lines.append("Sponsorship: Does not require sponsorship")

    # Preferences
    prefs = profile.get("preferences", {})
    if prefs.get("start_date"):
        lines.append(f"Available: {prefs['start_date']}")
    if prefs.get("remote_preference"):
        lines.append(f"Work Preference: {prefs['remote_preference']}")

    return "\n".join(lines)
