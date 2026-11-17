"""Managers' AWS web search (POST /api/web-search/jobs).

Synchronous with a time limit (AWS_WEB_SEARCH_ENDPOINT_TIMEOUT_SECONDS): a request makes at most
AWS_WEB_SEARCH_ENDPOINT_MAX_CALLS queries (about 0.4 s each, measured) and then reads at most a dozen employer
feeds in parallel, which together finish well inside a minute; a background run would add a queue round trip and a
results table for what is a short lookup.

Flow: queries (keywords x US locations x work mode, ATS host groups, one unfiltered query without job boards) ->
result addresses, deduplicated -> generic pages dropped -> every posting checked against the employer's live feed
(board API) or, for an employer page, the page's own structured posting data -> US / work mode / eligibility
screen -> deduplication -> the engine's keyword ranking against the candidate's resume and profile.

Data use: AWS titles, text and ranking stay inside this function; the answer carries only what the employer's feed
says. Nothing is written except the monthly counter (a number) and an audit entry with counts."""
import asyncio
import logging

from fastapi import HTTPException

from ...config import Settings
from ...deduplicator import deduplicate
from ...job_normalizer import ENRICH_VERSION, ats_key, enrich_job, us_location_status
from ...job_ranker import best_title_relevance, heuristic_score, work_mode_rejects, evaluate_eligibility
from ...job_verifier import Budget
from ...providers import registry, workday
from ...providers.aws_web_search import (AWS, AwsWebSearch, AwsWebSearchError, employer_posting_address, host_groups,
                                         query_text)
from ...providers.base import Ctx, HostLimiter, RobotsCache
from ...providers.paid import aggregator_hosts
from ...providers.searxng import extract, site_hosts, valid_board
from ...resume_parser import heuristic_profile
from ...schemas import Candidate as Lead
from ...schemas import Job, Profile, SearchRequest
from ..schemas import AwsWebSearchStatus, WebSearchJobOut, WebSearchJobsIn, WebSearchJobsOut, WebSearchStats

log = logging.getLogger("jobfinder.web_search")
MIN_RELEVANCE = 0.6
MAX_BOARDS = 12
MAX_PAGES = 8
FEED_WAIT_SECONDS = 40.0
MODE = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site", "any": "All"}



async def status(settings: Settings, aws: AwsWebSearch | None) -> AwsWebSearchStatus:
    """For the integrations page. Reads the counter only; never calls AWS."""
    out = AwsWebSearchStatus(enabled=bool(settings.aws_web_search_enabled), region=settings.aws_web_search_region,
                             web_search_id=settings.aws_web_search_id, limit=settings.aws_web_search_monthly_limit,
                             reserve=settings.aws_web_search_monthly_reserve)
    quota = aws.quota if aws else None
    if quota is None:
        out.note = "No PostgreSQL counter: AWS web search cannot be used."
        return out
    try:
        usage = await quota.usage()
    except Exception as exc:                      # the table is missing (migration not run) or the database is down
        out.note = f"The monthly counter could not be read ({type(exc).__name__}); run the database migration."
        return out
    return out.model_copy(update={**usage, "counter_available": True})


def plan_queries(data: WebSearchJobsIn, settings: Settings) -> list[tuple[str, list[str] | None]]:
    """(query text, include hosts or None for the unfiltered query) in the order they are sent."""
    groups = host_groups(site_hosts(settings.searxng_sites))
    places = data.locations or ["United States"]
    mode = "" if data.work_mode == "any" else data.work_mode
    first = [(query_text(data.keywords, p, mode), groups[0] if groups else None) for p in places]
    general = [(query_text(data.keywords, places[0], mode), None)]
    more = [(query_text(data.keywords, p, mode), g) for g in groups[1:] for p in places]
    planned = list(dict.fromkeys((q, tuple(g) if g else None) for q, g in [*first, *general, *more]))
    return [(q, list(g) if g else None) for q, g in planned][:max(1, settings.aws_web_search_endpoint_max_calls)]


