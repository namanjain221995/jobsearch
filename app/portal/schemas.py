"""API request and response models. These are the contract the Next.js frontend is generated from."""
import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, field_validator, model_validator

from ..schemas import Authorization, WorkMode

Role = Literal["ADMIN", "TEAM_LEAD", "MEMBER"]
AssignmentStatus = Literal["ASSIGNED", "VIEWED", "APPLYING", "APPLIED", "INTERVIEWING", "OFFER", "REJECTED", "WITHDRAWN", "SKIPPED"]
SearchStatus = Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
SearchMode = Literal["auto_assign", "review"]
Sponsorship = Literal["Yes", "No", "Unknown"]
T = TypeVar("T")


def _has_nul(value: Any, depth: int = 0) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if depth < 6 and isinstance(value, dict):
        return any(_has_nul(k, depth + 1) or _has_nul(v, depth + 1) for k, v in value.items())
    if depth < 6 and isinstance(value, (list, tuple)):
        return any(_has_nul(v, depth + 1) for v in value)
    return False


def _sane_day(value: date) -> date:
    if not date(2000, 1, 1) <= value <= date(2100, 12, 31):
        raise ValueError("Choose a date between 2000 and 2100")
    return value


Day = Annotated[date, AfterValidator(_sane_day)]          # date filters: "the day after" must always exist


class Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def _no_nul_characters(cls, data: Any) -> Any:
        """PostgreSQL cannot store the NUL character in text; refuse it here instead of failing in the database."""
        if isinstance(data, dict) and _has_nul(data):
            raise ValueError("Text must not contain the NUL character")
        return data


class Page(Model, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class Message(Model):
    message: str


# ---------------------------------------------------------------- auth
class LoginIn(Model):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    remember: bool = False


class TeamRef(Model):
    id: uuid.UUID
    name: str


class UserRef(Model):
    id: uuid.UUID
    name: str
    email: str = ""


class CandidateRef(Model):
    """A candidate as shown next to a job, a search or an event."""
    id: uuid.UUID
    name: str


class ExtraField(Model):
    """An optional candidate-profile field the Admin added. It is shown with every candidate's profile."""
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    label: str = Field(min_length=1, max_length=60)
    type: Literal["text", "number", "yes_no", "choice"] = "text"
    options: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(default_factory=list, max_length=20)      # for "choice"

    @field_validator("label", mode="before")
    @classmethod
    def _tidy_label(cls, value):
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("options", mode="before")
    @classmethod
    def _tidy_options(cls, value):
        """Surrounding and repeated white space goes; an option listed twice is kept once."""
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return value
        return list(dict.fromkeys(" ".join(item.split()) for item in value))

    @model_validator(mode="after")
    def _choice_needs_options(self):
        if self.type == "choice" and len(self.options) < 2:
            raise ValueError("A choice field needs at least two options")
        if self.type != "choice":
            self.options = []
        return self


class PasswordPolicy(Model):
    min_length: int
    max_length: int = 128
    character_kinds_required: int = 3              # of: lower case, upper case, digit, other


class UiSettings(Model):
    watermark_enabled: bool = True
    default_target_count: int = 50
    max_members_per_search: int = 10               # candidates per search run (the setting keeps its name)
    max_results_per_search_run: int = 500
    max_target_count: int = 1000                   # most jobs per candidate one search may ask for (server limit; the form's upper bound)
    default_min_match_score: int = 50
    deep_search_default: bool = True
    password_min_length: int = 10
    profile_extra_fields: list[ExtraField] = []    # what the candidate profile form shows beyond the built-in fields
    ai_providers: list[str] = []                   # outside AI services resume text is sent to when a search runs; empty = none


class MeOut(Model):
    id: uuid.UUID
    name: str
    email: str
    role: Role
    team: TeamRef | None = None
    home: str                              # dashboard path for this role
    unread_notifications: int = 0
    settings: UiSettings


class ForgotPasswordIn(Model):
    email: EmailStr


class ResetPasswordIn(Model):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordIn(Model):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------- invitations
def _person_name(value: str) -> str:
    """A name goes into e-mails the portal sends to addresses the inviter chooses, so it may not carry a message
    of its own: letters, digits, spaces and . , ' - ( ) only. No line breaks, no links."""
    value = " ".join(str(value).split()) if isinstance(value, str) and not any(ord(c) < 32 or ord(c) == 127 for c in value) else value
    if not isinstance(value, str) or not _PERSON_NAME.fullmatch(value):
        raise ValueError("Enter a name of 2 to 100 characters: letters, digits, spaces and . , ' - ( ) only")
    return value


_PERSON_NAME = re.compile(r"[^\W_][\w .,'’()\-]{1,99}", re.UNICODE)
PersonName = Annotated[str, BeforeValidator(_person_name)]


class InvitationCreate(Model):
    name: PersonName
    email: EmailStr
    role: Role = "MEMBER"                  # ADMIN: only an Admin may ask for it, and it never comes with a team
    team_id: uuid.UUID | None = None       # required for MEMBER invited by Admin; a Team Lead's own team is used (another team is refused)
    team_name: str | None = Field(default=None, max_length=120)      # Admin creating a Team Lead together with a new team
    team_description: str = Field(default="", max_length=2000)


class InvitationOut(Model):
    id: uuid.UUID
    email: str
    name: str
    role: Role
    team: TeamRef | None = None
    status: Literal["pending", "accepted", "expired", "revoked"]
    delivery: Literal["email", "manual"]
    expires_at: datetime
    accepted_at: datetime | None = None
    created_at: datetime
    invited_by: UserRef | None = None


class InvitationCreated(InvitationOut):
    invite_url: str                        # the link to copy and pass on; empty when the mail server delivered it (email_sent)
    email_sent: bool                       # true only when an SMTP server accepted the message


class InvitationPreview(Model):
    email: str
    name: str
    role: Role
    team_name: str | None = None
    expires_at: datetime
    password_min_length: int


class InvitationAccept(Model):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------- stats shared by tables / cards
class AssignmentStats(Model):
    """Definitions (also shown in the UI):
    assigned = active assignments; applied = assignments with a confirmed submission (applied_at set), whatever
    happened later; not_applied = active assignments without a confirmed submission, excluding skipped/withdrawn;
    interviews / offers = assignments that reached that stage; closed = REJECTED + WITHDRAWN + SKIPPED;
    completion_rate = applied / (assigned - skipped - withdrawn-before-applying), 0..100; link_opens = how many
    times "Open Job" was used (never counted as applied)."""
    assigned: int = 0
    not_applied: int = 0
    applied: int = 0
    interviews: int = 0
    offers: int = 0
    closed: int = 0
    viewed: int = 0
    link_opens: int = 0
    completion_rate: float = 0.0


# ---------------------------------------------------------------- teams / users
class TeamCreate(Model):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2000)


