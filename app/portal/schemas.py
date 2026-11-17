"""API request and response models. These are the contract the Next.js frontend is generated from."""
import uuid
from datetime import date, datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from ..schemas import Authorization, WorkMode

Role = Literal["ADMIN", "TEAM_LEAD", "MEMBER"]
AssignmentStatus = Literal["ASSIGNED", "VIEWED", "APPLYING", "APPLIED", "INTERVIEWING", "OFFER", "REJECTED", "WITHDRAWN", "SKIPPED"]
SearchStatus = Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
SearchMode = Literal["auto_assign", "review"]
Sponsorship = Literal["Yes", "No", "Unknown"]
T = TypeVar("T")


class Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


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


class UiSettings(Model):
    watermark_enabled: bool = True
    default_target_count: int = 50
    max_members_per_search: int = 10
    max_results_per_search_run: int = 500
    default_min_match_score: int = 50


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
class InvitationCreate(Model):
    name: str = Field(min_length=2, max_length=160)
    email: EmailStr
    role: Literal["TEAM_LEAD", "MEMBER"] = "MEMBER"
    team_id: uuid.UUID | None = None       # required for MEMBER invited by Admin; a Team Lead's own team is used (another team is refused)
    team_name: str | None = Field(default=None, max_length=120)      # Admin creating a Team Lead together with a new team
    team_description: str = Field(default="", max_length=2000)


class InvitationOut(Model):
    id: uuid.UUID
    email: str
    name: str
    role: Literal["TEAM_LEAD", "MEMBER"]
    team: TeamRef | None = None
    status: Literal["pending", "accepted", "expired", "revoked"]
    delivery: Literal["email", "manual"]
    expires_at: datetime
    accepted_at: datetime | None = None
    created_at: datetime
    invited_by: UserRef | None = None


class InvitationCreated(InvitationOut):
    invite_url: str                        # shown once to the authorized creator; copy it when e-mail is not configured
    email_sent: bool                       # true only when an SMTP server accepted the message


class InvitationPreview(Model):
    email: str
    name: str
    role: Literal["TEAM_LEAD", "MEMBER"]
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
    has_resume: bool = False
    profile_completion: int = 0                    # 0..100 (members only)
    last_login_at: datetime | None = None
    last_activity_at: datetime | None = None
    created_at: datetime
    stats: AssignmentStats | None = None           # members only


class UserUpdate(Model):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    is_active: bool | None = None                  # Admin: anyone but themselves; Team Lead: own members
    team_id: uuid.UUID | None = None               # Admin only: transfer a member to another team


class ResetLinkOut(Model):
    reset_url: str
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
    user_id: uuid.UUID
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
    user_id: uuid.UUID
    filename: str
    size_bytes: int
    version: int
    truncated: bool = False
    created_at: datetime
    uploaded_by: UserRef | None = None
    parsed_profile: dict = {}                      # skills, job_titles, education extracted from the text
    text: str | None = None                        # only on the single-resume endpoints
    ai_processing_notice: str = ("Resume text is sent to the configured AI providers (OpenAI, Anthropic) when a job "
                                 "search runs for this member.")


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
    member: UserRef
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
    """Shared overrides for one run. A field left out (null) means: use each member's own saved value."""
    job_titles: list[str] | None = Field(default=None, max_length=8)
    years_experience: float | None = Field(default=None, ge=0, le=40)
    work_authorization: Authorization | None = None
    sponsorship_needed: Sponsorship | None = None
    exclude_citizenship_required: bool | None = None
    work_mode: WorkMode | None = None
    preferred_states: list[str] | None = Field(default=None, max_length=30)
    remote_only: bool | None = None
    excluded_companies: list[str] | None = Field(default=None, max_length=100)


class SearchCreate(Model):
    member_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    team_id: uuid.UUID | None = None               # Admin: every active member of this team
    mode: SearchMode = "auto_assign"
    target_count: int = Field(default=50, ge=1, le=500)      # per member
    deep_search: bool = True
    min_match_score: int | None = Field(default=None, ge=0, le=100)
    filters: SearchFilters = SearchFilters()


class SearchTargetOut(Model):
    member: UserRef
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


class SearchRunOut(Model):
    id: uuid.UUID
    mode: SearchMode
    status: SearchStatus
    cancel_requested: bool = False
    attempts: int = 0                              # 2 means the run was resumed once after a worker restart
    target_count: int                              # per member
    deep_search: bool
    min_match_score: int
    filters: dict = {}
    initiated_by: UserRef | None = None
    team: TeamRef | None = None
    member_count: int = 0
    discovered_count: int = 0                      # postings examined (largest value over the members)
    verified_count: int = 0                        # verified, US-eligible, deduplicated candidates (largest over members)
    duplicate_count: int = 0
    matched_count: int = 0                         # results kept, summed over members
    assigned_count: int = 0                        # assignments created, summed over members
    stage: str = ""
    sources: dict = {}                             # per source: boards_ok / boards_failed / pending / postings
    warnings: list[str] = []
    error_summary: str = ""
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    targets: list[SearchTargetOut] = []


