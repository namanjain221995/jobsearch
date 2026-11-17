"""Organization analytics, notifications, audit trail, settings, integrations and health."""
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ...providers import registry
from ..deps import Principal, admin_only, any_user, client_ip, get_db, get_settings, member_in_scope
from ..models import AuditLog, Notification
from ..schemas import (AdminAnalytics, AuditOut, HealthOut, IntegrationsOut, MemberAnalytics, Message, NotificationOut, Page,
                       SettingsOut, SettingsUpdate)
from ..security import utcnow
from ..services import analytics
from ..services import settings as settings_service
from ..services.audit import audit
from ..services.common import paginate, team_ref, teams_by_id, user_ref, users_by_id

router = APIRouter(prefix="/api", tags=["admin"])


@router.get("/analytics/admin", response_model=AdminAnalytics)
async def admin_analytics(request: Request, principal: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db),
                          date_from: date | None = None, date_to: date | None = None, team_id: uuid.UUID | None = None,
                          member_id: uuid.UUID | None = None,
                          status: str | None = Query(default=None, pattern="^(ASSIGNED|VIEWED|APPLYING|APPLIED|INTERVIEWING|OFFER|REJECTED|WITHDRAWN|SKIPPED)$"),
                          company: str | None = Query(default=None, max_length=120), role: str | None = Query(default=None, max_length=120)):
    if company or role:                                  # free-text filters change with every keystroke: always live
        return await analytics.admin_analytics(db, date_from, date_to, team_id, member_id, status, company, role)
    params = {"from": date_from, "to": date_to, "team": team_id, "member": member_id, "status": status}
    return await request.app.state.cache.cached(
        "admin", params, AdminAnalytics,
        lambda: analytics.admin_analytics(db, date_from, date_to, team_id, member_id, status, None, None))


@router.get("/analytics/member/{member_id}", response_model=MemberAnalytics)
async def member_analytics(member_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db),
                           date_from: date | None = None, date_to: date | None = None):
    member = await member_in_scope(db, principal, member_id)
    if member.role != "MEMBER":
        raise HTTPException(404, "Member not found")
    return await analytics.member_analytics(db, member, date_from, date_to)


# ---------------------------------------------------------------- notifications
@router.get("/notifications", response_model=Page[NotificationOut])
async def list_notifications(principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db),
                             unread_only: bool = False, page: int = 1, page_size: int = 20):
    stmt = select(Notification).where(Notification.user_id == principal.user.id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    rows, total = await paginate(db, stmt.order_by(Notification.created_at.desc()), page, page_size)
    items = [NotificationOut(id=n.id, type=n.type, payload=n.payload, read_at=n.read_at, created_at=n.created_at) for (n,) in rows]
    return Page[NotificationOut](items=items, total=total, page=max(1, page), page_size=max(1, min(page_size, 200)))


@router.post("/notifications/read-all", response_model=Message)
async def read_all_notifications(principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    await db.execute(update(Notification).where(Notification.user_id == principal.user.id, Notification.read_at.is_(None)).values(read_at=utcnow()))
    await db.commit()
    return Message(message="All notifications marked as read")


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
async def read_notification(notification_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    n = await db.get(Notification, notification_id)
    if n is None or n.user_id != principal.user.id:
        raise HTTPException(404, "Notification not found")
    n.read_at = n.read_at or utcnow()
    out = NotificationOut(id=n.id, type=n.type, payload=n.payload, read_at=n.read_at, created_at=n.created_at)
    await db.commit()
    return out


# ---------------------------------------------------------------- audit trail
@router.get("/audit-logs", response_model=Page[AuditOut])
async def audit_logs(principal: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db),
                     action: str | None = Query(default=None, max_length=60), actor_id: uuid.UUID | None = None,
                     team_id: uuid.UUID | None = None, date_from: date | None = None, date_to: date | None = None,
                     page: int = 1, page_size: int = 50):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action.ilike(f"{action.strip()}%"))
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if team_id:
        stmt = stmt.where(AuditLog.team_id == team_id)
    if date_from:
        stmt = stmt.where(AuditLog.created_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc))
    if date_to:
        stmt = stmt.where(AuditLog.created_at < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc))
    rows, total = await paginate(db, stmt.order_by(AuditLog.created_at.desc()), page, page_size)
    logs = [r[0] for r in rows]
    users = await users_by_id(db, {l.actor_id for l in logs})
    teams = await teams_by_id(db, {l.team_id for l in logs})
    items = [AuditOut(id=l.id, action=l.action, target_type=l.target_type, target_id=l.target_id, actor=user_ref(users.get(l.actor_id)),
                      team=team_ref(teams.get(l.team_id)), metadata=l.meta or {}, ip=l.ip, created_at=l.created_at) for l in logs]
    return Page[AuditOut](items=items, total=total, page=max(1, page), page_size=max(1, min(page_size, 200)))


