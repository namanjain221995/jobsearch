"""Users, teams, member profiles and resumes."""
import asyncio
import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ...resume_parser import ResumeError, extract_text_safely, heuristic_profile
from ..models import (AuditLog, Invitation, JobAssignment, MemberProfile, PasswordReset, Resume, Team, TeamJobQueue, TeamLeadAssignment, User)
from ..schemas import ProfileIn, ProfileOut, ResumeOut, TeamOut, UserOut
from ..security import utcnow
from .audit import audit
from .common import stats_by, team_ref, user_ref, users_by_id


# ---------------------------------------------------------------- profiles
def profile_completion(profile: MemberProfile | None, has_resume: bool) -> tuple[int, list[str]]:
    checks = [
        ("Upload a resume", has_resume),
        ("Add at least one desired job title", bool(profile and profile.desired_titles)),
        ("Enter years of experience", bool(profile and profile.years_experience is not None)),
        ("List your skills", bool(profile and profile.skills)),
        ("Choose a preferred US location or remote work", bool(profile and (profile.preferred_states or profile.preferred_cities
                                                                              or profile.work_mode == "Remote"))),
    ]
    missing = [label for label, done in checks if not done]
    return round(100 * (len(checks) - len(missing)) / len(checks)), missing


async def get_profile(db: AsyncSession, user_id: uuid.UUID) -> MemberProfile:
    profile = (await db.execute(select(MemberProfile).where(MemberProfile.user_id == user_id))).scalar_one_or_none()
    if profile is None:
        profile = MemberProfile(user_id=user_id, desired_titles=[], skills=[], preferred_states=[], preferred_cities=[],
                                excluded_companies=[], required_keywords=[], extra={})
        db.add(profile)
        await db.flush()
    return profile


def profile_out(profile: MemberProfile, has_resume: bool) -> ProfileOut:
    completion, missing = profile_completion(profile, has_resume)
    return ProfileOut(
        user_id=profile.user_id, desired_titles=profile.desired_titles or [],
        years_experience=float(profile.years_experience) if profile.years_experience is not None else None,
        skills=profile.skills or [], preferred_states=profile.preferred_states or [], preferred_cities=profile.preferred_cities or [],
        work_mode=profile.work_mode, work_authorization=profile.work_authorization, sponsorship_needed=profile.sponsorship_needed,
        exclude_citizenship_required=profile.exclude_citizenship_required, salary_min=profile.salary_min, seniority=profile.seniority,
        excluded_companies=profile.excluded_companies or [], required_keywords=profile.required_keywords or [],
        extra=profile.extra or {}, completion=completion, missing=missing, updated_at=profile.updated_at)


EXTRA_TEXT_MAX = 500


def _answer(field, value):
    """One answer in its field's type; None means "no answer". Raises ValueError with what to do when it does not fit."""
    if value is None:
        return None
    if field.type == "number":
        if isinstance(value, bool):
            raise ValueError("enter a number")
        if isinstance(value, str) and not value.strip():
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("enter a number") from None
        if number != number or abs(number) > 1e12:
            raise ValueError("enter a number")
        return int(number) if number == int(number) else number
    if field.type == "yes_no":
        if value == "":
            return None
        if not isinstance(value, bool):
            raise ValueError("choose yes or no")
        return value
    if field.type == "choice":
        if value == "":
            return None
        if not isinstance(value, str) or value not in field.options:
            raise ValueError("choose one of the listed options")
        return value
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("enter text")
    text = " ".join(str(value).split())
    if len(text) > EXTRA_TEXT_MAX:
        raise ValueError(f"use at most {EXTRA_TEXT_MAX} characters")
    return text or None


def clean_extra(values: dict, definitions: list) -> dict:
    """Values for the extra fields the Admin defined, each in its field's type. A value that does not fit is refused
    (422 with the field's label); a key that is not a defined field is dropped; an empty answer is no answer."""
    out = {}
    for field in definitions:
        if field.key not in values:
            continue
        try:
            answer = _answer(field, values[field.key])
        except ValueError as problem:
            raise HTTPException(422, f"{field.label}: {problem}") from None
        if answer is not None:
            out[field.key] = answer
    return out


def fitting_extra(stored: dict, definitions: list) -> dict:
    """The stored answers that still belong to a defined field and still fit it. Used when the Admin changes the fields."""
    out = {}
    for field in definitions:
        try:
            answer = _answer(field, (stored or {}).get(field.key))
        except ValueError:
            continue
        if answer is not None:
            out[field.key] = answer
    return out


async def drop_unfitting_extra(db: AsyncSession, definitions: list) -> int:
    """After the Admin changed the extra fields: delete every stored answer to a field that was removed, and every
    answer that no longer fits its field (another type, an option that is gone). Returns how many answers went.
    Stored answers therefore always match the fields that exist; a field added again later starts empty."""
    dropped = 0
    profiles = (await db.execute(select(MemberProfile).where(MemberProfile.extra != {}))).scalars().all()
    for profile in profiles:
        kept = fitting_extra(profile.extra, definitions)
        if kept != profile.extra:
            dropped += len(set(profile.extra) - set(kept))
            profile.extra = kept
    return dropped


