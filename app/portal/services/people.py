"""Users, teams, candidate profiles and resumes."""
import hashlib
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ...resume_parser import ALLOWED_EXTENSIONS, ResumeError, extract_text_safely, heuristic_profile
from ..models import (AuditLog, Candidate, CandidateProfile, Invitation, JobAssignment, PasswordReset, Resume, Team, TeamJobQueue,
                      TeamLeadAssignment, User)
from ..schemas import ProfileIn, ProfileOut, ResumeOut, TeamOut, UserOut
from ..security import utcnow
from .audit import audit
from .common import stats_by, stats_by_member, team_ref, user_ref, users_by_id


# ---------------------------------------------------------------- profiles
def profile_completion(profile: CandidateProfile | None, has_resume: bool) -> tuple[int, list[str]]:
    checks = [
        ("Upload a resume", has_resume),
        ("Add at least one desired job title", bool(profile and profile.desired_titles)),
        ("Enter years of experience", bool(profile and profile.years_experience is not None)),
        ("List the candidate's skills", bool(profile and profile.skills)),
        ("Choose a preferred US location or remote work", bool(profile and (profile.preferred_states or profile.preferred_cities
                                                                              or profile.work_mode == "Remote"))),
    ]
    missing = [label for label, done in checks if not done]
    return round(100 * (len(checks) - len(missing)) / len(checks)), missing


async def get_profile(db: AsyncSession, candidate_id: uuid.UUID) -> CandidateProfile:
    profile = (await db.execute(select(CandidateProfile).where(CandidateProfile.candidate_id == candidate_id))).scalar_one_or_none()
    if profile is None:
        profile = CandidateProfile(candidate_id=candidate_id, desired_titles=[], skills=[], preferred_states=[], preferred_cities=[],
                                excluded_companies=[], required_keywords=[], extra={})
        db.add(profile)
        await db.flush()
    return profile


def profile_out(profile: CandidateProfile, has_resume: bool) -> ProfileOut:
    completion, missing = profile_completion(profile, has_resume)
    return ProfileOut(
        candidate_id=profile.candidate_id, desired_titles=profile.desired_titles or [],
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
    profiles = (await db.execute(select(CandidateProfile).where(CandidateProfile.extra != {}))).scalars().all()
    for profile in profiles:
        kept = fitting_extra(profile.extra, definitions)
        if kept != profile.extra:
            dropped += len(set(profile.extra) - set(kept))
            profile.extra = kept
    return dropped


async def update_profile(db: AsyncSession, profile: CandidateProfile, data: ProfileIn, definitions: list | None = None) -> None:
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
# What a stored resume file is served as. Fixed per extension: the type the uploader's browser claimed is never used.
DOWNLOAD_TYPES = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8",
                  "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9 ._()-]+")


def resume_extension(filename: str) -> str | None:
    """The extension of a stored resume when it is one of the kinds the upload accepts, else None."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext if ext in DOWNLOAD_TYPES and ext in ALLOWED_EXTENSIONS else None


def download_name(filename: str, ext: str) -> str:
    """A file name that is safe inside a Content-Disposition header and on any file system: ASCII letters, digits,
    spaces and . _ ( ) - only, no path, never empty, and always ending in the checked extension."""
    stem = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem = _UNSAFE_NAME.sub("_", stem).strip(" ._")[:80]
    return f"{stem or 'resume'}.{ext}"


async def current_resume(db: AsyncSession, candidate_id: uuid.UUID) -> Resume | None:
    return (await db.execute(select(Resume).where(Resume.candidate_id == candidate_id, Resume.is_current))).scalar_one_or_none()


async def has_file(db: AsyncSession, resume_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Of these resumes, those whose uploaded file is stored (asked of the database: the bytes are not loaded)."""
    if not resume_ids:
        return set()
    return set((await db.execute(select(Resume.id).where(Resume.id.in_(resume_ids), Resume.file_data.is_not(None)))).scalars())


async def resume_out(db: AsyncSession, resume: Resume, include_text: bool) -> ResumeOut:
    uploader = (await users_by_id(db, {resume.uploaded_by})).get(resume.uploaded_by)
    return ResumeOut(id=resume.id, candidate_id=resume.candidate_id, filename=resume.filename, size_bytes=resume.size_bytes,
                     has_file=bool(await has_file(db, [resume.id])) and resume_extension(resume.filename) is not None,
                     version=resume.version, truncated=resume.truncated, created_at=resume.created_at,
                     uploaded_by=user_ref(uploader), parsed_profile=resume.parsed_profile or {},
                     text=resume.text if include_text else None)


