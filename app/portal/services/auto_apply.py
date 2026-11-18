"""AI auto-apply: runs, consents and recorded application answers (Admin only; docs/auto-apply-api.md).

The API creates runs and queues worker jobs; the worker (autoapply/runner.py) does all browser work. Rules kept here:
- a run needs an active consent when it is created, approved and submitted; revoking the consent cancels every open
  run of the candidate;
- one open run per assignment (partial unique index), one submission per candidate and posting ever (another one);
- approval is for an exact preview: the client sends the answers hash it displayed, and the run must be
  ready_for_review with that hash; the employer's daily cap is checked again;
- nothing about runs or consents is returned to Team Leads or members."""
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from ...autoapply import records
from ...autoapply.detect import SUPPORTED, detect, employer_key
from ...autoapply.schema import answers_hash
from ...config import Settings
from ..autoapply_schemas import (ApplicationAnswersIn, ApplicationAnswersOut, AutoApplyBatchApproveIn, AutoApplyBatchApproveOut,
                                 AutoApplyEligibility, AutoApplyEligibilityOut, AutoApplyJobRef, AutoApplyRefusal, AutoApplyRunCreate,
                                 AutoApplyRunDetail, AutoApplyRunOut, AutoApplyRunsCreated, AutoApplyScreenshotOut, AutoApplySkipped,
                                 ConsentIn, ConsentOut, EffectiveValue)
from ..deps import Principal
from ..models import (AUTO_APPLY_OPEN, AutoApplyRun, AutoApplyScreenshot, Candidate, CandidateConsent, JobAssignment, JobPosting, Resume,
                      User)
from ..security import utcnow
from . import people
from .audit import audit, notify
from .common import candidate_ref, user_ref, users_by_id

R = AutoApplyRun
OPEN_ASSIGNMENT_STATUSES = ("ASSIGNED", "VIEWED", "APPLYING")
NOTIFY_ADMIN = {"needs_input": "auto_apply_needs_input", "blocked": "auto_apply_blocked", "ready_for_review": "auto_apply_ready",
                "submitted": "auto_apply_submitted", "failed": "auto_apply_failed"}


class Refused(Exception):
    def __init__(self, reason: str, detail: str):
        self.reason, self.detail = reason, detail


def ensure_enabled(settings: Settings) -> None:
    if not settings.auto_apply_enabled:
        raise HTTPException(409, "Auto-apply is switched off (AUTO_APPLY_ENABLED=false).")


# ---------------------------------------------------------------- consents
async def active_consent(db: AsyncSession, candidate_id: uuid.UUID) -> CandidateConsent | None:
    return (await db.execute(select(CandidateConsent).where(
        CandidateConsent.candidate_id == candidate_id, CandidateConsent.scope == "auto_apply", CandidateConsent.granted.is_(True),
        CandidateConsent.revoked_at.is_(None)).order_by(CandidateConsent.recorded_at.desc()).limit(1))).scalar_one_or_none()


async def consents_out(db: AsyncSession, rows: list[CandidateConsent]) -> list[ConsentOut]:
    users = await users_by_id(db, {r.recorded_by for r in rows} | {r.revoked_by for r in rows})
    return [ConsentOut(id=r.id, candidate_id=r.candidate_id, scope=r.scope, granted=r.granted, method=r.method, note=r.note,
                       document_reference=r.document_reference, may_attest=r.may_attest, may_accept_data_processing=r.may_accept_data_processing,
                       recorded_by=user_ref(users.get(r.recorded_by)), recorded_at=r.recorded_at, revoked_at=r.revoked_at,
                       revoked_by=user_ref(users.get(r.revoked_by)), active=r.granted and r.revoked_at is None) for r in rows]


async def list_consents(db: AsyncSession, candidate: Candidate) -> list[CandidateConsent]:
    return list((await db.execute(select(CandidateConsent).where(CandidateConsent.candidate_id == candidate.id)
                                  .order_by(CandidateConsent.recorded_at.desc(), CandidateConsent.id))).scalars())