class TeamUpdate(Model):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None
    team_lead_id: uuid.UUID | None = None          # assign / replace the Team Lead (must be a TEAM_LEAD user)
    remove_team_lead: bool = False


class TeamOut(Model):
    id: uuid.UUID
    name: str
    description: str
    is_active: bool
    created_at: datetime
    team_lead: UserRef | None = None
    team_lead_status: str | None = None            # invited | active | deactivated
    member_count: int = 0
    candidate_count: int = 0                       # active candidates of the team
    unassigned_candidates: int = 0                 # active candidates without a responsible member
    queue_pending: int = 0
    stats: AssignmentStats = AssignmentStats()


class UserOut(Model):
    id: uuid.UUID
    name: str
    email: str
    role: Role
    team: TeamRef | None = None
    status: Literal["invited", "active", "deactivated"]
    is_active: bool
    deactivated_by_admin: bool = False             # a Team Lead cannot reactivate such an account
    candidate_count: int = 0                       # members only: candidates currently assigned to them
    last_login_at: datetime | None = None
    last_activity_at: datetime | None = None
    created_at: datetime
    stats: AssignmentStats | None = None           # members only: totals over the candidates currently assigned to them


class UserUpdate(Model):
    name: PersonName | None = None
    is_active: bool | None = None                  # Admin: anyone but themselves; Team Lead: own members
    team_id: uuid.UUID | None = None               # Admin only: transfer a member to another team (their candidates stay and become unassigned)


class ResetLinkOut(Model):
    reset_url: str                         # empty when the mail server delivered the link (email_sent)
    expires_at: datetime
    email_sent: bool


class ProfileIn(Model):
    desired_titles: list[str] | None = Field(default=None, max_length=8)
    years_experience: float | None = Field(default=None, ge=0, le=40)
    skills: list[str] | None = Field(default=None, max_length=120)
    preferred_states: list[str] | None = Field(default=None, max_length=30)
    preferred_cities: list[str] | None = Field(default=None, max_length=30)
    work_mode: WorkMode | None = None
    work_authorization: Authorization | None = None
    sponsorship_needed: Sponsorship | None = None
    exclude_citizenship_required: bool | None = None
    salary_min: int | None = Field(default=None, ge=0, le=5_000_000)
    seniority: str | None = Field(default=None, max_length=40)
    excluded_companies: list[str] | None = Field(default=None, max_length=100)
    required_keywords: list[str] | None = Field(default=None, max_length=30)
    extra: dict | None = None

    @field_validator("desired_titles", "skills", "preferred_states", "preferred_cities", "excluded_companies", "required_keywords")
    @classmethod
    def _clean(cls, v):
        if v is None:
            return v
        out = list(dict.fromkeys(str(x).strip()[:120] for x in v if str(x).strip()))
        return out


class ProfileOut(Model):
    candidate_id: uuid.UUID
    desired_titles: list[str] = []
    years_experience: float | None = None
    skills: list[str] = []
    preferred_states: list[str] = []
    preferred_cities: list[str] = []
    work_mode: str = "All"
    work_authorization: str = "Other / Prefer Not to Say"
    sponsorship_needed: str = "Unknown"
    exclude_citizenship_required: bool = False
    salary_min: int | None = None
    seniority: str = ""
    excluded_companies: list[str] = []
    required_keywords: list[str] = []
    extra: dict = {}
    completion: int = 0                            # 0..100
    missing: list[str] = []                        # what would complete the profile
    updated_at: datetime | None = None


