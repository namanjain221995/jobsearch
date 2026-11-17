"""Executes one persisted search run in the background.

Each selected candidate is searched with the existing search engine (same sources, verification,
deduplication and ranking as before), with that candidate's profile and current resume. Results are stored as
global job postings plus per-candidate search results; in auto-assign mode qualifying jobs become assignments. Progress is written to the
run row about once a second, so any browser (or none) can follow it. Counts are the engine's real
counters; nothing is padded to reach a requested number."""
import asyncio
import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..job_normalizer import US_STATES, norm_company
from ..schemas import Job as EngineJob
from ..schemas import SearchRequest
from ..search_engine import STAGES, SearchEngine, SearchState
from .db import keep_analytics_cache
from .models import Candidate, Resume, SearchResult, SearchRun, SearchTarget, User
from .security import utcnow
from .services import assignments as assignments_service
from .services import jobs as jobs_service
from .services.audit import audit, notify
from .services.searches import entry_id, progress_entries

log = logging.getLogger("jobfinder.search")
MAX_ATTEMPTS = 2            # a run stopped by a worker shutdown, crash or timeout is resumed once
RESUMED_NOTE = "The background worker restarted during this search. It resumed automatically; candidates already finished were kept."
WAITING = "Waiting for the background worker"
LOST_ACCESS = "Stopped: the person who started this search no longer manages this team."
STAGE_LABELS = dict(STAGES) | {"done": "Finished", "": "Waiting"}
_STATE_NAMES = {name: abbr for abbr, name in US_STATES.items()}


@dataclass
class RunnerDeps:
    sessions: async_sessionmaker[AsyncSession]
    engine: SearchEngine
    settings: Settings


def still_manages(is_active: bool | None, role: str | None, user_team_id, run_team_id) -> bool:
    """Whether the person who started a run may still have jobs assigned in their name: an active Admin, or the
    active Team Lead of the run's team. Deactivation, removal from the team or a move to another team ends it."""
    if not is_active:
        return False
    return role == "ADMIN" or (role == "TEAM_LEAD" and run_team_id is not None and user_team_id == run_team_id)


def explanation_of(job: EngineJob) -> dict:
    return {"score_breakdown": job.score_breakdown, "score_source": job.score_source, "matched_query": job.matched_query,
            "matching_skills": job.matching_skills,
            "missing_skills": job.missing_skills, "reason": job.reason, "experience_comparison": job.experience_comparison,
            "authorization_status": job.authorization_status, "eligibility_note": job.eligibility_note}


DISCOVERY_KEYS = ("title_variants", "searx_queries", "searx_cached", "searx_results", "searx_failed", "new_boards",
                  "new_board_postings", "gained_by_source", "ai_leads", "ai_ranked", "company_model_scored",
                  "company_model_explained", "ai_calls", "rounds", "stop_reason", "serp_calls", "serp_cached", "serp_jobs",
                  "serp_listed", "serp_skipped", "tiny_searches", "tiny_fetches", "tiny_usd", "tiny_skipped")


def discovery_of(state: SearchState) -> dict:
    """What the wider-search features did for one candidate: real counters of the engine, nothing derived."""
    out = {k: state.stats[k] for k in DISCOVERY_KEYS if k in state.stats}
    if state.title_variants:
        out["variants"] = list(state.title_variants)
    if state.usage:
        out["usage"] = state.usage
    return out


def post_filter(jobs: list[EngineJob], filters: dict) -> list[EngineJob]:
    """Candidate preferences the engine itself does not know: excluded employers and preferred states."""
    excluded = {norm_company(c) for c in filters.get("excluded_companies") or [] if c}
    states = []
    for raw in filters.get("preferred_states") or []:
        s = str(raw).strip()
        abbr = s.upper() if s.upper() in US_STATES else _STATE_NAMES.get(s.lower())
        if abbr:
            states.append((abbr, US_STATES[abbr]))
    out = []
    for job in jobs:
        if excluded and norm_company(job.company) in excluded:
            continue
        if states and job.work_mode != "Remote":
            location = job.location
            if not any(re.search(rf"\b{abbr}\b", location) or name in location.lower() for abbr, name in states):
                continue
        out.append(job)
    return out


