"""Teams, their members, analytics and the queue of jobs an Admin delegated to a team."""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import Principal, admin_only, assignment_scope, client_ip, ensure_team, forbid, get_db, managers
from ..models import JobPosting, Team, TeamJobQueue, User
from ..schemas import (Day, Page, QueueDelegate, QueueItemOut, QueueUpdate, TeamAnalytics, TeamCreate, TeamOut, TeamUpdate, UserOut)
from ..services import analytics, people
from ..services import jobs as jobs_service
from ..services.audit import audit, notify
from ..services.common import team_ref, teams_by_id, user_ref, users_by_id

router = APIRouter(prefix="/api", tags=["teams"])


async def load_team(db: AsyncSession, principal: Principal, team_id: uuid.UUID) -> Team:
    if not principal.is_admin:
        ensure_team(principal, team_id)
    team = await db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found") if principal.is_admin else forbid()
    return team


@router.get("/teams", response_model=list[TeamOut])
async def list_teams(principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db), include_inactive: bool = True):
    stmt = select(Team).order_by(Team.name)
    if principal.is_team_lead:
        stmt = stmt.where(Team.id == principal.user.team_id)
    if not include_inactive:
        stmt = stmt.where(Team.is_active)
    return await people.teams_out(db, list((await db.execute(stmt)).scalars()))


@router.post("/teams", response_model=TeamOut, status_code=201)
async def create_team(data: TeamCreate, request: Request, principal: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db)):
    name = data.name.strip()
    if (await db.execute(select(Team.id).where(func.lower(Team.name) == name.lower()))).first():
        raise HTTPException(409, "A team with this name already exists")
    team = Team(name=name, description=data.description.strip())
    db.add(team)
    await db.flush()
    audit(db, principal.user, "team.created", "team", team.id, team.id, {"name": name}, client_ip(request))
    out = (await people.teams_out(db, [team]))[0]
    await db.commit()
    return out


@router.get("/teams/{team_id}", response_model=TeamOut)
async def get_team(team_id: uuid.UUID, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    return (await people.teams_out(db, [await load_team(db, principal, team_id)]))[0]


@router.patch("/teams/{team_id}", response_model=TeamOut)
async def update_team(team_id: uuid.UUID, data: TeamUpdate, request: Request, principal: Principal = Depends(admin_only),
                      db: AsyncSession = Depends(get_db)):
    team = await load_team(db, principal, team_id)
    changes = {}
    if data.name is not None and data.name.strip() != team.name:
        name = data.name.strip()
        if (await db.execute(select(Team.id).where(func.lower(Team.name) == name.lower(), Team.id != team.id))).first():
            raise HTTPException(409, "A team with this name already exists")
        team.name = changes["name"] = name
    if data.description is not None:
        team.description = data.description.strip()
        changes["description"] = True
    if data.is_active is not None and data.is_active != team.is_active:
        team.is_active = changes["is_active"] = data.is_active      # logical deactivation: members and history stay
    if changes:
        audit(db, principal.user, "team.updated", "team", team.id, team.id, changes, client_ip(request))
    if data.remove_team_lead:
        await people.set_team_lead(db, principal.user, team, None)
    elif data.team_lead_id:
        lead = await db.get(User, data.team_lead_id)
        if lead is None:
            raise HTTPException(404, "Team Lead not found")
        await people.set_team_lead(db, principal.user, team, lead)
    await db.flush()
    out = (await people.teams_out(db, [team]))[0]
    await db.commit()
    return out


@router.get("/teams/{team_id}/members", response_model=list[UserOut])
async def team_members(team_id: uuid.UUID, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db)):
    await load_team(db, principal, team_id)
    members = list((await db.execute(select(User).where(User.team_id == team_id, User.role == "MEMBER").order_by(User.name))).scalars())
    return await people.users_out(db, members)


@router.get("/analytics/team/{team_id}", response_model=TeamAnalytics, operation_id="team_analytics_by_analytics_path")     # same answer, second address
@router.get("/teams/{team_id}/analytics", response_model=TeamAnalytics)
async def team_analytics(team_id: uuid.UUID, request: Request, principal: Principal = Depends(managers),
                         db: AsyncSession = Depends(get_db), date_from: Day | None = None, date_to: Day | None = None):
    team = await load_team(db, principal, team_id)       # authorization first: a stored answer is never a way around it
    return await request.app.state.cache.cached(
        "team", {"team": team.id, "from": date_from, "to": date_to}, TeamAnalytics,
        lambda: analytics.team_analytics(db, team, date_from, date_to))


