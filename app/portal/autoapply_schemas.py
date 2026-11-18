"""Request / response models of the auto-apply feature (docs/auto-apply-api.md)."""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from ..autoapply.records import ApplicationAnswers
from .schemas import CandidateRef, Model, UserRef

RunStatus = Literal["queued", "preparing", "needs_input", "ready_for_review", "approved", "submitting", "submitted", "failed",
                    "blocked", "cancelled"]
Ats = Literal["greenhouse", "lever", "ashby", "workable", "unsupported"]
ConsentMethod = Literal["written", "email", "e_signature", "verbal_recorded"]


class AutoApplyRunCreate(Model):
    candidate_id: uuid.UUID
    assignment_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)


class AutoApplyJobRef(Model):
    id: uuid.UUID
    title: str
    company: str


class AutoApplyBlocker(Model):
    code: str
    detail: str = ""


class AutoApplyNeedsInput(Model):
    field_key: str
    label: str
    reason: str
    fill_in: str


class AutoApplyCounts(Model):
    fields: int = 0
    answered: int = 0
    needs_input: int = 0
    ai_drafted: int = 0


class AutoApplyRunOut(Model):
    id: uuid.UUID
    status: RunStatus
    ats: Ats
    job_url: str
    candidate: CandidateRef
    assignment_id: uuid.UUID
    job: AutoApplyJobRef
    blocked_reasons: list[AutoApplyBlocker] = []
    needs_input: list[AutoApplyNeedsInput] = []
    counts: AutoApplyCounts = AutoApplyCounts()
    outcome_unknown: bool = False
    error: str = ""
    created_by: UserRef | None = None
    approved_by: UserRef | None = None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    submitted_at: datetime | None = None
    finished_at: datetime | None = None


class AutoApplyFormField(Model):
    key: str
    label: str
    type: str
    required: bool = False
    options: list[str] = []
    section: str = ""
    multiple: bool = False


class AutoApplyAnswer(Model):
    field_key: str
    label: str
    value: str = ""
    source: str = ""
    evidence: list[str] = []
    classification: str = ""
    required: bool = False
    status: Literal["ok", "missing", "blocked"] = "ok"
    note: str = ""
    type: str = "text"


class AutoApplyFillEntry(Model):
    field_key: str
    label: str
    value: str = ""
    filled: bool = False
    error: str = ""


class AutoApplyScreenshotOut(Model):
    id: uuid.UUID
    kind: Literal["dry_run", "before_submit", "confirmation"]
    created_at: datetime
    size_bytes: int


class AutoApplyConfirmation(Model):
    detected: bool = False
    text_sha256: str = ""
    reference: str = ""
    url: str = ""


class AutoApplyGuardReport(Model):
    dry_run: bool = True
    blocked_requests: int = 0
    allowed_submissions: int = 0


class AutoApplyRunDetail(AutoApplyRunOut):
    form: list[AutoApplyFormField] = []
    answers: list[AutoApplyAnswer] = []
    fill_report: list[AutoApplyFillEntry] = []
    screenshots: list[AutoApplyScreenshotOut] = []
    answers_hash: str = ""
    confirmation: AutoApplyConfirmation | None = None
    submission_guard: AutoApplyGuardReport | None = None


class AutoApplySkipped(Model):
    assignment_id: uuid.UUID
    reason: Literal["not_found", "not_this_candidate", "withdrawn", "already_applied", "run_open", "already_submitted",
                    "no_application_url"]


class AutoApplyRunsCreated(Model):
    runs: list[AutoApplyRunOut]
    skipped: list[AutoApplySkipped] = []


class AutoApplyAnswerEdit(Model):
    field_key: str = Field(min_length=1, max_length=120)
    value: str = Field(max_length=5000)


class AutoApplyAnswersPatch(Model):
    answers: list[AutoApplyAnswerEdit] = Field(min_length=1, max_length=100)


class AutoApplyApproveIn(Model):
    answers_hash: str = Field(min_length=64, max_length=64)


class AutoApplyBatchItem(Model):
    run_id: uuid.UUID
    answers_hash: str = Field(min_length=64, max_length=64)


class AutoApplyBatchApproveIn(Model):
    items: list[AutoApplyBatchItem] = Field(min_length=1, max_length=50)


RefusalReason = Literal["not_found", "not_ready", "answers_changed", "no_consent", "assignment_changed", "employer_daily_cap",
                        "disabled", "already_submitted"]


class AutoApplyRefusal(Model):
    run_id: uuid.UUID
    reason: RefusalReason
    detail: str = ""


class AutoApplyBatchApproveOut(Model):
    approved: list[uuid.UUID] = []
    refused: list[AutoApplyRefusal] = []


class AutoApplyStatus(Model):
    enabled: bool
    worker_online: bool
    browser_concurrency: int
    employer_daily_cap: int
    run_timeout_seconds: float
    screenshot_retention_days: int
    supported_ats: list[str]


class AutoApplyEligibility(Model):
    """One open assignment of the candidate and whether a run can be started for it."""
    assignment_id: uuid.UUID
    job: AutoApplyJobRef
    status: str
    ats: Ats
    application_url: str = ""
    supported: bool
    eligible: bool                  # supported and no open run and never submitted
    reason_code: str = ""           # blocker code (unsupported_site, account_creation_required, custom_career_site) or skip reason
    reason: str = ""
    open_run_id: uuid.UUID | None = None
    open_run_status: RunStatus | None = None


class AutoApplyEligibilityOut(Model):
    candidate_id: uuid.UUID
    has_active_consent: bool
    has_resume_file: bool
    items: list[AutoApplyEligibility]


class ConsentIn(Model):
    scope: Literal["auto_apply"] = "auto_apply"
    granted: bool = True
    method: ConsentMethod
    note: str = Field(default="", max_length=2000)
    document_reference: str = Field(default="", max_length=300)
    may_attest: bool = False
    may_accept_data_processing: bool = False


class ConsentRevokeIn(Model):
    note: str = Field(default="", max_length=2000)


class ConsentOut(Model):
    id: uuid.UUID
    candidate_id: uuid.UUID
    scope: str
    granted: bool
    method: str
    note: str
    document_reference: str
    may_attest: bool
    may_accept_data_processing: bool
    recorded_by: UserRef | None = None
    recorded_at: datetime
    revoked_at: datetime | None = None
    revoked_by: UserRef | None = None
    active: bool


class ApplicationAnswersIn(ApplicationAnswers):
    """Only the fields that are sent change."""
    model_config = {"extra": "forbid"}


class EffectiveValue(Model):
    value: str | list[str] | bool
    source: str


class ApplicationAnswersOut(ApplicationAnswers):
    candidate_id: uuid.UUID
    effective: dict[str, EffectiveValue] = {}
    updated_at: datetime | None = None
    updated_by: UserRef | None = None