def synthetic_resume(filters: dict, candidate: Candidate) -> str:
    """Used only when a candidate has no resume: the skills and titles saved in their profile."""
    return (f"Desired roles: {', '.join(filters.get('job_titles') or [])}\n"
            f"Skills: {', '.join(filters.get('skills') or [])}\n"
            f"Experience: {filters.get('years_experience', 0):g} years\n")


async def execute_search(run_id: uuid.UUID, deps: RunnerDeps) -> None:
    async with deps.sessions() as db:
        run = (await db.execute(select(SearchRun).where(SearchRun.id == run_id).with_for_update())).scalar_one_or_none()
        if run is None or run.status != "queued":
            return                                         # cancelled before it started, or delivered twice
        if run.cancel_requested:
            run.status, run.completed_at = "cancelled", utcnow()
            await db.commit()
            return
        run.status, run.heartbeat_at, run.started_at = "running", utcnow(), run.started_at or utcnow()
        run.attempts = attempt = (run.attempts or 0) + 1
        run.completed_at, run.error_summary = None, ""
        targets = list((await db.execute(select(SearchTarget).where(SearchTarget.search_run_id == run_id))).scalars())
        candidate_ids = [t.candidate_id for t in targets]
        candidates = {c.id: c for c in (await db.execute(select(Candidate).where(Candidate.id.in_(candidate_ids)))).scalars()}
        current = {row[0]: (row[1], row[2]) for row in (await db.execute(
            select(Resume.candidate_id, Resume.id, Resume.text).where(Resume.candidate_id.in_(candidate_ids), Resume.is_current))).all()}
        resumes = {candidate_id: text for candidate_id, (_, text) in current.items()}
        for t in targets:
            if t.status != "completed":                    # the resume version this candidate is searched with now
                t.resume_id = current[t.candidate_id][0] if t.candidate_id in current else None
        initiator = await db.get(User, run.initiated_by) if run.initiated_by else None
        mode, target_count, deep, min_score, team_id = run.mode, run.target_count, run.deep_search, run.min_match_score, run.team_id
        await db.commit()

    tracker: dict[str, dict] = {}
    for t in targets:
        entry = {"candidate_id": str(t.candidate_id), "name": candidates[t.candidate_id].name if t.candidate_id in candidates else "",
                 "status": "queued", "stage": "", "message": "", "discovered": 0, "verified": 0, "duplicates": 0,
                 "matched": 0, "assigned": 0, "already_assigned": 0, "warnings": [], "round": 1, "discovery": {}}
        if t.status == "completed":                        # finished in an earlier attempt: results are saved, keep them
            entry.update({k: v for k, v in (t.stats or {}).items() if k in entry}, status="completed")
        tracker[str(t.candidate_id)] = entry
    states: dict[str, SearchState] = {}
    store_lock = asyncio.Lock()                            # one writer at a time for global job rows
    semaphore = asyncio.Semaphore(max(1, deps.settings.member_search_concurrency))

    async def persist(target: SearchTarget, jobs: list[EngineJob]) -> tuple[int, int]:
        async with store_lock, deps.sessions() as db:
            candidate = await db.get(Candidate, target.candidate_id)
            pairs = []
            for rank, job in enumerate(jobs, start=1):
                try:
                    posting = await jobs_service.upsert_engine_job(db, job, initiator.id if initiator else None)
                except ValueError:                           # no http(s) link: not a posting anyone could open
                    continue
                await db.execute(pg_insert(SearchResult).values(
                    id=uuid.uuid4(), search_run_id=run_id, candidate_id=target.candidate_id, job_id=posting.id, rank=rank,
                    match_score=job.score, match_explanation=explanation_of(job),
                ).on_conflict_do_update(constraint="uq_search_results_run_candidate_job",
                                        set_={"rank": rank, "match_score": job.score, "match_explanation": explanation_of(job)}))
                if job.score >= min_score:
                    pairs.append((posting.id, target.candidate_id))
            created, already = [], 0
            # Re-check the candidate right before assigning: they may have been deactivated or moved meanwhile.
            still_valid = candidate is not None and candidate.is_active and (team_id is None or candidate.team_id == team_id)
            starter = await db.get(User, initiator.id) if initiator is not None else None
            if starter is None or not still_manages(starter.is_active, starter.role, starter.team_id, team_id):
                still_valid = False                          # nothing is assigned in the name of someone who lost that right
                tracker[str(target.candidate_id)]["warnings"].append(LOST_ACCESS)
                pairs = []
            if mode == "auto_assign" and pairs and initiator is not None and still_valid:
                created, already = await assignments_service.create_assignments(
                    db, None, pairs, search_run_id=run_id, source="search", actor=initiator, skip_scope_checks=True)
            elif mode == "auto_assign" and pairs and not still_valid:
                tracker[str(target.candidate_id)]["warnings"].append("Candidate is no longer active in this team; nothing was assigned.")
            snapshot()
            entry = tracker[str(target.candidate_id)]
            done = {**{k: v for k, v in entry.items() if k not in ("candidate_id", "name")},
                    **finished(len(jobs), len(created), already)}
            await db.execute(update(SearchTarget).where(
                SearchTarget.search_run_id == run_id, SearchTarget.candidate_id == target.candidate_id,
            ).values(status="completed", stats=done))      # saved with the results, so a resumed run skips this candidate
            await db.commit()
            return len(created), already

    def finished(matched: int, assigned: int, already: int) -> dict:
        return {"status": "completed", "stage": "Finished", "matched": matched, "assigned": assigned, "already_assigned": already,
                "message": f"{matched} matching job(s) found" + (f", {assigned} assigned" if mode == "auto_assign" else "")}

    async def run_candidate(target: SearchTarget) -> None:
        mid = str(target.candidate_id)
        entry = tracker[mid]
        candidate = candidates.get(target.candidate_id)
        if candidate is None:
            entry.update(status="failed", message="Candidate no longer exists")
            return
        async with semaphore:
            entry["status"] = "running"
            filters = target.candidate_specific_filters or {}
            needs_buffer = bool(filters.get("excluded_companies") or filters.get("preferred_states"))
            want = min(deps.settings.result_cap, target_count * 2 if needs_buffer else target_count)
            try:
                request = SearchRequest(
                    resume_id="portal-candidate-search", job_titles=", ".join(filters.get("job_titles") or []),
                    years_experience=min(40.0, float(filters.get("years_experience") or 0)),
                    authorization=filters.get("work_authorization") or "Other / Prefer Not to Say",
                    sponsorship_needed=filters.get("sponsorship_needed") or "Unknown",
                    exclude_citizenship_required=bool(filters.get("exclude_citizenship_required")),
                    result_count=want, work_mode=filters.get("work_mode") or "All", deep_search=deep,
                    locations=[str(s)[:60] for s in (filters.get("preferred_states") or [])][:30])
                state = SearchState(f"{run_id}:{mid}", want)
                states[mid] = state
                await deps.engine.run(state, request, resumes.get(target.candidate_id) or synthetic_resume(filters, candidate))
                jobs = post_filter(state.results, filters)[:target_count]
                entry["stage"] = "Saving results"
                assigned, already = await persist(target, jobs)
                entry.update(finished(len(jobs), assigned, already))
            except asyncio.CancelledError:
                entry.update(status="cancelled", message="Cancelled")
                raise
            except Exception as exc:                        # one candidate failing must not sink the others
                log.error("Candidate search failed: %s", type(exc).__name__)
                entry.update(status="failed", message=f"Search failed ({type(exc).__name__})")

    def snapshot() -> tuple[dict, dict]:
        sources: dict = {}
        warnings: list[str] = [RESUMED_NOTE] if attempt > 1 else []
        for mid, state in states.items():
            entry = tracker[mid]
            entry.update(discovered=state.stats["total_discovered"], verified=state.stats["verified_active"],
                         duplicates=state.stats["rejected_duplicates"])
            entry["round"] = int(state.stats.get("round") or 1)
            entry["discovery"] = discovery_of(state)
            if entry["status"] == "running":
                label = STAGE_LABELS.get(state.stage, state.stage)
                entry["stage"] = f"Round {entry['round']}: {label}" if entry["round"] > 1 else label
            entry["warnings"] = list(dict.fromkeys([*entry["warnings"], *state.warnings]))[:8]
            if sum(v.get("boards_ok", 0) for v in state.sources.values()) >= sum(v.get("boards_ok", 0) for v in sources.values()):
                sources = {k: {x: y for x, y in v.items() if x != "errors"} for k, v in state.sources.items()}
            warnings.extend(state.warnings)
        entries = list(tracker.values())
        running = [e for e in entries if e["status"] == "running"]
        progress = {"stage": running[0]["stage"] if running else ("Finished" if all(e["status"] != "queued" for e in entries) else "Waiting"),
                    "candidates": entries, "sources": sources, "warnings": list(dict.fromkeys(warnings))[:10], "updated_at": utcnow().isoformat()}
        counts = {"discovered_count": max((e["discovered"] for e in entries), default=0),
                  "verified_count": max((e["verified"] for e in entries), default=0),
                  "duplicate_count": sum(e["duplicates"] for e in entries),
                  "matched_count": sum(e["matched"] for e in entries), "assigned_count": sum(e["assigned"] for e in entries)}
        return progress, counts

    tasks = [asyncio.create_task(run_candidate(t)) for t in targets if tracker[str(t.candidate_id)]["status"] != "completed"]
    cancelled = False                                      # asked for by a person
    lost_access = False                                    # the person who started the run may no longer act for this team
    interrupted = False                                    # the worker is stopping, or the job ran out of time

    async def monitor() -> None:
        nonlocal cancelled, lost_access
        last = None
        last_totals = (0, 0)
        while True:
            await asyncio.sleep(1.0)
            progress, counts = snapshot()
            comparable = ({k: v for k, v in progress.items() if k != "updated_at"}, counts)
            async with deps.sessions() as db:
                row = (await db.execute(select(SearchRun.cancel_requested, User.is_active, User.role, User.team_id)
                                        .outerjoin(User, User.id == SearchRun.initiated_by).where(SearchRun.id == run_id))).first()
                wants_cancel = bool(row and row[0])
                if row and initiator is not None and not still_manages(row[1], row[2], row[3], team_id):
                    wants_cancel = lost_access = True        # deactivated, removed from the team or moved: stop working for them
                values = {"heartbeat_at": utcnow()}
                if comparable != last:
                    values.update(progress=progress, **counts)
                    last = comparable
                await db.execute(update(SearchRun).where(SearchRun.id == run_id).values(**values))
                if (counts.get("matched_count"), counts.get("assigned_count")) == last_totals:
                    keep_analytics_cache(db)             # heartbeat / progress text only: no dashboard number moved
                last_totals = (counts.get("matched_count"), counts.get("assigned_count"))
                await db.commit()
            if wants_cancel:
                cancelled = True
                for task in tasks:
                    task.cancel()
                return

    monitor_task = asyncio.create_task(monitor())
    error = ""
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:                          # worker shutting down, or the job timed out
        interrupted = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        progress, counts = snapshot()
        resume = interrupted and not cancelled and attempt < MAX_ATTEMPTS
        for entry in tracker.values():
            if entry["status"] in ("completed", "failed") and not (resume and entry["status"] == "failed"):
                continue
            if resume:
                entry.update(status="queued", stage="", message=WAITING)
            elif interrupted and not cancelled:
                entry.update(status="failed", message="Interrupted: the background worker stopped")
            else:
                entry["status"] = "cancelled"
        statuses = [e["status"] for e in tracker.values()]
        if resume:
            status = "queued"
        elif cancelled:
            status, error = "cancelled", (LOST_ACCESS if lost_access else error)
        elif statuses and all(s == "failed" for s in statuses):
            status = "failed"
            error = "Interrupted: the background worker stopped during this search, twice." if interrupted else "Every candidate search failed."
        elif any(s != "completed" for s in statuses):
            status = "partial"
            error = "Interrupted: the background worker stopped; unfinished candidates were not searched." if interrupted else ""
        else:
            status = "completed"
        progress["stage"] = WAITING if resume else ("Cancelled" if cancelled else "Finished")
        async with deps.sessions() as db:
            await db.execute(update(SearchRun).where(SearchRun.id == run_id).values(
                status=status, completed_at=None if resume else utcnow(), heartbeat_at=utcnow(), progress=progress,
                error_summary=error, **counts))
            for entry in tracker.values():
                await db.execute(update(SearchTarget).where(
                    SearchTarget.search_run_id == run_id, SearchTarget.candidate_id == uuid.UUID(entry["candidate_id"])
                ).values(status=entry["status"], stats={k: v for k, v in entry.items() if k not in ("candidate_id", "name")}))
            if resume:
                audit(db, initiator, "search.interrupted", "search", run_id, team_id, {"attempt": attempt, "will_resume": True})
            else:
                if initiator is not None:
                    notify(db, initiator.id, "search_finished", {"search_run_id": str(run_id), "status": status,
                                                                  "matched": counts["matched_count"], "assigned": counts["assigned_count"],
                                                                  "candidates": len(tracker)})
                audit(db, initiator, "search.finished", "search", run_id, team_id, {"status": status, "attempt": attempt, **counts})
            await db.commit()


