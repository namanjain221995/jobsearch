"""Dashboards. Every number comes from SQL aggregates over real rows; nothing is estimated.

Date filter semantics (the same for cards, tables and charts): the range selects assignments by the day they
were assigned. Time series show, per day or week inside the range, how many jobs were assigned, how many
applications were confirmed and how many job links were opened. Days are UTC."""
import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import own_candidates
from ..models import ApplicationEvent, Candidate, JobAssignment, JobPosting, Resume, SearchRun, Team, TeamJobQueue, User
from ..schemas import (AdminAnalytics, AssignmentStats, CandidateAnalytics, CandidateRow, MemberAnalytics, MemberRow, NameValue,
                       SearchUsagePoint, SeriesPoint, TeamAnalytics, TeamRow)
from .common import candidate_ref, stats_by, stats_by_member, stats_total, team_ref, teams_by_id, user_ref, users_by_id
from . import candidates as candidates_service
from . import people

A = JobAssignment


def range_where(start: date | None, end: date | None) -> list:
    where = []
    if start:
        where.append(A.assigned_at >= datetime.combine(start, time.min, tzinfo=timezone.utc))
    if end:
        where.append(A.assigned_at < datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc))
    return where


def job_filter(company: str | None, title: str | None) -> list:
    where = []
    if company:
        where.append(A.job_id.in_(select(JobPosting.id).where(JobPosting.company.ilike(f"%{company.strip()}%"))))
    if title:
        where.append(A.job_id.in_(select(JobPosting.id).where(JobPosting.title.ilike(f"%{title.strip()}%"))))
    return where


def funnel(stats: AssignmentStats) -> list[NameValue]:
    return [NameValue(name="Assigned", value=stats.assigned), NameValue(name="Viewed", value=stats.viewed),
            NameValue(name="Applied", value=stats.applied), NameValue(name="Interview", value=stats.interviews),
            NameValue(name="Offer", value=stats.offers)]


async def status_distribution(db: AsyncSession, where: list) -> list[NameValue]:
    rows = (await db.execute(select(A.status, func.count()).where(A.is_active, *where).group_by(A.status))).all()
    order = ["ASSIGNED", "VIEWED", "APPLYING", "APPLIED", "INTERVIEWING", "OFFER", "REJECTED", "WITHDRAWN", "SKIPPED"]
    counts = dict(rows)
    return [NameValue(name=s, value=counts[s]) for s in order if counts.get(s)]


async def _bucketed(db: AsyncSession, unit: str, where: list, since: datetime) -> dict[date, dict[str, int]]:
    out: dict[date, dict[str, int]] = {}

    def add(rows, field):
        for bucket, count in rows:
            out.setdefault(bucket.date() if isinstance(bucket, datetime) else bucket, {})[field] = count

    assigned_bucket = func.date_trunc(unit, func.timezone("UTC", A.assigned_at))
    add((await db.execute(select(assigned_bucket, func.count()).where(A.is_active, A.assigned_at >= since, *where).group_by(assigned_bucket))).all(), "assigned")
    applied_bucket = func.date_trunc(unit, func.timezone("UTC", A.applied_at))
    add((await db.execute(select(applied_bucket, func.count()).where(A.is_active, A.applied_at >= since, *where).group_by(applied_bucket))).all(), "applied")
    interview_bucket = func.date_trunc(unit, func.timezone("UTC", A.interviewed_at))
    add((await db.execute(select(interview_bucket, func.count()).where(A.is_active, A.interviewed_at >= since, *where).group_by(interview_bucket))).all(), "interviews")
    offer_bucket = func.date_trunc(unit, func.timezone("UTC", A.offer_at))
    add((await db.execute(select(offer_bucket, func.count()).where(A.is_active, A.offer_at >= since, *where).group_by(offer_bucket))).all(), "offers")
    event_bucket = func.date_trunc(unit, func.timezone("UTC", ApplicationEvent.created_at))
    add((await db.execute(
        select(event_bucket, func.count()).select_from(ApplicationEvent).join(A, A.id == ApplicationEvent.assignment_id)
        .where(ApplicationEvent.event_type == "link_opened", ApplicationEvent.created_at >= since, A.is_active, *where)
        .group_by(event_bucket))).all(), "link_opens")
    return out