async def cancel_open_runs(db: AsyncSession, candidate_id: uuid.UUID, reason: str) -> int:
    """Open runs that have not started submitting are cancelled. A run already submitting cannot be stopped midway;
    the worker re-checks the consent right before the click."""
    rows = (await db.execute(update(R).where(R.candidate_id == candidate_id, R.status.in_(AUTO_APPLY_OPEN), R.status != "submitting")
                             .values(status="cancelled", error=reason, finished_at=utcnow()).returning(R.id))).scalars().all()
    return len(rows)


async def record_consent(db: AsyncSession, principal: Principal, candidate: Candidate, data: ConsentIn, ip: str) -> CandidateConsent:
    now = utcnow()
    previous = await active_consent(db, candidate.id)
    if previous is not None:                       # a new record supersedes the old one
        previous.revoked_at, previous.revoked_by = now, principal.user.id
    row = CandidateConsent(candidate_id=candidate.id, scope=data.scope, granted=data.granted, method=data.method, note=data.note.strip(),
                           document_reference=data.document_reference.strip(), may_attest=data.may_attest,
                           may_accept_data_processing=data.may_accept_data_processing, recorded_by=principal.user.id)
    db.add(row)
    cancelled = 0 if data.granted else await cancel_open_runs(db, candidate.id, "consent_revoked")
    audit(db, principal.user, "consent.recorded", "candidate", candidate.id, candidate.team_id,
          {"granted": data.granted, "method": data.method, "may_attest": data.may_attest,
           "may_accept_data_processing": data.may_accept_data_processing, "superseded": previous is not None, "runs_cancelled": cancelled}, ip)
    await db.flush()
    return row


