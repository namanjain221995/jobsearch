"""Request / response / domain models."""
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

Authorization = Literal[
    "US Citizen",
    "Green Card / US Permanent Resident",
    "Current Work Visa",
    "Employment Authorization Document (EAD)",
    "Require Visa Sponsorship",
    "Other / Prefer Not to Say",
]
WorkMode = Literal["All", "Remote", "Hybrid", "On-site"]


class SearchRequest(BaseModel):
    resume_id: str = Field(min_length=8, max_length=64)
    job_titles: str = Field(min_length=2, max_length=300)
    years_experience: float = Field(ge=0, le=40)
    authorization: Authorization = "Other / Prefer Not to Say"
    sponsorship_needed: Literal["Yes", "No", "Unknown"] = "Unknown"
    exclude_citizenship_required: bool = False
    result_count: int = Field(default=50, ge=0, le=1000)
    work_mode: WorkMode = "All"
    deep_search: bool = True      # include AI web search + browser crawl (slower, wider coverage)
    locations: list[str] = Field(default_factory=list, max_length=30)   # preferred states: only steer discovery queries, never filter here
    ats_platforms: list[str] = Field(default_factory=list, max_length=40)   # platform ids of app/ats_platforms.py; empty = all platforms and sources
    startup_focus: bool = False   # startup platforms first and with the larger share of the budgets; a tie-break in ranking, never a score change
    # Posting age (app/freshness.py): a posting whose known posting date is older than this many days is never
    # delivered. 0 = any time; allowed: 0, 1, 3, 7, 14, 30.
    posted_within_days: int = 7
    include_undated: bool = True  # deliver postings whose posting date is not stated (after dated ones of the same score)

    @field_validator("posted_within_days")
    @classmethod
    def _posted_within(cls, v: int) -> int:
        from .freshness import POSTED_WITHIN_CHOICES
        if v not in POSTED_WITHIN_CHOICES:
            raise ValueError("posted_within_days must be one of " + ", ".join(str(n) for n in POSTED_WITHIN_CHOICES))
        return v

    @field_validator("ats_platforms")
    @classmethod
    def _platforms(cls, v: list[str]) -> list[str]:
        from . import ats_platforms
        return ats_platforms.chosen_platform_ids(v)

    @field_validator("job_titles")
    @classmethod
    def _titles(cls, v: str) -> str:
        titles = [t.strip() for t in v.split(",") if t.strip()]
        if not titles:
            raise ValueError("Enter at least one job title")
        if len(titles) > 8:
            raise ValueError("Enter at most 8 job titles")
        return ", ".join(titles)

    @property
    def titles(self) -> list[str]:
        return [t.strip() for t in self.job_titles.split(",") if t.strip()]

    @property
    def needs_sponsorship(self) -> bool:
        return self.sponsorship_needed == "Yes" or self.authorization == "Require Visa Sponsorship"

    @property
    def is_citizen(self) -> bool:
        return self.authorization == "US Citizen"


class Profile(BaseModel):
    """Structured view of a resume. Never contains inferred sensitive attributes."""
    skills: list[str] = []
    tools: list[str] = []
    job_titles: list[str] = []
    education: list[str] = []
    industries: list[str] = []
    summary: str = ""
    related_titles: list[str] = []
    search_queries: list[str] = []
    source: str = "heuristic"

    @property
    def all_skills(self) -> list[str]:
        seen, out = set(), []
        for s in [*self.skills, *self.tools]:
            k = s.strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(s.strip())
        return out


class Job(BaseModel):
    id: str = ""
    title: str
    company: str
    location: str = "Not listed"
    work_mode: str = "Not listed"            # Remote / Hybrid / On-site / Not listed
    salary: str = "Not listed"
    experience_required: str = "Not listed"
    required_years: Optional[float] = None
    description: str = ""                     # plain text, server side only
    description_rich: str = ""                # same text with paragraphs / bullets kept as line breaks (display)
    raw_html: str = ""                        # unconverted source HTML, converted lazily by enrich_job
    enriched: int = 0                         # version of enrich_job that last ran (persisted in the index)
    skills: list[str] = []                    # skills named in the posting (filled by enrich_job)
    needs_detail: bool = False                # list-only record: description must be fetched before it can be shown
    detail_ref: str = ""                      # source-specific pointer used to fetch the detail
    aggregator: bool = False                  # listing copied by a job board rather than the employer's own ATS
    row_id: int = 0                           # index row (0 = not in the index)
    url: str                                  # original posting
    apply_url: str = ""                       # direct application link when the source provides one
    source: str                               # primary source
    sources: list[str] = []                   # provenance across providers
    ats: str = ""                             # greenhouse / lever / ashby / ""
    ats_id: str = ""
    board: str = ""
    country_hint: str = ""                    # structured country from the source when present
    posted_at: str = ""                       # the day the posting was published (ISO date), "" when unknown; never a "last updated" date
    posted_before: str = ""                   # ISO date, only without posted_at: the source says the posting is at least this old
                                              # ("Posted 30+ Days Ago", a "last updated" date). Server side only.
    verified_at: str = ""
    verification: str = ""                    # how the listing was verified
    verification_level: str = "verified"      # "verified": read from the employer's board API or page; "listed": a licensed job
                                              # listing API returned it and the employer's site was not checked
    citizenship_requirement: str = "Not stated"   # Required / Not required / Not stated
    sponsorship: str = "Not stated"               # Available / Not available / Not stated
    clearance_required: bool = False
    authorization_status: str = "Unknown"         # Eligible / Potential mismatch / Unknown
    eligibility_note: str = "Not specified"
    score: int = 0
    score_breakdown: dict[str, int] = {}
    score_source: str = "heuristic"
    matching_skills: list[str] = []
    missing_skills: list[str] = []
    reason: str = ""
    experience_comparison: str = ""
    title_relevance: float = 0.0
    matched_query: str = ""                   # the requested title or title variant this posting was found under

    def public(self) -> dict:
        d = self.model_dump(exclude={"description", "description_rich", "raw_html", "country_hint", "title_relevance", "required_years",
                                     "enriched", "skills", "posted_before",
                                     "needs_detail", "detail_ref", "row_id"})
        d["description_excerpt"] = self.description[:400]
        return d


class Candidate(BaseModel):
    """A job lead from AI web search. Untrusted until verified against its source."""
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    provider: str = ""
    cited: bool = False


class SearchStarted(BaseModel):
    search_id: str
    status: str


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""