async def update_profile(db: AsyncSession, profile: MemberProfile, data: ProfileIn, definitions: list | None = None) -> None:
    for field, value in data.model_dump(exclude_unset=True).items():
        if field == "extra":
            if value is not None:
                kept = {k: v for k, v in (profile.extra or {}).items() if k not in value}      # fields this request did not send
                profile.extra = {**kept, **clean_extra(value, definitions or [])}
            continue
        if value is None and field not in ("years_experience", "salary_min"):
            continue                                   # null on a non-nullable field means "leave unchanged"
        setattr(profile, field, value)


# ---------------------------------------------------------------- resumes
async def current_resume(db: AsyncSession, user_id: uuid.UUID) -> Resume | None:
    return (await db.execute(select(Resume).where(Resume.user_id == user_id, Resume.is_current))).scalar_one_or_none()


async def resume_out(db: AsyncSession, resume: Resume, include_text: bool) -> ResumeOut:
    uploader = (await users_by_id(db, {resume.uploaded_by})).get(resume.uploaded_by)
    return ResumeOut(id=resume.id, user_id=resume.user_id, filename=resume.filename, size_bytes=resume.size_bytes,
                     version=resume.version, truncated=resume.truncated, created_at=resume.created_at,
                     uploaded_by=user_ref(uploader), parsed_profile=resume.parsed_profile or {},
                     text=resume.text if include_text else None)


async def save_resume(db: AsyncSession, settings: Settings, member: User, uploader: User, filename: str, data: bytes,
                      content_type: str) -> Resume:
    """Extract text in memory, store text + structured profile, and make it the member's current resume.
    The uploaded file is not kept. Text of the replaced version is erased (its metadata stays as history)."""
    try:
        text, truncated = await extract_text_safely(filename, data, settings.max_upload_bytes)      # limited child process
    except ResumeError as exc:
        raise HTTPException(413 if "too large" in str(exc).lower() else 400, str(exc)) from None
    parsed = heuristic_profile(text)
    previous = await current_resume(db, member.id)
    version = 1
    if previous:
        version = previous.version + 1
        previous.is_current = False
        previous.text = ""
        await db.flush()
    resume = Resume(user_id=member.id, filename=filename[:200], content_type=(content_type or "")[:100], size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(), text=text, truncated=truncated, version=version, is_current=True,
                    uploaded_by=uploader.id,
                    parsed_profile={"skills": parsed.skills, "job_titles": parsed.job_titles, "education": parsed.education})
    db.add(resume)
    profile = await get_profile(db, member.id)
    if not profile.skills and parsed.skills:
        profile.skills = parsed.skills[:60]           # a starting point the member can correct
    audit(db, uploader, "resume.uploaded", "user", member.id, member.team_id, {"version": version, "size_bytes": len(data)})
    return resume


async def delete_resume(db: AsyncSession, actor: User, resume: Resume) -> None:
    await db.delete(resume)
    audit(db, actor, "resume.deleted", "user", resume.user_id, None, {"version": resume.version})


# ---------------------------------------------------------------- users
async def users_out(db: AsyncSession, users: list[User]) -> list[UserOut]:
    ids = [u.id for u in users]
    member_ids = [u.id for u in users if u.role == "MEMBER"]
    with_resume: set[uuid.UUID] = set()
    profiles: dict[uuid.UUID, MemberProfile] = {}
    stats = {}
    if member_ids:
        with_resume = set((await db.execute(select(Resume.user_id).where(Resume.user_id.in_(member_ids), Resume.is_current))).scalars())
        profiles = {p.user_id: p for p in (await db.execute(select(MemberProfile).where(MemberProfile.user_id.in_(member_ids)))).scalars()}
        stats = await stats_by(db, JobAssignment.member_id, [JobAssignment.member_id.in_(member_ids)])
    from ..schemas import AssignmentStats
    out = []
    by_admin = await deactivated_by_admin(db, [u.id for u in users if not u.is_active])
    for u in users:
        is_member = u.role == "MEMBER"
        completion = profile_completion(profiles.get(u.id), u.id in with_resume)[0] if is_member else 0
        out.append(UserOut(id=u.id, name=u.name, email=u.email, role=u.role, team=team_ref(u.team), status=u.status,
                           is_active=u.is_active, deactivated_by_admin=u.id in by_admin,
                           has_resume=u.id in with_resume, profile_completion=completion,
                           last_login_at=u.last_login_at, last_activity_at=u.last_activity_at, created_at=u.created_at,
                           stats=(stats.get(u.id) or AssignmentStats()) if is_member else None))
    return out