async def daily_series(db: AsyncSession, where: list, start: date | None, end: date | None, default_days: int = 30) -> list[SeriesPoint]:
    last = min(end, date.today()) if end else datetime.now(timezone.utc).date()
    first = start or (last - timedelta(days=default_days - 1))
    if (last - first).days > 366:
        first = last - timedelta(days=366)
    data = await _bucketed(db, "day", where, datetime.combine(first, time.min, tzinfo=timezone.utc))
    points, day = [], first
    while day <= last:
        points.append(SeriesPoint(label=day.isoformat(), **data.get(day, {})))
        day += timedelta(days=1)
    return points


async def weekly_series(db: AsyncSession, where: list, weeks: int = 12) -> list[SeriesPoint]:
    today = datetime.now(timezone.utc).date()
    this_week = today - timedelta(days=today.weekday())
    first = this_week - timedelta(weeks=weeks - 1)
    data = await _bucketed(db, "week", where, datetime.combine(first, time.min, tzinfo=timezone.utc))
    return [SeriesPoint(label=(first + timedelta(weeks=i)).isoformat(), **data.get(first + timedelta(weeks=i), {})) for i in range(weeks)]


def pending_vs_applied(stats: AssignmentStats) -> list[NameValue]:
    return [NameValue(name="Not applied", value=stats.not_applied), NameValue(name="Applied", value=stats.applied)]


def range_info(start: date | None, end: date | None) -> dict:
    return {"from": start.isoformat() if start else None, "to": end.isoformat() if end else None,
            "basis": "assignment date (UTC)"}


# ---------------------------------------------------------------- member / candidate
async def _pipeline(db: AsyncSession, scope: list, start: date | None, end: date | None) -> dict:
    """The numbers and charts of one pipeline (a candidate's, or all of a member's candidates)."""
    where = scope + range_where(start, end)
    stats = await stats_total(db, where)
    rows = (await db.execute(select(
        func.count().filter(A.status == "INTERVIEWING"), func.count().filter(A.status == "OFFER"),
        func.count().filter(A.status == "REJECTED", A.interviewed_at.is_not(None)),
        func.count().filter(A.status == "REJECTED", A.interviewed_at.is_(None)),
    ).where(A.is_active, *where))).one()
    outcomes = [NameValue(name="Interviewing now", value=rows[0]), NameValue(name="Offer", value=rows[1]),
                NameValue(name="Rejected after interview", value=rows[2]), NameValue(name="Rejected before interview", value=rows[3])]
    return dict(
        range=range_info(start, end), stats=stats, funnel=funnel(stats),
        status_distribution=await status_distribution(db, where), applications_over_time=await daily_series(db, scope, start, end),
        weekly_activity=await weekly_series(db, scope), pending_vs_applied=pending_vs_applied(stats),
        interview_outcomes=[o for o in outcomes if o.value])


async def candidate_rows(db: AsyncSession, candidates: list[Candidate], extra_where: list) -> list[CandidateRow]:
    ids = [c.id for c in candidates]
    if not ids:
        return []
    stats = await stats_by(db, A.candidate_id, [A.candidate_id.in_(ids), *extra_where])
    with_resume = set((await db.execute(select(Resume.candidate_id).where(Resume.candidate_id.in_(ids), Resume.is_current))).scalars())
    members = await users_by_id(db, {c.assigned_member_id for c in candidates})
    teams = await teams_by_id(db, {c.team_id for c in candidates})
    return [CandidateRow(candidate=candidate_ref(c), member=user_ref(members.get(c.assigned_member_id)), team=team_ref(teams.get(c.team_id)),
                         is_active=c.is_active, has_resume=c.id in with_resume, stats=stats.get(c.id) or AssignmentStats())
            for c in candidates]