async def revoke_consent(db: AsyncSession, principal: Principal, candidate: Candidate, consent_id: uuid.UUID, note: str, ip: str) -> CandidateConsent:
    row = (await db.execute(select(CandidateConsent).where(CandidateConsent.id == consent_id,
                                                           CandidateConsent.candidate_id == candidate.id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Consent not found")
    if row.revoked_at is None:
        row.revoked_at, row.revoked_by = utcnow(), principal.user.id
        if note.strip():
            row.note = (row.note + "\nRevoked: " + note.strip()).strip()[:4000]
    cancelled = await cancel_open_runs(db, candidate.id, "consent_revoked")
    audit(db, principal.user, "consent.revoked", "candidate", candidate.id, candidate.team_id,
          {"consent_id": str(row.id), "runs_cancelled": cancelled}, ip)
    await db.flush()
    return row


# ---------------------------------------------------------------- recorded application answers
async def answers_out(db: AsyncSession, candidate: Candidate) -> ApplicationAnswersOut:
    profile = await people.get_profile(db, candidate.id)
    stored = records.stored(profile.application_answers)
    eff = records.effective(profile.application_answers, profile=profile, candidate=candidate)
    users = await users_by_id(db, {profile.application_answers_updated_by})
    return ApplicationAnswersOut(**stored.model_dump(), candidate_id=candidate.id,
                                 effective={k: EffectiveValue(value=v.value, source=v.source) for k, v in eff.items()},
                                 updated_at=profile.updated_at if profile.application_answers else None,
                                 updated_by=user_ref(users.get(profile.application_answers_updated_by)))


async def update_answers(db: AsyncSession, principal: Principal, candidate: Candidate, data: ApplicationAnswersIn, ip: str) -> None:
    profile = await people.get_profile(db, candidate.id)
    sent = data.model_dump(exclude_unset=True)
    merged = {**(profile.application_answers or {}), **sent}
    profile.application_answers = records.ApplicationAnswers(**merged).model_dump()
    # Keys that were never sent stay unrecorded (unknown): store only what someone recorded.
    profile.application_answers = {k: v for k, v in profile.application_answers.items() if k in merged}
    profile.application_answers_updated_by = principal.user.id
    audit(db, principal.user, "application_answers.updated", "candidate", candidate.id, candidate.team_id, {"fields": sorted(sent)}, ip)


# ---------------------------------------------------------------- runs: output
def _counts(run: AutoApplyRun) -> dict:
    answers = run.answers or []
    return {"fields": len(run.form_schema or []), "answered": sum(1 for a in answers if a.get("value") and a.get("status") == "ok"),
            "needs_input": len(run.needs_input or []), "ai_drafted": sum(1 for a in answers if a.get("source") == "ai_draft")}


async def runs_out(db: AsyncSession, runs: list[AutoApplyRun]) -> list[AutoApplyRunOut]:
    if not runs:
        return []
    candidates = {c.id: c for c in (await db.execute(select(Candidate).where(Candidate.id.in_({r.candidate_id for r in runs})))).scalars()}
    jobs = {j.id: j for j in (await db.execute(select(JobPosting).where(JobPosting.id.in_({r.job_id for r in runs})))).scalars()}
    users = await users_by_id(db, {r.created_by for r in runs} | {r.approved_by for r in runs})
    return [AutoApplyRunOut(
        id=r.id, status=r.status, ats=r.ats, job_url=r.job_url, candidate=candidate_ref(candidates[r.candidate_id]),
        assignment_id=r.assignment_id, job=AutoApplyJobRef(id=r.job_id, title=jobs[r.job_id].title, company=jobs[r.job_id].company),
        blocked_reasons=r.blocked_reasons or [], needs_input=r.needs_input or [], counts=_counts(r), outcome_unknown=r.outcome_unknown,
        error=r.error, created_by=user_ref(users.get(r.created_by)), approved_by=user_ref(users.get(r.approved_by)),
        created_at=r.created_at, updated_at=r.updated_at, approved_at=r.approved_at, submitted_at=r.submitted_at, finished_at=r.finished_at)
        for r in runs]


async def run_detail(db: AsyncSession, run: AutoApplyRun) -> AutoApplyRunDetail:
    base = (await runs_out(db, [run]))[0]
    shots = (await db.execute(select(AutoApplyScreenshot.id, AutoApplyScreenshot.kind, AutoApplyScreenshot.created_at,
                                     AutoApplyScreenshot.size_bytes).where(AutoApplyScreenshot.run_id == run.id)
                              .order_by(AutoApplyScreenshot.created_at))).all()
    guard = run.guard_report or None
    return AutoApplyRunDetail(
        **base.model_dump(), form=run.form_schema or [], answers=run.answers or [], fill_report=run.fill_report or [],
        screenshots=[AutoApplyScreenshotOut(id=s[0], kind=s[1], created_at=s[2], size_bytes=s[3]) for s in shots],
        answers_hash=run.answers_hash, confirmation=run.confirmation or None,
        submission_guard={k: guard[k] for k in ("dry_run", "blocked_requests", "allowed_submissions") if k in guard} if guard else None)


async def load_run(db: AsyncSession, run_id: uuid.UUID, lock: bool = False) -> AutoApplyRun:
    stmt = select(R).where(R.id == run_id)
    if lock:
        stmt = stmt.with_for_update()
    run = (await db.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


# ---------------------------------------------------------------- runs: creating
async def _assignment_rows(db: AsyncSession, candidate_id: uuid.UUID):
    return (await db.execute(select(JobAssignment, JobPosting).join(JobPosting, JobPosting.id == JobAssignment.job_id)
                             .where(JobAssignment.candidate_id == candidate_id))).all()


async def _open_and_submitted(db: AsyncSession, candidate_id: uuid.UUID) -> tuple[dict, set]:
    rows = (await db.execute(select(R.id, R.assignment_id, R.job_id, R.status, R.outcome_unknown).where(R.candidate_id == candidate_id))).all()
    open_runs = {r.assignment_id: (r.id, r.status) for r in rows if r.status in AUTO_APPLY_OPEN}
    done = {r.job_id for r in rows if r.status in ("submitting", "submitted") or r.outcome_unknown}
    return open_runs, done


def application_url(posting: JobPosting) -> str:
    return (posting.apply_url or posting.canonical_url or "").strip()


async def create_runs(db: AsyncSession, principal: Principal, settings: Settings, data: AutoApplyRunCreate, ip: str
                      ) -> tuple[list[AutoApplyRun], list[AutoApplySkipped]]:
    ensure_enabled(settings)
    candidate = await db.get(Candidate, data.candidate_id)
    if candidate is None:
        raise HTTPException(404, "Candidate not found")
    if not candidate.is_active:
        raise HTTPException(409, "This candidate is deactivated")
    consent = await active_consent(db, candidate.id)
    if consent is None:
        raise HTTPException(409, "The candidate has no active auto-apply consent. Record the consent first.")
    if len(data.assignment_ids) > settings.auto_apply_max_runs_per_request:
        raise HTTPException(422, f"At most {settings.auto_apply_max_runs_per_request} assignments per request")
    wanted = list(dict.fromkeys(data.assignment_ids))
    found = {a.id: (a, p) for a, p in (await db.execute(select(JobAssignment, JobPosting).join(JobPosting, JobPosting.id == JobAssignment.job_id)
                                                        .where(JobAssignment.id.in_(wanted)))).all()}
    open_runs, done = await _open_and_submitted(db, candidate.id)
    runs, skipped = [], []
    for assignment_id in wanted:
        hit = found.get(assignment_id)
        reason = None
        if hit is None:
            reason = "not_found"
        else:
            a, posting = hit
            if a.candidate_id != candidate.id:
                reason = "not_this_candidate"
            elif not a.is_active:
                reason = "withdrawn"
            elif a.applied_at is not None or a.status not in OPEN_ASSIGNMENT_STATUSES:
                reason = "already_applied"
            elif a.id in open_runs:
                reason = "run_open"
            elif a.job_id in done:
                reason = "already_submitted"
            elif not application_url(posting):
                reason = "no_application_url"
        if reason:
            skipped.append(AutoApplySkipped(assignment_id=assignment_id, reason=reason))
            continue
        target = detect(application_url(posting))
        run = R(id=uuid.uuid4(), candidate_id=candidate.id, assignment_id=a.id, job_id=a.job_id, consent_id=consent.id, status="queued",
                ats=target.ats if target.ats in SUPPORTED else "unsupported", job_url=target.url or application_url(posting)[:2000],
                employer_key=employer_key(target.ats, target.company_slug, posting.company), created_by=principal.user.id)
        db.add(run)
        runs.append(run)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Another run for one of these assignments was started at the same time; reload and try again")
    if runs:
        audit(db, principal.user, "auto_apply.runs_created", "candidate", candidate.id, candidate.team_id,
              {"runs": [str(r.id) for r in runs], "skipped": len(skipped)}, ip)
    return runs, skipped


# ---------------------------------------------------------------- eligibility (start dialog)
async def eligibility(db: AsyncSession, candidate: Candidate) -> AutoApplyEligibilityOut:
    open_runs, done = await _open_and_submitted(db, candidate.id)
    items = []
    for a, posting in await _assignment_rows(db, candidate.id):
        if not a.is_active or a.applied_at is not None or a.status not in OPEN_ASSIGNMENT_STATUSES:
            continue
        url = application_url(posting)
        target = detect(url) if url else None
        supported = bool(target and target.ats in SUPPORTED)
        code, reason = "", ""
        if not url:
            code, reason = "no_application_url", "The job has no application address"
        elif not supported:
            code, reason = target.code or "unsupported_site", target.reason
        elif a.id in open_runs:
            code, reason = "run_open", "A run for this assignment is open"
        elif a.job_id in done:
            code, reason = "already_submitted", "An application to this posting was already submitted (or may have been)"
        run = open_runs.get(a.id)
        items.append(AutoApplyEligibility(
            assignment_id=a.id, job=AutoApplyJobRef(id=posting.id, title=posting.title, company=posting.company), status=a.status,
            ats=target.ats if supported else "unsupported", application_url=(target.url if supported else url)[:2000], supported=supported,
            eligible=supported and not code, reason_code=code, reason=reason,
            open_run_id=run[0] if run else None, open_run_status=run[1] if run else None))
    has_file = (await db.execute(select(Resume.id).where(Resume.candidate_id == candidate.id, Resume.is_current,
                                                         Resume.file_data.is_not(None)))).first() is not None
    return AutoApplyEligibilityOut(candidate_id=candidate.id, has_active_consent=await active_consent(db, candidate.id) is not None,
                                   has_resume_file=has_file, items=items)


# ---------------------------------------------------------------- runs: review, approve, cancel
async def patch_answers(db: AsyncSession, principal: Principal, run: AutoApplyRun, edits: list, ip: str) -> None:
    if run.status not in ("needs_input", "ready_for_review"):
        raise HTTPException(409, f"Answers can only be edited while the run is waiting for review (status {run.status})")
    by_key = {a["field_key"]: a for a in run.answers or []}
    merged = dict(run.admin_edits or {})
    for edit in edits:
        current = by_key.get(edit.field_key)
        if current is None:
            raise HTTPException(422, f"No field {edit.field_key!r} on this form")
        if current.get("classification", "").startswith("certification:"):
            raise HTTPException(422, "Certifications, consents and agreements cannot be answered for the candidate here")
        if current.get("classification", "").startswith("eeo:") or current.get("classification", "").startswith("legal:"):
            pass                                          # allowed: the Admin types what the candidate told them; recorded as admin_edit
        merged[edit.field_key] = edit.value
    run.admin_edits = merged
    run.status, run.answers_hash, run.error = "preparing", "", ""
    audit(db, principal.user, "auto_apply.answers_edited", "auto_apply_run", run.id, None, {"fields": sorted(e.field_key for e in edits)}, ip)


async def employer_submissions_today(db: AsyncSession, employer: str, exclude: uuid.UUID | None = None) -> int:
    since = utcnow() - timedelta(hours=24)
    stmt = select(func.count()).select_from(R).where(R.employer_key == employer, R.status.in_(("approved", "submitting", "submitted")),
                                                     func.coalesce(R.submitted_at, R.approved_at) >= since)
    if exclude is not None:
        stmt = stmt.where(R.id != exclude)
    return (await db.execute(stmt)).scalar_one()


async def check_approvable(db: AsyncSession, settings: Settings, run: AutoApplyRun, given_hash: str) -> None:
    if not settings.auto_apply_enabled:
        raise Refused("disabled", "Auto-apply is switched off")
    if run.status != "ready_for_review":
        raise Refused("not_ready", f"The run is {run.status}, not ready for review")
    if not run.answers_hash or run.answers_hash != answers_hash(run.answers or [], run.schema_hash) or given_hash != run.answers_hash:
        raise Refused("answers_changed", "The answers changed since this preview; reload the run and review it again")
    if await active_consent(db, run.candidate_id) is None:
        raise Refused("no_consent", "The candidate's consent is no longer active")
    a = await db.get(JobAssignment, run.assignment_id)
    if a is None or not a.is_active or a.applied_at is not None or a.status not in OPEN_ASSIGNMENT_STATUSES:
        raise Refused("assignment_changed", "The assignment was applied to or withdrawn meanwhile")
    done = (await db.execute(select(R.id).where(R.candidate_id == run.candidate_id, R.job_id == run.job_id, R.id != run.id,
                                                (R.status.in_(("submitting", "submitted"))) | R.outcome_unknown))).first()
    if done:
        raise Refused("already_submitted", "This posting was already applied to for the candidate")
    if await employer_submissions_today(db, run.employer_key, exclude=run.id) >= settings.auto_apply_employer_daily_cap:
        raise Refused("employer_daily_cap", f"{settings.auto_apply_employer_daily_cap} applications to this employer in the last 24 hours")


async def approve(db: AsyncSession, principal: Principal, settings: Settings, run: AutoApplyRun, given_hash: str, ip: str) -> None:
    await check_approvable(db, settings, run, given_hash)
    run.status, run.approved_by, run.approved_at = "approved", principal.user.id, utcnow()
    audit(db, principal.user, "auto_apply.approved", "auto_apply_run", run.id, None, {"answers_hash": run.answers_hash}, ip)


async def approve_batch(db: AsyncSession, principal: Principal, settings: Settings, data: AutoApplyBatchApproveIn, ip: str
                        ) -> AutoApplyBatchApproveOut:
    out = AutoApplyBatchApproveOut()
    for item in data.items:
        run = (await db.execute(select(R).where(R.id == item.run_id).with_for_update())).scalar_one_or_none()
        if run is None:
            out.refused.append(AutoApplyRefusal(run_id=item.run_id, reason="not_found", detail="Run not found"))
            continue
        try:
            await approve(db, principal, settings, run, item.answers_hash, ip)
            await db.flush()
            out.approved.append(run.id)
        except Refused as exc:
            out.refused.append(AutoApplyRefusal(run_id=item.run_id, reason=exc.reason, detail=exc.detail))
    return out


async def cancel(db: AsyncSession, principal: Principal, run: AutoApplyRun, ip: str) -> None:
    if run.status == "submitting":
        raise HTTPException(409, "The application is being submitted right now and cannot be cancelled")
    if run.status not in AUTO_APPLY_OPEN:
        raise HTTPException(409, f"The run is already {run.status}")
    run.status, run.error, run.finished_at = "cancelled", "cancelled_by_admin", utcnow()
    audit(db, principal.user, "auto_apply.cancelled", "auto_apply_run", run.id, None, {}, ip)


async def screenshot_bytes(db: AsyncSession, run: AutoApplyRun, shot_id: uuid.UUID) -> bytes:
    row = (await db.execute(select(AutoApplyScreenshot).where(AutoApplyScreenshot.id == shot_id, AutoApplyScreenshot.run_id == run.id)
                            .options(undefer(AutoApplyScreenshot.data)))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Screenshot not found (screenshots are deleted after the retention period)")
    return bytes(row.data)


# ---------------------------------------------------------------- notifications (written by the worker)
async def notify_outcome(db: AsyncSession, run: AutoApplyRun) -> None:
    kind = NOTIFY_ADMIN.get(run.status)
    if not kind:
        return
    candidate = await db.get(Candidate, run.candidate_id)
    posting = await db.get(JobPosting, run.job_id)
    payload = {"run_id": str(run.id), "candidate_id": str(run.candidate_id), "candidate_name": candidate.name if candidate else "",
               "assignment_id": str(run.assignment_id), "job_title": posting.title if posting else "",
               "company": posting.company if posting else "", "status": run.status,
               "reasons": [b.get("code") for b in run.blocked_reasons or []] + [n.get("fill_in") for n in run.needs_input or []][:20],
               "outcome_unknown": run.outcome_unknown}
    admins = {u for u in (run.created_by, run.approved_by) if u}
    for user_id in admins:
        user = await db.get(User, user_id)
        if user is not None and user.is_active and user.role == "ADMIN":
            notify(db, user_id, kind, payload)
    if run.status == "submitted" and candidate is not None and candidate.assigned_member_id:
        notify(db, candidate.assigned_member_id, "application_submitted", {
            "assignment_id": str(run.assignment_id), "candidate_id": str(candidate.id), "candidate_name": candidate.name,
            "job_title": posting.title if posting else "", "company": posting.company if posting else ""})
