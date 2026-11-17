"""AI job searches for selected members. Creating one returns immediately with its id; the work happens in the
background worker and continues whether or not anyone is watching."""
import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ...providers import registry
from ..deps import Principal, client_ip, get_db, get_settings, managers
from ..models import SearchPreset, SearchResult, SearchRun, SearchTarget
from ..schemas import (Message, Page, PresetIn, PresetOut, SearchCreate, SearchCreated, SearchOptions, SearchResultOut, SearchRunOut)
from ..security import utcnow
from ..services import searches as service
from ..services import settings as settings_service
from ..services.audit import audit
from ..services.common import paginate

router = APIRouter(prefix="/api", tags=["searches"])


def scope(principal: Principal):
    if principal.is_admin:
        return []
    clauses = [SearchRun.initiated_by == principal.user.id]
    if principal.user.team_id:
        clauses.append(SearchRun.team_id == principal.user.team_id)
    return [or_(*clauses)]


@router.get("/searches/options", response_model=SearchOptions)
async def search_options(request: Request, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db),
                         settings: Settings = Depends(get_settings)):
    portal = await settings_service.load(db, settings)
    off = set(portal.disabled_sources)
    sources = [{"name": s.name, "label": s.label, "kind": s.kind, "note": s.note, "enabled": s.name not in off}
               for s in registry.enabled_specs(settings).values()]
    from typing import get_args
    from ...schemas import Authorization, WorkMode
    return SearchOptions(
        sources=sources,
        ai_providers={"openai": {"configured": bool(settings.openai_api_key), "model": settings.openai_model},
                      "claude": {"configured": bool(settings.anthropic_api_key), "model": settings.anthropic_model}},
        limits=settings_service.ui(portal), authorizations=list(get_args(Authorization)), work_modes=list(get_args(WorkMode)),
        queue_available=await request.app.state.queue.ping())


@router.post("/searches", response_model=SearchCreated, status_code=202)
async def create_search(data: SearchCreate, request: Request, principal: Principal = Depends(managers),
                        db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    portal = await settings_service.load(db, settings)
    run, skipped = await service.create_search(db, settings, portal, principal, data, client_ip(request))
    await db.commit()                                   # the run exists before the worker can pick it up
    if not await request.app.state.queue.enqueue(run.id):
        run.status, run.completed_at = "failed", utcnow()
        run.error_summary = "The background worker queue (Redis) is not reachable. The search was not started."
        await db.commit()
        raise HTTPException(503, run.error_summary)
    return SearchCreated(search=(await service.runs_out(db, [run]))[0], skipped=skipped)


@router.get("/searches", response_model=Page[SearchRunOut])
async def list_searches(principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db),
                        status: str | None = Query(default=None, pattern="^(active|queued|running|completed|partial|failed|cancelled)$"),
                        team_id: uuid.UUID | None = None, member_id: uuid.UUID | None = None, mine: bool = False,
                        page: int = 1, page_size: int = 25):
    stmt = select(SearchRun).where(*scope(principal))
    if status == "active":
        stmt = stmt.where(SearchRun.status.in_(service.ACTIVE))
    elif status:
        stmt = stmt.where(SearchRun.status == status)
    if team_id:
        stmt = stmt.where(SearchRun.team_id == team_id)
    if member_id:
        stmt = stmt.where(SearchRun.id.in_(select(SearchTarget.search_run_id).where(SearchTarget.member_id == member_id)))
    if mine:
        stmt = stmt.where(SearchRun.initiated_by == principal.user.id)
    rows, total = await paginate(db, stmt.order_by(SearchRun.created_at.desc()), page, page_size)
    return Page[SearchRunOut](items=await service.runs_out(db, [r[0] for r in rows]), total=total, page=max(1, page),
                              page_size=max(1, min(page_size, 200)))


