"""Job assignments and application tracking.

Rules that matter:
- one assignment per (job, member); assigning again is a no-op, never a duplicate;
- opening a job link is logged as its own event and never counts as applying;
- "applied" means the member (or a manager, audited) confirmed the submission: applied_at is set;
- every change of state is written to application_events, so history survives corrections."""
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import Principal, assignment_scope, forbid
from ..models import (ASSIGNMENT_STATUSES, ApplicationEvent, AssignmentNote, JobAssignment, JobPosting, SearchResult,
                      TeamJobQueue, User, SearchRun)
from ..schemas import (AssignmentDetail, AssignmentOut, EventOut, JobAssignee, MatchExplanation, NoteOut, StatusUpdate)
from ..security import utcnow
from . import jobs as jobs_service
from .audit import audit, notify
from .common import PRE_APPLICATION, team_ref, teams_by_id, user_ref, users_by_id, viewer

A = JobAssignment
MEMBER_STATUSES = ASSIGNMENT_STATUSES          # members may record every stage of their own application


def match_from(explanation: dict | None) -> MatchExplanation:
    data = explanation or {}
    return MatchExplanation(**{k: data[k] for k in MatchExplanation.model_fields if k in data})


# ---------------------------------------------------------------- creating assignments
async def create_assignments(db: AsyncSession, principal: Principal, pairs: list[tuple[uuid.UUID, uuid.UUID]], *,
                             search_run_id: uuid.UUID | None = None, source: str = "manual",
                             queue_item_ids: list[uuid.UUID] | None = None, actor: User | None = None,
                             skip_scope_checks: bool = False) -> tuple[list[uuid.UUID], int]:
    """Create assignments for (job, member) pairs. Returns (new assignment ids, pairs that already existed).
    Scope is validated on the server: a Team Lead can only reach their own members and jobs they may see."""
    actor = actor or principal.user
    pairs = list(dict.fromkeys(pairs))
    if not pairs:
        return [], 0
    member_ids = {m for _, m in pairs}
    job_ids = {j for j, _ in pairs}
    members = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(member_ids)))).scalars()}
    if not skip_scope_checks:
        for member_id in member_ids:
            member = members.get(member_id)
            if member is None or member.role != "MEMBER":
                raise HTTPException(404 if principal.is_admin else 403, "Member not found" if principal.is_admin else "You do not have access to this resource")
            if principal.is_team_lead and (member.team_id != principal.user.team_id or not principal.user.team_id):
                raise forbid()
            if not member.is_active:
                raise HTTPException(409, f"{member.name} is deactivated and cannot receive jobs")
        if not principal.is_manager:
            raise forbid()
        visible = await jobs_service.visible_jobs(db, principal, job_ids)
        if visible != job_ids:
            raise HTTPException(404 if principal.is_admin else 403, "Job not found" if principal.is_admin else "You do not have access to one of these jobs")
    existing_jobs = set((await db.execute(select(JobPosting.id).where(JobPosting.id.in_(job_ids)))).scalars())
    if existing_jobs != job_ids:
        raise HTTPException(404, "Job not found")
    if search_run_id is not None and not skip_scope_checks:
        # The link to a search is stored and shown: it has to be a search the caller can open.
        run = await db.get(SearchRun, search_run_id)
        if run is None or (principal.is_team_lead and run.team_id != principal.user.team_id):
            raise HTTPException(404, "Search not found") if principal.is_admin else forbid()

    # Match details computed for this member by a search (this run first, otherwise the member's latest result).
    result_rows = (await db.execute(
        select(SearchResult).where(SearchResult.job_id.in_(job_ids), SearchResult.member_id.in_(member_ids))
        .order_by(SearchResult.created_at))).scalars()
    matches: dict[tuple[uuid.UUID, uuid.UUID], SearchResult] = {}
    for r in result_rows:
        key = (r.job_id, r.member_id)
        if key not in matches or r.search_run_id == search_run_id:
            matches[key] = r
    # A member's own match details travel with them, but an assignment is only linked to a search of the member's
    # current team (a search of the team they left belongs to that team).
    run_teams = dict((await db.execute(select(SearchRun.id, SearchRun.team_id).where(
        SearchRun.id.in_({m.search_run_id for m in matches.values()})))).all()) if matches else {}

    created: list[uuid.UUID] = []
    per_member: dict[uuid.UUID, int] = {}
    already = 0
    for job_id, member_id in pairs:
        member = members[member_id]
        match = matches.get((job_id, member_id))
        stmt = pg_insert(A).values(
            id=uuid.uuid4(), job_id=job_id, member_id=member_id, team_id=member.team_id, assigned_by=actor.id,
            search_run_id=search_run_id or (match.search_run_id if match and run_teams.get(match.search_run_id) == member.team_id else None),
            source=source,
            match_score=match.match_score if match else None, match_explanation=match.match_explanation if match else {},
            status="ASSIGNED", is_active=True,
        ).on_conflict_do_nothing(index_elements=[A.job_id, A.member_id]).returning(A.id)
        new_id = (await db.execute(stmt)).scalar_one_or_none()
        if new_id is None:
            # Already assigned. A previously withdrawn assignment is restored; progress is never overwritten.
            previous = (await db.execute(select(A).where(A.job_id == job_id, A.member_id == member_id))).scalar_one()
            if not previous.is_active:
                previous.is_active = True
                db.add(ApplicationEvent(assignment_id=previous.id, actor_id=actor.id, event_type="reassigned",
                                        old_status=previous.status, new_status=previous.status, meta={"source": source}))
                created.append(previous.id)
                per_member[member_id] = per_member.get(member_id, 0) + 1
            else:
                already += 1
            continue
        created.append(new_id)
        per_member[member_id] = per_member.get(member_id, 0) + 1
        db.add(ApplicationEvent(assignment_id=new_id, actor_id=actor.id, event_type="assigned", new_status="ASSIGNED",
                                meta={"source": source, "search_run_id": str(search_run_id) if search_run_id else None}))
    for member_id, count in per_member.items():
        notify(db, member_id, "jobs_assigned", {"count": count, "assigned_by": actor.name, "source": source,
                                                 "search_run_id": str(search_run_id) if search_run_id else None})
    if queue_item_ids:
        stmt = update(TeamJobQueue).where(TeamJobQueue.id.in_(queue_item_ids), TeamJobQueue.job_id.in_(job_ids)).values(status="assigned")
        if principal.is_team_lead:
            stmt = stmt.where(TeamJobQueue.team_id == principal.user.team_id)
        await db.execute(stmt)
    if created:
        teams = {members[m].team_id for m in per_member}
        audit(db, actor, "assignments.created", "assignment", None, next(iter(teams)) if len(teams) == 1 else None,
              {"created": len(created), "already_assigned": already, "source": source, "members": len(per_member),
               "search_run_id": str(search_run_id) if search_run_id else None})
    return created, already


