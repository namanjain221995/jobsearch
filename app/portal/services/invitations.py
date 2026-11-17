"""Invitations: the only way an account comes into existence (no public registration).

A token is random (256 bit), handed out once, stored only as a SHA-256 hash, valid for a limited time
and usable once. The role and team are fixed on the invitation row at creation time by the server,
so nothing in the link can be altered to obtain another role or team."""
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from .. import security
from ..deps import Principal
from ..models import Invitation, MemberProfile, PasswordReset, Team, TeamLeadAssignment, User
from ..schemas import InvitationCreate, InvitationOut
from ..security import utcnow
from .audit import audit, notify
from .common import team_ref, teams_by_id, user_ref, users_by_id
from .emailer import Emailer, invitation_mail, reset_mail

INVALID = "This invitation link is invalid or has expired."


def still_covers(manager: User | None, user: User) -> bool:
    """True while the manager who issued a one-time link may still act for this user. A link issued by a Team Lead
    dies when that lead is deactivated, stops leading the team, or the member moves to another team."""
    if manager is None or not manager.is_active:
        return False
    if manager.role == "ADMIN":
        return True
    return manager.role == "TEAM_LEAD" and user.role == "MEMBER" and manager.team_id is not None and manager.team_id == user.team_id


def invitation_status(inv: Invitation) -> str:
    if inv.accepted_at:
        return "accepted"
    if inv.revoked_at:
        return "revoked"
    return "expired" if inv.expires_at <= utcnow() else "pending"


async def invitations_out(db: AsyncSession, invitations: list[Invitation]) -> list[InvitationOut]:
    users = await users_by_id(db, {i.invited_by for i in invitations})
    teams = await teams_by_id(db, {i.team_id for i in invitations})
    return [InvitationOut(id=i.id, email=i.email, name=i.name, role=i.invited_role, team=team_ref(teams.get(i.team_id)),
                          status=invitation_status(i), delivery=i.delivery, expires_at=i.expires_at, accepted_at=i.accepted_at,
                          created_at=i.created_at, invited_by=user_ref(users.get(i.invited_by))) for i in invitations]


async def _issue(db: AsyncSession, settings: Settings, emailer: Emailer, actor: User, user: User, team: Team | None,
                 hours: int) -> tuple[Invitation, str, bool]:
    """Revoke earlier pending links for this user and create a fresh one."""
    pending = (await db.execute(select(Invitation).where(
        Invitation.user_id == user.id, Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None)))).scalars()
    for old in pending:
        old.revoked_at = utcnow()
    token, token_hash = security.new_token()
    url = f"{settings.public_base_url.rstrip('/')}/invite/accept?token={token}"
    subject, body = invitation_mail(user.name, team.name if team else None, user.role, url, hours)
    sent = await emailer.send(user.email, subject, body)
    invitation = Invitation(email=user.email, name=user.name, invited_role=user.role, team_id=team.id if team else None,
                            user_id=user.id, token_hash=token_hash, invited_by=actor.id,
                            delivery="email" if sent else "manual", expires_at=utcnow() + timedelta(hours=hours))
    db.add(invitation)
    await db.flush()
    return invitation, url, sent


async def create_invitation(db: AsyncSession, settings: Settings, emailer: Emailer, principal: Principal,
                            data: InvitationCreate, hours: int, ip: str) -> tuple[Invitation, str, bool]:
    actor = principal.user
    email = data.email.strip().lower()
    team: Team | None = None
    if principal.is_team_lead:
        # A Team Lead can only add members to the team they lead, whatever the request says.
        if data.role != "MEMBER" or (data.team_id and data.team_id != actor.team_id) or data.team_name:
            raise HTTPException(403, "Team Leads can only invite members into their own team")
        if not actor.team_id:
            raise HTTPException(409, "You are not assigned to a team yet")
        team = await db.get(Team, actor.team_id)
    elif principal.is_admin:
        if data.team_id:
            team = await db.get(Team, data.team_id)
            if team is None:
                raise HTTPException(404, "Team not found")
        elif data.role == "TEAM_LEAD" and data.team_name:
            name = data.team_name.strip()
            if (await db.execute(select(Team.id).where(func.lower(Team.name) == name.lower()))).first():
                raise HTTPException(409, "A team with this name already exists")
            team = Team(name=name, description=data.team_description.strip())
            db.add(team)
            await db.flush()
            audit(db, actor, "team.created", "team", team.id, team.id, {"name": name}, ip)
        elif data.role == "MEMBER":
            raise HTTPException(422, "Choose the team this member joins")
    else:
        raise HTTPException(403, "You do not have access to this resource")
    if team is not None and not team.is_active:
        raise HTTPException(409, "This team is deactivated")

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None:
        same_place = user.role == data.role and user.team_id == (team.id if team else None)
        if user.password_hash or not user.is_active or not same_place:
            # Same answer for every case so a Team Lead cannot probe which addresses exist in other teams.
            raise HTTPException(409, "This e-mail address cannot be invited. It may already belong to an account.")
        user.name = data.name.strip()                  # still pending: re-issue the link
    else:
        if data.role == "TEAM_LEAD" and team is not None:
            if (await db.execute(select(TeamLeadAssignment).where(TeamLeadAssignment.team_id == team.id))).first():
                raise HTTPException(409, "This team already has a Team Lead. Remove or replace them first.")
        user = User(name=data.name.strip(), email=email, role=data.role, team_id=team.id if team else None,
                    is_active=True, created_by=actor.id)
        db.add(user)
        await db.flush()
        if data.role == "MEMBER":
            db.add(MemberProfile(user_id=user.id, desired_titles=[], skills=[], preferred_states=[], preferred_cities=[],
                                 excluded_companies=[], required_keywords=[], extra={}))
        elif team is not None:
            db.add(TeamLeadAssignment(team_id=team.id, user_id=user.id))
    invitation, url, sent = await _issue(db, settings, emailer, actor, user, team, hours)
    audit(db, actor, "invitation.created", "user", user.id, team.id if team else None,
          {"role": data.role, "delivery": invitation.delivery}, ip)
    return invitation, url, sent


