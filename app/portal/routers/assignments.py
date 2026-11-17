"""Assignments and application tracking."""
import csv
import io
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...netguard import UnsafeURL, assert_public_url
from ...config import Settings
from ..deps import Principal, any_user, assignment_scope, client_ip, from_this_site, get_db, get_settings, managers
from ..models import JobAssignment, JobPosting, User
from ..schemas import (Day, AssignCreate, AssignmentDetail, AssignmentOut, BulkAssign, BulkAssignResult, Message, NoteIn, NoteOut, Page,
                       StatusUpdate)
from ..services import assignments as service
from ..services.common import paginate, user_ref

router = APIRouter(prefix="/api/assignments", tags=["assignments"])
A = JobAssignment


def filtered(principal: Principal, *, member_id, team_id, status, applied, q, company, date_from, date_to, search_run_id, include_inactive):
    stmt = select(A).where(assignment_scope(principal))
    if not include_inactive or principal.is_member:
        stmt = stmt.where(A.is_active)
    if member_id:
        stmt = stmt.where(A.member_id == member_id)
    if team_id:
        stmt = stmt.where(A.team_id == team_id)
    if status:
        stmt = stmt.where(A.status.in_([s for s in status.split(",") if s]))
    if applied is True:
        stmt = stmt.where(A.applied_at.is_not(None))
    elif applied is False:
        stmt = stmt.where(A.applied_at.is_(None), A.status.not_in(("SKIPPED", "WITHDRAWN")))
    if search_run_id:
        stmt = stmt.where(A.search_run_id == search_run_id)
    if q or company:
        job = select(JobPosting.id)
        if q:
            like = f"%{q.strip()}%"
            job = job.where(or_(JobPosting.title.ilike(like), JobPosting.company.ilike(like)))
        if company:
            job = job.where(JobPosting.company.ilike(f"%{company.strip()}%"))
        stmt = stmt.where(A.job_id.in_(job))
    if date_from:
        stmt = stmt.where(A.assigned_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc))
    if date_to:
        stmt = stmt.where(A.assigned_at < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc))
    return stmt


SORTS = {"recent": A.assigned_at.desc(), "oldest": A.assigned_at.asc(), "score": A.match_score.desc().nulls_last(),
         "applied": A.applied_at.desc().nulls_last(), "status": A.status.asc()}


@router.get("", response_model=Page[AssignmentOut])
async def list_assignments(principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db),
                           member_id: uuid.UUID | None = None, team_id: uuid.UUID | None = None,
                           status: str | None = Query(default=None, max_length=120), applied: bool | None = None,
                           q: str | None = Query(default=None, max_length=120), company: str | None = Query(default=None, max_length=120),
                           date_from: Day | None = None, date_to: Day | None = None, search_run_id: uuid.UUID | None = None,
                           include_inactive: bool = False, sort: str = "recent", page: int = 1, page_size: int = 25):
    stmt = filtered(principal, member_id=member_id, team_id=team_id, status=status, applied=applied, q=q, company=company,
                    date_from=date_from, date_to=date_to, search_run_id=search_run_id, include_inactive=include_inactive)
    # Jobs assigned in one batch share a timestamp: best match first keeps the order stable and useful.
    rows, total = await paginate(db, stmt.order_by(SORTS.get(sort, SORTS["recent"]), A.match_score.desc().nulls_last(), A.id), page, page_size)
    return Page[AssignmentOut](items=await service.assignments_out(db, [r[0] for r in rows]), total=total, page=max(1, page),
                               page_size=max(1, min(page_size, 200)))