# ---------------------------------------------------------------- reading
async def load_scoped(db: AsyncSession, principal: Principal, assignment_id: uuid.UUID) -> JobAssignment:
    """Fetch an assignment the caller may see, or refuse. Other people's assignments are indistinguishable from missing ones for members."""
    assignment = (await db.execute(select(A).where(A.id == assignment_id))).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(404, "Assignment not found") if principal.is_admin else forbid()
    allowed = principal.is_admin or (principal.is_team_lead and principal.user.team_id and assignment.team_id == principal.user.team_id) \
        or (principal.is_member and assignment.member_id == principal.user.id and assignment.is_active)
    if not allowed:
        if principal.is_member and assignment.member_id == principal.user.id:        # their own, taken back by a manager
            raise HTTPException(410, "This job is no longer assigned to you.")
        raise forbid()
    return assignment


async def assignments_out(db: AsyncSession, assignments: list[JobAssignment]) -> list[AssignmentOut]:
    if not assignments:
        return []
    ids = [a.id for a in assignments]
    postings = {p.id: p for p in (await db.execute(select(JobPosting).where(JobPosting.id.in_({a.job_id for a in assignments})))).scalars()}
    users = await users_by_id(db, {a.member_id for a in assignments} | {a.assigned_by for a in assignments})
    teams = await teams_by_id(db, {a.team_id for a in assignments})
    opens = {r[0]: (r[1], r[2]) for r in (await db.execute(
        select(ApplicationEvent.assignment_id, func.count(), func.max(ApplicationEvent.created_at))
        .where(ApplicationEvent.assignment_id.in_(ids), ApplicationEvent.event_type == "link_opened")
        .group_by(ApplicationEvent.assignment_id))).all()}
    seen_by = viewer.get()
    note_where = [AssignmentNote.assignment_id.in_(ids)]
    if seen_by is not None and seen_by[0] == "MEMBER":       # private notes are not counted for the member either
        note_where.append(AssignmentNote.is_private.is_(False))
    notes = dict((await db.execute(select(AssignmentNote.assignment_id, func.count()).where(*note_where)
                                   .group_by(AssignmentNote.assignment_id))).all())
    return [AssignmentOut(
        id=a.id, job=jobs_service.summary_out(postings[a.job_id]), member=user_ref(users.get(a.member_id)),
        team=team_ref(teams.get(a.team_id)), status=a.status, is_active=a.is_active, source=a.source, match_score=a.match_score,
        assigned_at=a.assigned_at, assigned_by=user_ref(users.get(a.assigned_by)), viewed_at=a.viewed_at, applied_at=a.applied_at,
        link_opens=opens.get(a.id, (0, None))[0], last_link_open_at=opens.get(a.id, (0, None))[1], note_count=notes.get(a.id, 0),
    ) for a in assignments if a.job_id in postings and a.member_id in users]


