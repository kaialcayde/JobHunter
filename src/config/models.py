"""Pydantic models for validating and processing profile.yaml and settings.yaml."""

import re
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator


# -- Profile Models ----------------------------------------------------

class Address(BaseModel):
    street: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    country: str = "United States"


class Personal(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    address: Address = Address()
    date_of_birth: str = ""

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if v and "@" not in v:
            raise ValueError(f"Invalid email format: '{v}' - must contain @")
        return v

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        if not v:
            return v
        digits = re.sub(r"\D", "", v)
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        elif len(digits) == 11 and digits[0] == "1":
            return f"{digits[1:4]}-{digits[4:7]}-{digits[7:]}"
        return v  # return as-is if unusual format


class WorkAuthorization(BaseModel):
    authorized_us: bool = True
    requires_sponsorship: bool = False


class Education(BaseModel):
    degree: str = ""
    field: str = ""
    minor: str = ""
    school: str = ""
    graduation_year: str = ""
    gpa: str = ""

    @field_validator("graduation_year", mode="before")
    @classmethod
    def coerce_year(cls, v) -> str:
        return str(v) if v else ""

    @field_validator("gpa", mode="before")
    @classmethod
    def coerce_gpa(cls, v) -> str:
        if v is None:
            return ""
        v = str(v)
        if v and v != "":
            try:
                gpa_val = float(v)
                if not (0 <= gpa_val <= 4.0):
                    raise ValueError(f"GPA {v} is outside 0.0-4.0 range")
            except ValueError as e:
                if "outside" in str(e):
                    raise
        return v


class WorkExperience(BaseModel):
    title: str = ""
    company: str = ""
    start_date: str = ""
    end_date: str = ""
    description: str = ""


class Skills(BaseModel):
    languages: list[str] = []
    frameworks: list[str] = []
    tools: list[str] = []


class Links(BaseModel):
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""

    @field_validator("linkedin", "github", "portfolio")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if v and not v.startswith("http"):
            raise ValueError(f"URL must start with http:// or https:// - got '{v}'")
        return v


class Preferences(BaseModel):
    desired_salary_min: int = 0
    desired_salary_max: int = 0
    willing_to_relocate: bool = True
    remote_preference: str = "any"
    start_date: str = "immediately"

    @field_validator("desired_salary_min", "desired_salary_max", mode="before")
    @classmethod
    def coerce_salary(cls, v) -> int:
        if isinstance(v, str):
            v = v.replace(",", "").replace("$", "").strip()
        return int(v) if v else 0

    @field_validator("remote_preference")
    @classmethod
    def validate_remote_pref(cls, v: str) -> str:
        allowed = {"remote", "hybrid", "onsite", "any"}
        if v.lower() not in allowed:
            raise ValueError(f"remote_preference must be one of {allowed}, got '{v}'")
        return v.lower()


class Diversity(BaseModel):
    gender: str = ""
    ethnicity: str = ""
    veteran_status: str = ""
    disability_status: str = ""


class Profile(BaseModel):
    personal: Personal = Personal()
    work_authorization: WorkAuthorization = WorkAuthorization()
    education: list[Education] = []
    work_experience: list[WorkExperience] = []
    skills: Skills = Skills()
    links: Links = Links()
    preferences: Preferences = Preferences()
    diversity: Diversity = Diversity()

    @model_validator(mode="after")
    def check_basics(self):
        if not self.personal.first_name:
            raise ValueError("personal.first_name is required - fill in config/profile.yaml")
        if not self.personal.email:
            raise ValueError("personal.email is required - fill in config/profile.yaml")
        return self


# -- Settings Models ---------------------------------------------------

class JobSearch(BaseModel):
    roles: list[str] = ["software engineer"]
    locations: list[str] = ["United States"]
    remote: bool = False
    job_type: str = "fulltime"
    experience_levels: list[str] = ["entry", "mid", "senior"]
    sites: list[str] = ["indeed"]
    results_per_search: int = 50
    hours_old: int = 72

    @field_validator("job_type")
    @classmethod
    def validate_job_type(cls, v: str) -> str:
        allowed = {"fulltime", "parttime", "contract", "internship"}
        if v.lower() not in allowed:
            raise ValueError(f"job_type must be one of {allowed}, got '{v}'")
        return v.lower()

    @field_validator("sites", mode="before")
    @classmethod
    def validate_sites(cls, v: list) -> list:
        allowed = {"indeed", "linkedin", "glassdoor", "zip_recruiter", "google", "bayt", "naukri"}
        for site in v:
            if site.lower() not in allowed:
                raise ValueError(f"Unknown site '{site}'. Allowed: {allowed}")
        return [s.lower() for s in v]


class OpenAIConfig(BaseModel):
    model: str = "gpt-4o"
    temperature: float = 0.7

    @field_validator("temperature")
    @classmethod
    def validate_temp(cls, v: float) -> float:
        if not (0 <= v <= 2.0):
            raise ValueError(f"temperature must be 0.0-2.0, got {v}")
        return v


class Tailoring(BaseModel):
    enabled: bool = True


class Automation(BaseModel):
    auto_submit: bool = True
    max_applications_per_day: int = 25
    max_applications_per_round: int = 0  # 0 = no per-round limit
    max_per_role: int = 0           # 0 = no per-role limit
    max_per_location: int = 0       # 0 = no per-location limit
    distribution: str = "round_robin"  # round_robin or sequential
    delay_between_applications_seconds: int = 30
    screenshot_before_submit: bool = True
    skip_captcha_sites: bool = True
    captcha_solving: bool = False  # solve CAPTCHAs via 2Captcha API (requires CAPTCHA_API_KEY in .env)
    parallel_browsers_per_site: int = Field(default=1, ge=1, le=3)  # 1 = sequential, >1 = one browser per job site in parallel
    verbose_logging: bool = True    # show step-by-step progress (loading, captcha, apply button, etc.)
    headless: bool = True
    vision_agent: bool = False      # enable LLM vision fallback for form filling
    vision_model: str = "gpt-4o-mini"  # cheapest vision-capable model
    vision_logging: bool = True     # log each vision agent step to console and log file
    vision_detail: str = "high"     # "low" (85 tokens) or "high" (12-17K tokens) per screenshot

    @field_validator("distribution")
    @classmethod
    def validate_distribution(cls, v: str) -> str:
        allowed = {"round_robin", "sequential"}
        if v.lower() not in allowed:
            raise ValueError(f"distribution must be one of {allowed}, got '{v}'")
        return v.lower()


class Scraping(BaseModel):
    cache_hours: int = 12
    cache_enabled: bool = True


class Scheduler(BaseModel):
    enabled: bool = True
    run_time: str = "09:00"
    log_dir: str = "data/logs"


class Filters(BaseModel):
    exclude_companies: list[str] = []
    min_salary: int = 0
    keywords_required: list[str] = []
    keywords_exclude: list[str] = []
    strict_title_match: bool = False

    @field_validator("min_salary", mode="before")
    @classmethod
    def coerce_salary(cls, v) -> int:
        if isinstance(v, str):
            v = v.replace(",", "").replace("$", "").strip()
        return int(v) if v else 0


class Settings(BaseModel):
    job_search: JobSearch = JobSearch()
    openai: OpenAIConfig = OpenAIConfig()
    tailoring: Tailoring = Tailoring()
    automation: Automation = Automation()
    scraping: Scraping = Scraping()
    scheduler: Scheduler = Scheduler()
    filters: Filters = Filters()
