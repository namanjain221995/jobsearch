"""PostgreSQL schema of the portal (users, teams, jobs, searches, assignments, tracking).

Conventions: UUID primary keys, timestamptz everywhere, logical deactivation instead of deletes,
JSONB for open-ended structures. The crawler's job index lives in separate `index_*` tables that are
created by raw SQL in the Alembic migration (see portal/index_pg.py)."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text,
                        UniqueConstraint, func)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, deferred, mapped_column, relationship

ROLES = ("ADMIN", "TEAM_LEAD", "MEMBER")
ASSIGNMENT_STATUSES = ("ASSIGNED", "VIEWED", "APPLYING", "APPLIED", "INTERVIEWING", "OFFER", "REJECTED", "WITHDRAWN", "SKIPPED")
SEARCH_STATUSES = ("queued", "running", "completed", "partial", "failed", "cancelled")
RESUME_SOURCES = ("upload", "salesforce")
VERIFICATION_STATUSES = ("verified", "unverified", "failed", "closed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class Base(DeclarativeBase):
    pass


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text("true"), nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()
    __table_args__ = (Index("uq_teams_name_lower", func.lower(name), unique=True),)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)          # stored lower-cased
    password_hash: Mapped[str | None] = mapped_column(Text)                   # NULL until the invitation is accepted
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text("true"), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()
    team: Mapped[Team | None] = relationship(lazy="joined", foreign_keys=[team_id])
    __table_args__ = (
        CheckConstraint(_in("role", ROLES), name="ck_users_role"),
        Index("uq_users_email", "email", unique=True),
        Index("ix_users_team_id", "team_id"),
        Index("ix_users_role", "role"),
    )

    @property
    def status(self) -> str:
        if not self.is_active:
            return "deactivated"
        return "active" if self.password_hash else "invited"


class TeamLeadAssignment(Base):
    """One primary Team Lead per team (team_id is the key). Extend with a role column for co-leads later."""
    __tablename__ = "team_lead_assignments"
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    created_at: Mapped[datetime] = created()


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[uuid.UUID] = pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    invited_role: Mapped[str] = mapped_column(String(20), nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)     # SHA-256 of the token; the token itself is never stored
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    delivery: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        CheckConstraint(_in("invited_role", ("TEAM_LEAD", "MEMBER")), name="ck_invitations_role"),
        Index("ix_invitations_team_id", "team_id"),
        Index("ix_invitations_user_id", "user_id"),
    )


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    remember: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"), nullable=False)
    ip: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    user_agent: Mapped[str] = mapped_column(String(300), default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = created()
    last_seen_at: Mapped[datetime] = created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_sessions_user_id", "user_id"),)


class PasswordReset(Base):
    __tablename__ = "password_resets"
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))   # NULL = requested by the user
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()
    __table_args__ = (Index("ix_password_resets_user_id", "user_id"),)


class Candidate(Base):
    """The person who needs a job. A record, never an account: a candidate cannot sign in. Belongs to one team and
    has at most one responsible member (a recruiter of that team) at a time; jobs and history stay with the candidate."""
    __tablename__ = "candidates"
    id: Mapped[uuid.UUID] = pk()
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))     # the API requires one on create
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str] = mapped_column(String(320), default="", server_default="", nullable=False)     # optional, lower-cased, not unique
    phone: Mapped[str] = mapped_column(String(40), default="", server_default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    assigned_member_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))   # the responsible member
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text("true"), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Set when the candidate was brought in from Salesforce (a Person Account's 18-character id). One Salesforce
    # candidate exists at most once in the whole portal. The snapshot holds the values last written from Salesforce,
    # so a refresh can tell a local edit from an unchanged field (see services/salesforce_sync.py).
    salesforce_id: Mapped[str | None] = mapped_column(String(18))
    salesforce_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    salesforce_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()
    __table_args__ = (
        Index("ix_candidates_team_id", "team_id"),
        Index("ix_candidates_assigned_member_id", "assigned_member_id"),
        Index("uq_candidates_salesforce_id", "salesforce_id", unique=True, postgresql_where=sa_text("salesforce_id IS NOT NULL")),
    )

    @property
    def source(self) -> str:
        return "salesforce" if self.salesforce_id else "manual"


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), primary_key=True)
    desired_titles: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)
    years_experience: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    skills: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)
    preferred_states: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)
    preferred_cities: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)
    work_mode: Mapped[str] = mapped_column(String(20), default="All", server_default="All", nullable=False)
    work_authorization: Mapped[str] = mapped_column(String(60), default="Other / Prefer Not to Say",
                                                    server_default="Other / Prefer Not to Say", nullable=False)
    sponsorship_needed: Mapped[str] = mapped_column(String(10), default="Unknown", server_default="Unknown", nullable=False)
    exclude_citizenship_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"), nullable=False)
    salary_min: Mapped[int | None] = mapped_column(Integer)
    seniority: Mapped[str] = mapped_column(String(40), default="", server_default="", nullable=False)
    excluded_companies: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)
    required_keywords: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)   # admin-defined optional fields
    # Answers the candidate gave for application forms (legal, EEO, links, ...): fixed schema in autoapply/records.py.
    # Never guessed; "unknown" until recorded. Only Admin and Team Lead read or write them.
    application_answers: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    application_answers_updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = updated()


class Resume(Base):
    """A candidate's resume: extracted text, structured profile and the uploaded file itself (so the responsible
    member can download it to apply). Resumes uploaded before the file was kept have file_data NULL."""
    __tablename__ = "resumes"
    id: Mapped[uuid.UUID] = pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), default="", server_default="", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    file_data: Mapped[bytes | None] = deferred(mapped_column(LargeBinary))      # loaded only by the download; erased when replaced
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"), nullable=False)
    parsed_profile: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text("true"), nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(20), default="upload", server_default="upload", nullable=False)     # upload | salesforce
    salesforce_content_version_id: Mapped[str | None] = mapped_column(String(18))     # the Salesforce file version this resume is a copy of
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        CheckConstraint(_in("source", RESUME_SOURCES), name="ck_resumes_source"),
        Index("ix_resumes_candidate_id", "candidate_id"),
        Index("uq_resumes_current", "candidate_id", unique=True, postgresql_where=sa_text("is_current")),
    )


class JobPosting(Base):
    """One row per distinct vacancy, shared by every team. Candidate-specific data lives on the assignment."""
    __tablename__ = "job_postings"
    id: Mapped[uuid.UUID] = pk()
    identity_key: Mapped[str] = mapped_column(String(200), nullable=False)       # "ats:<source>:<id>" | "url:<sha1>" | "manual:<uuid>"
    ctl_key: Mapped[str] = mapped_column(String(400), default="", server_default="", nullable=False)   # normalized company|title|location
    external_source: Mapped[str] = mapped_column(String(40), default="", server_default="", nullable=False)
    external_job_id: Mapped[str] = mapped_column(String(160), default="", server_default="", nullable=False)
    source_label: Mapped[str] = mapped_column(String(80), default="", server_default="", nullable=False)
    sources: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)   # provenance
    origin: Mapped[str] = mapped_column(String(20), default="search", server_default="search", nullable=False)       # search | manual | import
    company: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    country: Mapped[str] = mapped_column(String(60), default="", server_default="", nullable=False)
    state: Mapped[str] = mapped_column(String(60), default="", server_default="", nullable=False)
    city: Mapped[str] = mapped_column(String(120), default="", server_default="", nullable=False)
    location_text: Mapped[str] = mapped_column(String(400), default="", server_default="", nullable=False)
    work_mode: Mapped[str] = mapped_column(String(20), default="Not listed", server_default="Not listed", nullable=False)
    employment_type: Mapped[str] = mapped_column(String(40), default="", server_default="", nullable=False)
    seniority: Mapped[str] = mapped_column(String(40), default="", server_default="", nullable=False)
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str] = mapped_column(String(8), default="", server_default="", nullable=False)
    salary_text: Mapped[str] = mapped_column(String(160), default="", server_default="", nullable=False)            # as published
    experience_min: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    experience_max: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    experience_text: Mapped[str] = mapped_column(String(80), default="", server_default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)                 # plain text with line breaks; never HTML
    requirements: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)   # skills named in the posting
    canonical_url: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    apply_url: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    company_url: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    citizenship_requirement: Mapped[str] = mapped_column(String(20), default="Not stated", server_default="Not stated", nullable=False)
    sponsorship_status: Mapped[str] = mapped_column(String(20), default="Not stated", server_default="Not stated", nullable=False)
    clearance_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"), nullable=False)
    posted_at: Mapped[date | None] = mapped_column(Date)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_status: Mapped[str] = mapped_column(String(20), default="unverified", server_default="unverified", nullable=False)
    verification_note: Mapped[str] = mapped_column(String(300), default="", server_default="", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()
    __table_args__ = (
        CheckConstraint(_in("verification_status", VERIFICATION_STATUSES), name="ck_job_postings_verification"),
        Index("uq_job_postings_identity", "identity_key", unique=True),
        Index("ix_job_postings_source_id", "external_source", "external_job_id"),
        Index("ix_job_postings_ctl_key", "ctl_key"),
        Index("ix_job_postings_created_at", "created_at"),
        Index("ix_job_postings_company_lower", func.lower(company)),
    )


class SearchRun(Base):
    __tablename__ = "search_runs"
    id: Mapped[uuid.UUID] = pk()
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))     # NULL = candidates of several teams (Admin)
    mode: Mapped[str] = mapped_column(String(20), default="auto_assign", server_default="auto_assign", nullable=False)   # auto_assign | review
    search_filters: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)          # per candidate
    deep_search: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text("true"), nullable=False)
    min_match_score: Mapped[int] = mapped_column(Integer, default=50, server_default="50", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued", nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)     # times a worker started this run
    discovered_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    verified_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    matched_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    assigned_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    progress: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    error_summary: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(_in("status", SEARCH_STATUSES), name="ck_search_runs_status"),
        CheckConstraint(_in("mode", ("auto_assign", "review")), name="ck_search_runs_mode"),
        Index("ix_search_runs_status", "status"),
        Index("ix_search_runs_initiated_by", "initiated_by"),
        Index("ix_search_runs_team_id", "team_id"),
        Index("ix_search_runs_created_at", "created_at"),
    )


class SearchTarget(Base):
    __tablename__ = "search_targets"
    search_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("search_runs.id", ondelete="CASCADE"), primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), primary_key=True)
    resume_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("resumes.id", ondelete="SET NULL"))     # the resume version this search ran with
    candidate_specific_filters: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    assignment_mode: Mapped[str] = mapped_column(String(20), default="auto_assign", server_default="auto_assign", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued", nullable=False)   # queued|running|completed|failed|skipped|cancelled
    stats: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    __table_args__ = (Index("ix_search_targets_candidate_id", "candidate_id"),)


class SearchResult(Base):
    __tablename__ = "search_results"
    id: Mapped[uuid.UUID] = pk()
    search_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("search_runs.id", ondelete="CASCADE"), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    match_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    match_explanation: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        UniqueConstraint("search_run_id", "candidate_id", "job_id", name="uq_search_results_run_candidate_job"),
        Index("ix_search_results_run", "search_run_id"),
        Index("ix_search_results_job", "job_id"),
    )


class TeamJobQueue(Base):
    """Jobs an Admin delegated to a team; the Team Lead distributes them to the team's candidates."""
    __tablename__ = "team_job_queue"
    id: Mapped[uuid.UUID] = pk()
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False)
    delegated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", nullable=False)   # pending | assigned | dismissed
    note: Mapped[str] = mapped_column(String(500), default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()
    __table_args__ = (
        UniqueConstraint("team_id", "job_id", name="uq_team_job_queue_team_job"),
        CheckConstraint(_in("status", ("pending", "assigned", "dismissed")), name="ck_team_job_queue_status"),
        Index("ix_team_job_queue_team_status", "team_id", "status"),
    )


class JobAssignment(Base):
    """A job given to a candidate. The responsible member is not stored here: it is always the candidate's current
    assigned_member_id, so moving a candidate to another member moves every job and its history along."""
    __tablename__ = "job_assignments"
    id: Mapped[uuid.UUID] = pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_postings.id", ondelete="RESTRICT"), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="RESTRICT"), nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))     # the candidate's team
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    search_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("search_runs.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual", nullable=False)       # search | manual | queue
    match_score: Mapped[int | None] = mapped_column(Integer)
    match_explanation: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ASSIGNED", server_default="ASSIGNED", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text("true"), nullable=False)     # False = withdrawn by a manager (history kept)
    application_reference: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    assigned_at: Mapped[datetime] = created()
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    interviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = updated()
    __table_args__ = (
        UniqueConstraint("job_id", "candidate_id", name="uq_job_assignments_job_candidate"),
        CheckConstraint(_in("status", ASSIGNMENT_STATUSES), name="ck_job_assignments_status"),
        Index("ix_job_assignments_candidate_id", "candidate_id"),
        Index("ix_job_assignments_team_id", "team_id"),
        Index("ix_job_assignments_status", "status"),
        Index("ix_job_assignments_assigned_at", "assigned_at"),
        Index("ix_job_assignments_applied_at", "applied_at"),
        Index("ix_job_assignments_search_run", "search_run_id"),
    )


class ApplicationEvent(Base):
    __tablename__ = "application_events"
    id: Mapped[uuid.UUID] = pk()
    assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_assignments.id", ondelete="CASCADE"), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)      # assigned | viewed | link_opened | status_changed | note_added | unassigned
    old_status: Mapped[str | None] = mapped_column(String(20))
    new_status: Mapped[str | None] = mapped_column(String(20))
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        Index("ix_application_events_assignment_created", "assignment_id", "created_at"),
        Index("ix_application_events_type_created", "event_type", "created_at"),
    )