def allowed_statuses(principal: Principal, assignment: JobAssignment) -> list[str]:
    return [s for s in ASSIGNMENT_STATUSES if s != assignment.status]


async def detail(db: AsyncSession, principal: Principal, assignment: JobAssignment) -> AssignmentDetail:
    base = (await assignments_out(db, [assignment]))[0]
    posting = await db.get(JobPosting, assignment.job_id)
    events = list((await db.execute(select(ApplicationEvent).where(ApplicationEvent.assignment_id == assignment.id)
                                    .order_by(ApplicationEvent.created_at.desc()).limit(200))).scalars())
    note_stmt = select(AssignmentNote).where(AssignmentNote.assignment_id == assignment.id).order_by(AssignmentNote.created_at.desc())
    if principal.is_member:
        note_stmt = note_stmt.where(AssignmentNote.is_private.is_(False))
    notes = list((await db.execute(note_stmt.limit(200))).scalars())
    if principal.is_member:                                  # what managers do among themselves is not the member's history
        events = [e for e in events if member_may_see(e.event_type, e.meta)]
    actors = await users_by_id(db, {e.actor_id for e in events} | {n.author_id for n in notes})
    others: list[JobAssignee] = []
    urls = None
    if principal.is_manager:
        where = [A.job_id == assignment.job_id, A.id != assignment.id, A.is_active, assignment_scope(principal)]
        rows = list((await db.execute(select(A).where(*where).order_by(A.assigned_at.desc()).limit(100))).scalars())
        members = await users_by_id(db, {r.member_id for r in rows})
        teams = await teams_by_id(db, {r.team_id for r in rows})
        others = [JobAssignee(assignment_id=r.id, member=user_ref(members[r.member_id]), team=team_ref(teams.get(r.team_id)),
                              status=r.status, assigned_at=r.assigned_at, applied_at=r.applied_at) for r in rows if r.member_id in members]
        urls = {"canonical_url": posting.canonical_url, "apply_url": posting.apply_url}
    data = base.model_dump()
    data["job"] = jobs_service.detail_out(posting, hide_links=principal.is_member)      # members open jobs through Open Job only
    return AssignmentDetail(
        **data, match=match_from(assignment.match_explanation), application_reference=assignment.application_reference,
        interviewed_at=assignment.interviewed_at, offer_at=assignment.offer_at, closed_at=assignment.closed_at,
        events=[EventOut(id=e.id, event_type=e.event_type, old_status=e.old_status, new_status=e.new_status,
                         actor=user_ref(actors.get(e.actor_id)), metadata=e.meta or {}, created_at=e.created_at) for e in events],
        notes=[NoteOut(id=n.id, note=n.note, is_private=n.is_private, author=user_ref(actors.get(n.author_id)), created_at=n.created_at)
               for n in notes],
        allowed_statuses=allowed_statuses(principal, assignment), other_assignees=others, job_urls=urls)