async def run(settings: Settings, aws: AwsWebSearch, http, verifier, data: WebSearchJobsIn,
              resume_text: str = "", profile_fields: dict | None = None) -> WebSearchJobsOut:
    stats = dict.fromkeys(("queries_used", "results_seen", "duplicates", "dropped_generic", "dropped_unverifiable",
                           "dropped_not_relevant", "dropped_filtered", "verified", "returned"), 0)
    warnings: list[str] = []
    hosts = site_hosts(settings.searxng_sites)
    aggregators = aggregator_hosts(settings.aggregator_hosts)

    # 1. Queries, at most three at a time. Results live in this frame only.
    sem = asyncio.Semaphore(3)
    errors: list[AwsWebSearchError] = []

    async def ask(query: str, include):
        async with sem:
            try:
                answer = await aws.search(query, include=include, exclude=None if include else list(aggregators), interactive=True)
            except AwsWebSearchError as err:
                stats["queries_used"] += getattr(err, "queries", 0)
                errors.append(err)
                return include, []
            stats["queries_used"] += answer.queries
            return include, [r.url for r in answer.results]

    answers = await asyncio.gather(*(ask(q, g) for q, g in plan_queries(data, settings)))
    if errors and not any(urls for _, urls in answers):
        err = errors[0]
        if err.kind == "budget":
            raise HTTPException(429, "This month's AWS web search allowance is used up.")
        if err.kind == "disabled":
            raise HTTPException(503, "AWS web search is switched off.")
        if err.kind == "throttled":
            raise HTTPException(429, "AWS web search is busy. Try again in a minute.", headers={"Retry-After": "60"})
        raise HTTPException(502, f"AWS web search did not answer: {err.message}.")
    for kind in sorted({e.kind for e in errors}):
        warnings.append({"budget": "The monthly AWS web search allowance ran out during this request; some queries were not sent.",
                         "throttled": "AWS web search throttled some queries; the results may be incomplete."}.get(
            kind, f"Some AWS web search queries failed ({kind})."))

    # 2. Addresses -> what to check. Posting ids per board, board roots, employer pages.
    seen: set[str] = set()
    wanted: dict[tuple[str, str], set[str]] = {}      # (ats, board) -> job ids ("" = the board root itself)
    workday_leads: list[tuple[str, str]] = []
    pages: list[str] = []
    for include, urls in answers:
        for url in urls:
            stats["results_seen"] += 1
            if url in seen:
                stats["duplicates"] += 1
                continue
            seen.add(url)
            hit = extract(url, hosts)
            if hit is None:
                if include is None and employer_posting_address(url, aggregators) and len(pages) < MAX_PAGES:
                    pages.append(url)
                else:
                    stats["dropped_generic"] += 1
                continue
            if hit.kind == "page":
                if len(pages) < MAX_PAGES:
                    pages.append(hit.url)
                else:
                    stats["dropped_unverifiable"] += 1
                continue
            if not (hit.board and valid_board(hit.src, hit.board)) or hit.src not in registry.SPECS:
                stats["dropped_generic"] += 1
                continue
            if hit.src == "workday":
                if hit.kind == "posting":
                    workday_leads.append((hit.board, hit.url))
                else:
                    stats["dropped_generic"] += 1          # a Workday site root: searched per employer by the engine
                continue
            wanted.setdefault((hit.src, hit.board), set()).add(hit.job_id if hit.kind == "posting" else "")
    del answers

    # 3. The employer's live feed decides.
    ctx = Ctx(http, HostLimiter(default=4), RobotsCache(), min(30.0, settings.http_timeout_seconds))
    boards = list(wanted.items())
    for _, ids in boards[MAX_BOARDS:]:
        stats["dropped_unverifiable"] += len(ids)
    boards = boards[:MAX_BOARDS]
    found: list[Job] = []

    async def read_board(src: str, board: str, ids: set[str]) -> None:
        try:
            jobs = await registry.SPECS[src].fetch(ctx, board, "")
        except Exception:                              # board missing, feed down: nothing from it can be verified
            stats["dropped_unverifiable"] += len(ids)
            return
        by_key = {ats_key(j.ats, j.ats_id): j for j in jobs if j.ats_id}
        for job_id in ids - {""}:
            job = by_key.get(ats_key(src, job_id))
            if job is None:
                stats["dropped_unverifiable"] += 1       # closed, stale or never on this board
            else:
                found.append(job)
        if "" in ids:                                  # a board root: read the board, keep the matching postings
            found.extend(j for j in jobs if best_title_relevance(j.title, [data.keywords], []) >= MIN_RELEVANCE)

    async def read_workday(board: str, url: str) -> None:
        try:
            job = await workday.lead(ctx, board, url)
        except Exception:
            job = None
        if job is None:
            stats["dropped_unverifiable"] += 1
        else:
            found.append(job)

    async def read_page(url: str) -> None:
        verdict = await verifier.verify(Lead(url=url, provider=AWS, cited=True), Budget(0))
        if verdict.job is None:
            stats["dropped_unverifiable"] += 1
        else:
            found.append(verdict.job)

    tasks = [asyncio.create_task(read_board(s, b, ids)) for (s, b), ids in boards]
    tasks += [asyncio.create_task(read_workday(b, u)) for b, u in workday_leads[:4]]
    stats["dropped_unverifiable"] += max(0, len(workday_leads) - 4)
    tasks += [asyncio.create_task(read_page(u)) for u in pages]
    if tasks:
        _, late = await asyncio.wait(tasks, timeout=FEED_WAIT_SECONDS)
        for task in late:
            task.cancel()
        if late:
            stats["dropped_unverifiable"] += len(late)
            warnings.append(f"{len(late)} employer feed(s) did not answer in time; their postings were not returned.")

    # 4. Screen, deduplicate, score.
    fields = profile_fields or {}
    req = SearchRequest(resume_id="aws-web-search", job_titles=data.keywords.replace(",", " ")[:300],
                        years_experience=min(40.0, float(fields.get("years_experience") or 0)),
                        authorization=fields.get("work_authorization") or "Other / Prefer Not to Say",
                        sponsorship_needed=fields.get("sponsorship_needed") or "Unknown",
                        exclude_citizenship_required=bool(fields.get("exclude_citizenship_required")),
                        result_count=data.limit, work_mode=MODE[data.work_mode], deep_search=False)
    kept: list[Job] = []
    for job in found:
        if job.enriched < ENRICH_VERSION:
            enrich_job(job)
        job.title_relevance = best_title_relevance(job.title, req.titles, [])
        if job.title_relevance < MIN_RELEVANCE:
            stats["dropped_not_relevant"] += 1
            continue
        if not us_location_status(job.location, job.country_hint, job.description)[0] or work_mode_rejects(job, req) \
                or evaluate_eligibility(job, req)[0]:
            stats["dropped_filtered"] += 1
            continue
        if "AWS web search" not in job.sources:
            job.sources.append("AWS web search")
        kept.append(job)
    unique, removed = deduplicate(kept)
    stats["duplicates"] += removed
    profile = heuristic_profile(resume_text) if resume_text else Profile()
    skills = [str(s) for s in fields.get("skills") or [] if s]
    if skills:
        profile.skills = list(dict.fromkeys([*profile.skills, *skills]))
    for job in unique:
        heuristic_score(job, profile, req, [])
    unique.sort(key=lambda j: (-j.score, -j.title_relevance))
    stats["verified"] = len(unique)
    chosen = unique[: data.limit]
    stats["returned"] = len(chosen)
    usage = await aws.quota.usage()
    out = [WebSearchJobOut(title=j.title, company=j.company, location=j.location, work_mode=j.work_mode, posted_at=j.posted_at,
                           employer_url=j.url, apply_url=j.apply_url or j.url, source=j.source, verification=j.verification,
                           verified_at=j.verified_at, score=j.score, score_source=j.score_source, reason=j.reason,
                           matching_skills=j.matching_skills, missing_skills=j.missing_skills) for j in chosen]
    return WebSearchJobsOut(jobs=out, warnings=warnings, stats=WebSearchStats(
        **stats, monthly_used=usage["used"], monthly_limit=usage["limit"], monthly_remaining=usage["remaining"]))