class AssignmentNote(Base):
    __tablename__ = "assignment_notes"
    id: Mapped[uuid.UUID] = pk()
    assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_assignments.id", ondelete="CASCADE"), nullable=False)
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[str] = mapped_column(Text, nullable=False)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"), nullable=False)   # manager-only note (hidden from the member)
    created_at: Mapped[datetime] = created()
    __table_args__ = (Index("ix_assignment_notes_assignment", "assignment_id", "created_at"),)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()
    __table_args__ = (Index("ix_notifications_user_created", "user_id", "created_at"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = pk()
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    target_type: Mapped[str] = mapped_column(String(40), default="", server_default="", nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    ip: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_actor", "actor_id"),
        Index("ix_audit_logs_action", "action"),
    )


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = updated()


class SearchPreset(Base):
    __tablename__ = "search_presets"
    id: Mapped[uuid.UUID] = pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    filters: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = created()
    __table_args__ = (Index("ix_search_presets_owner", "owner_id"),)


class LegacyRecord(Base):
    """Rows carried over from the pre-PostgreSQL SQLite file that have no direct home in the new schema
    (old single-user search history and its last verified results). Kept so nothing is lost."""
    __tablename__ = "legacy_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    legacy_id: Mapped[str] = mapped_column(String(80), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created()
    __table_args__ = (UniqueConstraint("kind", "legacy_id", name="uq_legacy_records_kind_id"),)


class AwsWebSearchUsage(Base):
    """AWS web search queries reserved per calendar month (UTC). Only a number: no query, no result
    (providers/aws_web_search.py, PgQuota)."""
    __tablename__ = "aws_web_search_usage"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (CheckConstraint("used >= 0", name="ck_aws_web_search_usage_used"),
                      CheckConstraint("month ~ '^[0-9]{4}-[0-9]{2}$'", name="ck_aws_web_search_usage_month"))


# ---------------------------------------------------------------- AI auto-apply (Admin only; docs/auto-apply-api.md)
CONSENT_METHODS = ("written", "email", "e_signature", "verbal_recorded")
AUTO_APPLY_STATUSES = ("queued", "preparing", "needs_input", "ready_for_review", "approved", "submitting", "submitted", "failed",
                       "blocked", "cancelled")
AUTO_APPLY_OPEN = ("queued", "preparing", "needs_input", "ready_for_review", "approved", "submitting")


class CandidateConsent(Base):
    """The candidate's permission for the portal to apply on their behalf. Auto-apply needs one active row
    (granted, not revoked). `may_attest` / `may_accept_data_processing` say whether the candidate also allowed
    ticking "my answers are true and correct" and the employer's privacy notice; both default to no."""
    __tablename__ = "candidate_consents"
    id: Mapped[uuid.UUID] = pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False)
    scope: Mapped[str] = mapped_column(String(30), default="auto_apply", server_default="auto_apply", nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_text("true"), nullable=False)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    document_reference: Mapped[str] = mapped_column(String(300), default="", server_default="", nullable=False)
    may_attest: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"), nullable=False)
    may_accept_data_processing: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"), nullable=False)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    recorded_at: Mapped[datetime] = created()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    __table_args__ = (
        CheckConstraint(_in("scope", ("auto_apply",)), name="ck_candidate_consents_scope"),
        CheckConstraint(_in("method", CONSENT_METHODS), name="ck_candidate_consents_method"),
        Index("ix_candidate_consents_candidate", "candidate_id", "recorded_at"),
    )


