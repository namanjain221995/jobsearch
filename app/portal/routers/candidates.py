"""Candidates: the people the team finds jobs for. Their details, job-search profile, resume, numbers and activity.

Who may do what is decided here and in deps.candidate_in_scope on every call: an Admin everything, a Team Lead the
candidates of the team they lead today, a Member reads the candidates assigned to them today and changes nothing.
A candidate outside the caller's scope answers 404, exactly like one that does not exist."""
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from ...config import Settings
from ..deps import Principal, any_user, candidate_in_scope, candidate_scope, client_ip, get_db, get_settings, managers
from ..models import ApplicationEvent, Candidate, JobAssignment, JobPosting, Resume
from ..schemas import (ActivityItem, CandidateAnalytics, CandidateCreate, CandidateOut, CandidateUpdate, Day, Message, Page, ProfileIn,
                       ProfileOut, ResumeOut)
from ..services import analytics, people
from ..services import assignments as assignments_service
from ..services import candidates as service
from ..services import settings as settings_service
from ..services.audit import audit
from ..services.common import candidate_ref, paginate, user_ref, users_by_id

router = APIRouter(prefix="/api/candidates", tags=["candidates"])
A = JobAssignment
# Per-candidate values the list can be ordered by, computed inside the list query (the same definitions as the stats).
_NEWEST_JOB = select(func.max(A.assigned_at)).where(A.candidate_id == Candidate.id, A.is_active).correlate(Candidate).scalar_subquery()
_NOT_APPLIED = (select(func.count()).where(A.candidate_id == Candidate.id, A.is_active, A.applied_at.is_(None),
                                           A.status.not_in(("SKIPPED", "WITHDRAWN"))).correlate(Candidate).scalar_subquery())
SORTS = {"name": [Candidate.name], "recent": [Candidate.created_at.desc()], "updated": [Candidate.updated_at.desc()],
         "newest_job": [_NEWEST_JOB.desc().nulls_last(), Candidate.name], "not_applied": [_NOT_APPLIED.desc(), Candidate.name]}


@router.get("", response_model=Page[CandidateOut])
async def list_candidates(principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db),
                          team_id: uuid.UUID | None = None, assigned_member_id: uuid.UUID | None = None,
                          unassigned: bool | None = None, active: bool | None = None, has_resume: bool | None = None,
                          q: str | None = Query(default=None, max_length=100),
                          sort: str = Query(default="name", pattern="^(name|recent|updated|newest_job|not_applied)$"), page: int = 1, page_size: int = 50):
    """Filters only ever narrow the caller's scope. `unassigned=true`: candidates without a responsible member;
    `unassigned=false`: those with one. Sorts: `name`, `recent` (created), `updated`, `newest_job` (newest active
    assignment first, candidates without jobs last), `not_applied` (most jobs still to apply to first)."""
    stmt = select(Candidate).where(candidate_scope(principal))
    if team_id:
        stmt = stmt.where(Candidate.team_id == team_id)
    if assigned_member_id:
        stmt = stmt.where(Candidate.assigned_member_id == assigned_member_id)
    if unassigned is not None:
        stmt = stmt.where(Candidate.assigned_member_id.is_(None) if unassigned else Candidate.assigned_member_id.is_not(None))
    if active is not None:
        stmt = stmt.where(Candidate.is_active.is_(active))
    if has_resume is not None:
        current = exists().where(Resume.candidate_id == Candidate.id, Resume.is_current)
        stmt = stmt.where(current if has_resume else ~current)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Candidate.name.ilike(like), Candidate.email.ilike(like), Candidate.phone.ilike(like)))
    rows, total = await paginate(db, stmt.order_by(*SORTS[sort], Candidate.id), page, page_size)
    return Page[CandidateOut](items=await service.candidates_out(db, [r[0] for r in rows]), total=total, page=max(1, page),
                              page_size=max(1, min(page_size, 200)))