class SearchSkipped(Model):
    member: UserRef
    reason: str


class SearchCreated(Model):
    search: SearchRunOut
    skipped: list[SearchSkipped] = []              # selected members that cannot be searched (no resume / titles)


class MatchExplanation(Model):
    score_breakdown: dict = {}                     # skills /35, title /25, experience /20, arrangement /10, additional /10
    score_source: str = ""                         # "ai" or "heuristic" (keyword based)
    matching_skills: list[str] = []
    missing_skills: list[str] = []
    reason: str = ""
    experience_comparison: str = ""
    authorization_status: str = "Unknown"          # Eligible | Potential mismatch | Unknown
    eligibility_note: str = "Not specified"


class SearchResultOut(Model):
    id: uuid.UUID
    member: UserRef
    job: JobListItem
    rank: int
    match_score: int
    match: MatchExplanation
    assignment_id: uuid.UUID | None = None         # set when this member already has the job
    assignment_status: AssignmentStatus | None = None


class SearchOptions(Model):
    sources: list[dict]
    ai_providers: dict
    limits: UiSettings
    authorizations: list[str]
    work_modes: list[str]
    queue_available: bool


class PresetIn(Model):
    name: str = Field(min_length=2, max_length=120)
    filters: dict


class PresetOut(Model):
    id: uuid.UUID
    name: str
    filters: dict
    created_at: datetime


# ---------------------------------------------------------------- assignments
class AssignItem(Model):
    job_id: uuid.UUID
    member_id: uuid.UUID
    search_run_id: uuid.UUID | None = None


class AssignCreate(AssignItem):
    pass


class BulkAssign(Model):
    items: list[AssignItem] = Field(default_factory=list, max_length=1000)
    # Convenience: every job x every member (used by "assign selected jobs to selected members").
    job_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    member_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    search_run_id: uuid.UUID | None = None
    queue_item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)   # queue entries to mark as distributed


class BulkAssignResult(Model):
    created: int
    already_assigned: int
    assignment_ids: list[uuid.UUID] = []


class AssignmentOut(Model):
    id: uuid.UUID
    job: JobSummary
    member: UserRef
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
    is_private: bool = False                       # managers only; hidden from the member


class StatusUpdate(Model):
    status: AssignmentStatus
    applied_at: datetime | None = None             # optional: the real submission date when recording it later
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


class ActivityItem(Model):
    id: uuid.UUID
    event_type: str
    old_status: str | None = None
    new_status: str | None = None
    created_at: datetime
    assignment_id: uuid.UUID
    job_title: str
    company: str
    member: UserRef
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
    member: UserRef
    team: TeamRef | None = None
    status: str
    has_resume: bool
    last_activity_at: datetime | None = None
    stats: AssignmentStats


class TeamRow(Model):
    team: TeamRef
    team_lead: UserRef | None = None
    is_active: bool = True
    members: int = 0
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


class TeamAnalytics(Model):
    team: TeamRef
    team_lead: UserRef | None = None
    range: dict
    member_count: int
    active_searches: int
    queue_pending: int
    stats: AssignmentStats
    funnel: list[NameValue]
    status_distribution: list[NameValue]
    applications_over_time: list[SeriesPoint]
    weekly_activity: list[SeriesPoint]
    pending_vs_applied: list[NameValue]
    members: list[MemberRow]


class AdminAnalytics(Model):
    range: dict
    totals: dict                                   # teams, team_leads, members, indexed_jobs, job_postings, active_searches
    stats: AssignmentStats
    funnel: list[NameValue]
    status_distribution: list[NameValue]
    applications_over_time: list[SeriesPoint]
    weekly_activity: list[SeriesPoint]
    teams: list[TeamRow]
    search_usage: list[SearchUsagePoint]
    member_distribution: list[NameValue]           # members per completion-rate band


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
    profile_extra_fields: list[dict] = []          # optional extra profile fields defined by the Admin: {key, label, type}
    invitation_hours: int = 48


class SettingsUpdate(Model):
    watermark_enabled: bool | None = None
    default_target_count: int | None = Field(default=None, ge=1, le=500)
    default_min_match_score: int | None = Field(default=None, ge=0, le=100)
    max_members_per_search: int | None = Field(default=None, ge=1, le=50)
    max_results_per_search_run: int | None = Field(default=None, ge=1, le=5000)
    deep_search_default: bool | None = None
    disabled_sources: list[str] | None = None
    profile_extra_fields: list[dict] | None = Field(default=None, max_length=20)
    invitation_hours: int | None = Field(default=None, ge=1, le=720)


class IntegrationsOut(Model):
    ai_providers: dict                             # openai / claude: configured, model
    sources: list[dict]                            # name, label, kind, note, enabled, units, postings
    index: dict                                    # fresh_postings, stored_postings, fresh_units
    worker: dict                                   # online, last_seen, browser, active_searches
    email: dict                                    # backend: smtp | console | none
    queue: dict                                    # redis reachable


class HealthOut(Model):
    status: str
    database: bool
    redis: bool
    worker_online: bool
