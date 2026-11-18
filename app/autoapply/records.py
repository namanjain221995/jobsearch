"""The answers a candidate gave for application forms (stored in candidate_profiles.application_answers).

A fixed schema: legal questions, EEO self-identification, links, current position, location, contact. Every value
is "unknown" / empty until a person records it; nothing here is ever inferred from a name or a resume. The only
fallbacks are values the candidate already gave elsewhere in the portal (work authorization, sponsorship and
salary in the job-search profile, the phone on the candidate record, the name split into first and last)."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

YesNo = Literal["yes", "no", "unknown"]
EEO_FIELDS = ("eeo_gender", "eeo_race", "eeo_hispanic_latino", "eeo_veteran", "eeo_disability")
AUTHORIZED_WORK = {"US Citizen", "Green Card / US Permanent Resident", "Current Work Visa", "Employment Authorization Document (EAD)"}


def _text(limit: int):
    return Field(default="", max_length=limit)


class ApplicationAnswers(BaseModel):
    """Stored shape. Unknown keys are dropped; missing keys mean unknown."""
    model_config = ConfigDict(extra="ignore")

    authorized_us: YesNo = "unknown"
    sponsorship_now_or_future: YesNo = "unknown"
    age_18_or_over: YesNo = "unknown"
    willing_to_relocate: YesNo = "unknown"
    restrictive_agreements: YesNo = "unknown"
    earliest_start: str = _text(100)
    notice_period: str = _text(100)
    expected_salary: str = _text(100)
    previously_employed_by: list[str] = Field(default_factory=list, max_length=50)
    previous_employers_complete: bool = False
    eeo_gender: str = Field(default="decline", max_length=100)
    eeo_race: str = Field(default="decline", max_length=100)
    eeo_hispanic_latino: str = Field(default="decline", max_length=100)
    eeo_veteran: str = Field(default="decline", max_length=100)
    eeo_disability: str = Field(default="decline", max_length=100)
    linkedin_url: str = _text(300)
    github_url: str = _text(300)
    portfolio_url: str = _text(300)
    current_company: str = _text(160)
    current_title: str = _text(160)
    city: str = _text(100)
    state: str = _text(100)
    country: str = _text(100)
    phone: str = _text(40)
    first_name: str = _text(100)
    last_name: str = _text(100)
    preferred_name: str = _text(100)
    referral_source: str = _text(100)

    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, value):
        if isinstance(value, str):
            return " ".join(value.split())
        if isinstance(value, list):
            return list(dict.fromkeys(" ".join(str(v).split()) for v in value if str(v).strip()))
        return value

    @field_validator(*EEO_FIELDS)
    @classmethod
    def _eeo(cls, value: str) -> str:
        return value or "decline"

    @field_validator("linkedin_url", "github_url", "portfolio_url")
    @classmethod
    def _url(cls, value: str) -> str:
        if value and not value.lower().startswith(("https://", "http://")):
            raise ValueError("Enter a full address starting with https://")
        return value


class Effective(BaseModel):
    value: str | list[str] | bool
    source: str                 # application_answers | profile | candidate | default | unknown


def stored(raw: dict | None) -> ApplicationAnswers:
    """Parse what is stored, tolerating rows written by an older schema (invalid values count as unknown)."""
    data = dict(raw or {})
    try:
        return ApplicationAnswers(**data)
    except Exception:
        clean = {}
        for key, value in data.items():
            try:
                ApplicationAnswers(**{key: value})
                clean[key] = value
            except Exception:
                continue
        return ApplicationAnswers(**clean)


def split_name(name: str) -> tuple[str, str]:
    parts = (name or "").split()
    if len(parts) < 2:
        return (parts[0] if parts else ""), ""
    return " ".join(parts[:-1]), parts[-1]


def effective(raw: dict | None, *, profile=None, candidate=None) -> dict[str, Effective]:
    """The value each field has for a form, and where it comes from. Unknown stays unknown."""
    answers = stored(raw)
    recorded = set((raw or {}).keys())
    out: dict[str, Effective] = {}
    for key in ApplicationAnswers.model_fields:
        value = getattr(answers, key)
        if key in recorded and value not in ("", "unknown", [], None):
            out[key] = Effective(value=value, source="application_answers")
        else:
            out[key] = Effective(value=value, source="default" if key in EEO_FIELDS or key == "previous_employers_complete" else "unknown")
    if out["authorized_us"].source == "unknown" and profile is not None and profile.work_authorization in AUTHORIZED_WORK:
        out["authorized_us"] = Effective(value="yes", source="profile")
    if out["sponsorship_now_or_future"].source == "unknown" and profile is not None and profile.sponsorship_needed in ("Yes", "No"):
        out["sponsorship_now_or_future"] = Effective(value=profile.sponsorship_needed.lower(), source="profile")
    if out["expected_salary"].source == "unknown" and profile is not None and profile.salary_min:
        out["expected_salary"] = Effective(value=f"${int(profile.salary_min):,}", source="profile")
    if candidate is not None:
        if out["phone"].source == "unknown" and (candidate.phone or "").strip():
            out["phone"] = Effective(value=candidate.phone.strip(), source="candidate")
        first, last = split_name(candidate.name)
        if out["first_name"].source == "unknown" and first:
            out["first_name"] = Effective(value=first, source="candidate")
        if out["last_name"].source == "unknown" and last:
            out["last_name"] = Effective(value=last, source="candidate")
    return out