class AutoApplyRun(Base):
    """One attempt to apply one candidate to one assignment's posting. Holds the form snapshot (labels, types,
    options; no third-party personal data), the proposed answers with their sources, blockers and the outcome."""
    __tablename__ = "auto_apply_runs"
    id: Mapped[uuid.UUID] = pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False)
    assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_assignments.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False)
    consent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("candidate_consents.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued", nullable=False)
    ats: Mapped[str] = mapped_column(String(20), default="unsupported", server_default="unsupported", nullable=False)
    job_url: Mapped[str] = mapped_column(Text, nullable=False)
    employer_key: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
    form_schema: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)
    schema_hash: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    answers: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)
    answers_hash: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    admin_edits: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    blocked_reasons: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)
    needs_input: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)
    fill_report: Mapped[list] = mapped_column(JSONB, default=list, server_default=sa_text("'[]'::jsonb"), nullable=False)
    guard_report: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    confirmation: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    ai_usage: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False)
    outcome_unknown: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"), nullable=False)
    error: Mapped[str] = mapped_column(String(500), default="", server_default="", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = updated()
    __table_args__ = (
        CheckConstraint(_in("status", AUTO_APPLY_STATUSES), name="ck_auto_apply_runs_status"),
        CheckConstraint(_in("ats", ("greenhouse", "lever", "ashby", "workable", "unsupported")), name="ck_auto_apply_runs_ats"),
        # One open run per assignment at a time.
        Index("uq_auto_apply_runs_open", "assignment_id", unique=True,
              postgresql_where=sa_text("status IN ('queued', 'preparing', 'needs_input', 'ready_for_review', 'approved', 'submitting')")),
        # At most one application per candidate and posting, ever (a submit that may have gone through counts).
        Index("uq_auto_apply_runs_applied", "candidate_id", "job_id", unique=True,
              postgresql_where=sa_text("status IN ('submitting', 'submitted') OR outcome_unknown")),
        Index("ix_auto_apply_runs_candidate", "candidate_id", "created_at"),
        Index("ix_auto_apply_runs_status", "status"),
        Index("ix_auto_apply_runs_employer_submitted", "employer_key", "submitted_at"),
    )


class AutoApplyScreenshot(Base):
    """A JPEG of the form (dry run, just before submitting) or of the confirmation page. Size-capped; deleted
    after AUTO_APPLY_SCREENSHOT_RETENTION_DAYS by the worker."""
    __tablename__ = "auto_apply_screenshots"
    id: Mapped[uuid.UUID] = pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auto_apply_runs.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    content_type: Mapped[str] = mapped_column(String(40), default="image/jpeg", server_default="image/jpeg", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    created_at: Mapped[datetime] = created()
    __table_args__ = (
        CheckConstraint(_in("kind", ("dry_run", "before_submit", "confirmation")), name="ck_auto_apply_screenshots_kind"),
        CheckConstraint("size_bytes <= 2000000", name="ck_auto_apply_screenshots_size"),
        Index("ix_auto_apply_screenshots_run", "run_id"),
        Index("ix_auto_apply_screenshots_created", "created_at"),
    )
