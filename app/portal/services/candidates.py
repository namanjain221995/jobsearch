"""Candidates: the people who need a job. A candidate is a record, never an account.

Rules that matter:
- a candidate belongs to one team and has at most one responsible member at a time, always an active MEMBER of
  that same team; a member who is deactivated or leaves the team loses their candidates (they become unassigned);
- jobs, searches and history hang on the candidate, so they follow it from one member to the next;
- only managers change candidates; the responsible member reads them (details, profile, resume)."""
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import Principal, forbid
from ..models import Candidate, CandidateProfile, JobAssignment, Resume, Team, User
from ..schemas import AssignmentStats, CandidateCreate, CandidateOut, CandidateProfileSummary, CandidateUpdate
from . import people
from .audit import audit, notify
from .common import stats_by, team_ref, teams_by_id, user_ref, users_by_id


async def candidates_out(db: AsyncSession, candidates: list[Candidate]) -> list[CandidateOut]:
    if not candidates:
        return []
    ids = [c.id for c in candidates]
    resumes = dict((await db.execute(select(Resume.candidate_id, Resume).where(Resume.candidate_id.in_(ids), Resume.is_current))).all())
    with_file = await people.has_file(db, [r.id for r in resumes.values()])
    profiles = {p.candidate_id: p for p in (await db.execute(select(CandidateProfile).where(CandidateProfile.candidate_id.in_(ids)))).scalars()}
    stats = await stats_by(db, JobAssignment.candidate_id, [JobAssignment.candidate_id.in_(ids)])
    newest = dict((await db.execute(select(JobAssignment.candidate_id, func.max(JobAssignment.assigned_at)).where(
        JobAssignment.candidate_id.in_(ids), JobAssignment.is_active).group_by(JobAssignment.candidate_id))).all())
    users = await users_by_id(db, {c.assigned_member_id for c in candidates} | {c.created_by for c in candidates})
    teams = await teams_by_id(db, {c.team_id for c in candidates})
    out = []
    for c in candidates:
        resume, profile = resumes.get(c.id), profiles.get(c.id)
        summary = CandidateProfileSummary(
            desired_titles=profile.desired_titles or [], preferred_states=profile.preferred_states or [],
            years_experience=float(profile.years_experience) if profile.years_experience is not None else None,
            work_authorization=profile.work_authorization) if profile else CandidateProfileSummary()
        out.append(CandidateOut(
            id=c.id, name=c.name, email=c.email, phone=c.phone, notes=c.notes, team=team_ref(teams.get(c.team_id)),
            assigned_member=user_ref(users.get(c.assigned_member_id)), is_active=c.is_active, has_resume=resume is not None,
            resume_has_file=resume is not None and resume.id in with_file and people.resume_extension(resume.filename) is not None,
            profile_completion=people.profile_completion(profile, resume is not None)[0], profile_summary=summary,
            last_assigned_at=newest.get(c.id),
            created_by=user_ref(users.get(c.created_by)), created_at=c.created_at, updated_at=c.updated_at,
            stats=stats.get(c.id) or AssignmentStats()))
    return out


async def member_for(db: AsyncSession, principal: Principal, candidate: Candidate, member_id: uuid.UUID) -> User:
    """The user a candidate may be handed to: an active MEMBER of the candidate's own team. Anyone else answers the
    same way, whether they exist or not, so the check tells nothing about people in other teams."""
    member = await db.get(User, member_id)
    if member is None or member.role != "MEMBER" or not candidate.team_id or member.team_id != candidate.team_id:
        raise HTTPException(422, "A candidate can only be assigned to a member of the candidate's own team")
    if not member.is_active:
        raise HTTPException(409, f"{member.name} is deactivated and cannot be responsible for candidates")
    return member


def _hand_over(db: AsyncSession, actor: User, candidate: Candidate, member: User | None, ip: str = "", reason: str = "") -> None:
    """Set (or clear) the responsible member, with the audit entry and the notification to the new member."""
    previous = candidate.assigned_member_id
    if previous == (member.id if member else None):
        return
    candidate.assigned_member_id = member.id if member else None
    meta = {"from_member": str(previous) if previous else None, "to_member": str(member.id) if member else None}
    if reason:
        meta["reason"] = reason
    audit(db, actor, "candidate.assigned" if member else "candidate.unassigned", "candidate", candidate.id, candidate.team_id, meta, ip)
    if member is not None:
        notify(db, member.id, "candidate_assigned", {"candidate_id": str(candidate.id), "candidate_name": candidate.name,
                                                     "assigned_by": actor.name})


