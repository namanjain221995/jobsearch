"""Search pipeline.

Sources are read into a local SQLite index (one "unit" per employer board, search query or feed).
A search then works on the index, which is what makes it fast:

  1. show preliminary matches from the index straight away (when it already holds data);
  2. refresh stale units in parallel, bounded by a time limit; slow ones keep loading in the background;
  3. in parallel: AI resume analysis and AI web search for extra leads;
  4. verify leads (source API -> plain fetch -> headless browser -> AI extraction, cheapest first);
  5. screen (title, US location, work mode, authorization), deduplicate, score, AI-rank, audit.

Every stage has a budget. Nothing is shown that was not read from a live source."""
import asyncio
import json
import math
import time
import zlib
from datetime import datetime, timezone
from typing import Callable

import httpx

from . import job_verifier
from .ai_orchestrator import AIOrchestrator
from .browser_pool import BrowserPool
from .config import Settings
from .database import Database, pack
from .job_index import as_index
from .deduplicator import deduplicate
from .job_normalizer import (ENRICH_VERSION, ats_key, authorization_snippets, canonical_url, enrich_job, norm_company,
                             parse_ats, us_location_status)
from .job_ranker import (apply_ai_ranking, best_title_relevance, evaluate_eligibility, heuristic_score, index_tokens,
                         query_tokens, static_related_titles, work_mode_rejects)
from .job_verifier import Budget, PageVerifier, job_from_extraction
from .providers import registry, workday
from .providers.base import BoardNotFound, Ctx, HostLimiter, RobotsCache, SourceError, now_iso
from .providers.boards import seed_pairs
from .providers.registry import BOARD_LEAD_SOURCES, SPECS, Unit
from .resume_parser import heuristic_profile
from .schemas import Candidate, Job, Profile, SearchRequest

MIN_RELEVANCE = 0.6
AI_LEAD_RELEVANCE_FLOOR = 0.6
HOST_LIMITS = {"greenhouse": 10, "ashby": 8, "lever": 6, "workday": 1, "workable": 4, "amazon": 2,
               "remote-boards": 2, "adzuna": 2}
# Workday answers HTTP 429 when all its employers together get more than a slow trickle of requests.
# One request every 1.5 s; after any 429 the source is left alone for ten minutes (providers/base.py).
HOST_INTERVALS = {"workday": 1.5}
PACED_WAIT_SECONDS, DETAIL_WAIT_SECONDS = 25.0, 20.0            # deep search
FAST_PACED_WAIT_SECONDS, FAST_DETAIL_WAIT_SECONDS = 2.0, 6.0   # fast search
REJECT_KEYS = ("rejected_not_relevant", "rejected_non_us", "rejected_work_mode", "rejected_eligibility")

STAGES = [
    ("analyze", "Analyzing resume"), ("titles", "Identifying relevant job titles"),
    ("search", "Searching USA job openings"), ("boards", "Checking employer job boards"),
    ("verify", "Verifying application links"), ("dedupe", "Removing duplicate listings"),
    ("rank", "Ranking resume matches"), ("prepare", "Preparing results"),
]


def new_stats(requested: int) -> dict:
    return {"requested": requested, "total_discovered": 0, "unique_candidates": 0, "verified_active": 0,
            "rejected_duplicates": 0, "rejected_eligibility": 0, "rejected_non_us": 0, "rejected_not_relevant": 0,
            "rejected_work_mode": 0, "rejected_unverified": 0, "final_matches": 0,
            "boards_checked": 0, "boards_failed": 0, "sources_pending": 0, "ai_leads": 0, "ai_ranked": 0,
            "browser_pages": 0, "crawled_links": 0, "ai_extracted": 0, "detail_fetches": 0}


class SearchState:
    def __init__(self, search_id: str, requested: int):
        self.id = search_id
        self.status = "queued"          # queued / running / completed / cancelled / failed
        self.stage = ""
        self.stats = new_stats(requested)
        self.events: list[dict] = []
        self.results: list[Job] = []
        self.partial = False            # True while results are preliminary (keyword-scored, search still running)
        self.results_version = 0
        self.warnings: list[str] = []
        self.providers: dict = {}
        self.sources: dict[str, dict] = {}
        self.related_titles: list[str] = []
        self.timings: dict[str, float] = {}
        self.detail_wait = DETAIL_WAIT_SECONDS
        self.message = ""
        self.created = time.time()
        self.task: asyncio.Task | None = None

    def emit(self, stage: str, message: str) -> None:
        self.stage = stage
        self.events.append({"seq": len(self.events), "stage": stage, "message": message, "status": self.status,
                            "stats": dict(self.stats), "time": now_iso(), "partial": self.partial,
                            "results_version": self.results_version})

    def mark(self, name: str) -> None:
        self.timings[name] = round(time.time() - self.created, 1)

    def publish(self, jobs: list[Job], partial: bool) -> None:
        self.results = jobs
        self.partial = partial
        self.results_version += 1

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def summary(self) -> dict:
        return {"search_id": self.id, "status": self.status, "stage": self.stage, "message": self.message,
                "stats": self.stats, "warnings": self.warnings, "providers": self.providers,
                "sources": self.sources, "related_titles": self.related_titles, "partial": self.partial,
                "results_version": self.results_version, "timings": self.timings}