async def save_resume(db: AsyncSession, settings: Settings, candidate: Candidate, uploader: User, filename: str, data: bytes,
                      content_type: str, ip: str = "") -> Resume:
    """Extract the text, store text + structured profile + the uploaded file, and make it the candidate's current
    resume. Text and file of the replaced version are erased (its metadata stays as history)."""
    try:
        text, truncated = await extract_text_safely(filename, data, settings.max_upload_bytes)      # limited child process
    except ResumeError as exc:
        raise HTTPException(413 if "too large" in str(exc).lower() else 400, str(exc)) from None
    parsed = heuristic_profile(text)
    previous = await current_resume(db, candidate.id)
    version = 1
    if previous:
        version = previous.version + 1
        previous.is_current = False
        previous.text = ""
        previous.file_data = None
        await db.flush()
    resume = Resume(candidate_id=candidate.id, filename=filename[:200], content_type=(content_type or "")[:100], size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(), text=text, file_data=data, truncated=truncated, version=version,
                    is_current=True, uploaded_by=uploader.id,
                    parsed_profile={"skills": parsed.skills, "job_titles": parsed.job_titles, "education": parsed.education})
    db.add(resume)
    profile = await get_profile(db, candidate.id)
    if not profile.skills and parsed.skills:
        profile.skills = parsed.skills[:60]           # a starting point the Team Lead can correct
    audit(db, uploader, "resume.uploaded", "candidate", candidate.id, candidate.team_id, {"version": version, "size_bytes": len(data)}, ip)
    return resume


async def delete_resume(db: AsyncSession, actor: User, candidate: Candidate, resume: Resume, ip: str = "") -> None:
    await db.delete(resume)
    audit(db, actor, "resume.deleted", "candidate", candidate.id, candidate.team_id, {"version": resume.version}, ip)


# ---------------------------------------------------------------- users
async def users_out(db: AsyncSession, users: list[User]) -> list[UserOut]:
    """A member's numbers are the totals over the candidates assigned to them today. Members have no profile or
    resume of their own: those belong to candidates."""
    member_ids = [u.id for u in users if u.role == "MEMBER"]
    stats, counts = {}, {}
    if member_ids:
        stats = await stats_by_member(db, [Candidate.assigned_member_id.in_(member_ids)])
        counts = dict((await db.execute(select(Candidate.assigned_member_id, func.count()).where(
            Candidate.assigned_member_id.in_(member_ids)).group_by(Candidate.assigned_member_id))).all())
    from ..schemas import AssignmentStats
    out = []
    by_admin = await deactivated_by_admin(db, [u.id for u in users if not u.is_active])
    for u in users:
        is_member = u.role == "MEMBER"
        out.append(UserOut(id=u.id, name=u.name, email=u.email, role=u.role, team=team_ref(u.team), status=u.status,
                           is_active=u.is_active, deactivated_by_admin=u.id in by_admin, candidate_count=counts.get(u.id, 0),
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
    """Move a member (a recruiter) to another team. Candidates belong to their team and stay in it with all their
    jobs and history: the ones this member was responsible for become unassigned, for the Team Lead to hand on."""
    if member.role != "MEMBER":
        raise HTTPException(400, "Only members can be transferred between teams")
    if not team.is_active:
        raise HTTPException(400, "The destination team is deactivated")
    old_team_id = member.team_id
    if old_team_id == team.id:
        return
    from . import candidates as candidates_service        # imported here: candidates builds on this module
    await candidates_service.release_candidates(db, actor, member, "member_moved_to_another_team")
    member.team_id = team.id
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
    candidates = {row[0]: (row[1], row[2]) for row in (await db.execute(select(
        Candidate.team_id, func.count(), func.count().filter(Candidate.assigned_member_id.is_(None))).where(
        Candidate.team_id.in_(ids), Candidate.is_active).group_by(Candidate.team_id))).all()}
    queue = dict((await db.execute(select(TeamJobQueue.team_id, func.count()).where(
        TeamJobQueue.team_id.in_(ids), TeamJobQueue.status == "pending").group_by(TeamJobQueue.team_id))).all())
    stats = await stats_by(db, JobAssignment.team_id, [JobAssignment.team_id.in_(ids)])
    from ..schemas import AssignmentStats
    return [TeamOut(id=t.id, name=t.name, description=t.description, is_active=t.is_active, created_at=t.created_at,
                    team_lead=user_ref(leads.get(t.id)), team_lead_status=leads[t.id].status if t.id in leads else None,
                    member_count=members.get(t.id, 0), candidate_count=candidates.get(t.id, (0, 0))[0],
                    unassigned_candidates=candidates.get(t.id, (0, 0))[1], queue_pending=queue.get(t.id, 0),
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
