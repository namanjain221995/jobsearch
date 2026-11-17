"""Users, member profiles and resumes. Every handler re-checks who is asking and what they may touch."""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from .. import security
from ..deps import (Principal, any_user, client_ip, forbid, get_db, get_settings, managers, member_in_scope, user_scope)
from ..models import ApplicationEvent, AuditLog, JobAssignment, JobPosting, Resume, Team, User
from ..schemas import (Day, ActivityItem, InvitationCreate, InvitationCreated, MemberAnalytics, Message, Page, ProfileIn, ProfileOut, ResetLinkOut, ResumeOut, UserOut,
                       UserUpdate)
from ..security import utcnow
from ..services import analytics, people
from ..services import settings as settings_service
from ..services import assignments as assignments_service
from ..services import invitations as invitation_service
from ..services.audit import audit
from ..services.common import paginate, user_ref, users_by_id
from . import invitations as invitation_routes

router = APIRouter(prefix="/api", tags=["users"])


async def load_user(db: AsyncSession, principal: Principal, user_id: uuid.UUID) -> User:
    """Any user row the caller may see (Admin: all, Team Lead: own team, Member: self)."""
    user = (await db.execute(select(User).where(User.id == user_id, user_scope(principal)))).scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "User not found") if principal.is_admin else forbid()
    return user


@router.get("/users", response_model=Page[UserOut])
async def list_users(principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db),
                     role: str | None = Query(default=None, pattern="^(ADMIN|TEAM_LEAD|MEMBER)$"), team_id: uuid.UUID | None = None,
                     status: str | None = Query(default=None, pattern="^(invited|active|deactivated)$"),
                     q: str | None = Query(default=None, max_length=100), sort: str = "name", page: int = 1, page_size: int = 50):
    stmt = select(User).where(user_scope(principal))
    if principal.is_team_lead:
        stmt = stmt.where(User.role == "MEMBER")                    # a Team Lead manages members, not other leads
    if role:
        stmt = stmt.where(User.role == role)
    if team_id:
        stmt = stmt.where(User.team_id == team_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.name.ilike(like), User.email.ilike(like)))
    if status == "deactivated":
        stmt = stmt.where(User.is_active.is_(False))
    elif status == "invited":
        stmt = stmt.where(User.is_active, User.password_hash.is_(None))
    elif status == "active":
        stmt = stmt.where(User.is_active, User.password_hash.is_not(None))
    order = {"name": User.name, "recent": User.created_at.desc(), "activity": User.last_activity_at.desc().nulls_last()}.get(sort, User.name)
    rows, total = await paginate(db, stmt.order_by(order), page, page_size)
    return Page[UserOut](items=await people.users_out(db, [r[0] for r in rows]), total=total, page=max(1, page),
                         page_size=max(1, min(page_size, 200)))