class ResumeOut(Model):
    id: uuid.UUID
    candidate_id: uuid.UUID
    filename: str
    size_bytes: int
    has_file: bool = False                         # false for resumes uploaded before files were kept: nothing to download
    version: int
    truncated: bool = False
    created_at: datetime
    uploaded_by: UserRef | None = None
    parsed_profile: dict = {}                      # skills, job_titles, education extracted from the text
    text: str | None = None                        # only on the single-resume endpoints
    source: Literal["upload", "salesforce"] = "upload"      # uploaded in the portal, or copied from Salesforce


# ---------------------------------------------------------------- candidates
def _optional_email(value):
    return None if isinstance(value, str) and not value.strip() else value


def _phone(value):
    if value is None:
        return value
    value = " ".join(str(value).split()) if isinstance(value, str) else value
    if not isinstance(value, str) or not _PHONE.fullmatch(value):
        raise ValueError("Enter a phone number of at most 40 characters: digits, spaces and + ( ) - . / # x only")
    return value


_PHONE = re.compile(r"[0-9+()\-./# xXeEtT]{0,40}")
OptionalEmail = Annotated[EmailStr | None, BeforeValidator(_optional_email)]
Phone = Annotated[str, BeforeValidator(_phone)]


class CandidateCreate(Model):
    name: PersonName
    team_id: uuid.UUID | None = None               # required for an Admin; a Team Lead's own team is used (another team is refused)
    email: OptionalEmail = None
    phone: Phone = ""
    notes: str = Field(default="", max_length=10000)
    assigned_member_id: uuid.UUID | None = None    # an active MEMBER of the same team


class CandidateUpdate(Model):
    """Only the fields that are sent change. `assigned_member_id: null` takes the candidate away from its member;
    `email: null` (or "") removes the address."""
    name: PersonName | None = None
    email: OptionalEmail = None
    phone: Phone | None = None
    notes: str | None = Field(default=None, max_length=10000)
    assigned_member_id: uuid.UUID | None = None
    is_active: bool | None = None
    team_id: uuid.UUID | None = None               # Admin only: move the candidate (jobs and history move along) to another team


class CandidateProfileSummary(Model):
    """The few profile values a candidate card shows; the full profile is at /api/candidates/{id}/profile."""
    desired_titles: list[str] = []
    years_experience: float | None = None
    work_authorization: str = "Other / Prefer Not to Say"
    preferred_states: list[str] = []


class CandidateOut(Model):
    id: uuid.UUID
    name: str
    email: str = ""                                # "" when none was entered
    phone: str = ""
    notes: str = ""
    team: TeamRef | None = None
    assigned_member: UserRef | None = None         # the responsible member; null = unassigned
    is_active: bool
    has_resume: bool = False
    resume_has_file: bool = False                  # the current resume can be downloaded
    profile_completion: int = 0                    # 0..100
    profile_summary: CandidateProfileSummary = CandidateProfileSummary()
    last_assigned_at: datetime | None = None       # when the newest active assignment was made; null when there is none
    created_by: UserRef | None = None
    created_at: datetime
    updated_at: datetime
    stats: AssignmentStats = AssignmentStats()
    source: Literal["manual", "salesforce"] = "manual"      # typed in, or brought in from Salesforce
    salesforce_id: str | None = None               # the Person Account id; managers only (null for a Member)
    salesforce_url: str | None = None              # opens the record in Salesforce; managers only
    salesforce_synced_at: datetime | None = None   # last import or refresh from Salesforce


# ---------------------------------------------------------------- Salesforce (read-only)
SalesforceId = Annotated[str, Field(pattern=r"^[A-Za-z0-9]{18}$")]
TeamLeadScope = Literal["all", "own"]
ResumeSync = Literal["imported", "unchanged", "none", "kept_local", "refused_type", "refused_size", "unreadable", "unavailable"]


class SalesforceStatus(Model):
    configured: bool
    connected: bool | None = None                  # null when not configured; otherwise a check at most a minute old
    message: str = ""
    checked_at: datetime | None = None
    api_version: str = ""
    team_lead_scope: TeamLeadScope = "all"


RecruiterSync = Literal["assigned", "unchanged", "kept", "no_recruiter", "not_in_portal", "other_team"]


class SalesforceCandidate(Model):
    """A candidate as found in Salesforce (a Person Account), with whether it is already in the portal."""
    salesforce_id: str
    name: str
    email: str = ""
    phone: str = ""
    candidate_status: str = ""
    technology: str = ""
    niche: str = ""
    work_authorization: str = ""
    visa_status: str = ""
    years_experience: float | None = None
    preferred_job_type: str = ""
    preferred_locations: str = ""
    experience_level: str = ""
    assigned_recruiter: str = ""
    recruiter_member: UserRef | None = None        # the portal member with the recruiter's e-mail address, when the caller may see them
    marketing_team_lead: str = ""
    has_resume: bool = False
    imported_candidate_id: uuid.UUID | None = None      # only when the caller may see that candidate
    imported_elsewhere: bool = False               # in the portal, in a team the caller cannot see (nothing more is told)
    importable: bool = True


class SalesforceSearchOut(Model):
    items: list[SalesforceCandidate] = []
    limit_reached: bool = False                    # there may be more matches: narrow the search


class SalesforceImportIn(Model):
    salesforce_id: SalesforceId
    team_id: uuid.UUID | None = None               # required for an Admin when the candidate is new; a Team Lead's own team is used
    assigned_member_id: uuid.UUID | None = None    # an active MEMBER of that team