@router.get("/searches/{search_id}", response_model=SearchRunOut)
async def get_search(search_id: uuid.UUID, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    return (await service.runs_out(db, [await service.load_run(db, principal, search_id)]))[0]


@router.get("/searches/{search_id}/events")
async def search_events(search_id: uuid.UUID, request: Request, principal: Principal = Depends(managers),
                        db: AsyncSession = Depends(get_db)):
    """Server-Sent Events: a snapshot of the run whenever it changes, until it finishes."""
    await service.load_run(db, principal, search_id)
    sessions = request.app.state.sessions

    async def stream():
        last = None
        while True:
            async with sessions() as session:
                run = await session.get(SearchRun, search_id)
                payload = (await service.runs_out(session, [run]))[0].model_dump_json() if run else None
            if payload is None:
                return
            if payload != last:
                last = payload
                yield f"event: progress\ndata: {payload}\n\n"
            if json.loads(payload)["status"] not in service.ACTIVE:
                yield f"event: done\ndata: {payload}\n\n"
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/searches/{search_id}/results", response_model=Page[SearchResultOut])
async def search_results(search_id: uuid.UUID, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db),
                         member_id: uuid.UUID | None = None, min_score: int = Query(default=0, ge=0, le=100),
                         unassigned_only: bool = False, page: int = 1, page_size: int = 50):
    run = await service.load_run(db, principal, search_id)
    stmt = select(SearchResult).where(SearchResult.search_run_id == run.id, SearchResult.match_score >= min_score)
    if member_id:
        stmt = stmt.where(SearchResult.member_id == member_id)
    rows, total = await paginate(db, stmt.order_by(SearchResult.member_id, SearchResult.rank), page, page_size)
    items = await service.results_out(db, principal, [r[0] for r in rows])
    if unassigned_only:
        items = [i for i in items if i.assignment_id is None]
    return Page[SearchResultOut](items=items, total=total, page=max(1, page), page_size=max(1, min(page_size, 200)))


@router.post("/searches/{search_id}/cancel", response_model=SearchRunOut)
async def cancel_search(search_id: uuid.UUID, request: Request, principal: Principal = Depends(managers),
                        db: AsyncSession = Depends(get_db)):
    run = await service.load_run(db, principal, search_id)
    if run.status == "queued":
        run.status, run.completed_at, run.cancel_requested = "cancelled", utcnow(), True     # the worker skips it when it arrives
    elif run.status == "running":
        run.cancel_requested = True                     # the worker stops the member searches within about a second
    else:
        return (await service.runs_out(db, [run]))[0]
    audit(db, principal.user, "search.cancel_requested", "search", run.id, run.team_id, {}, client_ip(request))
    await db.commit()
    return (await service.runs_out(db, [run]))[0]


# ---------------------------------------------------------------- saved presets
@router.get("/search-presets", response_model=list[PresetOut])
async def list_presets(principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SearchPreset).where(SearchPreset.owner_id == principal.user.id).order_by(SearchPreset.name))).scalars()
    return [PresetOut(id=p.id, name=p.name, filters=p.filters, created_at=p.created_at) for p in rows]


@router.post("/search-presets", response_model=PresetOut, status_code=201)
async def create_preset(data: PresetIn, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    preset = SearchPreset(owner_id=principal.user.id, team_id=principal.user.team_id, name=data.name.strip(),
                          filters=SearchCreate(**{**data.filters, "member_ids": []}).model_dump(mode="json", exclude={"member_ids", "team_id"}))
    db.add(preset)
    await db.flush()
    out = PresetOut(id=preset.id, name=preset.name, filters=preset.filters, created_at=preset.created_at)
    await db.commit()
    return out


@router.delete("/search-presets/{preset_id}", response_model=Message)
async def delete_preset(preset_id: uuid.UUID, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    preset = await db.get(SearchPreset, preset_id)
    if preset is None or preset.owner_id != principal.user.id:
        raise HTTPException(404, "Preset not found")
    await db.delete(preset)
    await db.commit()
    return Message(message="Preset deleted")
