"""Team searches: validating a request, creating the persistent run, and reading it back.
The search itself runs in the background worker (see portal/search_runner.py)."""
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings
from ..deps import Principal, assignment_scope, forbid
from ..models import Candidate, CandidateProfile, JobAssignment, JobPosting, Resume, SearchResult, SearchRun, SearchTarget
from ..schemas import (SearchCreate, SearchResultOut, SearchRunOut, SearchSkipped, SearchTargetOut, SettingsOut)
from . import jobs as jobs_service
from .assignments import match_from
from .audit import audit
from .common import candidate_ref, candidates_by_id, team_ref, teams_by_id, user_ref, users_by_id

ACTIVE = ("queued", "running")


def effective_filters(profile: CandidateProfile | None, data: SearchCreate) -> dict:
    """What will be searched for one candidate: the candidate's saved preferences, overridden by whatever the
    person launching the search set explicitly."""
    f = data.filters
    def pick(override, saved, default):
        return override if override is not None else (saved if saved not in (None, "", []) else default)

    work_mode = pick(f.work_mode, profile.work_mode if profile else None, "All")
    if f.remote_only:
        work_mode = "Remote"
    return {
        "job_titles": [t.strip() for t in pick(f.job_titles, profile.desired_titles if profile else None, []) if t.strip()][:8],
        "years_experience": float(pick(f.years_experience, float(profile.years_experience) if profile and profile.years_experience is not None else None, 0)),
        "work_authorization": pick(f.work_authorization, profile.work_authorization if profile else None, "Other / Prefer Not to Say"),
        "sponsorship_needed": pick(f.sponsorship_needed, profile.sponsorship_needed if profile else None, "Unknown"),
        "exclude_citizenship_required": bool(pick(f.exclude_citizenship_required, profile.exclude_citizenship_required if profile else None, False)),
        "work_mode": work_mode,
        "preferred_states": pick(f.preferred_states, profile.preferred_states if profile else None, []),
        "excluded_companies": pick(f.excluded_companies, profile.excluded_companies if profile else None, []),
        "skills": (profile.skills if profile else []) or [],
    }


async def create_search(db: AsyncSession, settings: Settings, portal: SettingsOut, principal: Principal,
                        data: SearchCreate, ip: str) -> tuple[SearchRun, list[SearchSkipped]]:
    if not principal.is_manager:
        raise forbid()
    wanted: list[uuid.UUID] = list(dict.fromkeys(data.candidate_ids))
    if data.team_id:
        if principal.is_team_lead and data.team_id != principal.user.team_id:
            raise forbid()
        team_candidates = (await db.execute(select(Candidate.id).where(
            Candidate.team_id == data.team_id, Candidate.is_active).order_by(Candidate.name))).scalars()
        wanted = list(dict.fromkeys([*wanted, *team_candidates]))
    if not wanted:
        raise HTTPException(422, "Select at least one candidate")
    # Searches cost provider calls and worker time: one person cannot fill the queue, and the queue has an end.
    mine, everyone = (await db.execute(select(
        func.count().filter(SearchRun.initiated_by == principal.user.id), func.count()).where(SearchRun.status.in_(ACTIVE)))).one()
    if mine >= settings.max_active_searches_per_user:
        raise HTTPException(429, f"You already have {mine} searches queued or running. Wait for one to finish or cancel it.")
    if everyone >= settings.max_active_searches:
        raise HTTPException(429, "The search queue is full. Try again when a running search has finished.")
    candidates = await candidates_by_id(db, set(wanted))
    for candidate_id in wanted:
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise HTTPException(404, "Candidate not found") if principal.is_admin else forbid()
        if principal.is_team_lead and (not principal.user.team_id or candidate.team_id != principal.user.team_id):
            raise forbid()                              # never trust client-supplied ids: other teams are off limits
        if not candidate.is_active:
            raise HTTPException(409, f"{candidate.name} is deactivated")
    if len(wanted) > portal.max_members_per_search:
        raise HTTPException(422, f"At most {portal.max_members_per_search} candidates can be searched in one run")
    if len(wanted) * data.target_count > portal.max_results_per_search_run:
        raise HTTPException(422, f"{len(wanted)} candidates x {data.target_count} jobs exceeds the limit of "
                                 f"{portal.max_results_per_search_run} results per run. Lower the number of jobs or candidates.")

    profiles = {p.candidate_id: p for p in (await db.execute(select(CandidateProfile).where(CandidateProfile.candidate_id.in_(wanted)))).scalars()}
    with_resume = set((await db.execute(select(Resume.candidate_id).where(Resume.candidate_id.in_(wanted), Resume.is_current))).scalars())
    targets: list[tuple[Candidate, dict]] = []
    skipped: list[SearchSkipped] = []
    for candidate_id in wanted:
        candidate = candidates[candidate_id]
        filters = effective_filters(profiles.get(candidate_id), data)
        if not filters["job_titles"]:
            skipped.append(SearchSkipped(candidate=candidate_ref(candidate), reason="No desired job title saved; add one to the profile or set titles for this search"))
        elif candidate_id not in with_resume and not filters["skills"]:
            skipped.append(SearchSkipped(candidate=candidate_ref(candidate), reason="No resume uploaded and no skills in the profile"))
        else:
            targets.append((candidate, filters))
    if not targets:
        raise HTTPException(422, "None of the selected candidates can be searched: " + "; ".join(f"{s.candidate.name}: {s.reason}" for s in skipped))

    team_ids = {c.team_id for c, _ in targets}
    run = SearchRun(
        initiated_by=principal.user.id, team_id=next(iter(team_ids)) if len(team_ids) == 1 else None, mode=data.mode,
        search_filters=data.filters.model_dump(exclude_none=True), target_count=data.target_count, deep_search=data.deep_search,
        min_match_score=data.min_match_score if data.min_match_score is not None else portal.default_min_match_score,
        status="queued", progress={})
    db.add(run)
    await db.flush()
    for candidate, filters in targets:
        db.add(SearchTarget(search_run_id=run.id, candidate_id=candidate.id, candidate_specific_filters=filters,
                            assignment_mode=data.mode, status="queued", stats={}))
    audit(db, principal.user, "search.created", "search", run.id, run.team_id,
          {"candidates": len(targets), "target_count": data.target_count, "mode": data.mode, "deep_search": data.deep_search}, ip)
    return run, skipped