async def deactivated_by_admin(db: AsyncSession, user_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Of these deactivated accounts, those whose latest deactivation was made by an Admin (from the audit trail).
    A deactivation of unknown origin counts as an Admin decision. A Team Lead cannot reactivate these."""
    if not user_ids:
        return set()
    rows = (await db.execute(
        select(AuditLog.target_id, User.role).outerjoin(User, User.id == AuditLog.actor_id)
        .where(AuditLog.action == "user.deactivated", AuditLog.target_id.in_([str(i) for i in user_ids]))
        .distinct(AuditLog.target_id).order_by(AuditLog.target_id, AuditLog.created_at.desc()))).all()
    by_lead = {target for target, role in rows if role == "TEAM_LEAD"}
    return {i for i in user_ids if str(i) not in by_lead}


async def transfer_member(db: AsyncSession, actor: User, member: User, team: Team) -> None:
    """Move a member to another team. Their assignments move with them, so the new Team Lead manages the
    member's whole pipeline and the previous Team Lead no longer sees it. Application history is untouched."""
    if member.role != "MEMBER":
        raise HTTPException(400, "Only members can be transferred between teams")
    if not team.is_active:
        raise HTTPException(400, "The destination team is deactivated")
    old_team_id = member.team_id
    if old_team_id == team.id:
        return
    member.team_id = team.id
    await db.execute(update(JobAssignment).where(JobAssignment.member_id == member.id).values(team_id=team.id))
    # One-time links made under the old team stop working; the invitation history follows the member, so the
    # new team's lead (not the old one) can see it and send a fresh link.
    await db.execute(update(Invitation).where(Invitation.user_id == member.id, Invitation.accepted_at.is_(None),
                                              Invitation.revoked_at.is_(None)).values(revoked_at=utcnow()))
    await db.execute(update(Invitation).where(Invitation.user_id == member.id).values(team_id=team.id))
    await db.execute(update(PasswordReset).where(PasswordReset.user_id == member.id, PasswordReset.used_at.is_(None)).values(used_at=utcnow()))
    audit(db, actor, "member.transferred", "user", member.id, team.id, {"from_team": str(old_team_id) if old_team_id else None})


# ---------------------------------------------------------------- teams
async def team_lead_of(db: AsyncSession, team_id: uuid.UUID) -> User | None:
    return (await db.execute(select(User).join(TeamLeadAssignment, TeamLeadAssignment.user_id == User.id)
                             .where(TeamLeadAssignment.team_id == team_id))).scalar_one_or_none()


async def teams_out(db: AsyncSession, teams: list[Team]) -> list[TeamOut]:
    ids = [t.id for t in teams]
    if not ids:
        return []
    leads = {row[0]: row[1] for row in (await db.execute(
        select(TeamLeadAssignment.team_id, User).join(User, User.id == TeamLeadAssignment.user_id)
        .where(TeamLeadAssignment.team_id.in_(ids)))).all()}
    members = dict((await db.execute(select(User.team_id, func.count()).where(
        User.team_id.in_(ids), User.role == "MEMBER", User.is_active).group_by(User.team_id))).all())
    queue = dict((await db.execute(select(TeamJobQueue.team_id, func.count()).where(
        TeamJobQueue.team_id.in_(ids), TeamJobQueue.status == "pending").group_by(TeamJobQueue.team_id))).all())
    stats = await stats_by(db, JobAssignment.team_id, [JobAssignment.team_id.in_(ids)])
    from ..schemas import AssignmentStats
    return [TeamOut(id=t.id, name=t.name, description=t.description, is_active=t.is_active, created_at=t.created_at,
                    team_lead=user_ref(leads.get(t.id)), team_lead_status=leads[t.id].status if t.id in leads else None,
                    member_count=members.get(t.id, 0), queue_pending=queue.get(t.id, 0),
                    stats=stats.get(t.id) or AssignmentStats()) for t in teams]


async def set_team_lead(db: AsyncSession, actor: User, team: Team, lead: User | None) -> None:
    """Assign, replace or remove a team's (single) Team Lead."""
    current = (await db.execute(select(TeamLeadAssignment).where(TeamLeadAssignment.team_id == team.id))).scalar_one_or_none()
    if lead is None:
        if current:
            old = await db.get(User, current.user_id)
            await db.delete(current)
            if old and old.team_id == team.id:
                old.team_id = None
            audit(db, actor, "team.lead_removed", "team", team.id, team.id, {"user_id": str(current.user_id)})
        return
    if lead.role != "TEAM_LEAD":
        raise HTTPException(400, "Selected user is not a Team Lead")
    if current and current.user_id == lead.id:
        return
    other = (await db.execute(select(TeamLeadAssignment).where(TeamLeadAssignment.user_id == lead.id))).scalar_one_or_none()
    if other:
        raise HTTPException(409, "This Team Lead already leads another team")
    if current:
        old = await db.get(User, current.user_id)
        await db.delete(current)
        await db.flush()
        if old and old.team_id == team.id:
            old.team_id = None
    db.add(TeamLeadAssignment(team_id=team.id, user_id=lead.id))
    lead.team_id = team.id
    audit(db, actor, "team.lead_assigned", "team", team.id, team.id, {"user_id": str(lead.id)})


def touch(user: User) -> None:
    user.last_activity_at = utcnow()