# ---------------------------------------------------------------- delegated job queue
async def queue_out(db: AsyncSession, principal: Principal, rows: list[TeamJobQueue]) -> list[QueueItemOut]:
    if not rows:
        return []
    postings = {p.id: p for p in (await db.execute(select(JobPosting).where(JobPosting.id.in_({r.job_id for r in rows})))).scalars()}
    # "Assigned" and "applied" counts cover what the caller may see: a Team Lead's own team, not the organization.
    items = {i.id: i for i in await jobs_service.list_items(db, list(postings.values()), [assignment_scope(principal)])}
    teams = await teams_by_id(db, {r.team_id for r in rows})
    users = await users_by_id(db, {r.delegated_by for r in rows})
    return [QueueItemOut(id=r.id, team=team_ref(teams[r.team_id]), job=items[r.job_id], status=r.status, note=r.note,
                         delegated_by=user_ref(users.get(r.delegated_by)), created_at=r.created_at)
            for r in rows if r.job_id in items and r.team_id in teams]


@router.get("/teams/{team_id}/queue", response_model=Page[QueueItemOut])
async def team_queue(team_id: uuid.UUID, principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db),
                     status: str = "pending", page: int = 1, page_size: int = 50):
    await load_team(db, principal, team_id)
    stmt = select(TeamJobQueue).where(TeamJobQueue.team_id == team_id).order_by(TeamJobQueue.created_at.desc())
    if status != "all":
        stmt = stmt.where(TeamJobQueue.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    page, page_size = max(1, min(page, 1_000_000)), max(1, min(page_size, 200))
    rows = list((await db.execute(stmt.limit(page_size).offset((page - 1) * page_size))).scalars())
    return Page[QueueItemOut](items=await queue_out(db, principal, rows), total=total, page=page, page_size=page_size)


@router.post("/teams/{team_id}/queue", response_model=list[QueueItemOut], status_code=201)
async def delegate_jobs(team_id: uuid.UUID, data: QueueDelegate, request: Request, principal: Principal = Depends(admin_only),
                        db: AsyncSession = Depends(get_db)):
    """Admin hands jobs to a team; its Team Lead distributes them to the team's candidates."""
    team = await load_team(db, principal, team_id)
    if not team.is_active:
        raise HTTPException(409, "This team is deactivated")
    job_ids = list(dict.fromkeys(data.job_ids))
    found = set((await db.execute(select(JobPosting.id).where(JobPosting.id.in_(job_ids)))).scalars())
    if found != set(job_ids):
        raise HTTPException(404, "Job not found")
    existing = {r.job_id: r for r in (await db.execute(select(TeamJobQueue).where(
        TeamJobQueue.team_id == team_id, TeamJobQueue.job_id.in_(job_ids)))).scalars()}
    rows = []
    for job_id in job_ids:
        row = existing.get(job_id)
        if row is None:
            row = TeamJobQueue(team_id=team_id, job_id=job_id, delegated_by=principal.user.id, note=data.note.strip())
            db.add(row)
        elif row.status == "dismissed":
            row.status, row.note, row.delegated_by = "pending", data.note.strip(), principal.user.id
        rows.append(row)
    await db.flush()
    lead = await people.team_lead_of(db, team_id)
    if lead:
        notify(db, lead.id, "jobs_delegated", {"count": len(job_ids), "team_id": str(team_id), "delegated_by": principal.user.name})
    audit(db, principal.user, "jobs.delegated", "team", team_id, team_id, {"jobs": len(job_ids)}, client_ip(request))
    out = await queue_out(db, principal, rows)
    await db.commit()
    return out


@router.patch("/team-queue/{item_id}", response_model=QueueItemOut)
async def update_queue_item(item_id: uuid.UUID, data: QueueUpdate, principal: Principal = Depends(managers),
                            db: AsyncSession = Depends(get_db)):
    row = await db.get(TeamJobQueue, item_id)
    if row is None:
        raise HTTPException(404, "Queue item not found") if principal.is_admin else forbid()
    ensure_team(principal, row.team_id)
    row.status = data.status
    out = (await queue_out(db, principal, [row]))[0]
    await db.commit()
    return out
