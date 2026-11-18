"""AI auto-apply (Admin only), candidate consents (Admin only) and recorded application answers (Admin, Team Lead).
Contract: docs/auto-apply-api.md."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...autoapply.detect import SUPPORTED
from ...config import Settings
from ..autoapply_schemas import (ApplicationAnswersIn, ApplicationAnswersOut, AutoApplyAnswersPatch, AutoApplyApproveIn,
                                 AutoApplyBatchApproveIn, AutoApplyBatchApproveOut, AutoApplyEligibilityOut, AutoApplyRunCreate,
                                 AutoApplyRunDetail, AutoApplyRunOut, AutoApplyRunsCreated, AutoApplyStatus, ConsentIn, ConsentOut,
                                 ConsentRevokeIn, RunStatus)
from ..deps import Principal, admin_only, candidate_in_scope, client_ip, get_db, get_settings, managers
from ..models import AutoApplyRun, Candidate
from ..schemas import Page
from ..services import auto_apply as service
from ..services.common import paginate

router = APIRouter(prefix="/api", tags=["auto-apply"])


async def _enqueue(request: Request, kind: str, run: AutoApplyRun) -> None:
    queue = request.app.state.queue
    enqueue = getattr(queue, "enqueue_auto_apply", None)
    if enqueue is not None:
        await enqueue(kind, run.id, run.attempts or 0)          # a run the queue did not take is handed over by the worker


@router.get("/auto-apply/status", response_model=AutoApplyStatus)
async def auto_apply_status(request: Request, _: Principal = Depends(admin_only), settings: Settings = Depends(get_settings)):
    worker = await request.app.state.queue.worker_status()
    return AutoApplyStatus(enabled=settings.auto_apply_enabled, worker_online=bool(worker and worker.get("online")),
                           browser_concurrency=settings.auto_apply_browser_concurrency, employer_daily_cap=settings.auto_apply_employer_daily_cap,
                           run_timeout_seconds=settings.auto_apply_run_timeout_seconds,
                           screenshot_retention_days=settings.auto_apply_screenshot_retention_days, supported_ats=list(SUPPORTED))


@router.get("/auto-apply/candidates/{candidate_id}/eligibility", response_model=AutoApplyEligibilityOut)
async def auto_apply_eligibility(candidate_id: uuid.UUID, _: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    """The candidate's open assignments, each with its application system and whether a run can be started."""
    candidate = await db.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(404, "Candidate not found")
    return await service.eligibility(db, candidate)


@router.post("/auto-apply/runs", response_model=AutoApplyRunsCreated, status_code=201)
async def create_auto_apply_runs(data: AutoApplyRunCreate, request: Request, principal: Principal = Depends(admin_only),
                                 db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    runs, skipped = await service.create_runs(db, principal, settings, data, client_ip(request))
    await db.flush()
    for run in runs:
        await db.refresh(run)
    out = AutoApplyRunsCreated(runs=await service.runs_out(db, runs), skipped=skipped)
    await db.commit()
    for run in runs:
        await _enqueue(request, "prepare", run)
    return out


@router.get("/auto-apply/runs", response_model=Page[AutoApplyRunOut])
async def list_auto_apply_runs(_: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db),
                               candidate_id: uuid.UUID | None = None, assignment_id: uuid.UUID | None = None,
                               status: list[RunStatus] | None = Query(default=None),
                               ats: str | None = Query(default=None, pattern="^(greenhouse|lever|ashby|workable|unsupported)$"),
                               page: int = 1, page_size: int = 50):
    stmt = select(AutoApplyRun)
    if candidate_id:
        stmt = stmt.where(AutoApplyRun.candidate_id == candidate_id)
    if assignment_id:
        stmt = stmt.where(AutoApplyRun.assignment_id == assignment_id)
    if status:
        stmt = stmt.where(AutoApplyRun.status.in_(status))
    if ats:
        stmt = stmt.where(AutoApplyRun.ats == ats)
    rows, total = await paginate(db, stmt.order_by(AutoApplyRun.created_at.desc(), AutoApplyRun.id), page, page_size)
    return Page[AutoApplyRunOut](items=await service.runs_out(db, [r[0] for r in rows]), total=total, page=max(1, page),
                                 page_size=max(1, min(page_size, 200)))