def _entries_marked(progress: dict | None, status: str, message: str, stage: str) -> dict:
    """The stored progress with every candidate that was still waiting or being searched set to `status`."""
    entries = [{"candidate_id": entry_id(m), **{k: v for k, v in m.items() if k != "member_id"}} for m in progress_entries(progress)]
    entries = [m if m.get("status") in ("completed", "failed") and not (status == "queued" and m.get("status") == "failed")
               else {**m, "status": status, "stage": "", "message": message} for m in entries]
    return {**{k: v for k, v in (progress or {}).items() if k != "members"}, "candidates": entries, "stage": stage}


async def recover_interrupted(sessions: async_sessionmaker[AsyncSession], stale_seconds: int = 120,
                              exclude: set[str] | frozenset[str] = frozenset()) -> tuple[list[uuid.UUID], int]:
    """Runs left 'running' by a worker that died (no heartbeat for `stale_seconds`). Each is put back in the queue
    once, where it resumes with the candidates that were not finished. A run that was already resumed is closed
    honestly instead of hanging forever. `exclude` holds run ids this worker is executing right now.
    Returns (runs put back in the queue, runs closed)."""
    from datetime import timedelta
    cutoff = utcnow() - timedelta(seconds=stale_seconds)
    resumed: list[uuid.UUID] = []
    closed = 0
    async with sessions() as db:
        runs = list((await db.execute(select(SearchRun).where(
            SearchRun.status == "running", (SearchRun.heartbeat_at.is_(None)) | (SearchRun.heartbeat_at < cutoff),
        ).with_for_update(skip_locked=True))).scalars())
        for run in runs:
            if str(run.id) in exclude:
                continue
            if not run.cancel_requested and (run.attempts or 0) < MAX_ATTEMPTS:
                run.status, run.progress = "queued", _entries_marked(run.progress, "queued", WAITING, stage=WAITING)
                await db.execute(update(SearchTarget).where(
                    SearchTarget.search_run_id == run.id, SearchTarget.status != "completed").values(status="queued"))
                audit(db, None, "search.interrupted", "search", run.id, run.team_id, {"attempt": run.attempts, "will_resume": True})
                resumed.append(run.id)
                continue
            done = (await db.execute(select(SearchTarget.candidate_id).where(
                SearchTarget.search_run_id == run.id, SearchTarget.status == "completed").limit(1))).first() is not None
            ended = "cancelled" if run.cancel_requested else "failed"
            await db.execute(update(SearchTarget).where(
                SearchTarget.search_run_id == run.id, SearchTarget.status.in_(("queued", "running", "cancelled"))).values(status=ended))
            run.status = "cancelled" if run.cancel_requested else ("partial" if done else "failed")
            run.completed_at = utcnow()
            run.progress = _entries_marked(run.progress, ended, "Cancelled" if run.cancel_requested else "Interrupted: the background worker stopped",
                                           stage="Cancelled" if run.cancel_requested else "Finished")
            if not run.cancel_requested:
                run.error_summary = "Interrupted: the background worker stopped during this search" + (", twice." if (run.attempts or 0) >= MAX_ATTEMPTS else ".")
            if run.initiated_by:
                notify(db, run.initiated_by, "search_finished", {"search_run_id": str(run.id), "status": run.status,
                                                                 "matched": run.matched_count, "assigned": run.assigned_count})
            audit(db, None, "search.finished", "search", run.id, run.team_id, {"status": run.status, "attempt": run.attempts})
            closed += 1
        await db.commit()
    return resumed, closed