class SalesforceSync(Model):
    created: bool = False
    updated_fields: list[str] = []                 # written from Salesforce in this call
    kept_fields: list[str] = []                    # Salesforce differs, the local edit was kept
    resume: ResumeSync = "none"
    recruiter: RecruiterSync = "no_recruiter"      # what happened to the responsible member (see docs/salesforce-api.md)


class CandidateSyncOut(CandidateOut):
    sync: SalesforceSync = SalesforceSync()


# ---------------------------------------------------------------- jobs
class JobBase(Model):
    company: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=2, max_length=300)
    country: str = Field(default="United States", max_length=60)
    state: str = Field(default="", max_length=60)
    city: str = Field(default="", max_length=120)
    work_mode: Literal["Remote", "Hybrid", "On-site", "Not listed"] = "Not listed"
    employment_type: str = Field(default="", max_length=40)
    seniority: str = Field(default="", max_length=40)
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    salary_currency: str = Field(default="", max_length=8)
    salary_text: str = Field(default="", max_length=160)
    experience_min: float | None = Field(default=None, ge=0, le=60)
    experience_max: float | None = Field(default=None, ge=0, le=60)
    experience_text: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=60000)
    requirements: list[str] = Field(default_factory=list, max_length=80)
    canonical_url: str = Field(default="", max_length=2000)
    apply_url: str = Field(default="", max_length=2000)
    company_url: str = Field(default="", max_length=2000)
    citizenship_requirement: Literal["Required", "Not required", "Not stated"] = "Not stated"
    sponsorship_status: Literal["Available", "Not available", "Not stated"] = "Not stated"
    clearance_required: bool = False
    posted_at: date | None = None


class JobCreate(JobBase):
    pass


class JobUpdate(Model):
    company: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, min_length=2, max_length=300)
    state: str | None = Field(default=None, max_length=60)
    city: str | None = Field(default=None, max_length=120)
    work_mode: Literal["Remote", "Hybrid", "On-site", "Not listed"] | None = None
    employment_type: str | None = Field(default=None, max_length=40)
    seniority: str | None = Field(default=None, max_length=40)
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = Field(default=None, max_length=8)
    salary_text: str | None = Field(default=None, max_length=160)
    experience_text: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=60000)
    requirements: list[str] | None = None
    canonical_url: str | None = Field(default=None, max_length=2000)
    apply_url: str | None = Field(default=None, max_length=2000)
    citizenship_requirement: Literal["Required", "Not required", "Not stated"] | None = None
    sponsorship_status: Literal["Available", "Not available", "Not stated"] | None = None
    posted_at: date | None = None


class JobSummary(Model):
    """Fields safe for every role. Raw posting URLs are deliberately absent: members reach the posting only
    through the audited /api/assignments/{id}/open-job redirect."""
    id: uuid.UUID
    company: str
    title: str
    country: str
    state: str
    city: str
    location_text: str
    work_mode: str
    employment_type: str
    seniority: str
    salary_text: str
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = ""
    experience_text: str
    requirements: list[str] = []
    citizenship_requirement: str
    sponsorship_status: str
    clearance_required: bool = False
    posted_at: date | None = None
    last_verified_at: datetime | None = None
    verification_status: str
    source_label: str
    origin: str
    has_apply_link: bool = False
    created_at: datetime


class JobDetail(JobSummary):
    description: str = ""                          # plain text with line breaks; never HTML
    verification_note: str = ""
    sources: list[str] = []


class JobAssignee(Model):
    assignment_id: uuid.UUID
    candidate: CandidateRef
    member: UserRef | None = None                  # the member responsible for the candidate today; null = unassigned
    team: TeamRef | None = None
    status: AssignmentStatus
    assigned_at: datetime
    applied_at: datetime | None = None


class JobManagerView(JobDetail):
    """Admin / Team Lead view: includes the posting URLs and who has the job."""
    canonical_url: str = ""
    apply_url: str = ""
    company_url: str = ""
    external_source: str = ""
    external_job_id: str = ""
    assignment_count: int = 0
    queue_teams: list[TeamRef] = []
    assignees: list[JobAssignee] = []


class JobListItem(JobSummary):
    """Row of the manager job list."""
    canonical_url: str = ""
    assignment_count: int = 0
    applied_count: int = 0


class JobImportIn(Model):
    url: str = Field(min_length=10, max_length=2000)


class JobImportDraft(Model):
    """Details read from a posting URL for the Admin to review before saving. Nothing is stored yet."""
    verified: bool
    note: str
    job: JobCreate | None = None


class CsvImportResult(Model):
    created: int
    updated: int
    skipped: int
    errors: list[str] = []


class QueueDelegate(Model):
    job_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)


class QueueItemOut(Model):
    id: uuid.UUID
    team: TeamRef
    job: JobListItem
    status: Literal["pending", "assigned", "dismissed"]
    note: str
    delegated_by: UserRef | None = None
    created_at: datetime


class QueueUpdate(Model):
    status: Literal["pending", "dismissed"]


