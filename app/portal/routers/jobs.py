"""Global job postings (managers only). Members reach jobs exclusively through their assignments."""
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import Principal, admin_only, client_ip, forbid, get_db, managers
from ..models import JobAssignment, JobPosting
from ..schemas import (CsvImportResult, JobCreate, JobImportDraft, JobImportIn, JobListItem, JobManagerView, JobUpdate, Page)
from ..services import jobs as service
from ..services.common import paginate

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


async def load_job(db: AsyncSession, principal: Principal, job_id: uuid.UUID) -> JobPosting:
    if not await service.visible_jobs(db, principal, {job_id}):
        raise HTTPException(404, "Job not found") if principal.is_admin else forbid()
    return await db.get(JobPosting, job_id)


@router.get("", response_model=Page[JobListItem])
async def list_jobs(principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db),
                    q: str | None = Query(default=None, max_length=120), company: str | None = Query(default=None, max_length=120),
                    origin: str | None = Query(default=None, pattern="^(search|manual|import)$"),
                    verification: str | None = Query(default=None, pattern="^(verified|unverified|failed|closed)$"),
                    work_mode: str | None = None, assigned: bool | None = None, page: int = 1, page_size: int = 50):
    stmt = select(JobPosting)
    scope_where = []
    if principal.is_team_lead:
        if not principal.user.team_id:
            return Page[JobListItem](items=[], total=0, page=1, page_size=page_size)
        stmt = stmt.where(service.tl_job_filter(principal.user.team_id, principal.user.id))
        scope_where = [JobAssignment.team_id == principal.user.team_id]
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(JobPosting.title.ilike(like), JobPosting.company.ilike(like)))
    if company:
        stmt = stmt.where(JobPosting.company.ilike(f"%{company.strip()}%"))
    if origin:
        stmt = stmt.where(JobPosting.origin == origin)
    if verification:
        stmt = stmt.where(JobPosting.verification_status == verification)
    if work_mode:
        stmt = stmt.where(JobPosting.work_mode == work_mode)
    if assigned is not None:
        has = exists().where(JobAssignment.job_id == JobPosting.id, JobAssignment.is_active, *scope_where)
        stmt = stmt.where(has if assigned else ~has)
    rows, total = await paginate(db, stmt.order_by(JobPosting.created_at.desc()), page, page_size)
    return Page[JobListItem](items=await service.list_items(db, [r[0] for r in rows], scope_where), total=total,
                             page=max(1, page), page_size=max(1, min(page_size, 200)))


@router.post("", response_model=JobManagerView, status_code=201)
async def create_job(data: JobCreate, principal: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    posting = await service.create_manual(db, principal.user, data)
    out = await service.manager_view(db, principal, posting)
    await db.commit()
    return out


@router.post("/import-url", response_model=JobImportDraft)
async def import_url(data: JobImportIn, request: Request, principal: Principal = Depends(admin_only)):
    """Read a posting page and return its details for review. Nothing is saved by this call."""
    return await service.import_from_url(request.app.state.verifier, data.url.strip())


@router.post("/import-csv", response_model=CsvImportResult)
async def import_csv(file: UploadFile = File(...), principal: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    raw = await file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "CSV file is too large (2 MB maximum)")
    result = await service.import_csv(db, principal.user, raw)
    await db.commit()
    return result


@router.get("/{job_id}", response_model=JobManagerView)
async def get_job(job_id: uuid.UUID, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    return await service.manager_view(db, principal, await load_job(db, principal, job_id))


@router.patch("/{job_id}", response_model=JobManagerView)
async def update_job(job_id: uuid.UUID, data: JobUpdate, principal: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    posting = await load_job(db, principal, job_id)
    await service.update_job(db, principal.user, posting, data)
    await db.flush()
    await db.refresh(posting)
    out = await service.manager_view(db, principal, posting)
    await db.commit()
    return out


@router.post("/{job_id}/verify", response_model=JobManagerView)
async def verify_job(job_id: uuid.UUID, request: Request, principal: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    posting = await load_job(db, principal, job_id)
    await service.verify_posting(db, request.app.state.verifier, principal.user, posting)
    await db.flush()
    await db.refresh(posting)
    out = await service.manager_view(db, principal, posting)
    await db.commit()
    return out