@router.get("/auto-apply/runs/{run_id}", response_model=AutoApplyRunDetail)
async def get_auto_apply_run(run_id: uuid.UUID, _: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    return await service.run_detail(db, await service.load_run(db, run_id))


@router.get("/auto-apply/runs/{run_id}/screenshots/{screenshot_id}", response_class=Response,
            responses={200: {"content": {"image/jpeg": {}}}, 404: {"description": "No such screenshot (or deleted by retention)"}})
async def get_auto_apply_screenshot(run_id: uuid.UUID, screenshot_id: uuid.UUID, _: Principal = Depends(admin_only),
                                    db: AsyncSession = Depends(get_db)):
    data = await service.screenshot_bytes(db, await service.load_run(db, run_id), screenshot_id)
    return Response(data, media_type="image/jpeg", headers={"Content-Disposition": "inline", "Cache-Control": "no-store, private",
                                                            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"})


@router.patch("/auto-apply/runs/{run_id}/answers", response_model=AutoApplyRunDetail)
async def patch_auto_apply_answers(run_id: uuid.UUID, data: AutoApplyAnswersPatch, request: Request, principal: Principal = Depends(admin_only),
                                   db: AsyncSession = Depends(get_db)):
    run = await service.load_run(db, run_id, lock=True)
    await service.patch_answers(db, principal, run, data.answers, client_ip(request))
    await db.flush()
    await db.refresh(run)
    out = await service.run_detail(db, run)
    await db.commit()
    await _enqueue(request, "prepare", run)
    return out


@router.post("/auto-apply/runs/approve-batch", response_model=AutoApplyBatchApproveOut)
async def approve_auto_apply_batch(data: AutoApplyBatchApproveIn, request: Request, principal: Principal = Depends(admin_only),
                                   db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    service.ensure_enabled(settings)
    out = await service.approve_batch(db, principal, settings, data, client_ip(request))
    runs = [await db.get(AutoApplyRun, i) for i in out.approved]
    await db.commit()
    for run in runs:
        await _enqueue(request, "submit", run)
    return out


@router.post("/auto-apply/runs/{run_id}/approve", response_model=AutoApplyRunDetail, status_code=202)
async def approve_auto_apply_run(run_id: uuid.UUID, data: AutoApplyApproveIn, request: Request, principal: Principal = Depends(admin_only),
                                 db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    service.ensure_enabled(settings)
    run = await service.load_run(db, run_id, lock=True)
    try:
        await service.approve(db, principal, settings, run, data.answers_hash, client_ip(request))
    except service.Refused as exc:
        raise HTTPException(409, f"{exc.reason}: {exc.detail}") from None
    await db.flush()
    await db.refresh(run)
    out = await service.run_detail(db, run)
    await db.commit()
    await _enqueue(request, "submit", run)
    return out


@router.post("/auto-apply/runs/{run_id}/cancel", response_model=AutoApplyRunDetail)
async def cancel_auto_apply_run(run_id: uuid.UUID, request: Request, principal: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    run = await service.load_run(db, run_id, lock=True)
    await service.cancel(db, principal, run, client_ip(request))
    await db.flush()
    await db.refresh(run)
    out = await service.run_detail(db, run)
    await db.commit()
    return out


# ---------------------------------------------------------------- consents (Admin only)
async def _admin_candidate(db: AsyncSession, candidate_id: uuid.UUID) -> Candidate:
    candidate = await db.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(404, "Candidate not found")
    return candidate


@router.get("/candidates/{candidate_id}/consents", response_model=list[ConsentOut])
async def list_candidate_consents(candidate_id: uuid.UUID, _: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    candidate = await _admin_candidate(db, candidate_id)
    return await service.consents_out(db, await service.list_consents(db, candidate))


@router.post("/candidates/{candidate_id}/consents", response_model=ConsentOut, status_code=201)
async def record_candidate_consent(candidate_id: uuid.UUID, data: ConsentIn, request: Request, principal: Principal = Depends(admin_only),
                                   db: AsyncSession = Depends(get_db)):
    candidate = await _admin_candidate(db, candidate_id)
    row = await service.record_consent(db, principal, candidate, data, client_ip(request))
    await db.refresh(row)
    out = (await service.consents_out(db, [row]))[0]
    await db.commit()
    return out


@router.post("/candidates/{candidate_id}/consents/{consent_id}/revoke", response_model=ConsentOut)
async def revoke_candidate_consent(candidate_id: uuid.UUID, consent_id: uuid.UUID, request: Request, data: ConsentRevokeIn | None = None,
                                   principal: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    candidate = await _admin_candidate(db, candidate_id)
    row = await service.revoke_consent(db, principal, candidate, consent_id, data.note if data else "", client_ip(request))
    await db.refresh(row)
    out = (await service.consents_out(db, [row]))[0]
    await db.commit()
    return out


# ---------------------------------------------------------------- recorded application answers (Admin, Team Lead)
@router.get("/candidates/{candidate_id}/application-answers", response_model=ApplicationAnswersOut)
async def get_application_answers(candidate_id: uuid.UUID, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    candidate = await candidate_in_scope(db, principal, candidate_id)
    out = await service.answers_out(db, candidate)
    await db.commit()
    return out


@router.patch("/candidates/{candidate_id}/application-answers", response_model=ApplicationAnswersOut)
async def update_application_answers(candidate_id: uuid.UUID, data: ApplicationAnswersIn, request: Request,
                                     principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    candidate = await candidate_in_scope(db, principal, candidate_id, write=True)
    await service.update_answers(db, principal, candidate, data, client_ip(request))
    await db.flush()
    out = await service.answers_out(db, candidate)
    await db.commit()
    return out