# ---------------------------------------------------------------- searches
class SearchFilters(Model):
    """Shared overrides for one run. A field left out (null) means: use each candidate's own saved value."""
    job_titles: list[str] | None = Field(default=None, max_length=8)
    years_experience: float | None = Field(default=None, ge=0, le=40)
    work_authorization: Authorization | None = None
    sponsorship_needed: Sponsorship | None = None
    exclude_citizenship_required: bool | None = None
    work_mode: WorkMode | None = None
    preferred_states: list[str] | None = Field(default=None, max_length=30)
    remote_only: bool | None = None
    excluded_companies: list[str] | None = Field(default=None, max_length=100)
    # The two below are settings of the search itself, not candidate overrides. Stored filters and presets written
    # before they existed have no such keys and mean the defaults.
    ats_platforms: list[str] = Field(default=[], max_length=40)   # platform ids of GET /api/searches/options; empty = all platforms and sources
    startup_focus: bool = False                    # startup platforms first and with the larger share of the budgets; tie-break in ranking only

    @field_validator("ats_platforms")
    @classmethod
    def _known_platforms(cls, v: list[str]) -> list[str]:
        from ..ats_platforms import chosen_platform_ids
        return chosen_platform_ids(v)


class SearchCreate(Model):
    candidate_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    team_id: uuid.UUID | None = None               # every active candidate of this team
    mode: SearchMode = "auto_assign"
    target_count: int = Field(default=50, ge=1, le=1000)     # per candidate; the server's own ceiling (limits.max_target_count) may be lower
    deep_search: bool = True
    min_match_score: int | None = Field(default=None, ge=0, le=100)
    filters: SearchFilters = SearchFilters()


class SearchTargetOut(Model):
    candidate: CandidateRef
    resume_id: uuid.UUID | None = None             # the resume version this search ran with (null: not started, no resume, or a search from before this was recorded)
    status: str                                    # queued | running | completed | failed | skipped | cancelled
    stage: str = ""
    message: str = ""
    discovered: int = 0
    verified: int = 0
    duplicates: int = 0
    matched: int = 0
    assigned: int = 0
    filters: dict = {}
    warnings: list[str] = []
    round: int = 1                                 # discovery round this candidate's search is in (or ended in)
    # title variants, search-engine queries, new boards, postings gained, rounds, stop reason, AI calls. Also (counts only):
    #   platform_filter: [platform id], startup_focus: bool, filtered_out: leads / postings dropped by the platform filter
    #   platforms: {platform id | "other": {label, startup, coverage: full | limited | none | "", considered, verified,
    #               unique, rejected, reasons: {not_relevant, non_us, work_mode, eligibility, ai_audit}, duplicates, matched, leads}}
    #   providers: {name: {kind: search_engine | ai, queries, results, leads, new_boards, new_board_postings, verified, matched}}
    #   seconds_to_first_verified, total_seconds: number | null;  coverage_warnings: [sentence]
    discovery: dict = {}


class SearchRunOut(Model):
    id: uuid.UUID
    mode: SearchMode
    status: SearchStatus
    cancel_requested: bool = False
    attempts: int = 0                              # 2 means the run was resumed once after a worker restart
    target_count: int                              # per candidate
    deep_search: bool
    min_match_score: int
    filters: dict = {}
    initiated_by: UserRef | None = None
    team: TeamRef | None = None
    candidate_count: int = 0
    discovered_count: int = 0                      # postings examined (largest value over the candidates)
    verified_count: int = 0                        # verified, US-eligible, deduplicated postings (largest over the candidates)
    duplicate_count: int = 0
    matched_count: int = 0                         # results kept, summed over the candidates
    assigned_count: int = 0                        # assignments created, summed over the candidates
    stage: str = ""
    sources: dict = {}                             # per source: boards_ok / boards_failed / pending / postings
    warnings: list[str] = []
    error_summary: str = ""
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    targets: list[SearchTargetOut] = []


class SearchSkipped(Model):
    candidate: CandidateRef
    reason: str


class SearchCreated(Model):
    search: SearchRunOut
    skipped: list[SearchSkipped] = []              # selected candidates that cannot be searched (no resume / titles)


class MatchExplanation(Model):
    score_breakdown: dict = {}                     # skills /35, title /25, experience /20, arrangement /10, additional /10
    score_source: str = ""                         # "ai" (OpenAI / Claude), "company_model" (company rerank model, calibrated) or "heuristic" (keyword based)
    matched_query: str = ""                        # the requested title or title variant this posting was found under
    matching_skills: list[str] = []
    missing_skills: list[str] = []
    reason: str = ""
    experience_comparison: str = ""
    authorization_status: str = "Unknown"          # Eligible | Potential mismatch | Unknown
    eligibility_note: str = "Not specified"


class SearchResultOut(Model):
    id: uuid.UUID
    candidate: CandidateRef
    job: JobListItem
    rank: int
    match_score: int
    match: MatchExplanation
    assignment_id: uuid.UUID | None = None         # set when this candidate already has the job
    assignment_status: AssignmentStatus | None = None


class SearchPlatformOption(Model):
    id: str
    label: str
    startup: bool = False                          # one of the startup-focused platforms
    reader: str = ""                               # name of its board reader among `sources`, "" when there is none
    reader_enabled: bool = False                   # a board reader for it is switched on
    access: str = ""                               # public_api | public_pages | restricted
    coverage: str = "full"                         # full | limited (public pages, through web search only) | none (cannot be searched)
    hint: str = ""                                 # one sentence: what can be read, and why