# ---------------------------------------------------------------- settings / integrations
@router.get("/settings", response_model=SettingsOut)
async def get_portal_settings(principal: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db),
                              settings: Settings = Depends(get_settings)):
    return await settings_service.load(db, settings)


@router.patch("/settings", response_model=SettingsOut)
async def update_portal_settings(data: SettingsUpdate, request: Request, principal: Principal = Depends(admin_only),
                                 db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    if data.disabled_sources is not None:
        unknown = {s.lower() for s in data.disabled_sources} - set(registry.SPECS)
        if unknown:
            raise HTTPException(422, f"Unknown source(s): {', '.join(sorted(unknown))}")
    out = await settings_service.save(db, settings, principal.user, data)
    audit(db, principal.user, "settings.updated", "settings", None, None, {"fields": sorted(data.model_dump(exclude_none=True))},
          client_ip(request))
    await db.commit()
    return out


@router.get("/integrations", response_model=IntegrationsOut)
async def integrations(request: Request, principal: Principal = Depends(admin_only), db: AsyncSession = Depends(get_db),
                       settings: Settings = Depends(get_settings)):
    portal = await settings_service.load(db, settings)
    off = set(portal.disabled_sources)
    rows = (await db.execute(text(
        "SELECT src, count(*) AS units, coalesce(sum(count), 0) AS postings FROM index_units "
        "WHERE ok AND expires_at > extract(epoch from now()) GROUP BY src"))).all()
    fresh = {r[0]: (int(r[1]), int(r[2])) for r in rows}
    stored = (await db.execute(text("SELECT count(*) FROM index_postings"))).scalar_one()
    sources = [{"name": s.name, "label": s.label, "kind": s.kind, "note": s.note, "enabled": s.name not in off,
                "available": s.needs(settings), "units": fresh.get(s.name, (0, 0))[0], "postings": fresh.get(s.name, (0, 0))[1]}
               for s in registry.SPECS.values()]
    queue = request.app.state.queue
    worker = await queue.worker_status()
    return IntegrationsOut(
        ai_providers={"openai": {"configured": bool(settings.openai_api_key), "model": settings.openai_model},
                      "claude": {"configured": bool(settings.anthropic_api_key), "model": settings.anthropic_model}},
        sources=sources,
        index={"fresh_postings": sum(v[1] for v in fresh.values()), "fresh_units": sum(v[0] for v in fresh.values()),
               "stored_postings": int(stored)},
        worker=worker or {"online": False},
        email={"backend": request.app.state.emailer.backend, "from": settings.smtp_from if settings.smtp_configured else ""},
        queue={"redis": await queue.ping()})


@router.get("/health", response_model=HealthOut)
async def health(request: Request, db: AsyncSession = Depends(get_db)):
    """Liveness for load balancers and Docker. Reveals no configuration."""
    database = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        database = False
    queue = request.app.state.queue
    redis_ok = await queue.ping()
    worker = await queue.worker_status() if redis_ok else None
    return HealthOut(status="ok" if database else "degraded", database=database, redis=redis_ok, worker_online=bool(worker and worker.get("online")))