async def create_candidate(db: AsyncSession, principal: Principal, data: CandidateCreate, ip: str) -> Candidate:
    if not principal.is_manager:
        raise forbid()
    team_id = data.team_id
    if principal.is_team_lead:
        if not principal.user.team_id or (team_id is not None and team_id != principal.user.team_id):
            raise forbid()                                  # a Team Lead adds candidates to the team they lead, nowhere else
        team_id = principal.user.team_id
    if team_id is None:
        raise HTTPException(422, "Choose the team this candidate belongs to")
    team = await db.get(Team, team_id)
    if team is None:
        raise HTTPException(404, "Team not found")
    if not team.is_active:
        raise HTTPException(409, "This team is deactivated")
    candidate = Candidate(team_id=team.id, name=data.name.strip(), email=(data.email or "").strip().lower(), phone=data.phone,
                          notes=data.notes.strip(), is_active=True, created_by=principal.user.id)
    db.add(candidate)
    await db.flush()
    await people.get_profile(db, candidate.id)
    audit(db, principal.user, "candidate.created", "candidate", candidate.id, team.id, {}, ip)
    if data.assigned_member_id is not None:
        _hand_over(db, principal.user, candidate, await member_for(db, principal, candidate, data.assigned_member_id), ip)
    return candidate


async def update_candidate(db: AsyncSession, principal: Principal, candidate: Candidate, data: CandidateUpdate, ip: str) -> None:
    if not principal.is_manager:
        raise forbid()
    sent = data.model_fields_set
    changed = []
    if data.name is not None and data.name.strip() != candidate.name:
        candidate.name = data.name.strip()
        changed.append("name")
    if "email" in sent and (data.email or "").strip().lower() != candidate.email:
        candidate.email = (data.email or "").strip().lower()
        changed.append("email")
    if data.phone is not None and data.phone != candidate.phone:
        candidate.phone = data.phone
        changed.append("phone")
    if data.notes is not None and data.notes.strip() != candidate.notes:
        candidate.notes = data.notes.strip()
        changed.append("notes")
    if changed:
        audit(db, principal.user, "candidate.updated", "candidate", candidate.id, candidate.team_id, {"fields": changed}, ip)
    if data.team_id is not None and data.team_id != candidate.team_id:
        if not principal.is_admin:
            raise forbid()                                  # only an Admin moves a candidate between teams
        team = await db.get(Team, data.team_id)
        if team is None:
            raise HTTPException(404, "Team not found")
        if not team.is_active:
            raise HTTPException(400, "The destination team is deactivated")
        old_team = candidate.team_id
        if "assigned_member_id" not in sent or data.assigned_member_id is None:
            _hand_over(db, principal.user, candidate, None, ip, "candidate_moved_to_another_team")
        candidate.team_id = team.id
        # The candidate's jobs and their history move along, so the new team's lead manages the whole pipeline.
        await db.execute(update(JobAssignment).where(JobAssignment.candidate_id == candidate.id).values(team_id=team.id))
        audit(db, principal.user, "candidate.transferred", "candidate", candidate.id, team.id,
              {"from_team": str(old_team) if old_team else None}, ip)
    if "assigned_member_id" in sent:
        member = await member_for(db, principal, candidate, data.assigned_member_id) if data.assigned_member_id is not None else None
        _hand_over(db, principal.user, candidate, member, ip)
    if data.is_active is not None and data.is_active != candidate.is_active:
        candidate.is_active = data.is_active                # logical: jobs, searches and history stay
        audit(db, principal.user, "candidate.activated" if data.is_active else "candidate.deactivated", "candidate",
              candidate.id, candidate.team_id, {}, ip)


async def release_candidates(db: AsyncSession, actor: User | None, member: User, reason: str, ip: str = "") -> int:
    """A member who is deactivated or leaves the team stops being responsible for anyone: their candidates become
    unassigned (one audit entry each) and the Team Lead is told, so somebody else can take them over."""
    candidates = list((await db.execute(select(Candidate).where(Candidate.assigned_member_id == member.id).order_by(Candidate.name))).scalars())
    for candidate in candidates:
        candidate.assigned_member_id = None
        audit(db, actor, "candidate.unassigned", "candidate", candidate.id, candidate.team_id,
              {"from_member": str(member.id), "to_member": None, "reason": reason}, ip)
    by_team: dict[uuid.UUID, int] = {}
    for candidate in candidates:
        if candidate.team_id:
            by_team[candidate.team_id] = by_team.get(candidate.team_id, 0) + 1
    for team_id, count in by_team.items():
        lead = await people.team_lead_of(db, team_id)
        if lead is not None and (actor is None or lead.id != actor.id):
            notify(db, lead.id, "candidates_unassigned", {"count": count, "member_name": member.name, "reason": reason,
                                                          "team_id": str(team_id)})
    return len(candidates)


async def counts(db: AsyncSession, where: list) -> tuple[int, int]:
    """(active candidates, active candidates without a responsible member) matching `where`."""
    row = (await db.execute(select(func.count(), func.count().filter(Candidate.assigned_member_id.is_(None)))
                            .where(Candidate.is_active, *where))).one()
    return row[0], row[1]