class SearchOptions(Model):
    platforms: list[SearchPlatformOption] = []     # the ATS platforms a search can be limited to
    sources: list[dict]
    ai_providers: dict
    limits: UiSettings
    authorizations: list[str]
    work_modes: list[str]
    queue_available: bool


class PresetIn(Model):
    name: str = Field(min_length=2, max_length=120)
    filters: dict                                  # the search form's values; checked against SearchCreate when saved

    @field_validator("filters")
    @classmethod
    def _filters_fit_a_search(cls, value: dict) -> dict:
        SearchCreate.model_validate({**value, "candidate_ids": [], "team_id": None})      # same limits as starting a search
        return value


class PresetOut(Model):
    id: uuid.UUID
    name: str
    filters: dict
    created_at: datetime


# ---------------------------------------------------------------- assignments
class AssignItem(Model):
    job_id: uuid.UUID
    candidate_id: uuid.UUID
    search_run_id: uuid.UUID | None = None


class AssignCreate(AssignItem):
    pass


class BulkAssign(Model):
    items: list[AssignItem] = Field(default_factory=list, max_length=1000)
    # Convenience: every job x every candidate (used by "assign selected jobs to selected candidates").
    job_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    candidate_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    search_run_id: uuid.UUID | None = None
    queue_item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)   # queue entries to mark as distributed


class BulkAssignResult(Model):
    created: int
    already_assigned: int
    assignment_ids: list[uuid.UUID] = []


class AssignmentOut(Model):
    id: uuid.UUID
    job: JobSummary
    candidate: CandidateRef
    member: UserRef | None = None                  # the member responsible for the candidate today; null = unassigned
    team: TeamRef | None = None
    status: AssignmentStatus
    is_active: bool = True
    source: str
    match_score: int | None = None
    assigned_at: datetime
    assigned_by: UserRef | None = None
    viewed_at: datetime | None = None
    applied_at: datetime | None = None
    link_opens: int = 0
    last_link_open_at: datetime | None = None
    note_count: int = 0


class EventOut(Model):
    id: uuid.UUID
    event_type: str
    old_status: str | None = None
    new_status: str | None = None
    actor: UserRef | None = None
    metadata: dict = {}
    created_at: datetime


class NoteOut(Model):
    id: uuid.UUID
    note: str
    is_private: bool
    author: UserRef | None = None
    created_at: datetime


class NoteIn(Model):
    note: str = Field(min_length=1, max_length=4000)
    is_private: bool = False                       # managers only; hidden from the responsible member


class StatusUpdate(Model):
    status: AssignmentStatus
    applied_at: AwareDatetime | None = None        # optional: the real submission time (with time zone) when recording it later
    reference: str | None = Field(default=None, max_length=200)      # confirmation / reference number
    note: str | None = Field(default=None, max_length=4000)


class AssignmentDetail(AssignmentOut):
    job: JobDetail
    match: MatchExplanation = MatchExplanation()
    application_reference: str = ""
    interviewed_at: datetime | None = None
    offer_at: datetime | None = None
    closed_at: datetime | None = None
    events: list[EventOut] = []
    notes: list[NoteOut] = []
    allowed_statuses: list[AssignmentStatus] = []
    other_assignees: list[JobAssignee] = []        # managers only, within their scope
    job_urls: dict | None = None                   # managers only: {"canonical_url": ..., "apply_url": ...}


class AssignmentSections(Model):
    """How many active assignments are in each section of a job list, within the caller's scope and the filters
    sent. Every assignment is in exactly one section, by its status, so the four add up to `total`:
    to_apply = ASSIGNED, VIEWED, APPLYING; applied = APPLIED; in_progress = INTERVIEWING, OFFER;
    closed = REJECTED, WITHDRAWN, SKIPPED."""
    to_apply: int = 0
    applied: int = 0
    in_progress: int = 0
    closed: int = 0
    total: int = 0


class ActivityItem(Model):
    id: uuid.UUID
    event_type: str
    old_status: str | None = None
    new_status: str | None = None
    created_at: datetime
    assignment_id: uuid.UUID
    job_title: str
    company: str
    candidate: CandidateRef
    member: UserRef | None = None                  # the member responsible for the candidate today
    actor: UserRef | None = None


# ---------------------------------------------------------------- analytics
class SeriesPoint(Model):
    label: str                                     # ISO date (day) or ISO week start
    assigned: int = 0
    applied: int = 0
    link_opens: int = 0
    interviews: int = 0
    offers: int = 0


class NameValue(Model):
    name: str
    value: float


class MemberRow(Model):
    """A member as the person responsible for candidates: totals over the candidates assigned to them today."""
    member: UserRef
    team: TeamRef | None = None
    status: str
    candidates: int = 0
    last_activity_at: datetime | None = None
    stats: AssignmentStats


class CandidateRow(Model):
    candidate: CandidateRef
    member: UserRef | None = None                  # responsible member; null = unassigned
    team: TeamRef | None = None
    is_active: bool = True
    has_resume: bool = False
    stats: AssignmentStats


class TeamRow(Model):
    team: TeamRef
    team_lead: UserRef | None = None
    is_active: bool = True
    members: int = 0
    candidates: int = 0
    stats: AssignmentStats