MANAGER_ONLY_EVENTS = ("link_opened_by_manager",)


def member_may_see(event_type: str, meta: dict | None) -> bool:
    return event_type not in MANAGER_ONLY_EVENTS and not (meta or {}).get("private")


# ---------------------------------------------------------------- tracking
async def mark_viewed(db: AsyncSession, principal: Principal, assignment: JobAssignment) -> None:
    """The member opened the job details inside the app."""
    if not principal.is_member or assignment.viewed_at is not None:
        return
    now = utcnow()
    old = assignment.status
    await db.flush()                                     # nothing pending on this row may be lost by the reload below
    # Two requests can arrive together (a page and its prefetch): only the one whose UPDATE finds the row
    # still unviewed records the event.
    first = (await db.execute(update(A).where(A.id == assignment.id, A.viewed_at.is_(None)).values(
        viewed_at=now, status=case((A.status == "ASSIGNED", "VIEWED"), else_=A.status)).returning(A.id))).scalar_one_or_none()
    await db.refresh(assignment)
    if first is None:
        return
    db.add(ApplicationEvent(assignment_id=assignment.id, actor_id=principal.user.id, event_type="viewed", old_status=old,
                            new_status=assignment.status, meta={}))
    principal.user.last_activity_at = now


async def record_link_open(db: AsyncSession, principal: Principal, assignment: JobAssignment) -> str:
    """Log that the job page was opened and return the stored destination. Never marks the job as applied."""
    posting = await db.get(JobPosting, assignment.job_id)
    target = (posting.apply_url or posting.canonical_url or "").strip()
    if not target:
        raise HTTPException(409, "This job has no link to open")
    now = utcnow()
    if principal.is_member:
        old = assignment.status
        if assignment.viewed_at is None:
            assignment.viewed_at = now
        if old == "ASSIGNED":
            assignment.status = "VIEWED"
        db.add(ApplicationEvent(assignment_id=assignment.id, actor_id=principal.user.id, event_type="link_opened",
                                old_status=old, new_status=assignment.status, meta={"target": "apply" if posting.apply_url else "posting"}))
        principal.user.last_activity_at = now
    else:
        db.add(ApplicationEvent(assignment_id=assignment.id, actor_id=principal.user.id, event_type="link_opened_by_manager",
                                old_status=assignment.status, new_status=assignment.status, meta={}))
    return target