@router.get("/export.csv")
async def export_assignments(principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db),
                             member_id: uuid.UUID | None = None, team_id: uuid.UUID | None = None,
                             status: str | None = Query(default=None, max_length=120), applied: bool | None = None,
                             date_from: Day | None = None, date_to: Day | None = None):
    """Report of the caller's scope (Admin: organization, Team Lead: own team). Posting URLs are included for managers only."""
    stmt = filtered(principal, member_id=member_id, team_id=team_id, status=status, applied=applied, q=None, company=None,
                    date_from=date_from, date_to=date_to, search_run_id=None, include_inactive=False)
    rows = list((await db.execute(stmt.order_by(A.assigned_at.desc()).limit(5000))).scalars())
    outs = await service.assignments_out(db, rows)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["member", "email", "team", "company", "title", "location", "status", "match_score", "assigned_at", "applied_at",
                     "link_opens", "assigned_by", "source"])

    def cell(value) -> str:
        text = "" if value is None else str(value)
        return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text       # no spreadsheet formulas

    for o in outs:
        writer.writerow([cell(v) for v in (o.member.name, o.member.email, o.team.name if o.team else "", o.job.company, o.job.title,
                                           o.job.location_text, o.status, o.match_score, o.assigned_at.isoformat(),
                                           o.applied_at.isoformat() if o.applied_at else "", o.link_opens,
                                           o.assigned_by.name if o.assigned_by else "", o.source)])
    return Response(buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="assignments.csv"', "Cache-Control": "no-store"})


@router.post("", response_model=BulkAssignResult, status_code=201)
async def create_assignment(data: AssignCreate, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    created, already = await service.create_assignments(db, principal, [(data.job_id, data.member_id)],
                                                        search_run_id=data.search_run_id, source="manual")
    await db.commit()
    return BulkAssignResult(created=len(created), already_assigned=already, assignment_ids=created)


@router.post("/bulk", response_model=BulkAssignResult, status_code=201)
async def bulk_assign(data: BulkAssign, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    pairs = [(i.job_id, i.member_id) for i in data.items] + [(j, m) for j in data.job_ids for m in data.member_ids]
    if not pairs:
        raise HTTPException(422, "Select at least one job and one member")
    if len(pairs) > 2000:
        raise HTTPException(422, "Too many assignments in one request (2000 maximum)")
    run_id = data.search_run_id or next((i.search_run_id for i in data.items if i.search_run_id), None)
    created, already = await service.create_assignments(
        db, principal, pairs, search_run_id=run_id, source="queue" if data.queue_item_ids else ("search" if run_id else "manual"),
        queue_item_ids=data.queue_item_ids)
    await db.commit()
    return BulkAssignResult(created=len(created), already_assigned=already, assignment_ids=created)


@router.get("/{assignment_id}", response_model=AssignmentDetail)
async def get_assignment(assignment_id: uuid.UUID, request: Request, principal: Principal = Depends(any_user),
                         db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    assignment = await service.load_scoped(db, principal, assignment_id)
    if from_this_site(request, settings):                # a link on another site must not make a job count as "viewed"
        await service.mark_viewed(db, principal, assignment)
    await db.flush()
    out = await service.detail(db, principal, assignment)
    await db.commit()
    return out


@router.patch("/{assignment_id}/status", response_model=AssignmentDetail)
async def update_status(assignment_id: uuid.UUID, data: StatusUpdate, principal: Principal = Depends(any_user),
                        db: AsyncSession = Depends(get_db)):
    assignment = await service.load_scoped(db, principal, assignment_id)
    await service.set_status(db, principal, assignment, data)
    await db.flush()
    out = await service.detail(db, principal, assignment)
    await db.commit()
    return out


@router.get("/{assignment_id}/open-job")
async def open_job(assignment_id: uuid.UUID, request: Request, principal: Principal = Depends(any_user),
                   db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Audited redirect to the posting. The destination is read from the stored job record only (nothing from the
    request), must be a public http(s) address, and opening it is logged without changing the applied state."""
    if not from_this_site(request, settings):            # only the portal's own pages may trigger a recorded link-open
        raise HTTPException(403, "Open this job from the portal.")
    assignment = await service.load_scoped(db, principal, assignment_id)
    target = await service.record_link_open(db, principal, assignment)
    try:
        await assert_public_url(target)
    except UnsafeURL:
        await db.rollback()
        raise HTTPException(409, "This job link cannot be opened safely. Ask your Team Lead to check the posting.") from None
    await db.commit()
    return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/{assignment_id}/notes", response_model=NoteOut, status_code=201)
async def add_note(assignment_id: uuid.UUID, data: NoteIn, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    assignment = await service.load_scoped(db, principal, assignment_id)
    note = await service.add_note(db, principal, assignment, data.note, data.is_private)
    out = NoteOut(id=note.id, note=note.note, is_private=note.is_private, author=user_ref(principal.user), created_at=note.created_at)
    await db.commit()
    return out


@router.delete("/{assignment_id}", response_model=Message)
async def withdraw_assignment(assignment_id: uuid.UUID, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    assignment = await service.load_scoped(db, principal, assignment_id)
    await service.withdraw(db, principal, assignment)
    await db.commit()
    return Message(message="Assignment withdrawn. Its history is kept.")