def progress_entries(progress: dict | None) -> list[dict]:
    """The per-candidate entries of a run's live progress. Runs written before candidates existed call the list
    "members" and the id "member_id"; the ids are the same (each such member became the candidate with that id)."""
    progress = progress or {}
    return progress.get("candidates") or progress.get("members") or []


def entry_id(entry: dict) -> str | None:
    return entry.get("candidate_id") or entry.get("member_id")


def can_see_run(principal: Principal, run: SearchRun) -> bool:
    """Admin: every run. Team Lead: runs of the team they lead today. Having started a run gives no lasting
    access: a lead who was removed from a team, or moved to another one, leaves that team's searches behind."""
    if principal.is_admin:
        return True
    return principal.is_team_lead and principal.user.team_id is not None and run.team_id == principal.user.team_id


async def load_run(db: AsyncSession, principal: Principal, run_id: uuid.UUID) -> SearchRun:
    run = await db.get(SearchRun, run_id)
    if run is None:
        raise HTTPException(404, "Search not found") if principal.is_admin else forbid()
    if not can_see_run(principal, run):
        raise forbid()
    return run


async def runs_out(db: AsyncSession, runs: list[SearchRun], with_targets: bool = True) -> list[SearchRunOut]:
    if not runs:
        return []
    run_ids = [r.id for r in runs]
    targets = list((await db.execute(select(SearchTarget).where(SearchTarget.search_run_id.in_(run_ids)))).scalars())
    users = await users_by_id(db, {r.initiated_by for r in runs})
    candidates = await candidates_by_id(db, {t.candidate_id for t in targets})
    teams = await teams_by_id(db, {r.team_id for r in runs})
    by_run: dict[uuid.UUID, list[SearchTarget]] = {}
    for t in targets:
        by_run.setdefault(t.search_run_id, []).append(t)
    out = []
    for run in runs:
        progress = run.progress or {}
        live = {entry_id(m): m for m in progress_entries(progress)}
        target_out = []
        for t in sorted(by_run.get(run.id, []), key=lambda t: candidates[t.candidate_id].name if t.candidate_id in candidates else ""):
            if t.candidate_id not in candidates:
                continue
            m = {**(t.stats or {}), **live.get(str(t.candidate_id), {})}
            target_out.append(SearchTargetOut(
                candidate=candidate_ref(candidates[t.candidate_id]), resume_id=t.resume_id, status=m.get("status") or t.status,
                stage=m.get("stage", ""),
                message=m.get("message", ""), discovered=m.get("discovered", 0), verified=m.get("verified", 0),
                duplicates=m.get("duplicates", 0), matched=m.get("matched", 0), assigned=m.get("assigned", 0),
                filters={k: v for k, v in (t.candidate_specific_filters or {}).items() if k != "skills"},
                warnings=m.get("warnings", [])))
        out.append(SearchRunOut(
            id=run.id, mode=run.mode, status=run.status, cancel_requested=run.cancel_requested, attempts=run.attempts or 0, target_count=run.target_count,
            deep_search=run.deep_search, min_match_score=run.min_match_score, filters=run.search_filters or {},
            initiated_by=user_ref(users.get(run.initiated_by)), team=team_ref(teams.get(run.team_id)),
            candidate_count=len(by_run.get(run.id, [])), discovered_count=run.discovered_count, verified_count=run.verified_count,
            duplicate_count=run.duplicate_count, matched_count=run.matched_count, assigned_count=run.assigned_count,
            stage=progress.get("stage", ""), sources=progress.get("sources", {}), warnings=progress.get("warnings", []),
            error_summary=run.error_summary, created_at=run.created_at, started_at=run.started_at, completed_at=run.completed_at,
            targets=target_out if with_targets else []))
    return out


async def results_out(db: AsyncSession, principal: Principal, rows: list[SearchResult]) -> list[SearchResultOut]:
    if not rows:
        return []
    postings = {p.id: p for p in (await db.execute(select(JobPosting).where(JobPosting.id.in_({r.job_id for r in rows})))).scalars()}
    candidates = await candidates_by_id(db, {r.candidate_id for r in rows})
    # Assignment state and counters only as far as the caller may see assignments: a candidate who moved to another
    # team took their assignments along, and the old team's results say nothing about them any more.
    visible = assignment_scope(principal)
    existing = {(a.job_id, a.candidate_id): a for a in (await db.execute(select(JobAssignment).where(
        JobAssignment.job_id.in_(list(postings)), JobAssignment.candidate_id.in_(list(candidates)), JobAssignment.is_active, visible))).scalars()}
    items = {i.id: i for i in await jobs_service.list_items(db, list(postings.values()), [visible])}
    out = []
    for r in rows:
        if r.job_id not in items or r.candidate_id not in candidates:
            continue
        assignment = existing.get((r.job_id, r.candidate_id))
        out.append(SearchResultOut(id=r.id, candidate=candidate_ref(candidates[r.candidate_id]), job=items[r.job_id], rank=r.rank,
                                   match_score=r.match_score, match=match_from(r.match_explanation),
                                   assignment_id=assignment.id if assignment else None,
                                   assignment_status=assignment.status if assignment else None))
    return out