async def member_analytics(db: AsyncSession, member: User, start: date | None, end: date | None) -> MemberAnalytics:
    """A member's dashboard: totals over the candidates this member is responsible for today."""
    scope = [A.candidate_id.in_(own_candidates(member.id))]
    candidates = list((await db.execute(select(Candidate).where(Candidate.assigned_member_id == member.id).order_by(Candidate.name))).scalars())
    return MemberAnalytics(member=user_ref(member), team=team_ref(member.team), **await _pipeline(db, scope, start, end),
                           candidates=await candidate_rows(db, candidates, range_where(start, end)))


async def candidate_analytics(db: AsyncSession, candidate: Candidate, start: date | None, end: date | None) -> CandidateAnalytics:
    member = await db.get(User, candidate.assigned_member_id) if candidate.assigned_member_id else None
    team = await db.get(Team, candidate.team_id) if candidate.team_id else None
    return CandidateAnalytics(candidate=candidate_ref(candidate), member=user_ref(member), team=team_ref(team),
                              **await _pipeline(db, [A.candidate_id == candidate.id], start, end))


# ---------------------------------------------------------------- team
async def member_rows(db: AsyncSession, members: list[User], extra_where: list) -> list[MemberRow]:
    """Per responsible member: the assignments of the candidates each member holds today."""
    ids = [m.id for m in members]
    if not ids:
        return []
    outs = await people.users_out(db, members)
    stats = await stats_by_member(db, [Candidate.assigned_member_id.in_(ids), *extra_where])
    return [MemberRow(member=user_ref(m), team=team_ref(m.team), status=o.status, candidates=o.candidate_count,
                      last_activity_at=m.last_activity_at, stats=stats.get(m.id) or AssignmentStats())
            for m, o in zip(members, outs)]


async def team_analytics(db: AsyncSession, team: Team, start: date | None, end: date | None) -> TeamAnalytics:
    scope = [A.team_id == team.id]
    where = scope + range_where(start, end)
    stats = await stats_total(db, where)
    members = list((await db.execute(select(User).where(User.team_id == team.id, User.role == "MEMBER").order_by(User.name))).scalars())
    candidates = list((await db.execute(select(Candidate).where(Candidate.team_id == team.id).order_by(Candidate.name))).scalars())
    active = (await db.execute(select(func.count()).select_from(SearchRun).where(
        SearchRun.team_id == team.id, SearchRun.status.in_(("queued", "running"))))).scalar_one()
    queue = (await db.execute(select(func.count()).select_from(TeamJobQueue).where(
        TeamJobQueue.team_id == team.id, TeamJobQueue.status == "pending"))).scalar_one()
    return TeamAnalytics(
        team=team_ref(team), team_lead=user_ref(await people.team_lead_of(db, team.id)), range=range_info(start, end),
        member_count=sum(1 for m in members if m.is_active),
        candidate_count=sum(1 for c in candidates if c.is_active),
        unassigned_candidates=sum(1 for c in candidates if c.is_active and c.assigned_member_id is None),
        active_searches=active, queue_pending=queue, stats=stats,
        funnel=funnel(stats), status_distribution=await status_distribution(db, where),
        applications_over_time=await daily_series(db, scope, start, end), weekly_activity=await weekly_series(db, scope),
        pending_vs_applied=pending_vs_applied(stats), members=await member_rows(db, members, where),
        candidates=await candidate_rows(db, candidates, where))