def _signature(job: Job) -> str:
    """Changes when the posting's own content changes; used to keep per-posting work across refreshes."""
    text = "\x1f".join((job.title, job.location, job.url, job.raw_html or job.description))
    return f"{zlib.crc32(text.encode()):08x}{len(text):x}v{ENRICH_VERSION}"     # a new analysis version re-reads the source text


def _rows(jobs: list[Job], existing: dict[str, tuple[str, bytes]]) -> list[tuple[str, str, str, str, bytes]]:
    """Index rows for a freshly read unit. An unchanged posting keeps its stored record, which may already
    hold its fetched description and extracted fields."""
    rows = []
    for job in jobs:
        akey = ats_key(job.ats, job.ats_id) or canonical_url(job.url) or job.url
        sig = _signature(job)
        old = existing.get(akey)
        rows.append((akey, job.title, index_tokens(job.title), sig, old[1] if old and old[0] == sig else pack(job)))
    return rows


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _guard(url: str) -> None:
    await job_verifier.assert_public_url(url)      # looked up at call time so tests can replace it


class SearchEngine:
    def __init__(self, settings: Settings, db: Database, client: httpx.AsyncClient | None = None,
                 seeds: list[tuple[str, str]] | None = None,
                 ai_factory: Callable[[], AIOrchestrator] | None = None,
                 browser: BrowserPool | None = None, sources: list[str] | None = None):
        self.settings = settings
        self.db = db                            # the object passed in (a SQLite Database or an async index)
        self.index = as_index(db)               # always the async interface
        self.client = client or self._default_client()
        self.seeds = seed_pairs() if seeds is None else seeds
        self.ai_factory = ai_factory or (lambda: AIOrchestrator.from_settings(settings))
        specs = registry.enabled_specs(settings)
        self.specs = specs if sources is None else {n: s for n, s in specs.items() if n in sources}
        self.limiter = HostLimiter(default=4, limits=HOST_LIMITS, intervals=HOST_INTERVALS)
        self.ctx = Ctx(self.client, self.limiter, RobotsCache(), settings.http_timeout_seconds, options={
            "workday_pages": settings.workday_pages, "adzuna_app_id": settings.adzuna_app_id,
            "adzuna_app_key": settings.adzuna_app_key})
        self.browser = browser if browser is not None else BrowserPool(
            settings.browser_concurrency, enabled=settings.browser_enabled, guard=_guard)
        self.verifier = PageVerifier(self.client, browser=self.browser)
        self._inflight: dict[Unit, asyncio.Task] = {}
        self._background: set[asyncio.Task] = set()
        # Workday: which facet means "United States" per employer, remembered so it is discovered only once.
        self._facets_saved: set[str] = set()
        self._facets_loaded = False

    @staticmethod
    def _default_client() -> httpx.AsyncClient:
        limits = httpx.Limits(max_connections=80, max_keepalive_connections=40)
        try:
            return httpx.AsyncClient(http2=True, limits=limits, follow_redirects=False)     # multiplexed requests per host
        except ImportError:
            return httpx.AsyncClient(limits=limits, follow_redirects=False)

    async def aclose(self) -> None:
        tasks = [*self._inflight.values(), *self._background]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.browser.aclose()
        await self.client.aclose()

    # ------------------------------------------------------------ index refresh
    async def _load_facets(self) -> None:
        if self._facets_loaded:
            return
        self._facets_loaded = True
        for key, value in (await self.index.meta_prefix("wd_us_facet:")).items():
            try:
                workday._us_facet.setdefault(key, json.loads(value))
                self._facets_saved.add(key)
            except ValueError:
                pass

    async def _keys(self) -> dict[str, list[str]]:
        """Known employer keys per source: seed list plus boards found in earlier searches, minus dead ones."""
        await self._load_facets()
        dead = await self.index.dead_boards()
        known = await self.index.known_boards()
        out: dict[str, list[str]] = {}
        for src, key in dict.fromkeys([*self.seeds, *known]):
            if (src, key) not in dead and src in self.specs and self.specs[src].keyed:
                out.setdefault(src, []).append(key)
        return out

    def _spawn(self, unit: Unit, origin: str = "seed") -> asyncio.Task:
        task = self._inflight.get(unit)
        if task is None or task.done():
            task = asyncio.create_task(self._refresh_unit(unit, origin))
            self._inflight[unit] = task
            task.add_done_callback(lambda t, u=unit: self._inflight.pop(u, None) if self._inflight.get(u) is t else None)
        return task

    async def _refresh_unit(self, unit: Unit, origin: str) -> tuple[Unit, int, str]:
        """Read one unit from its source into the index. Never raises (except on cancellation)."""
        spec = SPECS[unit.src]
        try:
            jobs = await spec.fetch(self.ctx, unit.key, unit.scope)
            await self._store_unit(unit, jobs, registry.ttl(spec, self.settings))
            if spec.keyed:
                await self.index.record_board(unit.src, unit.key, True, len(jobs), origin)
            return unit, len(jobs), ""
        except SourceError as exc:
            error = str(exc)
            if isinstance(exc, BoardNotFound) and spec.keyed and spec.kind == "board":
                await self.index.record_board(unit.src, unit.key, False, 0, origin)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = type(exc).__name__
        permanent = "robots.txt disallows" in error or error.startswith(("HTTP 404", "HTTP 410", "HTTP 422"))
        await self.index.fail_unit(unit.src, unit.key, unit.scope, error, 6 * 3600.0 if permanent else 180.0)
        return unit, 0, error

    async def _store_unit(self, unit: Unit, jobs: list[Job], ttl: int) -> None:
        existing = await self.index.unit_rows(unit.src, unit.key, unit.scope)
        rows = await asyncio.to_thread(_rows, jobs, existing)          # packing is CPU work
        await self.index.replace_unit(unit.src, unit.key, unit.scope, rows, ttl)
        if unit.src == "workday" and unit.key not in self._facets_saved and workday._us_facet.get(unit.key):
            await self.index.meta_set("wd_us_facet:" + unit.key, json.dumps(workday._us_facet[unit.key]))
            self._facets_saved.add(unit.key)

    async def _ensure(self, units: list[Unit], deadline: float, state: SearchState | None = None,
                      origin: str = "seed", paced_wait: float | None = None) -> tuple[list[tuple[Unit, str]], list[Unit]]:
        """Make units fresh. Returns (failed, still_loading). Units still loading keep running after the deadline."""
        if not units:
            return [], []
        states = await self.index.unit_states()
        now = time.time()
        failed: list[tuple[Unit, str]] = []
        tasks: dict[asyncio.Task, Unit] = {}
        for unit in dict.fromkeys(units):
            st = states.get((unit.src, unit.key, unit.scope))
            if st and st[1] > now:
                if not st[2]:
                    failed.append((unit, "failed recently; will be retried later"))
                continue
            tasks[self._spawn(unit, origin)] = unit
        ready = len(units) - len(tasks) - len(failed)
        pending = set(tasks)
        start = time.monotonic()
        # Paced sources (Workday) get a shorter wait of their own; they finish in the background.
        limit = {t: start + (min(deadline, SPECS[u.src].wait if paced_wait is None else paced_wait) if SPECS[u.src].wait else deadline)
                 for t, u in tasks.items()}
        while pending:
            now = time.monotonic()
            waiting = {t for t in pending if limit[t] > now}
            if not waiting:
                break
            done, _ = await asyncio.wait(waiting, timeout=max(0.05, min(1.5, max(limit[t] for t in waiting) - now)))
            pending -= done
            for task in done:
                if task.cancelled():
                    continue
                unit, _, error = task.result()
                if error:
                    failed.append((unit, error))
                else:
                    ready += 1
            if state and done:
                state.stats["boards_checked"] = ready
                state.stats["boards_failed"] = len(failed)
                state.stats["sources_pending"] = len(pending)
                state.emit(state.stage or "search", "Reading job sources")
        return failed, [tasks[t] for t in pending]

    def prewarm(self) -> None:
        """Start reading every known board and feed in the background (query-based sources wait for a search)."""
        async def go():
            keys = await self._keys()
            units = registry.plan_units(self.specs, keys, [])
            await self._ensure(units[: self.settings.max_boards_per_search], deadline=3600.0)

        task = asyncio.create_task(go())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _queries(self, titles: list[str], related: list[str]) -> list[str]:
        n = max(1, self.settings.search_queries_per_search)
        out = [" ".join(t.lower().split()) for t in titles[:n]]
        if len(out) < n and related:
            out.append(" ".join(related[0].lower().split()))
        return list(dict.fromkeys(out))[:n]

    async def _source_report(self, state: SearchState, failed: list[tuple[Unit, str]], pending: list[Unit]) -> None:
        index = await self.index.index_stats()
        report: dict[str, dict] = {}

        def entry(src: str) -> dict:
            label = SPECS[src].label if src in SPECS else src
            return report.setdefault(label, {"boards_ok": 0, "boards_failed": 0, "pending": 0, "postings": 0, "errors": []})

        for src, info in index["by_source"].items():
            e = entry(src)
            e["boards_ok"], e["postings"] = info["units"], info["postings"]
        for unit, error in failed:
            e = entry(unit.src)
            e["boards_failed"] += 1
            if len(e["errors"]) < 5:
                e["errors"].append(f"{unit.key}: {error}")
        for unit in pending:
            entry(unit.src)["pending"] += 1
        state.sources = report
        # Live progress: postings available to this search so far (AI leads and crawled links are added later).
        state.stats["total_discovered"] = index["fresh_postings"] + state.stats["ai_leads"] + state.stats["crawled_links"]
        state.stats["boards_checked"] = index["fresh_units"]
        state.stats["boards_failed"] = len(failed)
        state.stats["sources_pending"] = len(pending)
        for label, e in report.items():
            if e["boards_failed"]:
                throttled = any("429" in err or "throttling" in err for err in e["errors"])
                state.warn(f"{label}: {e['boards_failed']} source(s) could not be read; coverage is incomplete."
                           + (f" {label} is rate-limiting this machine (HTTP 429); the app has backed off and retries later." if throttled else ""))
        if pending:
            slow = sorted({SPECS[u.src].label for u in pending})
            state.warn(f"{len(pending)} source request(s) ({', '.join(slow)}) were still loading at the time limit. They keep "
                       f"loading in the background; search again in a few minutes for wider coverage.")

    # ------------------------------------------------------------ candidates
    def _screen(self, jobs: list[Job], req: SearchRequest) -> tuple[list[Job], dict, list[Job]]:
        """US location, work mode and authorization filters. CPU only; runs in a worker thread."""
        counts = dict.fromkeys(REJECT_KEYS, 0)
        kept: list[Job] = []
        touched: list[Job] = []
        for job in jobs:
            if job.enriched < ENRICH_VERSION:
                enrich_job(job)
                touched.append(job)
            is_us, _ = us_location_status(job.location, job.country_hint, job.description)
            if not is_us:
                counts["rejected_non_us"] += 1
                continue
            if work_mode_rejects(job, req):
                counts["rejected_work_mode"] += 1
                continue
            reject, _ = evaluate_eligibility(job, req)
            if reject:
                counts["rejected_eligibility"] += 1
                continue
            kept.append(job)
        return kept, counts, touched

    async def _fetch_details(self, pending: list[Job], counts: dict, state: SearchState | None) -> list[Job]:
        """Fetch descriptions for list-only records (best title matches first, bounded)."""
        todo: list[Job] = []
        for job in sorted(pending, key=lambda j: -j.title_relevance):
            is_us, reason = us_location_status(job.location, job.country_hint)
            if not is_us and reason != "US eligibility not confirmed":
                counts["rejected_non_us"] += 1          # clearly outside the US: no request needed
                continue
            todo.append(job)
        budget = max(0, self.settings.max_detail_fetches)
        skipped = max(0, len(todo) - budget)
        todo = todo[:budget]

        async def one(job: Job):
            spec = SPECS.get(job.ats)
            if not spec or not spec.detail:
                return job, "error"
            try:
                return job, await spec.detail(self.ctx, job)
            except SourceError:
                return job, "error"

        tasks = [asyncio.create_task(one(j)) for j in todo]
        ready: list[Job] = []
        closed: list[str] = []
        if tasks:
            wait = state.detail_wait if state else DETAIL_WAIT_SECONDS
            done, late = await asyncio.wait(tasks, timeout=min(self.settings.fetch_deadline_seconds, wait))
            for task in late:
                task.cancel()
            skipped += len(late)
            for task in done:
                if task.cancelled() or task.exception():
                    continue
                job, result = task.result()
                if result is None:
                    closed.append(ats_key(job.ats, job.ats_id))     # the source says the posting is gone
                elif isinstance(result, Job):
                    ready.append(result)
            if closed:
                await self.index.delete_akeys([k for k in closed if k])
        if state:
            state.stats["detail_fetches"] += len(ready)
            if skipped:
                state.warn(f"{skipped} posting(s) from paced sources could not be opened within the time limit and were left out.")
        return ready

    async def _collect(self, req: SearchRequest, related: list[str], expected: dict[str, set[str]], *,
                       max_age: float | None = None, details: bool = True, counts: dict | None = None,
                       state: SearchState | None = None) -> list[Job]:
        """Relevant, screened postings from the index. `expected` maps posting keys named by AI leads to providers."""
        titles = req.titles
        tokens = query_tokens([*titles, *related])
        rows = await self.index.candidates(tokens, list(expected), max_age)

        def pick() -> dict[str, tuple[int, float, float]]:
            chosen: dict[str, tuple[int, float, float]] = {}
            for row_id, akey, title, _src, fetched_at in rows:
                relevance = best_title_relevance(title, titles, related)
                if akey in expected:
                    relevance = max(relevance, AI_LEAD_RELEVANCE_FLOOR)
                if relevance < MIN_RELEVANCE:
                    continue
                prev = chosen.get(akey)
                if prev is None or fetched_at > prev[2]:
                    chosen[akey] = (row_id, relevance, fetched_at)
            return chosen

        chosen = await asyncio.to_thread(pick)
        relevance_by_row = {row_id: (rel, fetched_at) for row_id, rel, fetched_at in chosen.values()}
        jobs = await self.index.load_postings(list(relevance_by_row))
        for job in jobs:
            job.title_relevance, read_at = relevance_by_row.get(job.row_id, (0.0, 0.0))
            job.verified_at = max(job.verified_at, _iso(read_at))      # when its source last listed it
            for provider in sorted(expected.get(ats_key(job.ats, job.ats_id), ())):
                label = f"{provider} web search"
                if label not in job.sources:
                    job.sources.append(label)
        local = counts if counts is not None else {}
        for key in REJECT_KEYS:
            local[key] = 0
        ready = [j for j in jobs if not j.needs_detail]
        pending = [j for j in jobs if j.needs_detail]
        if details and pending:
            ready.extend(await self._fetch_details(pending, local, state))
        kept, screen_counts, touched = await asyncio.to_thread(self._screen, ready, req)
        for key, value in screen_counts.items():
            local[key] += value
        local["relevant"] = len(chosen)
        if touched:
            await self.index.update_postings(touched)
        return kept

    # ------------------------------------------------------------ AI leads + crawl
    async def _resolve_leads(self, state: SearchState, req: SearchRequest, related: list[str],
                             leads: list[Candidate], ai: AIOrchestrator) -> tuple[dict[str, set[str]], list[Job]]:
        """Check every AI lead against its source. Returns (index keys named by leads, verified jobs from other pages)."""
        stats = state.stats
        expected: dict[str, set[str]] = {}
        board_units: dict[Unit, None] = {}
        workday_leads: list[tuple[str, Candidate]] = []
        page_leads: dict[str, Candidate] = {}
        for lead in leads:
            parsed = parse_ats(lead.url)
            src = parsed[0] if parsed else ""
            board, job_id = (parsed[1], parsed[2]) if parsed else ("", "")
            if src in BOARD_LEAD_SOURCES and board and src in self.specs:
                board_units[Unit(src, board, "")] = None
                if job_id:
                    expected.setdefault(ats_key(src, job_id), set()).add(lead.provider)
            elif src == "workable" and board and src in self.specs:
                board_units[Unit(src, board, "")] = None
                if job_id:
                    expected.setdefault(ats_key(src, job_id), set()).add(lead.provider)
            elif src == "workday" and board and src in self.specs:
                await self.index.record_board("workday", board, True, 0, "ai")   # searched from now on
                if job_id:
                    workday_leads.append((board, lead))
            else:
                page_leads.setdefault(lead.url, lead)

        verified: list[Job] = []

        async def check_boards():
            if not board_units:
                return
            await self._ensure(list(board_units), min(30.0, self.settings.fetch_deadline_seconds), state, origin="ai")
            live = await self.index.live_akeys(list(expected))
            for key in [k for k in expected if k not in live]:
                del expected[key]
                stats["rejected_unverified"] += 1       # not in the employer's live board: closed or never existed

        async def check_workday(board: str, lead: Candidate):
            try:
                job = await workday.lead(self.ctx, board, lead.url)
            except SourceError:
                job = None
            if job is None:
                stats["rejected_unverified"] += 1
                return
            job.sources.append(f"{lead.provider} web search")
            verified.append(job)

        browser_budget = Budget(self.settings.max_browser_pages_per_search)
        crawl_budget = Budget(self.settings.max_crawl_links_per_search)
        sem = asyncio.Semaphore(8)
        to_extract: list[tuple[job_verifier.Page, Candidate]] = []
        crawl_queue: dict[str, Candidate] = {}

        async def check_page(cand: Candidate, follow_listing: bool):
            async with sem:
                verdict = await self.verifier.verify(cand, browser_budget)
            if verdict.job:
                verified.append(verdict.job)
                return
            if verdict.page is None:
                stats["rejected_unverified"] += 1
                return
            links = self.verifier.listing_links(verdict.page, req.titles, related, cand) if follow_listing else []
            if len(links) >= 2:                          # a careers/listing page: follow the matching job links
                for link in links:
                    if link.url not in page_leads and crawl_budget.take():
                        crawl_queue.setdefault(link.url, link)
            elif len(to_extract) < self.settings.max_ai_page_extractions:
                to_extract.append((verdict.page, cand))
            else:
                stats["rejected_unverified"] += 1

        first = list(page_leads.values())
        budget = self.settings.max_page_verifications
        if len(first) > budget:
            state.warn(f"Verification budget reached: {len(first) - budget} web lead(s) were not checked.")

        async def check_pages():
            await asyncio.gather(*(check_page(c, True) for c in first[:budget]))
            if crawl_queue:
                stats["crawled_links"] += len(crawl_queue)
                await asyncio.gather(*(check_page(c, False) for c in crawl_queue.values()))

        async def check_workday_leads():
            checks = [asyncio.create_task(check_workday(b, l)) for b, l in workday_leads[:10]]
            if checks:                                   # paced source: bounded wait
                _, late = await asyncio.wait(checks, timeout=state.detail_wait)
                for task in late:
                    task.cancel()
                    stats["rejected_unverified"] += 1

        await asyncio.gather(check_boards(), check_pages(), check_workday_leads())

        if to_extract and ai.available:
            pages = [(str(i), f"URL: {page.url}\n{page.text[:2500]}") for i, (page, _) in enumerate(to_extract)]
            extracted = await ai.extract_pages(pages)
            for i, (page, cand) in enumerate(to_extract):
                job, _ = job_from_extraction(page, cand, extracted.get(str(i)) or {})
                if job:
                    stats["ai_extracted"] += 1
                    verified.append(job)
                else:
                    stats["rejected_unverified"] += 1
        else:
            stats["rejected_unverified"] += len(to_extract)
        stats["browser_pages"] += browser_budget.used
        return expected, verified

    # ------------------------------------------------------------ scoring helpers
    @staticmethod
    def _score(jobs: list[Job], profile: Profile, req: SearchRequest, related: list[str]) -> None:
        for job in jobs:
            heuristic_score(job, profile, req, related)

    @staticmethod
    def _order(job: Job):
        # AI-assessed jobs rank ahead of keyword-only ones: a keyword score was never checked against the posting by a model.
        return (job.score_source != "ai", -job.score, job.company, job.title)

    def _select(self, ranked: list[Job], target: int) -> list[Job]:
        """Top `target` jobs, spread across employers: a soft cap per employer and at most two postings with
        the same employer and title (the same role opened in several cities). Caps are lifted only to fill the list."""
        per_company = max(2, math.ceil(target / 8))
        final, overflow = [], []
        companies: dict[str, int] = {}
        roles: dict[tuple[str, str], int] = {}
        for job in ranked:
            company = norm_company(job.company)
            role = (company, " ".join(job.title.lower().split()))
            if companies.get(company, 0) < per_company and roles.get(role, 0) < 2:
                companies[company] = companies.get(company, 0) + 1
                roles[role] = roles.get(role, 0) + 1
                final.append(job)
            else:
                overflow.append(job)
        final = (final + overflow)[:target] if len(final) < target else final[:target]
        final.sort(key=self._order)
        return final

    # ------------------------------------------------------------ pipeline
    async def run(self, state: SearchState, req: SearchRequest, resume_text: str) -> None:
        stats = state.stats
        cap = max(1, self.settings.search_max_results)
        target = min(req.result_count or cap, cap)
        ai = self.ai_factory()
        state.providers = ai.status
        state.status = "running"
        titles = req.titles
        wanted = {t.lower() for t in titles}

        state.emit("analyze", "Analyzing resume")
        base_profile: Profile = heuristic_profile(resume_text)
        related0 = [t for t in static_related_titles(titles, base_profile.all_skills) if t.lower() not in wanted][:8]
        keys = await self._keys()
        use_web = ai.available and req.deep_search
        if not ai.available:
            state.warn("No AI provider configured: using keyword-based resume analysis and scoring. No AI web search was performed.")
        elif not req.deep_search:
            state.warn("AI web search and browser crawl were switched off for this search (faster, narrower coverage).")

        # Everything slow starts now and runs side by side.
        profile_task = asyncio.create_task(ai.extract_profile(resume_text, req, base_profile)) if ai.available else None
        known_names = sorted({k.split("|")[0] for ks in keys.values() for k in ks})
        discover_task = asyncio.create_task(ai.discover(base_profile, req, related0, known_names)) if use_web else None
        units = registry.plan_units(self.specs, keys, self._queries(titles, related0))[: self.settings.max_boards_per_search]
        # Deep search runs about a minute anyway (AI web search), so paced sources get real waiting time.
        # Fast search does not wait for them: it uses what the index already holds and lets them load on.
        state.detail_wait = DETAIL_WAIT_SECONDS if use_web else FAST_DETAIL_WAIT_SECONDS
        refresh_task = asyncio.create_task(self._ensure(units, self.settings.fetch_deadline_seconds + (30.0 if use_web else 0.0), state,
                                                        paced_wait=PACED_WAIT_SECONDS if use_web else FAST_PACED_WAIT_SECONDS))
        side_tasks = [t for t in (profile_task, discover_task, refresh_task) if t]
        try:
            await self._pipeline(state, req, resume_text, ai, target, base_profile, related0,
                                 profile_task, discover_task, refresh_task)
        except BaseException:
            for task in side_tasks:
                task.cancel()
            raise
        state.mark("total")
        state.emit("done", state.message)        # persisting results is the caller's job (legacy app or portal worker)

    async def _pipeline(self, state, req, resume_text, ai, target, base_profile, related0,
                        profile_task, discover_task, refresh_task) -> None:
        stats = state.stats
        wanted = {t.lower() for t in req.titles}

        # 1. Preliminary results from what the index already holds (no network).
        state.emit("titles", "Identifying relevant job titles")
        early = await self._collect(req, related0, {}, max_age=self.settings.index_stale_ok_seconds, details=False)
        if early:
            early, _ = await asyncio.to_thread(deduplicate, early)
            self._score(early, base_profile, req, related0)
            early.sort(key=self._order)
            state.publish(self._select(early, target), partial=True)
            state.mark("first_results")
            state.emit("titles", "Preliminary matches ready")

        profile = (await profile_task) if profile_task else base_profile
        related = [t for t in (profile.related_titles or related0) if t.lower() not in wanted][:8]
        state.related_titles = related

        # 2. Sources: wait for the refresh (bounded), then screen the index.
        state.emit("search", "Searching USA job openings")
        failed, pending = await refresh_task
        state.emit("boards", "Checking employer job boards")
        await self._source_report(state, failed, pending)
        state.mark("sources_read")
        counts: dict = {}
        kept = await self._collect(req, related, {}, counts=counts, state=state)
        stats["unique_candidates"] = stats["verified_active"] = len(kept)      # so far; final numbers follow deduplication
        if kept:
            preview, _ = await asyncio.to_thread(deduplicate, [j.model_copy(deep=True) for j in kept])
            self._score(preview, profile, req, related)
            preview.sort(key=self._order)
            state.publish(self._select(preview, target), partial=True)
            if "first_results" not in state.timings:
                state.mark("first_results")
            state.emit("boards", "Preliminary matches updated")

        # 3. AI web-search leads: verify against sources, pages, browser, AI extraction.
        state.emit("verify", "Verifying application links")
        leads: list[Candidate] = []
        if discover_task:
            try:
                leads = await asyncio.wait_for(discover_task, timeout=self.settings.ai_discovery_deadline_seconds)
            except asyncio.TimeoutError:
                state.warn("AI web search hit its time limit; results use the sources already read.")
        stats["ai_leads"] = len(leads)
        expected, lead_jobs = await self._resolve_leads(state, req, related, leads, ai)
        lead_counts = dict.fromkeys(REJECT_KEYS, 0)
        if lead_jobs:
            relevant = []
            for job in lead_jobs:
                job.title_relevance = best_title_relevance(job.title, req.titles, related)
                if job.ats == "workday":
                    job.title_relevance = max(job.title_relevance, AI_LEAD_RELEVANCE_FLOOR)
                if job.title_relevance < MIN_RELEVANCE:
                    lead_counts["rejected_not_relevant"] += 1
                else:
                    relevant.append(job)
            lead_kept, screen_counts, _ = await asyncio.to_thread(self._screen, relevant, req)
            for key, value in screen_counts.items():
                lead_counts[key] += value
        else:
            lead_kept = []
        if leads:
            state.detail_wait = min(state.detail_wait, 6.0)      # second pass: do not wait long for paced details again
            kept = await self._collect(req, related, expected, counts=counts, state=state)   # lead boards are in the index now
            await self._source_report(state, failed, pending)
        index = await self.index.index_stats()
        stats["total_discovered"] = index["fresh_postings"] + len(leads) + stats["crawled_links"]
        stats["rejected_not_relevant"] = max(0, index["fresh_postings"] - counts.get("relevant", 0)) + lead_counts["rejected_not_relevant"]
        for key in ("rejected_non_us", "rejected_work_mode", "rejected_eligibility"):
            stats[key] = counts.get(key, 0) + lead_counts[key]
        state.mark("leads_verified")
        for name, st in ai.status.items():
            if not st["configured"] and ai.available:
                state.warn(f"{name} is not configured: running with the other provider only.")
        if not self.browser.available and req.deep_search and ai.available:
            state.warn(f"Headless browser crawler is off: {self.browser.unavailable_reason}. Pages that need JavaScript were skipped.")

        # 4. Deduplicate and score.
        state.emit("dedupe", "Removing duplicate listings")
        unique, removed = await asyncio.to_thread(deduplicate, [*kept, *lead_kept])
        stats["rejected_duplicates"] += removed
        stats["unique_candidates"] = len(unique)
        stats["verified_active"] = len(unique)
        state.emit("rank", "Ranking resume matches")
        self._score(unique, profile, req, related)
        unique.sort(key=self._order)

        # 5. AI ranking (parallel batches) and audit, side by side.
        if ai.available and unique:
            unique = await self._ai_rank(state, req, resume_text, ai, profile, unique, target)
        for name, st in ai.status.items():
            if st["configured"] and st["failed_calls"]:
                state.warn(f"{name} degraded: {st['failed_calls']} call(s) failed ({st['last_error']}).")
        state.mark("ranked")

        state.emit("prepare", "Preparing results")
        unique, removed = deduplicate(unique)        # second pass: nothing may be duplicated after ranking
        stats["rejected_duplicates"] += removed
        unique.sort(key=self._order)
        final = self._select(unique, target)
        stats["final_matches"] = len(final)
        state.message = self._outcome(stats, target, len(unique), ai)
        state.status = "completed"
        state.publish(final, partial=False)

    async def _ai_rank(self, state: SearchState, req: SearchRequest, resume_text: str, ai: AIOrchestrator,
                       profile: Profile, unique: list[Job], target: int) -> list[Job]:
        stats = state.stats
        # The AI pool is spread across employers too, so calls are not spent on near-identical postings.
        pool = self._select(unique, min(self.settings.max_ai_ranked_jobs, max(target * 2, target + 30)))
        size = max(1, self.settings.ai_rank_batch_size)
        batches = [pool[i:i + size] for i in range(0, len(pool), size)]
        sem = asyncio.Semaphore(max(1, self.settings.ai_rank_concurrency))
        # The audit only needs posting text, so it runs at the same time as ranking.
        to_audit = [j for j in pool if j.title_relevance < 0.85 or authorization_snippets(j.description, 1)]
        audit_task = asyncio.create_task(ai.audit(req, to_audit[: self.settings.max_audit_jobs]))

        async def rank(batch: list[Job]):
            async with sem:
                rankings = await ai.rank_batch(profile, req, batch)
            for job in batch:
                if job.id in rankings:
                    apply_ai_ranking(job, rankings[job.id], profile, resume_text)
                    stats["ai_ranked"] += 1
            state.emit("rank", "Ranking resume matches")

        try:
            await asyncio.gather(*(rank(b) for b in batches))
            audits = await audit_task
        except BaseException:
            audit_task.cancel()
            raise
        if stats["ai_ranked"] < len(pool):
            state.warn(f"AI ranking covered {stats['ai_ranked']} of {len(pool)} shortlisted jobs; the rest use keyword-based scores.")
        dropped = set()
        for job in pool:
            audit = audits.get(job.id)
            if not audit:
                continue
            if audit.get("relevant") is False and job.title_relevance < 0.85:
                dropped.add(job.id)
                stats["rejected_not_relevant"] += 1
                continue
            evidence = " ".join(str(audit.get("evidence") or "").lower().split())
            grounded = len(evidence) >= 12 and evidence in " ".join(job.description.lower().split())
            if grounded:     # an eligibility claim counts only when its quote exists in the posting
                job.citizenship_requirement = audit.get("citizenship_requirement") or job.citizenship_requirement
                job.sponsorship = audit.get("sponsorship") or job.sponsorship
                job.clearance_required = bool(audit.get("clearance_required"))
                reject, _ = evaluate_eligibility(job, req)
                if reject:
                    dropped.add(job.id)
                    stats["rejected_eligibility"] += 1
        unique = [j for j in unique if j.id not in dropped]
        unique.sort(key=self._order)
        return unique

    @staticmethod
    def _outcome(stats: dict, target: int, qualifying: int, ai: AIOrchestrator) -> str:
        found = stats["final_matches"]
        if found >= target:
            extra = f" ({qualifying} qualified in total; showing the top {target})" if qualifying > target else ""
            return f"Found {found} verified matching jobs{extra}."
        reasons = [f"{stats['total_discovered']} postings were examined across {stats['boards_checked']} source reads"
                   + (f" and {stats['ai_leads']} AI web-search leads" if stats["ai_leads"] else "")]
        for key, text in (("rejected_not_relevant", "did not match the requested roles"),
                          ("rejected_non_us", "were outside the United States or lacked confirmed US eligibility"),
                          ("rejected_eligibility", "conflicted with the work-authorization filters"),
                          ("rejected_work_mode", "did not match the work arrangement"),
                          ("rejected_unverified", "could not be verified as open postings"),
                          ("rejected_duplicates", "were duplicates")):
            if stats[key]:
                reasons.append(f"{stats[key]} {text}")
        if stats["boards_failed"]:
            reasons.append(f"{stats['boards_failed']} sources could not be read")
        if stats["sources_pending"]:
            reasons.append(f"{stats['sources_pending']} source requests were still loading")
        if not ai.available:
            reasons.append("AI web search was unavailable (no API keys configured)")
        return f"Found {found} of {target} requested jobs. " + "; ".join(reasons) + "."