class SearchUsagePoint(Model):
    label: str
    runs: int = 0
    matched: int = 0
    assigned: int = 0


class MemberAnalytics(Model):
    member: UserRef
    team: TeamRef | None = None
    range: dict
    stats: AssignmentStats
    funnel: list[NameValue]
    status_distribution: list[NameValue]
    applications_over_time: list[SeriesPoint]
    weekly_activity: list[SeriesPoint]
    pending_vs_applied: list[NameValue]
    interview_outcomes: list[NameValue]
    candidates: list[CandidateRow] = []            # the candidates assigned to this member today; their stats add up to `stats`


class CandidateAnalytics(Model):
    """One candidate's numbers, in the shape of the member dashboard."""
    candidate: CandidateRef
    member: UserRef | None = None
    team: TeamRef | None = None
    range: dict
    stats: AssignmentStats
    funnel: list[NameValue]
    status_distribution: list[NameValue]
    applications_over_time: list[SeriesPoint]
    weekly_activity: list[SeriesPoint]
    pending_vs_applied: list[NameValue]
    interview_outcomes: list[NameValue]


class TeamAnalytics(Model):
    team: TeamRef
    team_lead: UserRef | None = None
    range: dict
    member_count: int
    candidate_count: int = 0                       # active candidates
    unassigned_candidates: int = 0                 # active candidates without a responsible member
    active_searches: int
    queue_pending: int
    stats: AssignmentStats
    funnel: list[NameValue]
    status_distribution: list[NameValue]
    applications_over_time: list[SeriesPoint]
    weekly_activity: list[SeriesPoint]
    pending_vs_applied: list[NameValue]
    members: list[MemberRow]                       # per responsible member
    candidates: list[CandidateRow] = []            # per candidate (all of the team's, unassigned ones included)


class AdminAnalytics(Model):
    range: dict
    totals: dict                                   # teams, team_leads, members, candidates, unassigned_candidates, indexed_jobs, job_postings, active_searches
    stats: AssignmentStats
    funnel: list[NameValue]
    status_distribution: list[NameValue]
    applications_over_time: list[SeriesPoint]
    weekly_activity: list[SeriesPoint]
    teams: list[TeamRow]
    search_usage: list[SearchUsagePoint]
    member_distribution: list[NameValue]           # responsible members per completion-rate band
    candidate_distribution: list[NameValue] = []   # candidates per completion-rate band


# ---------------------------------------------------------------- notifications / admin
class NotificationOut(Model):
    id: uuid.UUID
    type: str
    payload: dict
    read_at: datetime | None = None
    created_at: datetime


class AuditOut(Model):
    id: uuid.UUID
    action: str
    target_type: str
    target_id: str
    actor: UserRef | None = None
    team: TeamRef | None = None
    metadata: dict = {}
    ip: str = ""
    created_at: datetime


class SettingsOut(Model):
    watermark_enabled: bool = True
    default_target_count: int = 50
    default_min_match_score: int = 50
    max_members_per_search: int = 10
    max_results_per_search_run: int = 500
    deep_search_default: bool = True
    disabled_sources: list[str] = []
    profile_extra_fields: list[ExtraField] = []    # optional extra candidate-profile fields defined by the Admin
    invitation_hours: int = 48
    password_min_length: int = 10                  # from the environment (PASSWORD_MIN_LENGTH); shown, not editable here
    salesforce_team_lead_scope: TeamLeadScope = "all"      # which Salesforce candidates a Team Lead can find: all, or only their own
    test_mode: bool = False                        # the portal holds test data; ended once with POST /api/admin/go-live, never by PATCH


class GoLiveIn(Model):
    confirm: Literal["REAL DATA"]                  # typed by the Admin; anything else is refused


class GoLiveOut(Model):
    message: str
    removed: dict[str, int] = {}                   # teams, users, candidates, searches that were deleted


class SettingsUpdate(Model):
    watermark_enabled: bool | None = None
    default_target_count: int | None = Field(default=None, ge=1, le=1000)
    default_min_match_score: int | None = Field(default=None, ge=0, le=100)
    max_members_per_search: int | None = Field(default=None, ge=1, le=50)
    max_results_per_search_run: int | None = Field(default=None, ge=1, le=5000)
    deep_search_default: bool | None = None
    disabled_sources: list[str] | None = None
    profile_extra_fields: list[ExtraField] | None = Field(default=None, max_length=20)
    salesforce_team_lead_scope: TeamLeadScope | None = None

    @field_validator("profile_extra_fields")
    @classmethod
    def _unique_keys(cls, value):
        if value is not None and len({f.key for f in value}) != len(value):
            raise ValueError("Each extra field needs its own key")
        return value
    invitation_hours: int | None = Field(default=None, ge=1, le=720)


class IntegrationsOut(Model):
    ai_providers: dict                             # openai / claude: configured, model
    sources: list[dict]                            # name, label, kind, note, enabled, units, postings
    index: dict                                    # fresh_postings, stored_postings, fresh_units
    worker: dict                                   # online, last_seen, browser, active_searches
    email: dict                                    # backend: smtp | console | none
    queue: dict                                    # redis reachable
    salesforce: SalesforceStatus = SalesforceStatus(configured=False)
    aws_web_search: "AwsWebSearchStatus | None" = None


