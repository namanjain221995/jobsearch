"""Search pipeline.

Sources are read into a local SQLite index (one "unit" per employer board, search query or feed).
A search then works on the index, which is what makes it fast:

  1. show preliminary matches from the index straight away (when it already holds data);
  2. refresh stale units in parallel, bounded by a time limit; slow ones keep loading in the background;
  3. in parallel: AI resume analysis and AI web search for extra leads;
  4. verify leads (source API -> plain fetch -> headless browser -> AI extraction, cheapest first);
  5. screen (title, US location, work mode, authorization), deduplicate, score, AI-rank, audit.

Wider searches (all optional, see search_plan.py): the requested titles are widened with real-world variants;
a SearXNG instance is asked for employer boards and postings on the ATS hosts this app can read; AI web
search gets a budget that grows with the number of results requested; and steps 3-4 repeat in rounds while
fewer verified matches than requested exist and budgets remain. Every board found by any of these enters the
index through the same registration path and is then read through its official API like a seed board.

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
from .job_ranker import (MIN_ANCHORS, SCORER_ORDER, apply_ai_ranking, apply_explanation, apply_model_score,
                         best_matching_title, best_title_relevance, evaluate_eligibility, fit_calibration, heuristic_score,
                         redistribute_scores,
                         index_tokens, query_tokens, static_related_titles, work_mode_rejects)
from .job_verifier import Budget, PageVerifier, job_from_extraction
from .providers import registry, workday
from .providers.base import BoardNotFound, Ctx, HostLimiter, RobotsCache, SourceError, now_iso
from .providers.boards import seed_pairs
from .providers import aws_web_search
from .providers.aws_web_search import AWS, AwsWebSearch, AwsWebSearchError, employer_posting_address
from .providers.paid import (GOOGLE_JOBS, TINYFISH, SerpApiClient, TinyFishClient, aggregator_hosts, serp_item)
from .providers.registry import BOARD_LEAD_SOURCES, SPECS, Unit
from .providers.searxng import SearxClient, build_queries, extract, site_hosts, valid_board
from .providers.techsara_provider import TechsaraProvider
from .resume_parser import heuristic_profile
from .schemas import Candidate, Job, Profile, SearchRequest
from .search_plan import Plan, cached_variants, clean_variants, discovery_locations, plan_for, store_variants

MIN_RELEVANCE = 0.6
AI_LEAD_RELEVANCE_FLOOR = 0.6
HOST_LIMITS = {"greenhouse": 10, "ashby": 8, "lever": 6, "workday": 1, "workable": 4, "amazon": 2,
               "remote-boards": 2, "adzuna": 2, "smartrecruiters": 2, "bamboohr": 2}
# Workday answers HTTP 429 when all its employers together get more than a slow trickle of requests.
# One request every 1.5 s; after any 429 the source is left alone for ten minutes (providers/base.py).
HOST_INTERVALS = {"workday": 1.5}
PACED_WAIT_SECONDS, DETAIL_WAIT_SECONDS = 25.0, 20.0            # deep search
FAST_PACED_WAIT_SECONDS, FAST_DETAIL_WAIT_SECONDS = 2.0, 6.0   # fast search
AI_PROVIDERS = {"OpenAI", "Claude"}          # only a lead a model named gets the relevance floor; a search-engine hit does not
SEARX = "SearXNG"
TECHSARA_WEB = "TechSara web search"
WORKDAY_NEW_TENANTS_PER_ROUND = 6           # Workday is paced (one request every 1.5 s): few new tenants are queried while a search waits
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
            "browser_pages": 0, "crawled_links": 0, "ai_extracted": 0, "detail_fetches": 0,
            # wider searches
            "round": 1, "rounds": [], "stop_reason": "", "title_variants": 0, "searx_queries": 0, "searx_cached": 0,
            "searx_results": 0, "searx_failed": 0, "searx_leads": 0, "tsweb_searches": 0, "tsweb_results": 0, "tsweb_failed": 0,
            "tsweb_leads": 0, "new_boards": 0, "new_board_postings": 0,
            "gained_by_source": {}, "company_model_scored": 0, "company_model_explained": 0, "ai_calls": 0,
            # paid sources
            "serp_calls": 0, "serp_cached": 0, "serp_jobs": 0, "serp_listed": 0, "serp_skipped": "",
            "tiny_searches": 0, "tiny_fetches": 0, "tiny_usd": 0.0, "tiny_skipped": "",
            # AWS web search (queries = units of the monthly allowance; result text is never kept)
            "aws_budget": 0, "aws_queries": 0, "aws_results": 0, "aws_duplicates": 0, "aws_generic": 0, "aws_leads": 0,
            "aws_skipped": ""}


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
        self.title_variants: list[str] = []
        self.usage: dict = {}             # calls / tokens per AI provider (no content)
        self.plan: Plan | None = None
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
                "sources": self.sources, "related_titles": self.related_titles, "title_variants": self.title_variants,
                "usage": self.usage, "partial": self.partial,
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
        self.searx = SearxClient(settings.searxng_url, self.client, self.index, settings.searxng_timeout_seconds,
                                 settings.searxng_min_interval_seconds, settings.searxng_cache_hours)
        self.tsweb = (TechsaraProvider(settings.techsara_api_key, settings.techsara_base_url, settings.techsara_model,
                                       settings.techsara_rerank_model, client=self.client)
                      if settings.techsara_configured and settings.techsara_web_search_enabled else None)
        self._unit_counts: dict[Unit, int] = {}      # postings of units read by this process (statistics of new boards)
        self.serpapi = SerpApiClient(settings.serpapi_api_key, self.client, self.index, settings.serpapi_monthly_limit,
                                     settings.serpapi_monthly_reserve, settings.serpapi_cache_hours, settings.serpapi_account_check_seconds)
        self.tinyfish = TinyFishClient(settings.tinyfish_api_key, self.client, self.index, settings.tinyfish_search_usd,
                                       settings.tinyfish_fetch_usd, settings.tinyfish_daily_usd, settings.tinyfish_wallet_reserve_usd,
                                       settings.tinyfish_wallet_check_seconds, settings.tinyfish_timeout_seconds)
        # AWS web search: the monthly allowance is a PostgreSQL row shared by every process (None without PostgreSQL).
        self.aws = AwsWebSearch(settings, aws_web_search.quota_for(getattr(self.index, "_pool", None), settings))
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
        # Workable employer accounts found by discovery: Workable is otherwise read by query, so they are kept apart.
        self._accounts = [key for src, key in known if src == "workable" and key != "*" and (src, key) not in dead and src in self.specs]
        return out

    def _account_units(self) -> list[Unit]:
        return [Unit("workable", key, "") for key in getattr(self, "_accounts", [])]

    @property
    def searx_on(self) -> bool:
        return self.searx.enabled and "searxng" in self.specs

    @property
    def tsweb_on(self) -> bool:
        return self.tsweb is not None and "techsara_web" in self.specs

    @property
    def engines_on(self) -> bool:
        """Whether any engine that takes the `site:` queries of a search is available."""
        return self.searx_on or self.tsweb_on

    @property
    def serp_on(self) -> bool:
        return self.serpapi.enabled and "serpapi" in self.specs

    @property
    def tiny_on(self) -> bool:
        return self.tinyfish.enabled and "tinyfish" in self.specs

    @property
    def aws_on(self) -> bool:
        return self.aws.enabled and "aws_web_search" in self.specs

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
            account = unit.src == "workable" and unit.key != "*"        # one employer's Workable account
            if spec.keyed or account:
                await self.index.record_board(unit.src, unit.key, True, len(jobs), origin)
            if len(self._unit_counts) > 20000:
                self._unit_counts.clear()
            self._unit_counts[unit] = len(jobs)
            return unit, len(jobs), ""
        except SourceError as exc:
            error = str(exc)
            if isinstance(exc, BoardNotFound) and ((spec.keyed and spec.kind == "board") or (unit.src == "workable" and unit.key != "*")):
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
            units = [*registry.plan_units(self.specs, keys, []), *self._account_units()]
            await self._ensure(units[: self.settings.max_boards_per_search], deadline=3600.0)

        task = asyncio.create_task(go())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        # Boards imported with `python -m app.portal.import_boards` are read little by little in the background
        # (docs/board-sources.md). Nothing happens until such an import was run; BOARD_CATALOG_TRICKLE=0 switches it off.
        from . import board_catalog
        board_catalog.start_catalog_trickle(self)

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
        budget = max(0, state.plan.detail_fetches if state and state.plan else self.settings.max_detail_fetches)
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
                if akey in expected and expected[akey] & AI_PROVIDERS:      # a model named this posting for this candidate
                    relevance = max(relevance, AI_LEAD_RELEVANCE_FLOOR)
                if relevance < MIN_RELEVANCE:
                    continue
                prev = chosen.get(akey)
                if prev is None or fetched_at > prev[2]:
                    chosen[akey] = (row_id, relevance, fetched_at)
            return chosen

        chosen = await asyncio.to_thread(pick)
        matched = len(chosen)
        limit = max(1, self.settings.max_index_candidates)
        if matched > limit:                       # memory bound: the best title matches are screened, the rest are counted
            best = sorted(chosen.items(), key=lambda kv: (-kv[1][1], -kv[1][2]))[:limit]
            chosen = dict(best)
            if state:
                state.warn(f"{matched} indexed postings matched the titles; the {limit} closest title matches were screened "
                           f"(MAX_INDEX_CANDIDATES).")
        relevance_by_row = {row_id: (rel, fetched_at) for row_id, rel, fetched_at in chosen.values()}
        jobs = await self.index.load_postings(list(relevance_by_row))
        for job in jobs:
            job.title_relevance, read_at = relevance_by_row.get(job.row_id, (0.0, 0.0))
            job.matched_query = best_matching_title(job.title, titles, related)
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
        local["relevant"] = matched
        if touched:
            await self.index.update_postings(touched)
        return kept

    # ------------------------------------------------------------ AI leads + crawl
    async def _resolve_leads(self, state: SearchState, req: SearchRequest, related: list[str],
                             leads: list[Candidate], ai: AIOrchestrator) -> tuple[dict[str, set[str]], list[Job]]:
        """Check every AI lead against its source. Returns (index keys named by leads, verified jobs from other pages)."""
        stats = state.stats
        expected: dict[str, set[str]] = {}
        board_units: dict[Unit, str] = {}          # unit -> origin recorded with the board ("ai" / "searxng")
        workday_leads: list[tuple[str, Candidate]] = []
        workday_new: dict[str, str] = {}
        page_leads: dict[str, Candidate] = {}
        keys = await self._keys()
        known = {(src, key) for src, ks in keys.items() for key in ks} | {("workable", k) for k in getattr(self, "_accounts", [])}
        for lead in leads:
            parsed = parse_ats(lead.url)
            src = parsed[0] if parsed else ""
            board, job_id = (parsed[1], parsed[2]) if parsed else ("", "")
            origin = {SEARX: "searxng", TECHSARA_WEB: "techsara_web", TINYFISH: "tinyfish", GOOGLE_JOBS: "serpapi", AWS: "aws_web_search"}.get(lead.provider, "ai")
            if origin != "ai" and parsed and not (board and valid_board(src, board)):
                stats["rejected_unverified"] += 1          # an address from a search engine is used only when well formed
                continue
            if src in BOARD_LEAD_SOURCES and board and src in self.specs:
                board_units.setdefault(Unit(src, board, ""), origin)
                if job_id:
                    expected.setdefault(ats_key(src, job_id), set()).add(lead.provider)
            elif src == "workable" and board and src in self.specs:
                board_units.setdefault(Unit(src, board, ""), origin)
                if job_id:
                    expected.setdefault(ats_key(src, job_id), set()).add(lead.provider)
            elif src == "workday" and board and src in self.specs:
                if ("workday", board) not in known:
                    workday_new.setdefault(board, origin)
                await self.index.record_board("workday", board, True, 0, origin)   # searched from now on
                if job_id:
                    workday_leads.append((board, lead))
            else:
                page_leads.setdefault(lead.url, lead)

        verified: list[Job] = []
        new_units = [u for u in board_units if (u.src, u.key) not in known]
        deadline = min(30.0, self.settings.fetch_deadline_seconds) if stats["round"] <= 1 and not new_units else \
            max(min(30.0, self.settings.fetch_deadline_seconds), self.settings.round_fetch_deadline_seconds)

        async def check_boards():
            if not board_units and not workday_new:
                return
            # The one registration path for every discovered board, whoever found it: read it through the source's
            # own API; a board that answers is recorded in the index (and read again by every later search), one
            # that does not exist is recorded as dead.
            by_origin: dict[str, list[Unit]] = {}
            for unit, origin in board_units.items():
                by_origin.setdefault(origin, []).append(unit)
            # New Workday tenants are queried for the first requested title now (paced, a few per round); the rest
            # are searched by later searches, like every recorded tenant.
            query = self._queries(req.titles, related)[:1]
            for board, origin in list(workday_new.items())[:WORKDAY_NEW_TENANTS_PER_ROUND]:
                by_origin.setdefault(origin, []).extend(Unit("workday", board, q) for q in query)
            results = await asyncio.gather(*(self._ensure(units, deadline, state, origin=origin, paced_wait=state.detail_wait)
                                             for origin, units in by_origin.items()))
            unread = {u for failed, pending in results for u in [*(f[0] for f in failed), *pending]}
            for unit in new_units:
                if unit not in unread:
                    stats["new_boards"] += 1
                    gained = self._unit_counts.get(unit, 0)
                    stats["new_board_postings"] += gained
                    label = SPECS[unit.src].label
                    stats["gained_by_source"][label] = stats["gained_by_source"].get(label, 0) + gained
            stats["new_boards"] += len(workday_new)
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
                # A page our own fetch and browser could not turn into a posting (JavaScript-only, browser unavailable)
                # may be read through TinyFetch - never when the site itself refused an automated browser.
                if (verdict.job is None and verdict.page is not None and not verdict.page.rendered and self.tiny_on
                        and "blocks automated browsers" not in verdict.reason
                        and stats["tiny_fetches"] < (state.plan.tiny_fetches if state.plan else 0)):
                    stats["tiny_fetches"] += 1
                    html, note = await self.tinyfish.fetch_html(verdict.page.url)
                    stats["tiny_usd"] = round(self.tinyfish.spent, 4)
                    if note:
                        stats["tiny_fetches"] -= 1 if note in ("daily spending ceiling reached", "wallet balance is at the reserve", "not configured") else 0
                        stats["tiny_skipped"] = note if "ceiling" in note or "reserve" in note else stats["tiny_skipped"]
                    else:
                        page = job_verifier.Page(verdict.page.url, 200, html, job_verifier.html_to_text(html), rendered=True)
                        job, _ = job_verifier._evaluate(page, cand)
                        if job:
                            job.verification = job.verification.replace("rendered in a headless browser", "read through TinyFetch")
                            verdict = job_verifier.Verdict(job=job)
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
            elif len(to_extract) < extraction_limit:
                to_extract.append((verdict.page, cand))
            else:
                stats["rejected_unverified"] += 1

        first = list(page_leads.values())
        # Page reads of the whole search share one budget; what an earlier round used is gone.
        total = state.plan.page_verifications if state.plan else self.settings.max_page_verifications
        budget = max(0, total - getattr(state, "pages_checked", 0))
        state.pages_checked = getattr(state, "pages_checked", 0) + min(budget, len(first))
        # Pages without structured data are read by a model; with the company model (no bill) five times as many.
        extraction_limit = self.settings.max_ai_page_extractions * (5 if getattr(ai, "techsara", None) is not None else 1)
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

        if to_extract and (ai.available or getattr(ai, "techsara", None) is not None):
            pages = [(str(i), f"URL: {page.url}\n{page.text[:2500]}") for i, (page, _) in enumerate(to_extract)]
            size = max(1, self.settings.max_ai_page_extractions)           # one call per batch of this size
            extracted: dict[str, dict] = {}
            for part in await asyncio.gather(*(ai.extract_pages(pages[i:i + size]) for i in range(0, len(pages), size))):
                extracted.update(part)
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
        # Jobs scored by the company rerank model sit between the two (a model read them, without a written judgement).
        return (SCORER_ORDER.get(job.score_source, 2), -job.score, job.company, job.title)

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
        cap = self.settings.result_cap
        target = min(req.result_count or cap, cap)
        ai = self.ai_factory()
        state.plan = plan = plan_for(self.settings, target, req.deep_search)
        if hasattr(ai, "configure"):
            ai.configure(plan)                    # AI budgets scale with the number of results requested
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
        expand_task = asyncio.create_task(self._expand_titles(state, ai, titles, base_profile))
        units = [*registry.plan_units(self.specs, keys, self._queries(titles, related0)), *self._account_units()][: plan.boards]
        # Deep search runs about a minute anyway (AI web search), so paced sources get real waiting time.
        # Fast search does not wait for them: it uses what the index already holds and lets them load on.
        state.detail_wait = DETAIL_WAIT_SECONDS if use_web else FAST_DETAIL_WAIT_SECONDS
        refresh_task = asyncio.create_task(self._ensure(units, self.settings.fetch_deadline_seconds + (30.0 if use_web else 0.0), state,
                                                        paced_wait=PACED_WAIT_SECONDS if use_web else FAST_PACED_WAIT_SECONDS))
        side_tasks = [t for t in (profile_task, discover_task, refresh_task, expand_task) if t]
        state.side_tasks = side_tasks
        try:
            await self._pipeline(state, req, resume_text, ai, target, base_profile, related0,
                                 profile_task, discover_task, refresh_task, expand_task, known_names)
        except BaseException:
            for task in side_tasks:
                task.cancel()
            raise
        finally:
            stats["ai_calls"] = getattr(ai, "calls", 0)
            if hasattr(ai, "usage"):
                state.usage = ai.usage()
            if hasattr(ai, "aclose"):
                await ai.aclose()
        state.mark("total")
        state.emit("done", state.message)        # persisting results is the caller's job (legacy app or portal worker)

    # ------------------------------------------------------------ wider searches: variants, SearXNG, rounds
    async def _expand_titles(self, state: SearchState, ai, titles: list[str], profile: Profile) -> list[str]:
        """Real-world variants of the requested titles: one model call per search, validated, capped and cached."""
        s = self.settings
        if not s.title_expansion or s.title_expansion_max <= 0 or not hasattr(ai, "expand_titles"):
            return []
        if not (ai.available or getattr(ai, "techsara", None) is not None):
            return []
        cached = await cached_variants(self.index, titles, s.title_expansion_cache_hours * 3600.0)
        if cached is not None:
            return clean_variants(cached, titles, s.title_expansion_max)
        try:
            raw, _ = await ai.expand_titles(titles, profile)
        except asyncio.CancelledError:
            raise
        except Exception:
            return []
        variants = clean_variants(raw, titles, s.title_expansion_max)
        if variants:
            await store_variants(self.index, titles, variants)
        return variants

    async def _searx_round(self, state: SearchState, queries: list[str]) -> list[Candidate]:
        """Send one slice of `site:` queries (paced by the client) and turn result addresses into leads.
        Only addresses are used. A board address or a posting address on a board ATS becomes a lead that the
        normal lead check resolves: the board is registered and read through its API. Never raises."""
        stats, plan = state.stats, state.plan
        if not queries:
            return []
        hosts = site_hosts(self.settings.searxng_sites)
        ctx = await self._hit_context(state)
        out: dict[str, Candidate] = {}
        model_task = asyncio.create_task(self._tsweb_round(state, queries, hosts, ctx)) if self.tsweb_on else None
        try:
            await self._searx_queries(state, queries, hosts, ctx, out)
        finally:
            if model_task is not None:
                for url, lead in (await model_task).items():
                    out.setdefault(url, lead)
        stats["searx_leads"] += len(out)
        return list(out.values())

    async def _tsweb_round(self, state: SearchState, queries: list[str], hosts: list[str], ctx: tuple) -> dict[str, Candidate]:
        """The same `site:` queries through the company model's web search tool, a few at a time. Never raises."""
        stats, s = state.stats, self.settings
        out: dict[str, Candidate] = {}
        left = max(0, s.techsara_web_searches_per_search - stats["tsweb_searches"])
        if left <= 0 or getattr(state, "tsweb_down", False):
            return out

        async def one(query: str) -> None:
            if getattr(state, "tsweb_down", False):
                return
            try:
                urls, note = await self.tsweb.web_search(query, s.techsara_web_search_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                urls, note = [], "request failed"
            stats["tsweb_searches"] += 1
            if note:
                stats["tsweb_failed"] += 1
                if stats["tsweb_failed"] >= 3 and not stats["tsweb_results"] and not getattr(state, "tsweb_down", False):
                    state.tsweb_down = True
                    state.warn(f"The company model's web search did not answer ({note}); the search went on without it.")
                return
            stats["tsweb_results"] += len(urls)
            self._hits_to_leads(state, urls, hosts, TECHSARA_WEB, ctx, out)

        await asyncio.gather(*(one(q) for q in queries[:left]))
        stats["tsweb_leads"] += len(out)
        return out

    async def _searx_queries(self, state: SearchState, queries: list[str], hosts: list[str], ctx: tuple, out: dict) -> None:
        stats = state.stats
        if not self.searx_on or getattr(state, "searx_down", False):
            return
        for query in queries:
            answer = await self.searx.search(query, self.settings.searxng_pages_per_query, useful=lambda u: extract(u, hosts) is not None)
            stats["searx_cached" if answer.cached else "searx_queries"] += 1
            if answer.error:
                stats["searx_failed"] += 1
                state.searx_failures = getattr(state, "searx_failures", 0) + 1
                if state.searx_failures >= 3:                          # slow or down: stop asking, the search goes on without it
                    state.searx_down = True
                    state.warn(f"Search-engine discovery (SearXNG) did not answer ({answer.error}); the search went on without it.")
                    break
                continue
            state.searx_failures = 0
            if answer.unresponsive:
                state.__dict__.setdefault("searx_unresponsive", set()).update(answer.unresponsive)
            stats["searx_results"] += len(answer.urls)
            self._hits_to_leads(state, answer.urls, hosts, SEARX, ctx, out)
            state.emit(state.stage or "search", "Asking the search engine for employer job boards")

    async def _hit_context(self, state: SearchState) -> tuple[set, set, set]:
        keys = await self._keys()
        known = {(src, key) for src, ks in keys.items() for key in ks} | {("workable", k) for k in getattr(self, "_accounts", [])}
        return known, await self.index.dead_boards(), state.__dict__.setdefault("searx_boards", set())

    def _hits_to_leads(self, state: SearchState, urls: list[str], hosts: list[str], provider: str, ctx: tuple, out: dict) -> None:
        """Result addresses of a search engine -> leads. Only well-formed addresses on the configured ATS hosts count;
        new boards are limited by the search's budget of new boards (shared by every search engine)."""
        known, dead, boards = ctx
        for url in urls:
            hit = extract(url, hosts)
            if hit is None:
                continue
            if hit.kind == "page":
                pages = state.__dict__.setdefault("searx_pages", set())
                if hit.url in pages or len(pages) < self.settings.searxng_page_leads_per_search:
                    pages.add(hit.url)
                    out.setdefault(hit.url, Candidate(url=hit.url, provider=provider, cited=True))
                continue
            pair = (hit.src, hit.board)
            if hit.src not in self.specs or pair in dead:
                continue
            if pair not in known and pair not in boards:
                if len(boards) >= state.plan.new_boards:      # budget of new boards for this search is used up
                    continue
                boards.add(pair)
            elif pair in known and hit.kind == "board":
                continue                                # a board the index already reads: nothing to do
            out.setdefault(hit.url, Candidate(url=hit.url, provider=provider, cited=True))

    async def _tiny_round(self, state: SearchState, queries: list[str]) -> list[Candidate]:
        """TinySearch (paid) as a second discovery engine: the same `site:` queries, the same handling of results."""
        stats = state.stats
        if not queries or not self.tiny_on or stats["tiny_skipped"]:
            return []
        hosts = site_hosts(self.settings.searxng_sites)
        ctx = await self._hit_context(state)
        out: dict[str, Candidate] = {}
        for query in queries:
            if stats["tiny_searches"] >= state.plan.tiny_searches:
                break
            urls, note = await self.tinyfish.search(query.replace('"', "") + " jobs")
            if note in ("daily spending ceiling reached", "wallet balance is at the reserve", "not configured"):
                stats["tiny_skipped"] = note
                state.warn(f"TinyFish was skipped: {note}.")
                break
            stats["tiny_searches"] += 1
            stats["tiny_usd"] = round(self.tinyfish.spent, 4)
            if note:
                state.tiny_failures = getattr(state, "tiny_failures", 0) + 1
                if state.tiny_failures >= 3:
                    stats["tiny_skipped"] = f"it did not answer ({note})"
                    state.warn(f"TinyFish did not answer ({note}); the search went on without it.")
                    break
                continue
            self._hits_to_leads(state, urls, hosts, TINYFISH, ctx, out)
            state.emit(state.stage or "search", "Asking a second search engine for employer job boards")
        return list(out.values())

    async def _serp_round(self, state: SearchState, pairs: list[tuple[str, str]], calls: int) -> tuple[list[Candidate], list[Job]]:
        """Google Jobs through SerpApi (paid, monthly allowance): up to `calls` result pages for the given
        (title, location) pairs. Returns (leads to verify, listed-only jobs). Never raises."""
        stats, s = state.stats, self.settings
        leads: dict[str, Candidate] = {}
        listed: list[Job] = []
        if calls <= 0 or not self.serp_on or stats["serp_skipped"]:
            return [], []
        hosts = aggregator_hosts(s.aggregator_hosts)
        seen: set = state.__dict__.setdefault("serp_seen", set())
        used = 0
        for title, location in pairs:
            token = ""
            for _ in range(max(1, s.serpapi_pages_per_query)):
                if used >= calls:
                    break
                items, token, note = await self.serpapi.search(title, location, token)
                if note == "budget":
                    left = await self.serpapi.remaining()
                    stats["serp_skipped"] = (f"this month's SerpApi allowance is used up ({await self.serpapi.used_this_month()} calls counted this "
                                             f"month, {s.serpapi_monthly_reserve} kept in reserve, {left} usable)")
                    state.warn(f"Google Jobs (SerpApi) was skipped: {stats['serp_skipped']}.")
                    return list(leads.values()), listed
                if note == "cached":
                    stats["serp_cached"] += 1
                else:
                    used += 1
                    stats["serp_calls"] += 1
                    if note:
                        state.warn(f"Google Jobs (SerpApi) did not answer one request ({note}).")
                        break
                fresh = 0
                for item in items:
                    key = (norm_company(str(item.get("company_name") or "")), " ".join(str(item.get("title") or "").lower().split()),
                           str(item.get("location") or "").lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    fresh += 1
                    stats["serp_jobs"] += 1
                    lead, job = serp_item(item, hosts, s.serpapi_include_listed_only)
                    if lead:
                        leads.setdefault(lead.url, lead)
                    elif job:
                        listed.append(job)
                state.emit(state.stage or "search", "Reading Google Jobs listings")
                if not token or fresh < 3:               # page deeper only while pages keep bringing new jobs
                    break
            if used >= calls:
                break
        stats["serp_listed"] += len(listed)
        return list(leads.values()), listed

    async def _aws_prepare(self, state: SearchState, req: SearchRequest, titles: list[str], locations: list[str]) -> None:
        """The query plan of AWS web search for one candidate search and its paced budget (units of the month)."""
        stats, s = state.stats, self.settings
        titles = list(dict.fromkeys(t for t in titles if t))[:8]
        locs = list(dict.fromkeys(["remote" if req.work_mode == "Remote" else "", *locations]))
        locs = [l for l in locs if l][:16] or ["United States"]
        pairs = []                                       # diagonal order: early calls cover different titles and places
        for d in range(len(titles) + len(locs) - 1):
            for i, title in enumerate(titles):
                if 0 <= d - i < len(locs):
                    pairs.append((title, locs[d - i]))
        state.aws_pairs, state.aws_index = pairs, 0
        state.aws_mode = {"Remote": "remote", "Hybrid": "hybrid", "On-site": "onsite"}.get(req.work_mode, "")
        try:
            remaining = await self.aws.quota.remaining(interactive=False)
        except Exception as exc:                        # the counter is unreadable: AWS is not used (never uncounted)
            stats["aws_skipped"] = f"the monthly usage counter could not be read ({type(exc).__name__})"
            state.warn(f"AWS web search was skipped: {stats['aws_skipped']}.")
            return
        stats["aws_budget"] = aws_web_search.paced_calls(state.plan.aws_calls, remaining, s.aws_web_search_expected_searches_per_day)
        if stats["aws_budget"] <= 0 and state.plan.aws_calls > 0:
            stats["aws_skipped"] = (f"this month's AWS web search allowance for searches is used up ({s.aws_web_search_monthly_limit} "
                                    f"per month, {s.aws_web_search_monthly_reserve} kept for the web search endpoint)")
            state.warn(f"AWS web search was skipped: {stats['aws_skipped']}.")

    async def _aws_round(self, state: SearchState, calls: int) -> list[Candidate]:
        """Up to `calls` AWS web search queries. Only result addresses are used, exactly like SearXNG results: a posting
        or board on an ATS this app reads becomes a lead that the lead check verifies against the employer's live
        feed; an employer page from the unfiltered query may become a page lead (the page itself decides). Titles,
        text and ranking of the results are dropped here. Never raises."""
        stats, s = state.stats, self.settings
        if calls <= 0 or not self.aws_on or stats["aws_skipped"]:
            return []
        hosts = site_hosts(s.searxng_sites)
        rotation: list = [*aws_web_search.host_groups(hosts), None]       # None: any site except job boards / aggregators
        exclude = [h for h in aggregator_hosts(s.aggregator_hosts)]
        ctx = await self._hit_context(state)
        seen: set = state.__dict__.setdefault("aws_seen", set())          # in memory, this search only
        out: dict[str, Candidate] = {}
        made = 0
        while made < calls and state.aws_pairs and stats["aws_queries"] < stats["aws_budget"]:
            title, location = state.aws_pairs.pop(0)
            group = rotation[state.aws_index % len(rotation)]
            state.aws_index += 1
            made += 1
            try:
                answer = await self.aws.search(aws_web_search.query_text(title, location, state.aws_mode),
                                               include=group, exclude=None if group else exclude)
            except AwsWebSearchError as err:
                stats["aws_queries"] += getattr(err, "queries", 0)
                if err.kind == "budget":
                    stats["aws_skipped"] = "this month's AWS web search allowance is used up"
                elif err.kind in ("access_denied", "not_found", "credentials", "disabled", "validation", "counter"):
                    stats["aws_skipped"] = err.message
                else:
                    state.aws_failures = getattr(state, "aws_failures", 0) + 1
                    if state.aws_failures >= 3:
                        stats["aws_skipped"] = f"it did not answer ({err.message})"
                if stats["aws_skipped"]:
                    state.warn(f"AWS web search was skipped: {stats['aws_skipped']}.")
                    break
                continue
            state.aws_failures = 0
            stats["aws_queries"] += answer.queries
            stats["aws_results"] += len(answer.results)
            urls: list[str] = []
            for result in answer.results:
                if result.url in seen:
                    stats["aws_duplicates"] += 1
                    continue
                seen.add(result.url)
                if extract(result.url, hosts) is not None:
                    urls.append(result.url)
                elif group is None and employer_posting_address(result.url, aggregator_hosts(s.aggregator_hosts)):
                    pages = state.__dict__.setdefault("searx_pages", set())
                    if len(pages) < s.searxng_page_leads_per_search:
                        pages.add(result.url)
                        out.setdefault(result.url, Candidate(url=result.url, provider=AWS, cited=True))
                else:
                    stats["aws_generic"] += 1                 # a search, category or aggregator page: never a vacancy
            self._hits_to_leads(state, urls, hosts, AWS, ctx, out)
            del answer                                        # nothing of the response outlives this loop
            state.emit(state.stage or "search", "Asking AWS web search for employer job postings")
        stats["aws_leads"] += len(out)
        return list(out.values())

    def _more_discovery(self, state: SearchState, ai, use_web: bool, next_round: int, queries_left: int) -> bool:
        plan = state.plan
        ai_more = use_web and hasattr(ai, "discover_round") and any(
            getattr(ai, name, None) is not None and plan.per_round(total, next_round) > 0
            for name, total in (("openai", plan.openai_calls), ("claude", plan.claude_calls)))
        searx_more = queries_left > 0 and ((self.searx_on and not getattr(state, "searx_down", False))
                                           or (self.tsweb_on and not getattr(state, "tsweb_down", False)
                                               and state.stats["tsweb_searches"] < self.settings.techsara_web_searches_per_search))
        stats = state.stats
        tiny_more = self.tiny_on and not stats["tiny_skipped"] and stats["tiny_searches"] < plan.tiny_searches and getattr(state, "tiny_left", 0) > 0
        serp_more = self.serp_on and not stats["serp_skipped"] and stats["serp_calls"] < plan.serp_calls and getattr(state, "serp_left", 0) > 0
        aws_more = (self.aws_on and not stats["aws_skipped"] and stats["aws_queries"] < stats["aws_budget"]
                    and bool(getattr(state, "aws_pairs", None)))
        return bool(ai_more or searx_more or tiny_more or serp_more or aws_more)

    # ------------------------------------------------------------ pipeline
    async def _pipeline(self, state, req, resume_text, ai, target, base_profile, related0,
                        profile_task, discover_task, refresh_task, expand_task=None, known_names=()) -> None:
        stats, plan = state.stats, state.plan
        wanted = {t.lower() for t in req.titles}
        use_web = ai.available and req.deep_search
        preview_size = max(300, target * 3)             # previews only need the head of the list (memory)

        # 1. Preliminary results from what the index already holds (no network).
        state.emit("titles", "Identifying relevant job titles")
        early = await self._collect(req, related0, {}, max_age=self.settings.index_stale_ok_seconds, details=False)
        if early:
            early.sort(key=lambda j: -j.title_relevance)
            early, _ = await asyncio.to_thread(deduplicate, early[:preview_size])
            self._score(early, base_profile, req, related0)
            early.sort(key=self._order)
            state.publish(self._select(early, target), partial=True)
            state.mark("first_results")
            state.emit("titles", "Preliminary matches ready")
        del early

        profile = (await profile_task) if profile_task else base_profile
        related = [t for t in (profile.related_titles or related0) if t.lower() not in wanted][:8]
        variants: list[str] = []
        if expand_task is not None:
            try:
                variants = await asyncio.wait_for(expand_task, timeout=min(45.0, self.settings.ai_timeout_seconds))
            except asyncio.TimeoutError:
                state.warn("Title expansion did not answer in time; the search uses the requested titles only.")
            # A variant widens what is found; every posting is still screened and scored against the candidate.
            variants = clean_variants(variants, [*req.titles, *related], self.settings.title_expansion_max)
        stats["title_variants"] = len(variants)
        state.title_variants = variants
        related = [*related, *variants]
        state.related_titles = related

        # Search-engine discovery: the query plan of the whole search, handed out round by round.
        locations = discovery_locations(req.locations)
        all_queries = build_queries([*req.titles, *variants, *related[:8]], site_hosts(self.settings.searxng_sites), locations)
        queries = all_queries[: plan.searx_queries] if self.engines_on else []
        used = plan.per_round(plan.searx_queries, 1)
        searx_task = asyncio.create_task(self._searx_round(state, queries[:used])) if queries else None
        if searx_task:
            state.side_tasks.append(searx_task)
        queries = queries[used:]
        # Paid sources wait for round 2: they are spent only when the free ones left the search short.
        # TinySearch gets the queries SearXNG is not going to send; SerpApi gets title x location pairs.
        tiny_queries = (all_queries[len(queries) + used:] or all_queries)[: plan.tiny_searches] if self.tiny_on else []
        places = [f"{p}, United States" for p in req.locations[:6]] or ["United States"]
        serp_pairs = [(t, p) for p in places for t in [*req.titles, *variants]] if self.serp_on else []
        state.tiny_left, state.serp_left = len(tiny_queries), len(serp_pairs)
        # AWS web search runs from round 1 (fast, own monthly allowance): title variants x US locations, paced.
        aws_task = None
        if self.aws_on:
            await self._aws_prepare(state, req, [*req.titles, *variants], locations)
            first = plan.per_round(stats["aws_budget"], 1)
            if first:
                aws_task = asyncio.create_task(self._aws_round(state, first))
                state.side_tasks.append(aws_task)

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
            head = sorted(kept, key=lambda j: -j.title_relevance)[:preview_size]
            preview, _ = await asyncio.to_thread(deduplicate, [j.model_copy(deep=True) for j in head])
            self._score(preview, profile, req, related)
            preview.sort(key=self._order)
            state.publish(self._select(preview, target), partial=True)
            if "first_results" not in state.timings:
                state.mark("first_results")
            state.emit("boards", "Preliminary matches updated")
            del head, preview

        # 3. Discovery rounds. Each round: verify the leads of that round against sources, pages, browser and AI
        # extraction; screen the index again (boards registered by the round are in it now); count verified matches.
        # Another round starts only while the count is below the target and budgets remain.
        state.emit("verify", "Verifying application links")
        ai_leads: list[Candidate] = []
        if discover_task:
            try:
                ai_leads = await asyncio.wait_for(discover_task, timeout=self.settings.ai_discovery_deadline_seconds)
            except asyncio.TimeoutError:
                state.warn("AI web search hit its time limit; results use the sources already read.")
        searx_leads: list[Candidate] = (await searx_task) if searx_task else []
        if aws_task:
            searx_leads = [*searx_leads, *(await aws_task)]
        expected: dict[str, set[str]] = {}
        lead_jobs: list[Job] = []
        seen_leads: set[str] = set()
        lead_counts = dict.fromkeys(REJECT_KEYS, 0)
        lead_kept: list[Job] = []
        round_no, previous, round_queries = 1, None, used if searx_task else 0
        stale = 0
        listed_new: list[Job] = []
        while True:
            stats["round"] = round_no
            stats["ai_leads"] += len(ai_leads)
            fresh: list[Candidate] = []
            for lead in [*ai_leads, *searx_leads]:          # a lead is checked once per search, whoever names it again
                if lead.url not in seen_leads:
                    seen_leads.add(lead.url)
                    fresh.append(lead)
            boards_before, postings_before = stats["new_boards"], stats["new_board_postings"]
            found, jobs = await self._resolve_leads(state, req, related, fresh, ai)
            for key, providers in found.items():
                expected.setdefault(key, set()).update(providers)
            lead_jobs.extend(jobs)
            lead_jobs.extend(listed_new)                    # Google Jobs listings without an employer link (marked as such)
            listed_new = []
            lead_counts = dict.fromkeys(REJECT_KEYS, 0)
            if lead_jobs:
                relevant = []
                for job in lead_jobs:
                    job.title_relevance = best_title_relevance(job.title, req.titles, related)
                    job.matched_query = best_matching_title(job.title, req.titles, related)
                    if job.ats == "workday" and AI_PROVIDERS & {s.replace(" web search", "") for s in job.sources}:
                        job.title_relevance = max(job.title_relevance, AI_LEAD_RELEVANCE_FLOOR)
                    if job.title_relevance < MIN_RELEVANCE:
                        lead_counts["rejected_not_relevant"] += 1
                    else:
                        relevant.append(job)
                lead_kept, screen_counts, _ = await asyncio.to_thread(self._screen, relevant, req)
                for key, value in screen_counts.items():
                    lead_counts[key] += value
            if fresh:
                state.detail_wait = min(state.detail_wait, 6.0)      # second pass: do not wait long for paced details again
                kept = await self._collect(req, related, expected, counts=counts, state=state)   # lead boards are in the index now
                await self._source_report(state, failed, pending)
            unique, removed = await asyncio.to_thread(deduplicate, [*kept, *lead_kept])
            # What counts towards the target: verified jobs whose keyword score reaches the minimum match score. A job
            # that only shares a title word is not progress (a loose title variant can match hundreds of those).
            self._score(unique, profile, req, related)
            bar = self.settings.default_min_match_score
            matches = sum(1 for j in unique if j.score >= bar)
            gained = matches - (previous or 0)
            stats["unique_candidates"] = stats["verified_active"] = len(unique)
            stats["rounds"].append({"round": round_no, "matches": matches, "verified": len(unique), "gained": gained, "leads": len(fresh),
                                    "searx_queries": round_queries, "new_boards": stats["new_boards"] - boards_before,
                                    "new_board_postings": stats["new_board_postings"] - postings_before,
                                    "seconds": round(time.time() - state.created, 1)})
            state.emit("verify", f"Round {round_no}: {matches} verified jobs at or above the minimum score so far "
                                 f"({len(unique)} verified with a matching title)")
            elapsed = time.time() - state.created
            stale = stale + 1 if (round_no > 1 and gained <= 0) else 0
            need = max(1, self.settings.search_stale_rounds)
            if matches >= target:
                reason = "the requested number of verified matching jobs was reached (by keyword score, before the final ranking)"
            elif stale >= need:
                reason = (f"round {round_no} added no new matching job" if need == 1 else
                          f"{need} rounds in a row added no new matching job")
            elif not self._more_discovery(state, ai, use_web, round_no + 1, len(queries)):
                reason = ("every discovery budget of this search is used up or switched off (AI web search, search-engine "
                          "discovery, paid sources); all indexed boards were read")
            elif elapsed >= self.settings.search_max_seconds:
                reason = f"the time ceiling of one search ({self.settings.search_max_seconds:g} s) was reached"
            elif round_no >= max(1, self.settings.search_max_rounds):
                reason = f"the safety limit of {self.settings.search_max_rounds} rounds was reached"
            else:
                reason = ""
            if reason:
                stats["stop_reason"] = reason
                break
            previous = matches
            round_no += 1
            stats["round"] = round_no
            state.emit("search", f"Round {round_no}: searching further title variants and locations")
            used = plan.per_round(plan.searx_queries, round_no)
            round_queries = min(used, len(queries))
            names = sorted({k.split("|")[0] for ks in (await self._keys()).values() for k in ks})

            async def more_ai() -> list[Candidate]:
                if not (use_web and hasattr(ai, "discover_round")):
                    return []
                try:
                    return await asyncio.wait_for(
                        ai.discover_round(round_no, profile, req, [*req.titles, *variants, *related[:8]], locations, names),
                        timeout=self.settings.ai_discovery_deadline_seconds)
                except asyncio.TimeoutError:
                    state.warn("AI web search hit its time limit in a later round; the round used what had arrived.")
                    return []

            tiny_n = plan.per_round(plan.tiny_searches, round_no - 1)       # paid sources start in round 2
            serp_n = plan.per_round(plan.serp_calls, round_no - 1)
            aws_n = plan.per_round(stats["aws_budget"], round_no) if self.aws_on else 0
            ai_leads, searx_leads, tiny_leads, (serp_leads, listed_new), aws_leads = await asyncio.gather(
                more_ai(), self._searx_round(state, queries[:used]), self._tiny_round(state, tiny_queries[:tiny_n]),
                self._serp_round(state, serp_pairs[: max(1, serp_n)], serp_n), self._aws_round(state, aws_n))
            queries, tiny_queries, serp_pairs = queries[used:], tiny_queries[tiny_n:], serp_pairs[max(1, serp_n):]
            state.tiny_left, state.serp_left = len(tiny_queries), len(serp_pairs)
            searx_leads = [*searx_leads, *tiny_leads, *serp_leads, *aws_leads]
            state.emit("verify", f"Round {round_no}: verifying application links")

        state.emit("verify", f"Search stopped after {round_no} round(s): {stats['stop_reason']}")
        if round_no > 1 or (matches < target and (use_web or self.searx_on or self.serp_on or self.tiny_on)
                            and self.settings.search_max_rounds > 1):
            state.warn(f"The search ran {round_no} round(s) and stopped because {stats['stop_reason']}.")
        if self.searx_on:
            down = sorted(getattr(state, "searx_unresponsive", ()))
            if stats["searx_queries"] + stats["searx_cached"] and not stats["searx_results"] and not getattr(state, "searx_down", False):
                state.warn("Search-engine discovery (SearXNG) returned no results"
                           + (f" (engines not answering: {', '.join(down[:4])})" if down else "") + "; the search went on without it.")
        index = await self.index.index_stats()
        stats["total_discovered"] = index["fresh_postings"] + stats["ai_leads"] + stats["searx_leads"] + stats["crawled_links"]
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
        stats["rejected_duplicates"] += removed
        stats["unique_candidates"] = len(unique)
        stats["verified_active"] = len(unique)
        state.emit("rank", "Ranking resume matches")
        self._score(unique, profile, req, related)
        unique.sort(key=self._order)

        # 5. AI ranking (parallel batches) and audit, side by side.
        if (ai.available or getattr(ai, "can_rerank", False)) and unique:
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
        if round_no > 1:
            state.message += f" The search ran {round_no} rounds and stopped because {stats['stop_reason']}."
        state.status = "completed"
        state.publish(final, partial=False)

    async def _ai_rank(self, state: SearchState, req: SearchRequest, resume_text: str, ai: AIOrchestrator,
                       profile: Profile, unique: list[Job], target: int) -> list[Job]:
        """Scoring in three layers, all on the one 0-100 scale (see job_ranker):
        1. the keyword score of every job (already done by the caller);
        2. with the company model: its reranker reads every job against the profile;
        3. OpenAI / Claude judge the top slice with a written explanation; their scores are also the anchors that
           put the reranker's relevance on the 0-100 scale for all other jobs."""
        stats, plan, s = state.stats, state.plan, self.settings
        size_limit = plan.ai_ranked if plan else s.max_ai_ranked_jobs
        pool_size = min(size_limit, max(target * 2, target + 30)) if ai.available else 0
        raw: dict[str, float] = {}
        if getattr(ai, "can_rerank", False):
            state.emit("rank", "Scoring every candidate job with the company model")
            raw = await ai.rerank(profile, req, unique[: max(0, s.techsara_rerank_max_jobs)])
            if len(raw) < MIN_ANCHORS:
                if unique[:1] and len(unique) >= MIN_ANCHORS:
                    state.warn("The company model did not answer; ranking used OpenAI / Claude for the top jobs and keyword scores for the rest.")
                raw = {}
        if raw:
            # The judged slice: the jobs the reranker likes best, plus a sample spread over all the others so that
            # the calibration has anchors across the whole range (not only among good matches).
            ordered = sorted((j for j in unique if j.id in raw), key=lambda j: (-raw[j.id], self._order(j)))
            sample = min(len(ordered), max(MIN_ANCHORS, pool_size // 8)) if pool_size else 0
            pool = self._select(ordered, max(0, pool_size - sample))
            chosen = {j.id for j in pool}
            rest = [j for j in ordered if j.id not in chosen]
            if rest and sample:
                step = len(rest) / min(sample, len(rest))
                pool.extend(rest[int(i * step)] for i in range(min(sample, len(rest))))
        else:
            # The AI pool is spread across employers too, so calls are not spent on near-identical postings.
            pool = self._select(unique, pool_size)
        size = max(1, s.ai_rank_batch_size)
        batches = [pool[i:i + size] for i in range(0, len(pool), size)]
        sem = asyncio.Semaphore(max(1, s.ai_rank_concurrency))
        # The audit only needs posting text, so it runs at the same time as ranking.
        to_audit = [j for j in pool if j.title_relevance < 0.85 or authorization_snippets(j.description, 1)]

        async def no_audit() -> dict:
            return {}

        audit_task = asyncio.create_task(ai.audit(req, to_audit[: (plan.audit_jobs if plan else s.max_audit_jobs)]) if ai.available else no_audit())

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
        if raw:
            await self._company_scores(state, req, resume_text, ai, profile, unique, raw)
        elif ai.available and len(unique) > stats["ai_ranked"]:
            note = f" Keyword-based score: this job was not among the {stats['ai_ranked']} jobs judged by AI in this search."
            for job in unique:
                if job.score_source == "heuristic" and note not in job.reason:
                    job.reason = (job.reason + note)[:500]
        unique.sort(key=self._order)
        return unique

    async def _company_scores(self, state: SearchState, req: SearchRequest, resume_text: str, ai, profile: Profile,
                              unique: list[Job], raw: dict[str, float]) -> None:
        """Put the reranker's relevance on the 0-100 scale and give the jobs it scored a written explanation."""
        stats, s = state.stats, self.settings
        anchors = [(raw[j.id], j.score) for j in unique if j.score_source == "ai" and j.id in raw]
        reference = "OpenAI or Claude judged"
        to_scale = fit_calibration(anchors)
        mapped: dict[str, int] = {}
        if to_scale is None:
            # No AI judgements to compare with: the keyword scores of these jobs are handed out again in the order of
            # the model's relevance, so exactly as many jobs pass the minimum score as with keyword scoring alone.
            pool = [(j.id, raw[j.id], j.score) for j in unique if j.score_source == "heuristic" and j.id in raw]
            if len(pool) < MIN_ANCHORS:
                state.warn("Too few jobs to calibrate the company model's scores; keyword scores were kept.")
                return
            mapped, anchors = redistribute_scores(pool), pool
            reference = "have a keyword score (the same keyword scores, handed out in the order of the model's relevance)"
        scored: list[Job] = []
        for job in unique:
            if job.id in raw and job.score_source != "ai":
                score = to_scale(raw[job.id]) if to_scale is not None else mapped[job.id]
                apply_model_score(job, raw[job.id], score, len(anchors), reference)
                scored.append(job)
        stats["company_model_scored"] = len(scored)
        if not scored or s.techsara_explain_max_jobs <= 0 or not hasattr(ai, "explain_batch"):
            return
        state.emit("rank", "Writing explanations with the company model")
        scored.sort(key=lambda j: -j.score)
        todo = scored[: s.techsara_explain_max_jobs]
        size = max(1, s.techsara_explain_batch)
        stop_at = time.monotonic() + max(0.0, s.techsara_explain_seconds)
        sem = asyncio.Semaphore(max(1, s.techsara_concurrency))

        async def explain(batch: list[Job]) -> None:
            async with sem:
                if time.monotonic() >= stop_at:      # out of time: these keep the rerank note as their explanation
                    return
                rankings = await ai.explain_batch(profile, req, batch)
            for job in batch:
                if job.id in rankings:
                    apply_explanation(job, rankings[job.id], profile, resume_text)
                    stats["company_model_explained"] += 1

        await asyncio.gather(*(explain(todo[i:i + size]) for i in range(0, len(todo), size)))

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
