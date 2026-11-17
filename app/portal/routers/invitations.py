"""Invitations (accounts are created only this way) and first-time password setup."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ..deps import Principal, client_ip, forbid, get_db, get_settings, managers, same_origin
from ..models import Invitation, Team
from ..schemas import (InvitationAccept, InvitationCreate, InvitationCreated, InvitationOut, InvitationPreview, Message, Page)
from ..services import invitations as service
from ..services import settings as settings_service
from ..services.audit import audit
from ..services.common import paginate

router = APIRouter(prefix="/api/invitations", tags=["invitations"])


def scoped(principal: Principal):
    stmt = select(Invitation)
    if principal.is_team_lead:
        stmt = stmt.where(Invitation.team_id == principal.user.team_id, Invitation.invited_role == "MEMBER")
    return stmt


async def created_out(db: AsyncSession, invitation: Invitation, url: str, sent: bool) -> InvitationCreated:
    base = (await service.invitations_out(db, [invitation]))[0]
    return InvitationCreated(**base.model_dump(), invite_url=url, email_sent=sent)


@router.post("", response_model=InvitationCreated, status_code=201)
async def create_invitation(data: InvitationCreate, request: Request, principal: Principal = Depends(managers),
                            db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    portal = await settings_service.load(db, settings)
    invitation, url, sent = await service.create_invitation(db, settings, request.app.state.emailer, principal, data,
                                                            portal.invitation_hours, client_ip(request))
    out = await created_out(db, invitation, url, sent)
    await db.commit()
    return out


@router.get("", response_model=Page[InvitationOut])
async def list_invitations(principal: Principal = Depends(managers), db: AsyncSession = Depends(get_db),
                           status: str | None = Query(default=None, pattern="^(pending|accepted|expired|revoked)$"),
                           team_id: uuid.UUID | None = None, page: int = 1, page_size: int = 50):
    stmt = scoped(principal).order_by(Invitation.created_at.desc())
    if team_id and principal.is_admin:
        stmt = stmt.where(Invitation.team_id == team_id)
    rows = list((await db.execute(stmt.limit(1000))).scalars())
    outs = await service.invitations_out(db, rows)
    if status:
        outs = [o for o in outs if o.status == status]
    page, page_size = max(1, page), max(1, min(page_size, 200))
    return Page[InvitationOut](items=outs[(page - 1) * page_size: page * page_size], total=len(outs), page=page, page_size=page_size)


async def _own(db: AsyncSession, principal: Principal, invitation_id: uuid.UUID) -> Invitation:
    invitation = (await db.execute(scoped(principal).where(Invitation.id == invitation_id))).scalar_one_or_none()
    if invitation is None:
        raise HTTPException(404, "Invitation not found") if principal.is_admin else forbid()
    return invitation


@router.post("/{invitation_id}/resend", response_model=InvitationCreated)
async def resend_invitation(invitation_id: uuid.UUID, request: Request, principal: Principal = Depends(managers),
                            db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)):
    invitation = await _own(db, principal, invitation_id)
    portal = await settings_service.load(db, settings)
    fresh, url, sent = await service.resend(db, settings, request.app.state.emailer, principal, invitation,
                                            portal.invitation_hours, client_ip(request))
    out = await created_out(db, fresh, url, sent)
    await db.commit()
    return out


@router.post("/{invitation_id}/revoke", response_model=InvitationOut)
async def revoke_invitation(invitation_id: uuid.UUID, request: Request, principal: Principal = Depends(managers),
                            db: AsyncSession = Depends(get_db)):
    invitation = await _own(db, principal, invitation_id)
    if service.invitation_status(invitation) == "pending":
        invitation.revoked_at = service.utcnow()
        audit(db, principal.user, "invitation.revoked", "user", invitation.user_id, invitation.team_id, {}, client_ip(request))
    out = (await service.invitations_out(db, [invitation]))[0]
    await db.commit()
    return out


@router.get("/validate", response_model=InvitationPreview)
async def validate_invitation(token: str = Query(min_length=20, max_length=200), db: AsyncSession = Depends(get_db),
                              settings: Settings = Depends(get_settings)):
    found = await service.find_valid(db, token)
    if not found:
        raise HTTPException(404, service.INVALID)                    # one message for every failure reason
    invitation, user = found
    team = await db.get(Team, invitation.team_id) if invitation.team_id else None
    return InvitationPreview(email=user.email, name=user.name, role=invitation.invited_role, team_name=team.name if team else None,
                             expires_at=invitation.expires_at, password_min_length=settings.password_min_length)


@router.post("/accept", response_model=Message, dependencies=[Depends(same_origin)])
async def accept_invitation(data: InvitationAccept, request: Request, db: AsyncSession = Depends(get_db),
                            settings: Settings = Depends(get_settings)):
    await service.accept(db, settings, data.token, data.password, client_ip(request))
    await db.commit()
    return Message(message="Your account is ready. Sign in with your e-mail address and new password.")