AtsState = Literal["verified_working", "configured_unverified", "public_discovery_only",
                   "temporarily_unavailable", "unsupported"]


class AtsPlatformHealth(Model):
    """One applicant-tracking system on GET /api/integrations/ats. `state` is computed from the evidence next to it
    (services/ats_health.py); a platform is `verified_working` only after a read that brought postings."""
    id: str
    label: str
    startup: bool
    access: str                                    # a key of ats_platforms.ACCESS
    access_note: str                               # the sentence for that key
    hosts: list[str]
    docs: str = ""
    note: str = ""
    reader: str = ""                               # registry source name of the board reader, "" when there is none
    reader_enabled: bool = False                   # the reader is installed, configured and switched on
    state: AtsState
    reason: str
    boards_known: int = 0
    boards_read_ok: int = 0                        # boards read successfully, with postings, inside the window
    boards_failed: int = 0                         # boards whose reads inside the window failed
    open_postings: int = 0                         # postings stored by successful reads (a posting found by two queries counts twice)
    last_success_at: datetime | None = None


class ProviderHealth(Model):
    """One search provider. Never "working": only whether it is set up and switched on, plus usage where it is counted."""
    id: str
    label: str
    kind: Literal["ai", "web_search", "catalogue"]
    state: Literal["configured", "not_configured", "switched_off"]
    reason: str
    detail: str = ""                               # the model name, or what the provider is used for
    month: str = ""                                # AWS web search: the month the counter is for
    used: int | None = None
    limit: int | None = None


class AtsHealthOut(Model):
    generated_at: datetime
    window_days: int                               # a successful read counts for this many days
    platforms: list[AtsPlatformHealth]
    providers: list[ProviderHealth]


class HealthOut(Model):
    status: str
    database: bool
    redis: bool
    worker_online: bool


# ---------------------------------------------------------------- AWS web search (managers)
_US_PLAIN = {"united states", "usa", "us", "remote", "remote us", "remote, us", "remote (us)", "nationwide"}


def _us_location(value: str) -> str:
    from ..job_normalizer import US_STATES
    text = " ".join(str(value).split()).strip(" ,")
    if not text or len(text) > 60 or not re.fullmatch(r"[A-Za-z .,'()-]+", text):
        raise ValueError("Locations are US cities or states, e.g. 'Austin, TX' or 'Texas'")
    names = {n.lower() for n in US_STATES.values()}
    last = text.rsplit(",", 1)[-1].strip()
    if text.lower() in _US_PLAIN or text.lower() in names or text.upper() in US_STATES \
            or ("," in text and (last.upper() in US_STATES or last.lower() in names)):
        return text
    raise ValueError(f"'{text}' is not a US location: use 'City, ST', a state, 'Remote' or 'United States'")


class WebSearchJobsIn(Model):
    """POST /api/web-search/jobs. Runs AWS web search queries, verifies every hit against the employer's live feed
    and returns only verified jobs. Nothing of the AWS results is stored."""
    keywords: str = Field(min_length=2, max_length=150)
    locations: list[str] = Field(default_factory=list, max_length=5)
    work_mode: Literal["remote", "hybrid", "onsite", "any"] = "any"
    limit: int = Field(default=20, ge=1, le=50)
    candidate_id: uuid.UUID | None = None

    @field_validator("keywords")
    @classmethod
    def _keywords(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 2 or not re.search(r"[A-Za-z]", v) or re.search(r"[<>{}\[\]\\|`]|https?:", v):
            raise ValueError("Enter job title keywords (letters, numbers and simple punctuation)")
        return v

    @field_validator("locations")
    @classmethod
    def _locations(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(_us_location(x) for x in v))


class WebSearchJobOut(Model):
    title: str
    company: str
    location: str
    work_mode: str
    posted_at: str = ""
    employer_url: str                  # the posting on the employer's own ATS / site (also the source URL)
    apply_url: str = ""
    source: str                        # the employer feed that verified it (Greenhouse, Lever, ...)
    verification: str
    verified_at: str = ""
    score: int
    score_source: str                  # "heuristic" (keyword match against the resume / profile, the engine's ranking)
    reason: str = ""
    matching_skills: list[str] = []
    missing_skills: list[str] = []
    discovered_via: Literal["aws_web_search"] = "aws_web_search"


class WebSearchStats(Model):
    queries_used: int                  # units of the monthly allowance this request used
    results_seen: int
    duplicates: int
    dropped_generic: int               # search / category / aggregator pages: never vacancies
    dropped_unverifiable: int          # not listed (any more) in the employer's live feed, or the feed could not be read
    dropped_not_relevant: int
    dropped_filtered: int              # outside the US, other work mode, eligibility
    verified: int
    returned: int
    monthly_used: int
    monthly_limit: int
    monthly_remaining: int


class WebSearchJobsOut(Model):
    jobs: list[WebSearchJobOut]
    stats: WebSearchStats
    warnings: list[str] = []
    candidate_id: uuid.UUID | None = None


class AwsWebSearchStatus(Model):
    enabled: bool
    region: str
    web_search_id: str
    month: str = ""
    used: int = 0
    limit: int = 0
    reserve: int = 0
    remaining: int = 0
    remaining_for_searches: int = 0
    counter_available: bool = False
    note: str = ""


IntegrationsOut.model_rebuild()