async def set_status(db: AsyncSession, principal: Principal, assignment: JobAssignment, data: StatusUpdate) -> None:
    """Transactional status change with timestamps kept consistent and the previous state recorded."""
    if not assignment.is_active:
        raise HTTPException(409, "This assignment was withdrawn")
    now = utcnow()
    old, new = assignment.status, data.status
    meta: dict = {}
    if data.applied_at is not None:
        if data.applied_at > now + timedelta(minutes=5):
            raise HTTPException(422, "The application date cannot be in the future")
        if data.applied_at < assignment.assigned_at - timedelta(minutes=5):
            raise HTTPException(422, "The application date cannot be earlier than the day this job was assigned")
        if new in PRE_APPLICATION or new == "SKIPPED":
            raise HTTPException(422, "An application date only makes sense once the application was submitted")
    if new in ("APPLIED", "INTERVIEWING", "OFFER", "REJECTED"):
        if assignment.applied_at is None:
            assignment.applied_at = data.applied_at or now
            if new != "APPLIED" and data.applied_at is None:
                meta["applied_at_inferred"] = True          # stage recorded without an earlier "applied" entry
        elif data.applied_at is not None and data.applied_at != assignment.applied_at:
            meta["previous_applied_at"] = assignment.applied_at.isoformat()
            assignment.applied_at = data.applied_at
    if new in PRE_APPLICATION or new == "SKIPPED":
        if assignment.applied_at is not None:               # correction of a mistaken "applied"
            meta.update(correction=True, previous_applied_at=assignment.applied_at.isoformat())
            assignment.applied_at = assignment.interviewed_at = assignment.offer_at = None
    if new == "APPLIED" and old in ("INTERVIEWING", "OFFER"):
        meta["correction"] = True
        assignment.interviewed_at = assignment.offer_at = None
    if new == "INTERVIEWING":
        assignment.interviewed_at = assignment.interviewed_at or now
        if old == "OFFER":
            meta["correction"] = True
            assignment.offer_at = None
    if new == "OFFER":
        assignment.offer_at = assignment.offer_at or now
    assignment.closed_at = now if new in ("REJECTED", "WITHDRAWN", "SKIPPED") else None
    if new != "ASSIGNED" and assignment.viewed_at is None and principal.is_member:
        assignment.viewed_at = now
    if data.reference is not None:
        assignment.application_reference = data.reference.strip()
        meta["reference_recorded"] = bool(data.reference.strip())
    assignment.status = new
    if principal.user.id != assignment.member_id:
        meta["on_behalf_of_member"] = True
    if old != new or meta:
        db.add(ApplicationEvent(assignment_id=assignment.id, actor_id=principal.user.id, event_type="status_changed",
                                old_status=old, new_status=new, meta=meta))
        audit(db, principal.user, "assignment.status_changed", "assignment", assignment.id, assignment.team_id,
              {"old": old, "new": new, **{k: v for k, v in meta.items() if k in ("correction", "on_behalf_of_member")}})
    if data.note and data.note.strip():
        await add_note(db, principal, assignment, data.note, False)
    if principal.is_member:
        principal.user.last_activity_at = now


async def add_note(db: AsyncSession, principal: Principal, assignment: JobAssignment, note: str, is_private: bool) -> AssignmentNote:
    row = AssignmentNote(assignment_id=assignment.id, author_id=principal.user.id, note=note.strip(),
                         is_private=bool(is_private and principal.is_manager))
    db.add(row)
    db.add(ApplicationEvent(assignment_id=assignment.id, actor_id=principal.user.id, event_type="note_added",
                            old_status=assignment.status, new_status=assignment.status, meta={"private": row.is_private}))
    if principal.is_member:
        principal.user.last_activity_at = utcnow()
    await db.flush()
    return row


async def withdraw(db: AsyncSession, principal: Principal, assignment: JobAssignment) -> None:
    """Manager removes an assignment from a member's list. The row and its history stay."""
    if not assignment.is_active:
        return
    assignment.is_active = False
    db.add(ApplicationEvent(assignment_id=assignment.id, actor_id=principal.user.id, event_type="unassigned",
                            old_status=assignment.status, new_status=assignment.status, meta={}))
    audit(db, principal.user, "assignment.withdrawn", "assignment", assignment.id, assignment.team_id, {"status": assignment.status})