@router.post("", response_model=CandidateOut, status_code=201)
async def create_candidate(data: CandidateCreate, request: Request, principal: Principal = Depends(managers),
                           db: AsyncSession = Depends(get_db)):
    candidate = await service.create_candidate(db, principal, data, client_ip(request))
    await db.flush()
    await db.refresh(candidate)
    out = (await service.candidates_out(db, [candidate]))[0]
    await db.commit()
    return out


@router.get("/{candidate_id}", response_model=CandidateOut)
async def get_candidate(candidate_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    return (await service.candidates_out(db, [await candidate_in_scope(db, principal, candidate_id)]))[0]


@router.patch("/{candidate_id}", response_model=CandidateOut)
async def update_candidate(candidate_id: uuid.UUID, data: CandidateUpdate, request: Request, principal: Principal = Depends(any_user),
                           db: AsyncSession = Depends(get_db)):
    candidate = await candidate_in_scope(db, principal, candidate_id, write=True)
    await service.update_candidate(db, principal, candidate, data, client_ip(request))
    await db.flush()
    await db.refresh(candidate)
    out = (await service.candidates_out(db, [candidate]))[0]
    await db.commit()
    return out


# ---------------------------------------------------------------- job-search profile
@router.get("/{candidate_id}/profile", response_model=ProfileOut)
async def get_candidate_profile(candidate_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    candidate = await candidate_in_scope(db, principal, candidate_id)
    profile = await people.get_profile(db, candidate.id)
    out = people.profile_out(profile, await people.current_resume(db, candidate.id) is not None)
    await db.commit()
    return out


@router.patch("/{candidate_id}/profile", response_model=ProfileOut)
async def update_candidate_profile(candidate_id: uuid.UUID, data: ProfileIn, request: Request, principal: Principal = Depends(any_user),
                                   db: AsyncSession = Depends(get_db)):
    candidate = await candidate_in_scope(db, principal, candidate_id, write=True)
    profile = await people.get_profile(db, candidate.id)
    portal = await settings_service.load(db, request.app.state.settings)
    await people.update_profile(db, profile, data, portal.profile_extra_fields)
    audit(db, principal.user, "profile.updated", "candidate", candidate.id, candidate.team_id,
          {"fields": sorted(data.model_dump(exclude_unset=True))}, client_ip(request))
    await db.flush()
    await db.refresh(profile)
    out = people.profile_out(profile, await people.current_resume(db, candidate.id) is not None)
    await db.commit()
    return out


# ---------------------------------------------------------------- resume
@router.post("/{candidate_id}/resume", response_model=ResumeOut, status_code=201)
async def upload_candidate_resume(candidate_id: uuid.UUID, request: Request, file: UploadFile = File(...),
                                  principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db),
                                  settings: Settings = Depends(get_settings)):
    """A Team Lead or Admin uploads the candidate's resume (PDF, DOCX or TXT). The text is extracted for searching
    and the file itself is kept, so the responsible member can download it when applying. It replaces the current one."""
    candidate = await candidate_in_scope(db, principal, candidate_id, write=True)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > settings.max_upload_bytes + 64 * 1024:
        raise HTTPException(413, f"File is too large. Maximum size is {settings.max_upload_bytes // (1024 * 1024)} MB.")
    data = await file.read(settings.max_upload_bytes + 1)
    await file.close()
    resume = await people.save_resume(db, settings, candidate, principal.user, (file.filename or "resume")[:200], data,
                                      file.content_type or "", client_ip(request))
    await db.flush()
    out = await people.resume_out(db, resume, include_text=True)
    await db.commit()
    return out


@router.get("/{candidate_id}/resume", response_model=ResumeOut | None)
async def get_candidate_resume(candidate_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    """The current resume with its extracted text, or null when the candidate has none."""
    candidate = await candidate_in_scope(db, principal, candidate_id)
    resume = await people.current_resume(db, candidate.id)
    return await people.resume_out(db, resume, include_text=True) if resume else None


@router.get("/{candidate_id}/resume/download", response_class=Response,
            responses={200: {"content": {"application/octet-stream": {}}, "description": "The uploaded resume file, as an attachment"},
                       404: {"description": "No candidate, no resume, or a resume uploaded before files were kept"}})
async def download_candidate_resume(candidate_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    """The file that was uploaded, for exactly the people who may see the candidate. Always an attachment, with a
    content type chosen by the server from the file's extension, so a browser never renders or interprets it."""
    candidate = await candidate_in_scope(db, principal, candidate_id)
    resume = (await db.execute(select(Resume).where(Resume.candidate_id == candidate.id, Resume.is_current)
                               .options(undefer(Resume.file_data)))).scalar_one_or_none()
    ext = people.resume_extension(resume.filename) if resume else None
    if resume is None or resume.file_data is None or ext is None:
        raise HTTPException(404, "No resume file is stored for this candidate. Resumes uploaded before files were kept have "
                                 "only their text; upload the resume again to keep the file.")
    return Response(bytes(resume.file_data), media_type=people.DOWNLOAD_TYPES[ext], headers={
        "Content-Disposition": f'attachment; filename="{people.download_name(resume.filename, ext)}"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store, private",
        "Content-Security-Policy": "default-src 'none'; sandbox"})


@router.delete("/{candidate_id}/resume", response_model=Message)
async def delete_candidate_resume(candidate_id: uuid.UUID, request: Request, principal: Principal = Depends(any_user),
                                  db: AsyncSession = Depends(get_db)):
    """Deletes the current resume: its text and its file."""
    candidate = await candidate_in_scope(db, principal, candidate_id, write=True)
    resume = await people.current_resume(db, candidate.id)
    if resume is None:
        raise HTTPException(404, "This candidate has no resume")
    await people.delete_resume(db, principal.user, candidate, resume, client_ip(request))
    await db.commit()
    return Message(message="Resume deleted")


# ---------------------------------------------------------------- numbers and activity
@router.get("/{candidate_id}/analytics", response_model=CandidateAnalytics)
async def candidate_analytics(candidate_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db),
                              date_from: Day | None = None, date_to: Day | None = None):
    return await analytics.candidate_analytics(db, await candidate_in_scope(db, principal, candidate_id), date_from, date_to)


async def activity_page(db: AsyncSession, principal: Principal, where: list, page: int, page_size: int) -> Page[ActivityItem]:
    """Application events of the assignments matching `where`, newest first."""
    stmt = (select(ApplicationEvent, JobAssignment, JobPosting, Candidate)
            .join(JobAssignment, JobAssignment.id == ApplicationEvent.assignment_id)
            .join(JobPosting, JobPosting.id == JobAssignment.job_id)
            .join(Candidate, Candidate.id == JobAssignment.candidate_id)
            .where(*where).order_by(ApplicationEvent.created_at.desc()))
    if principal.is_member:                              # nothing managers keep among themselves
        private = ApplicationEvent.meta["private"].astext
        stmt = stmt.where(JobAssignment.is_active, ApplicationEvent.event_type.not_in(assignments_service.MANAGER_ONLY_EVENTS),
                          or_(private.is_(None), private != "true"))
    rows, total = await paginate(db, stmt, page, page_size)
    users = await users_by_id(db, {e.actor_id for e, _, _, _ in rows} | {c.assigned_member_id for _, _, _, c in rows})
    items = [ActivityItem(id=e.id, event_type=e.event_type, old_status=e.old_status, new_status=e.new_status, created_at=e.created_at,
                          assignment_id=a.id, job_title=p.title, company=p.company, candidate=candidate_ref(c),
                          member=user_ref(users.get(c.assigned_member_id)), actor=user_ref(users.get(e.actor_id))) for e, a, p, c in rows]
    return Page[ActivityItem](items=items, total=total, page=max(1, page), page_size=max(1, min(page_size, 200)))


@router.get("/{candidate_id}/activity", response_model=Page[ActivityItem])
async def candidate_activity(candidate_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db),
                             page: int = 1, page_size: int = 50):
    candidate = await candidate_in_scope(db, principal, candidate_id)
    return await activity_page(db, principal, [JobAssignment.candidate_id == candidate.id], page, page_size)
