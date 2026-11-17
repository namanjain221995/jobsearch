"""Shared building blocks: references, pagination and the one definition of every assignment metric."""
import uuid
from contextvars import ContextVar

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ApplicationEvent, Candidate, JobAssignment, Team, User
from ..schemas import AssignmentStats, CandidateRef, TeamRef, UserRef

A = JobAssignment
PRE_APPLICATION = ("ASSIGNED", "VIEWED", "APPLYING")
CLOSED = ("REJECTED", "WITHDRAWN", "SKIPPED")


# Who the current request is answered for: (role, user id). Set once per request by deps.authenticated.
viewer: ContextVar[tuple[str, uuid.UUID] | None] = ContextVar("portal_viewer", default=None)


def user_ref(user: User | None) -> UserRef | None:
    """A person as shown next to an assignment, a note or an event. The e-mail address is the sign-in name, so a
    Member is only given their own; managers see the addresses of the people they manage or work with."""
    if user is None:
        return None
    seen_by = viewer.get()
    hidden = seen_by is not None and seen_by[0] == "MEMBER" and seen_by[1] != user.id
    return UserRef(id=user.id, name=user.name, email="" if hidden else user.email)


def candidate_ref(candidate: Candidate | None) -> CandidateRef | None:
    return CandidateRef(id=candidate.id, name=candidate.name) if candidate else None


def team_ref(team: Team | None) -> TeamRef | None:
    return TeamRef(id=team.id, name=team.name) if team else None


def stat_columns() -> list:
    """Aggregates over job_assignments. Every dashboard uses exactly these, so numbers always agree."""
    active = A.is_active
    return [
        func.count().filter(active).label("assigned"),
        func.count().filter(active, A.applied_at.is_not(None)).label("applied"),
        func.count().filter(active, A.applied_at.is_(None), A.status.not_in(("SKIPPED", "WITHDRAWN"))).label("not_applied"),
        func.count().filter(active, A.interviewed_at.is_not(None)).label("interviews"),
        func.count().filter(active, A.offer_at.is_not(None)).label("offers"),
        func.count().filter(active, A.status.in_(CLOSED)).label("closed"),
        func.count().filter(active, or_(A.viewed_at.is_not(None), A.status != "ASSIGNED")).label("viewed"),
        func.count().filter(active, or_(A.status == "SKIPPED", and_(A.status == "WITHDRAWN", A.applied_at.is_(None)))).label("excluded"),
    ]


def to_stats(row, link_opens: int = 0) -> AssignmentStats:
    if row is None:
        return AssignmentStats(link_opens=link_opens)
    eligible = (row.assigned or 0) - (row.excluded or 0)
    rate = round(100.0 * (row.applied or 0) / eligible, 1) if eligible > 0 else 0.0
    return AssignmentStats(assigned=row.assigned or 0, not_applied=row.not_applied or 0, applied=row.applied or 0,
                           interviews=row.interviews or 0, offers=row.offers or 0, closed=row.closed or 0,
                           viewed=row.viewed or 0, link_opens=link_opens, completion_rate=rate)


async def stats_by(db: AsyncSession, group_column, where: list) -> dict:
    """{group value: AssignmentStats} for assignments matching `where`, including link-open counts.
    Grouping by a column of `candidates` joins each assignment to its candidate."""
    by_candidate_column = group_column.table.name == Candidate.__tablename__
    stmt = select(group_column.label("g"), *stat_columns()).select_from(A)
    opens_stmt = (select(group_column, func.count()).select_from(ApplicationEvent)
                  .join(A, A.id == ApplicationEvent.assignment_id))
    if by_candidate_column:
        stmt = stmt.join(Candidate, Candidate.id == A.candidate_id)
        opens_stmt = opens_stmt.join(Candidate, Candidate.id == A.candidate_id)
    rows = (await db.execute(stmt.where(*where).group_by(group_column))).all()
    opens = dict((await db.execute(
        opens_stmt.where(ApplicationEvent.event_type == "link_opened", A.is_active, *where).group_by(group_column))).all())
    return {row.g: to_stats(row, opens.get(row.g, 0)) for row in rows}


async def stats_by_member(db: AsyncSession, where: list) -> dict:
    """{member id: AssignmentStats} per responsible member (the candidate's current member). Assignments of
    unassigned candidates are under the key None."""
    return await stats_by(db, Candidate.assigned_member_id, where)


async def stats_total(db: AsyncSession, where: list) -> AssignmentStats:
    row = (await db.execute(select(*stat_columns()).where(*where))).one()
    opens = (await db.execute(
        select(func.count()).select_from(ApplicationEvent).join(A, A.id == ApplicationEvent.assignment_id)
        .where(ApplicationEvent.event_type == "link_opened", A.is_active, *where))).scalar_one()
    return to_stats(row, opens)


async def paginate(db: AsyncSession, stmt: Select, page: int, page_size: int) -> tuple[list, int]:
    page, page_size = max(1, min(page, 1_000_000)), max(1, min(page_size, 200))      # bounded: the offset must stay a sane integer
    total = (await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    rows = (await db.execute(stmt.limit(page_size).offset((page - 1) * page_size))).all()
    return rows, total


async def users_by_id(db: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, User]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return {u.id: u for u in (await db.execute(select(User).where(User.id.in_(wanted)))).scalars()}


async def teams_by_id(db: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, Team]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return {t.id: t for t in (await db.execute(select(Team).where(Team.id.in_(wanted)))).scalars()}


async def candidates_by_id(db: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, Candidate]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return {c.id: c for c in (await db.execute(select(Candidate).where(Candidate.id.in_(wanted)))).scalars()}


async def candidates_with_members(db: AsyncSession, ids: set[uuid.UUID | None]) -> tuple[dict[uuid.UUID, Candidate], dict[uuid.UUID, User]]:
    """Candidates by id, and their responsible members by user id."""
    candidates = await candidates_by_id(db, ids)
    return candidates, await users_by_id(db, {c.assigned_member_id for c in candidates.values()})