@router.post("/users", response_model=InvitationCreated, status_code=201)
async def create_user(data: InvitationCreate, request: Request, principal: Principal = Depends(managers),
                      db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Create an account. Nobody ever chooses another person's password: the account is created in the
    "invited" state and the invitation is returned, exactly as POST /api/invitations does (same rules:
    a Team Lead can only add members to their own team)."""
    return await invitation_routes.create_invitation(data, request, principal, db, settings)


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(user_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    return (await people.users_out(db, [await load_user(db, principal, user_id)]))[0]


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: uuid.UUID, data: UserUpdate, request: Request, principal: Principal = Depends(any_user),
                      db: AsyncSession = Depends(get_db)):
    user = await load_user(db, principal, user_id)
    ip = client_ip(request)
    is_self = user.id == principal.user.id
    if principal.is_member and (not is_self or data.is_active is not None or data.team_id is not None):
        raise forbid()                                              # members may only rename themselves
    if principal.is_team_lead and not is_self and user.role != "MEMBER":
        raise forbid()
    if data.name is not None and data.name.strip() != user.name:
        user.name = data.name.strip()
        audit(db, principal.user, "user.renamed", "user", user.id, user.team_id, {}, ip)
    if data.is_active is not None and data.is_active != user.is_active:
        if is_self:
            raise HTTPException(409, "You cannot deactivate your own account")
        if data.is_active and principal.is_team_lead and await people.deactivated_by_admin(db, [user.id]):
            raise HTTPException(403, "This account was deactivated by an administrator. Only an administrator can reactivate it.")
        user.is_active = data.is_active
        if not data.is_active:
            await security.revoke_user_sessions(db, user.id)       # a deactivated account is signed out everywhere
        audit(db, principal.user, "user.activated" if data.is_active else "user.deactivated", "user", user.id, user.team_id, {}, ip)
    if data.team_id is not None and data.team_id != user.team_id:
        if not principal.is_admin:
            raise forbid()                                          # only an Admin moves people between teams
        team = await db.get(Team, data.team_id)
        if team is None:
            raise HTTPException(404, "Team not found")
        await people.transfer_member(db, principal.user, user, team)
    await db.flush()
    await db.refresh(user)
    out = (await people.users_out(db, [user]))[0]
    await db.commit()
    return out


@router.post("/users/{user_id}/reset-link", response_model=ResetLinkOut)
async def create_reset_link(user_id: uuid.UUID, request: Request, principal: Principal = Depends(managers),
                            db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    """A manager creates a one-time password reset link for a user in their scope (used when e-mail is not configured)."""
    user = await load_user(db, principal, user_id)
    if principal.is_team_lead and user.role != "MEMBER":
        raise forbid()
    if not user.is_active or not user.password_hash:
        raise HTTPException(409, "This account is not active yet. Resend the invitation instead.")
    url, sent, reset = await invitation_service.issue_reset(db, settings, request.app.state.emailer, user, principal.user)
    audit(db, principal.user, "auth.reset_link_created", "user", user.id, user.team_id, {"email_sent": sent}, client_ip(request))
    await db.commit()
    return ResetLinkOut(reset_url="" if sent else url, expires_at=reset.expires_at, email_sent=sent)     # e-mailed: not handed over as well


# ---------------------------------------------------------------- member dashboard / activity
@router.get("/users/{user_id}/dashboard", response_model=MemberAnalytics)
async def member_dashboard(user_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db),
                           date_from: Day | None = None, date_to: Day | None = None):
    member = await member_in_scope(db, principal, user_id)
    if member.role != "MEMBER":
        raise HTTPException(404, "Member not found")
    return await analytics.member_analytics(db, member, date_from, date_to)


@router.get("/users/{user_id}/activity", response_model=Page[ActivityItem])
async def member_activity(user_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db),
                          page: int = 1, page_size: int = 50):
    member = await member_in_scope(db, principal, user_id)
    stmt = (select(ApplicationEvent, JobAssignment, JobPosting)
            .join(JobAssignment, JobAssignment.id == ApplicationEvent.assignment_id)
            .join(JobPosting, JobPosting.id == JobAssignment.job_id)
            .where(JobAssignment.member_id == member.id).order_by(ApplicationEvent.created_at.desc()))
    if principal.is_member:                              # nothing managers keep among themselves
        private = ApplicationEvent.meta["private"].astext
        stmt = stmt.where(JobAssignment.is_active, ApplicationEvent.event_type.not_in(assignments_service.MANAGER_ONLY_EVENTS),
                          or_(private.is_(None), private != "true"))
    rows, total = await paginate(db, stmt, page, page_size)
    actors = await users_by_id(db, {e.actor_id for e, _, _ in rows})
    items = [ActivityItem(id=e.id, event_type=e.event_type, old_status=e.old_status, new_status=e.new_status, created_at=e.created_at,
                          assignment_id=a.id, job_title=p.title, company=p.company, member=user_ref(member),
                          actor=user_ref(actors.get(e.actor_id))) for e, a, p in rows]
    return Page[ActivityItem](items=items, total=total, page=max(1, page), page_size=max(1, min(page_size, 200)))


# ---------------------------------------------------------------- profile
@router.get("/users/{user_id}/profile", response_model=ProfileOut)
async def get_profile(user_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    member = await member_in_scope(db, principal, user_id)
    profile = await people.get_profile(db, member.id)
    out = people.profile_out(profile, await people.current_resume(db, member.id) is not None)
    await db.commit()
    return out


@router.patch("/users/{user_id}/profile", response_model=ProfileOut)
async def update_profile(user_id: uuid.UUID, data: ProfileIn, request: Request, principal: Principal = Depends(any_user),
                         db: AsyncSession = Depends(get_db)):
    member = await member_in_scope(db, principal, user_id)
    profile = await people.get_profile(db, member.id)
    portal = await settings_service.load(db, request.app.state.settings)
    await people.update_profile(db, profile, data, portal.profile_extra_fields)
    audit(db, principal.user, "profile.updated", "user", member.id, member.team_id,
          {"fields": sorted(data.model_dump(exclude_unset=True))}, client_ip(request))
    if principal.is_member:
        people.touch(principal.user)
    await db.flush()
    await db.refresh(profile)
    out = people.profile_out(profile, await people.current_resume(db, member.id) is not None)
    await db.commit()
    return out


# ---------------------------------------------------------------- resumes
@router.post("/resumes/upload", response_model=ResumeOut, status_code=201)
async def upload_resume(request: Request, file: UploadFile = File(...), member_id: uuid.UUID | None = Form(default=None),
                        principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db),
                        settings: Settings = Depends(get_settings)):
    """A member uploads their own resume; a Team Lead or Admin may upload one for a member in their scope."""
    member = await member_in_scope(db, principal, member_id or principal.user.id)
    if member.role != "MEMBER":
        raise HTTPException(400, "Resumes belong to members")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > settings.max_upload_bytes + 64 * 1024:
        raise HTTPException(413, f"File is too large. Maximum size is {settings.max_upload_bytes // (1024 * 1024)} MB.")
    data = await file.read(settings.max_upload_bytes + 1)
    await file.close()
    resume = await people.save_resume(db, settings, member, principal.user, (file.filename or "resume")[:200], data, file.content_type or "")
    if principal.is_member:
        people.touch(principal.user)
    await db.flush()
    out = await people.resume_out(db, resume, include_text=True)
    await db.commit()
    return out


async def _resume_in_scope(db: AsyncSession, principal: Principal, resume_id: uuid.UUID) -> Resume:
    resume = await db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(404, "Resume not found") if principal.is_admin else forbid()
    await member_in_scope(db, principal, resume.user_id)
    return resume


@router.get("/resumes/{resume_id}", response_model=ResumeOut)
async def get_resume(resume_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    return await people.resume_out(db, await _resume_in_scope(db, principal, resume_id), include_text=True)


@router.delete("/resumes/{resume_id}", response_model=Message)
async def delete_resume(resume_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    resume = await _resume_in_scope(db, principal, resume_id)
    await people.delete_resume(db, principal.user, resume)
    await db.commit()
    return Message(message="Resume deleted")


@router.get("/members/{member_id}/resume", response_model=ResumeOut | None)
async def member_resume(member_id: uuid.UUID, principal: Principal = Depends(any_user), db: AsyncSession = Depends(get_db)):
    member = await member_in_scope(db, principal, member_id)
    resume = await people.current_resume(db, member.id)
    return await people.resume_out(db, resume, include_text=True) if resume else None