async def resend(db: AsyncSession, settings: Settings, emailer: Emailer, principal: Principal, invitation: Invitation,
                 hours: int, ip: str) -> tuple[Invitation, str, bool]:
    user = await db.get(User, invitation.user_id)
    if user is None or user.password_hash or not user.is_active:
        raise HTTPException(409, "This invitation was already accepted or the account is deactivated")
    if not still_covers(principal.user, user):               # the person was moved to another team meanwhile
        raise HTTPException(403, "You do not have access to this resource")
    team = await db.get(Team, user.team_id) if user.team_id else None
    if team is not None and not team.is_active:
        raise HTTPException(409, "This team is deactivated")
    fresh, url, sent = await _issue(db, settings, emailer, principal.user, user, team, hours)
    audit(db, principal.user, "invitation.resent", "user", user.id, user.team_id, {"delivery": fresh.delivery}, ip)
    return fresh, url, sent


async def find_valid(db: AsyncSession, token: str) -> tuple[Invitation, User] | None:
    if not token or len(token) > 200:
        return None
    row = (await db.execute(select(Invitation, User).join(User, User.id == Invitation.user_id)
                            .where(Invitation.token_hash == security.hash_token(token)))).first()
    if not row:
        return None
    invitation, user = row
    if invitation_status(invitation) != "pending" or user.password_hash or not user.is_active:
        return None
    if invitation.team_id != user.team_id:                   # moved to another team after the link was made
        return None
    if invitation.invited_by is not None and not still_covers(await db.get(User, invitation.invited_by), user):
        return None
    return invitation, user


async def accept(db: AsyncSession, settings: Settings, token: str, password: str, ip: str) -> User:
    found = await find_valid(db, token)
    if not found:
        raise HTTPException(400, INVALID)
    invitation, user = found
    problem = security.password_problem(password, settings, user.email, user.name)
    if problem:
        raise HTTPException(422, problem)
    # Role and team come from the stored invitation, never from the request.
    user.password_hash = await security.hash_password_async(password)
    invitation.accepted_at = utcnow()
    audit(db, user, "invitation.accepted", "user", user.id, user.team_id, {"role": user.role}, ip)
    if invitation.invited_by:
        notify(db, invitation.invited_by, "invitation_accepted", {"user_id": str(user.id), "name": user.name, "role": user.role})
    return user


# ---------------------------------------------------------------- password reset
async def retire_reset_links(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Every reset link of this user that was not used yet stops working."""
    await db.execute(update(PasswordReset).where(PasswordReset.user_id == user_id, PasswordReset.used_at.is_(None))
                     .values(used_at=utcnow()))


async def prepare_reset(db: AsyncSession, settings: Settings, user: User, created_by: User | None) -> tuple[str, PasswordReset, tuple[str, str]]:
    """Create the only valid reset link of this user. Returns (url, row, (mail subject, mail body)); nothing is sent."""
    await retire_reset_links(db, user.id)
    token, token_hash = security.new_token()
    reset = PasswordReset(user_id=user.id, token_hash=token_hash, created_by=created_by.id if created_by else None,
                          expires_at=utcnow() + timedelta(minutes=settings.reset_minutes))
    db.add(reset)
    url = f"{settings.public_base_url.rstrip('/')}/reset-password?token={token}"
    return url, reset, reset_mail(user.name, url, settings.reset_minutes)


async def issue_reset(db: AsyncSession, settings: Settings, emailer: Emailer, user: User, created_by: User | None) -> tuple[str, bool, PasswordReset]:
    url, reset, (subject, body) = await prepare_reset(db, settings, user, created_by)
    sent = await emailer.send(user.email, subject, body)
    return url, sent, reset


async def reset_password(db: AsyncSession, settings: Settings, token: str, password: str, ip: str) -> None:
    invalid = HTTPException(400, "This reset link is invalid or has expired.")
    if not token or len(token) > 200:
        raise invalid
    row = (await db.execute(select(PasswordReset, User).join(User, User.id == PasswordReset.user_id)
                            .where(PasswordReset.token_hash == security.hash_token(token)))).first()
    if not row:
        raise invalid
    reset, user = row
    if reset.used_at or reset.expires_at <= utcnow() or not user.is_active:
        raise invalid
    if reset.created_by is not None and not still_covers(await db.get(User, reset.created_by), user):
        raise invalid                                         # made by a manager who no longer manages this person
    problem = security.password_problem(password, settings, user.email, user.name)
    if problem:
        raise HTTPException(422, problem)
    user.password_hash = await security.hash_password_async(password)
    await retire_reset_links(db, user.id)                     # this link and every other outstanding one
    await security.revoke_user_sessions(db, user.id)        # every existing login ends
    audit(db, user, "auth.password_reset", "user", user.id, user.team_id, {}, ip)