# ---------------------------------------------------------------- admin
async def admin_analytics(db: AsyncSession, start: date | None, end: date | None, team_id: uuid.UUID | None,
                          member_id: uuid.UUID | None, status: str | None, company: str | None, title: str | None,
                          candidate_id: uuid.UUID | None = None) -> AdminAnalytics:
    scope: list = []
    if team_id:
        scope.append(A.team_id == team_id)
    if member_id:                                            # the responsible member: the candidates they hold today
        scope.append(A.candidate_id.in_(own_candidates(member_id)))
    if candidate_id:
        scope.append(A.candidate_id == candidate_id)
    if status:
        scope.append(A.status == status)
    scope += job_filter(company, title)
    where = scope + range_where(start, end)
    stats = await stats_total(db, where)

    teams = list((await db.execute(select(Team).order_by(Team.name))).scalars())
    if team_id:
        teams = [t for t in teams if t.id == team_id]
    team_stats = await stats_by(db, A.team_id, where) if teams else {}
    outs = {t.id: t for t in await people.teams_out(db, teams)}
    team_rows = [TeamRow(team=team_ref(t), team_lead=outs[t.id].team_lead, is_active=t.is_active, members=outs[t.id].member_count,
                         candidates=outs[t.id].candidate_count, stats=team_stats.get(t.id) or AssignmentStats()) for t in teams]

    role_counts = dict((await db.execute(select(User.role, func.count()).where(User.is_active).group_by(User.role))).all())
    indexed = (await db.execute(text("SELECT count(*) FROM index_postings"))).scalar_one()
    fresh = (await db.execute(text(
        "SELECT coalesce(sum(count), 0) FROM index_units WHERE ok AND expires_at > extract(epoch from now())"))).scalar_one()
    candidate_total, candidate_unassigned = await candidates_service.counts(db, [Candidate.team_id == team_id] if team_id else [])
    totals = {
        "teams": sum(1 for t in outs.values() if t.is_active) if not team_id else len(teams),
        "team_leads": role_counts.get("TEAM_LEAD", 0), "members": role_counts.get("MEMBER", 0),
        "candidates": candidate_total, "unassigned_candidates": candidate_unassigned,
        "indexed_jobs": int(indexed), "indexed_jobs_fresh": int(fresh),
        "job_postings": (await db.execute(select(func.count()).select_from(JobPosting))).scalar_one(),
        "active_searches": (await db.execute(select(func.count()).select_from(SearchRun).where(
            SearchRun.status.in_(("queued", "running"))))).scalar_one(),
    }

    since = datetime.now(timezone.utc) - timedelta(days=29)
    day = func.date_trunc("day", func.timezone("UTC", SearchRun.created_at))
    usage_rows = {r[0].date(): r for r in (await db.execute(
        select(day, func.count(), func.coalesce(func.sum(SearchRun.matched_count), 0), func.coalesce(func.sum(SearchRun.assigned_count), 0))
        .where(SearchRun.created_at >= since, *( [SearchRun.team_id == team_id] if team_id else [])).group_by(day))).all()}
    usage = []
    for i in range(30):
        d = since.date() + timedelta(days=i)
        r = usage_rows.get(d)
        usage.append(SearchUsagePoint(label=d.isoformat(), runs=r[1] if r else 0, matched=int(r[2]) if r else 0, assigned=int(r[3]) if r else 0))

    per_member = {m: s for m, s in (await stats_by_member(db, where)).items() if m is not None}     # unassigned candidates have no member
    per_candidate = await stats_by(db, A.candidate_id, where)
    bands = [("0-20%", 0, 20), ("20-40%", 20, 40), ("40-60%", 40, 60), ("60-80%", 60, 80), ("80-100%", 80, 100.01)]

    def banded(groups: dict) -> list[NameValue]:
        return [NameValue(name=label, value=sum(1 for s in groups.values() if s.assigned and low <= s.completion_rate < high))
                for label, low, high in bands]

    distribution, candidate_distribution = banded(per_member), banded(per_candidate)
    return AdminAnalytics(
        range=range_info(start, end), totals=totals, stats=stats, funnel=funnel(stats),
        status_distribution=await status_distribution(db, where), applications_over_time=await daily_series(db, scope, start, end),
        weekly_activity=await weekly_series(db, scope), teams=team_rows, search_usage=usage, member_distribution=distribution,
        candidate_distribution=candidate_distribution)
