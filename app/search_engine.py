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
import itertools
import math
import re
import time
import zlib
from datetime import datetime, timezone
from typing import Callable

import httpx

from . import board_catalog, job_verifier
from .ai_orchestrator import AIOrchestrator
from .browser_pool import BrowserPool
from .config import Settings
from .database import Database, pack, unpack
from .job_index import as_index
from .deduplicator import deduplicate
from .job_normalizer import (ENRICH_VERSION, ats_key, authorization_snippets, canonical_url, enrich_job, norm_company,
                             parse_ats, us_location_status)
from .job_ranker import (MIN_ANCHORS, SCORER_ORDER, apply_ai_ranking, apply_explanation, apply_model_score,
                         best_matching_title, best_title_relevance, evaluate_eligibility, fit_calibration, heuristic_score,
                         redistribute_scores,
                         index_tokens, query_tokens, static_related_titles, work_mode_rejects)
from .job_verifier import Budget, PageVerifier, job_from_extraction
from .rank_cache import RankCache
from . import career_sites
from .providers import registry, workday
from .providers.base import BoardNotFound, Ctx, HostLimiter, RobotsCache, SourceError, now_iso
from .providers.boards import seed_pairs
from .providers import aws_web_search
from .providers.aws_web_search import AWS, AwsWebSearch, AwsWebSearchError, employer_posting_address
from .providers.paid import (GOOGLE_JOBS, TINYFISH, SerpApiClient, TinyFishClient, aggregator_hosts, serp_item)
from .providers.registry import BOARD_LEAD_SOURCES, SPECS, Unit
from .providers.searxng import SearxClient, build_queries, extract, site_hosts, valid_board
from .providers.ollama_search import OllamaSearch
from .providers.techsara_provider import TechsaraProvider
from .resume_parser import heuristic_profile
from .schemas import Candidate, Job, Profile, SearchRequest
from .search_plan import Plan, QueryLedger, cached_variants, clean_variants, discovery_locations, plan_for, store_variants
from .search_scope import (NO_SCOPE, OTHER, Scope, bump, count_platforms, host_reader, platform_of_job, platform_of_url,
                           platform_stats)

MIN_RELEVANCE = 0.6
AI_LEAD_RELEVANCE_FLOOR = 0.6
HOST_LIMITS = {"greenhouse": 10, "ashby": 8, "lever": 6, "workday": 1, "workable": 4, "amazon": 2,
               "remote-boards": 2, "adzuna": 2, "smartrecruiters": 2, "bamboohr": 2,
               "recruitee": 2, "pinpoint": 2, "rippling": 2}
# Workday answers HTTP 429 when all its employers together get more than a slow trickle of requests.
# One request every 1.5 s; after any 429 the source is left alone for ten minutes (providers/base.py).
# The startup ATS readers (providers/ats_startup.py) are read at one request per second per platform.
HOST_INTERVALS = {"workday": 1.5, "recruitee": 1.0, "pinpoint": 1.0, "rippling": 1.0}
PACED_WAIT_SECONDS, DETAIL_WAIT_SECONDS = 25.0, 20.0            # deep search
FAST_PACED_WAIT_SECONDS, FAST_DETAIL_WAIT_SECONDS = 2.0, 6.0   # fast search
def paced_unit(unit: Unit) -> bool:
    """Whether the unit's source is read at about one request a second (see PACED_UNITS_PER_SEARCH)."""
    spec = SPECS.get(unit.src)
    return unit.src in HOST_INTERVALS or bool(spec and spec.wait) or (unit.src == "workable" and unit.key != "*")


AI_PROVIDERS = {"OpenAI", "Claude"}          # only a lead a model named gets the relevance floor; a search-engine hit does not
SEARX = "SearXNG"
TECHSARA_WEB = "TechSara web search"
OLLAMA = "Ollama web search"
# Sources whose limiter lets through about one request a second (Workday, Recruitee, Workable accounts, ...). A search
# waits only a short while for them and asks for at most this many stale units of each per search, its best title
# matches first (_reading_plan); the rest of their boards is read by the background catalogue reader.
PACED_UNITS_PER_SEARCH = 40
CAREER_PAGE = registry.CAREER_PAGE           # remembered postings on employers' own sites (see registry and _remember_pages)
CAREER_PAGES_PER_SEARCH = 200                # how many of them one search checks again: those whose title fits, one page request each
CAREER_PAGE_SOURCE = "Company career site"   # Job.source of a posting read from that memory
RELEVANT_READ_SECONDS = 3 * 86400.0          # how old a board's last read may be to say which titles the board lists
WORKDAY_NEW_TENANTS_PER_ROUND = 6           # Workday is paced (one request every 1.5 s): few new tenants are queried while a search waits
# Open-web queries of the plain search engines (no `site:`): the two forms that returned postings on employers' own
# sites in the measurement of 2026-11-21 (`intitle:` / `inurl:` forms returned none that passed the address test).
OPEN_WEB_QUERY_FORMS = ('"{title}" job opening {place} -linkedin -indeed -glassdoor -ziprecruiter', '"{title}" careers apply {place}')
PROBES_PER_SITE = 2                        # page leads of one site that are checked before the site has shown what it serves
PROBE_SECONDS = 8.0                         # how long those first checks are waited for before the other leads start
_SITE_REFUSES = re.compile(r"Page returned HTTP (401|403|429)|Disallowed by robots\.txt|Source does not permit")
MIN_EXTRACTION_SECONDS = 4.0                # the AI page check is not started with less of the lead check's time box left
KNOWN_EMPLOYERS_WAIT_SECONDS = 8.0          # how long the first AI web search waits to learn which employers the index already has matches for
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
            # search-engine queries of this search by normalised query and host (search_plan.QueryLedger): how many were
            # asked, by which engine, how many an engine left out because another had asked them, and second chances
            "query_ledger": {},
            # leads on a career platform that may not be read (or whose reader is off): dropped before any request
            "leads_not_readable": 0,
            "gained_by_source": {}, "company_model_scored": 0, "company_model_explained": 0, "ai_calls": 0,
            # ranking stage: seconds it took, and model answers of earlier searches that were used again (rank_cache.py)
            "rank_seconds": None, "rerank_sent": 0, "reranked": 0, "ai_ranked_reused": 0, "rank_reused": {},
            # postings verified on employers' own sites in this search and kept in the index (source "career_page")
            "pages_remembered": 0,
            # lead check: its wall time (all rounds), page checks still running at the time box, what those verified
            # afterwards and how many of them were remembered, browser / AI page checks the time box cut
            "lead_check_seconds": 0.0, "pages_late": 0, "pages_late_verified": 0, "pages_late_remembered": 0, "page_checks_cut": 0,
            "pages_site_refused": 0,                          # page leads not requested because their site had refused its first pages
            # paid sources
            "serp_calls": 0, "serp_cached": 0, "serp_jobs": 0, "serp_listed": 0, "serp_skipped": "",
            "tiny_searches": 0, "tiny_fetches": 0, "tiny_usd": 0.0, "tiny_skipped": "",
            "ollama_searches": 0, "ollama_results": 0, "ollama_leads": 0, "ollama_failed": 0, "ollama_skipped": "", "query_ideas": 0,
            # AWS web search (queries = units of the monthly allowance; result text is never kept)
            "aws_budget": 0, "aws_queries": 0, "aws_results": 0, "aws_duplicates": 0, "aws_generic": 0, "aws_leads": 0,
            "aws_open_queries": 0, "aws_open_leads": 0,       # its "any site" queries and the employer-site page leads they gave
            # open-web queries (no `site:`) of the plain search engines and the employer-site page leads they gave
            "open_engine_queries": 0, "open_engine_results": 0, "open_engine_leads": 0,
            "aws_skipped": "",
            # ATS platform filter, startup focus and where the jobs came from (search_scope.py). Counts only.
            "platform_filter": [], "startup_focus": False, "filtered_out": 0, "platforms": {}, "providers": {},
            "coverage_warnings": [], "seconds_to_first_verified": None, "total_seconds": None}


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


def _scope_of(state) -> Scope:
    """The platform filter / startup focus of a running search (none for a state that was not started by `run`)."""
    return getattr(state, "scope", None) or NO_SCOPE


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
    if job_verifier.never_fetched(url):            # a host that is never fetched (job boards, restricted career platforms)
        raise job_verifier.UnsafeURL("Host does not permit automated access")      # is not contacted from inside a rendered page either
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
        # Model judgements of (candidate, posting text) pairs, shared by every search of this process (rank_cache.py).
        self.rank_cache = RankCache(self.index, max(0.0, settings.rank_cache_days) * 86400.0)
        specs = registry.enabled_specs(settings)
        self.specs = specs if sources is None else {n: s for n, s in specs.items() if n in sources}
        self.limiter = HostLimiter(default=4, limits=HOST_LIMITS, intervals=HOST_INTERVALS)
        self.ctx = Ctx(self.client, self.limiter, RobotsCache(), settings.http_timeout_seconds, options={
            "workday_pages": settings.workday_pages, "rippling_pages": getattr(settings, "rippling_pages", 20), "adzuna_app_id": settings.adzuna_app_id,
            "adzuna_app_key": settings.adzuna_app_key})
        self.browser = browser if browser is not None else BrowserPool(
            settings.browser_concurrency, enabled=settings.browser_enabled, guard=_guard)
        self.verifier = PageVerifier(self.client, browser=self.browser)
        self.ctx.options["career_page_reader"] = self._read_career_page
        # The site of a remembered posting is read for its other openings, in the background only (career_sites.py).
        self.career_sites = career_sites.Crawler.for_engine(self)
        self._page_gate = asyncio.Semaphore(24)      # remembered pages checked at the same time (the verifier paces each host itself)
        self.searx = SearxClient(settings.searxng_url, self.client, self.index, settings.searxng_timeout_seconds,
                                 settings.searxng_min_interval_seconds, settings.searxng_cache_hours)
        self.tsweb = (TechsaraProvider(settings.techsara_api_key, settings.techsara_base_url, settings.techsara_model,
                                       settings.techsara_rerank_model, client=self.client,
                                       web_concurrency=settings.techsara_web_search_concurrency)
                      if settings.techsara_configured and settings.techsara_web_search_enabled else None)
        self._unit_counts: dict[Unit, int] = {}      # postings of units read by this process (statistics of new boards)
        self.serpapi = SerpApiClient(settings.serpapi_api_key, self.client, self.index, settings.serpapi_monthly_limit,
                                     settings.serpapi_monthly_reserve, settings.serpapi_cache_hours, settings.serpapi_account_check_seconds)
        self.ollama = OllamaSearch(settings.ollama_api_key, self.client, concurrency=settings.ollama_web_search_concurrency)
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
            if (src, key) not in dead and src in self.specs and self.specs[src].keyed and src != CAREER_PAGE:
                out.setdefault(src, []).append(key)
        # Remembered postings on employers' own sites are addresses, not employer boards: kept apart like the accounts below.
        self._pages = [key for src, key in known if src == CAREER_PAGE and (src, key) not in dead and src in self.specs]
        # Workable employer accounts found by discovery: Workable is otherwise read by query, so they are kept apart.
        self._accounts = [key for src, key in known if src == "workable" and key != "*" and (src, key) not in dead and src in self.specs]
        return out

    def _account_units(self) -> list[Unit]:
        return [Unit("workable", key, "") for key in getattr(self, "_accounts", [])]

    def _page_units(self) -> list[Unit]:
        return [Unit(CAREER_PAGE, key, "") for key in getattr(self, "_pages", [])]

    async def _read_career_page(self, url: str) -> list[Job]:
        """Check one remembered posting again on its own page: a plain request, no browser and no model, with what the
        portal knew about it (title, employer, place) as the claim the page must bear out. An empty answer means the
        page no longer proves an open posting, and the posting leaves the index; a page that could not be reached is a
        failed read and is tried again later."""
        claim = Candidate(url=url, provider=CAREER_PAGE_SOURCE)
        try:
            stored = await self.index.unit_rows(CAREER_PAGE, url, "")
            for _sig, data in stored.values():
                old = unpack(data)
                claim = Candidate(url=url, provider=CAREER_PAGE_SOURCE, title=old.title, company=old.company, location=old.location)
                break
        except Exception:
            pass
        async with self._page_gate:
            verdict = await self.verifier.verify(claim)
        if verdict.job is not None:
            return [verdict.job]
        reason = verdict.reason or ""
        if reason.startswith(("Page could not be fetched", "The site's robots.txt could not be read", "URL rejected")) \
                or re.match(r"Page returned HTTP (5\d\d|429|403|202)", reason):
            raise SourceError(reason)                    # not an answer about the posting: tried again later
        return []

    async def _remember_pages(self, jobs: list[Job]) -> int:
        """Keep the postings a search verified on a page of the employer's own site (or of a platform without a board
        reader) by a plain request. Later searches check them again on their page and find them without a web search;
        the background reader keeps them current. Pages that needed a browser are not kept: checking them again would
        cost a browser page each."""
        if CAREER_PAGE not in self.specs:
            return 0
        kept = 0
        ttl = registry.ttl(SPECS[CAREER_PAGE], self.settings)
        for job in jobs:
            url = job.url or ""
            if job.ats in self.specs or not url.startswith("https://") or len(url) > 600 or job_verifier.never_fetched(url) \
                    or not (job.verification or "").startswith("Posting page fetched") or not job.title or not job.company \
                    or not board_catalog.us_postings([job]):         # the portal searches US jobs: a posting elsewhere is not kept
                continue
            unit = Unit(CAREER_PAGE, url, "")
            try:
                await self._store_unit(unit, [job], ttl)
                # Recorded like an imported board ("catalog"): the background reader then reads it again once a day, and
                # a page that no longer lists a posting drops out of the known boards by itself (index.known_boards).
                await self.index.record_board(CAREER_PAGE, url, True, 1, "catalog")
                await board_catalog.note_read(self.index, unit, [job])
                kept += 1
                if self.career_sites is not None:
                    await self.career_sites.note_posting(job)       # one INSERT for a host seen for the first time; never raises
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
        return kept

    @property
    def searx_on(self) -> bool:
        return self.searx.enabled and "searxng" in self.specs

    @property
    def tsweb_on(self) -> bool:
        return self.tsweb is not None and "techsara_web" in self.specs

    @property
    def engines_on(self) -> bool:
        """Whether any engine that takes the `site:` queries of a search is available."""
        return self.searx_on or self.tsweb_on or self.ollama_on

    @property
    def serp_on(self) -> bool:
        return self.serpapi.enabled and "serpapi" in self.specs

    @property
    def ollama_on(self) -> bool:
        return self.ollama.enabled and "ollama_web" in self.specs

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
                await board_catalog.note_read(self.index, unit, jobs)      # US postings found: orders the catalogue reads
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
        await board_catalog.note_read(self.index, unit, None)              # a miss: the catalogue retries it later and later
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
        limit = {t: start + (min(deadline, (SPECS[u.src].wait or PACED_WAIT_SECONDS) if paced_wait is None else paced_wait)
                             if paced_unit(u) else deadline)
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
            await self._ensure(registry.within_budget(units, self.settings.max_boards_per_search), deadline=3600.0)

        task = asyncio.create_task(go())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        # Boards imported with `python -m app.portal.import_boards` are read little by little in the background
        # (docs/board-sources.md). Nothing happens until such an import was run; BOARD_CATALOG_TRICKLE=0 switches it off.
        board_catalog.start_catalog_trickle(self)

    def _queries(self, titles: list[str], related: list[str]) -> list[str]:
        n = max(1, self.settings.search_queries_per_search)
        out = [" ".join(t.lower().split()) for t in titles[:n]]
        if len(out) < n and related:
            out.append(" ".join(related[0].lower().split()))
        return list(dict.fromkeys(out))[:n]

    async def _source_report(self, state: SearchState, failed: list[tuple[Unit, str]], pending: list[Unit]) -> None:
        index = await self.index.index_stats()
        scope = _scope_of(state)
        report: dict[str, dict] = {}

        def entry(src: str) -> dict:
            label = SPECS[src].label if src in SPECS else src
            return report.setdefault(label, {"boards_ok": 0, "boards_failed": 0, "pending": 0, "postings": 0, "errors": []})

        for src, info in index["by_source"].items():
            if not scope.allows_source(src):            # platform filter: the other sources were not read for this search
                continue
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
        state.stats["total_discovered"] = self._fresh_postings(index, scope) + state.stats["ai_leads"] + state.stats["crawled_links"]
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

    @staticmethod
    def _fresh_postings(index: dict, scope: Scope) -> int:
        """Fresh postings in the index that this search could use (under a platform filter: of the chosen platforms)."""
        if not scope.filtered:
            return index["fresh_postings"]
        return sum(info["postings"] for src, info in index["by_source"].items() if scope.allows_source(src))

    # ------------------------------------------------------------ candidates
    def _screen(self, jobs: list[Job], req: SearchRequest, by: dict | None = None) -> tuple[list[Job], dict, list[Job]]:
        """US location, work mode and authorization filters. CPU only; runs in a worker thread.
        `by` collects the same rejections per platform ({platform id: {reason: n}})."""
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
                bump(by, job, "non_us")
                continue
            if work_mode_rejects(job, req):
                counts["rejected_work_mode"] += 1
                bump(by, job, "work_mode")
                continue
            reject, _ = evaluate_eligibility(job, req)
            if reject:
                counts["rejected_eligibility"] += 1
                bump(by, job, "eligibility")
                continue
            kept.append(job)
        return kept, counts, touched

    async def _fetch_details(self, pending: list[Job], counts: dict, state: SearchState | None, by: dict | None = None) -> list[Job]:
        """Fetch descriptions for list-only records (best title matches first, bounded)."""
        todo: list[Job] = []
        for job in sorted(pending, key=lambda j: -j.title_relevance):
            is_us, reason = us_location_status(job.location, job.country_hint)
            if not is_us and reason != "US eligibility not confirmed":
                counts["rejected_non_us"] += 1          # clearly outside the US: no request needed
                bump(by, job, "non_us")
                continue
            todo.append(job)
        budget = max(0, state.plan.detail_fetches if state and state.plan else self.settings.max_detail_fetches)
        skipped = max(0, len(todo) - budget)
        todo = todo[:budget]

        async def one(job: Job):
            spec = self.specs.get(job.ats)          # a reader that is switched off makes no request, also not for stored postings
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
                       state: SearchState | None = None, scope: Scope = NO_SCOPE) -> list[Job]:
        """Relevant, screened postings from the index. `expected` maps posting keys named by AI leads to providers.
        With a platform filter only postings read by the chosen platforms' readers are taken."""
        titles = req.titles
        tokens = query_tokens([*titles, *related])
        rows = await self.index.candidates(tokens, list(expected), max_age)
        if scope.filtered:
            allowed = {src for src in {r[3] for r in rows} if scope.allows_source(src)}
            rows = [r for r in rows if r[3] in allowed]

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
        by: dict = {}                              # per platform: postings considered and why some were screened out
        for job in jobs:
            bump(by, job, "considered")
        ready = [j for j in jobs if not j.needs_detail]
        pending = [j for j in jobs if j.needs_detail]
        if details and pending:
            ready.extend(await self._fetch_details(pending, local, state, by))
        kept, screen_counts, touched = await asyncio.to_thread(self._screen, ready, req, by)
        for key, value in screen_counts.items():
            local[key] += value
        local["relevant"] = matched
        local["platforms"] = by
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
        board_provider: dict[Unit, str] = {}       # unit -> the provider that named it first (statistics only)
        tally: dict = state.__dict__.setdefault("provider_boards", {})      # provider -> [new boards, their postings]
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
            if src in ("workable", *BOARD_LEAD_SOURCES) and board and src in self.specs:
                board_provider.setdefault(Unit(src, board, ""), lead.provider)
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
                    if board not in workday_new:
                        tally.setdefault(lead.provider, [0, 0])[0] += 1
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
        # The time box of this lead check (LEAD_VERIFICATION_SECONDS; 0 = none). Cheap checks come first: the board
        # reads and one plain request per page lead, in the order the leads arrived. The browser and the AI page check
        # get what is left of it. What is verified by then goes into this search; nothing waits beyond it.
        # A larger request makes up to twice a round's calls and gets up to twice the time for their leads (Plan.lead_seconds).
        box = max(0.0, float((state.plan.lead_seconds if state.plan is not None else getattr(self.settings, "lead_verification_seconds", 0.0)) or 0.0))
        box_start = time.monotonic()
        if box:
            deadline = min(deadline, box)

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
                    found = tally.setdefault(board_provider.get(unit, ""), [0, 0])
                    found[0], found[1] = found[0] + 1, found[1] + gained
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
        # Leads on many different hosts: the page verifier itself keeps each host to two requests at a time.
        sem = asyncio.Semaphore(max(1, getattr(self.settings, "page_verification_concurrency", 8)))
        to_extract: list[tuple[job_verifier.Page, Candidate, Job | None]] = []
        to_render: list[Candidate] = []
        crawl_queue: dict[str, Candidate] = {}
        # With an AI page check to follow, a page that only bears out the lead's own claim waits for it: the model
        # reads the page itself (and its location) and can say no. Without one, the claim-based check decides.
        can_extract = bool(ai.available or getattr(ai, "techsara", None) is not None)
        browser_on = bool(getattr(self.browser, "available", False))
        # Pages TinyFetch may still read in this search when our own browser is off or could not show the page.
        render_extra = max(0, (state.plan.tiny_fetches if state.plan else 0) - stats["tiny_fetches"]) if self.tiny_on else 0

        def left() -> float | None:
            """Seconds of the time box that are left (None: no time box, every check is waited for)."""
            return max(0.0, box_start + box - time.monotonic()) if box else None

        async def plain(cand: Candidate) -> job_verifier.Verdict:
            """One plain request, never the browser: the cheap check every page lead gets first."""
            async with sem:
                return await self.verifier.verify(cand, None, defer_claim=can_extract)

        async def rendered(cand: Candidate) -> job_verifier.Verdict:
            """The page again with the browser (one page of the browser budget), for a page a plain request could not
            show; and through TinyFetch when our own browser could not turn it into a posting either - never when the
            site itself refused an automated browser."""
            async with sem:
                verdict = await self.verifier.verify(cand, browser_budget, defer_claim=can_extract)
                if (verdict.job is None and verdict.page is not None and self.tiny_on
                        and verdict.extractable and job_verifier.render_may_help(verdict.page)
                        and "blocks automated browsers" not in verdict.reason
                        and stats["tiny_fetches"] < (state.plan.tiny_fetches if state.plan else 0)):
                    stats["tiny_fetches"] += 1
                    html, note = await self.tinyfish.fetch_html(verdict.page.url)
                    stats["tiny_usd"] = round(self.tinyfish.spent, 4)
                    if note:
                        stats["tiny_fetches"] -= 1 if note in ("daily spending ceiling reached", "wallet balance is at the reserve", "not configured") else 0
                        stats["tiny_skipped"] = note if "ceiling" in note or "reserve" in note else stats["tiny_skipped"]
                    else:
                        page = job_verifier.Page(verdict.page.url, 200, html, job_verifier.page_text(html), rendered=True,
                                                 requested=verdict.page.requested)
                        job, _ = job_verifier._evaluate(page, cand)
                        if job:
                            job.verification = job.verification.replace("rendered in a headless browser", "read through TinyFetch")
                            verdict = job_verifier.Verdict(job=job)
            return verdict

        sites: dict[str, list[int]] = {}             # site -> [pages verified as postings, pages the site refused to serve]

        def site_of(url: str) -> str:
            return (url.split("/")[2] if url.count("/") >= 2 else "").lower().removeprefix("www.")

        def settle(cand: Candidate, verdict: job_verifier.Verdict, follow_listing: bool, may_render: bool) -> None:
            """What one checked page means for this search."""
            seen = sites.setdefault(site_of(cand.url), [0, 0])
            seen[0] += verdict.job is not None
            seen[1] += verdict.job is None and bool(_SITE_REFUSES.match(verdict.reason or ""))
            if verdict.job:
                verified.append(verdict.job)
                return
            if verdict.page is None:
                stats["rejected_unverified"] += 1
                return
            links = self.verifier.listing_links(verdict.page, req.titles, related, cand) if follow_listing else []
            # A careers/listing page: follow the matching job links. Two matches say "list" about any page; one is
            # enough on a page already known not to be a posting itself (a list, search or home address).
            if len(links) >= 2 or (links and not verdict.extractable):
                for link in links:
                    if link.url not in page_leads and crawl_budget.take():
                        crawl_queue.setdefault(link.url, link)
            elif (may_render and verdict.extractable and verdict.claimed is None and job_verifier.render_may_help(verdict.page)
                  and len(to_render) < (browser_budget.left if browser_on else 0) + render_extra):
                to_render.append(cand)                       # the browser (or TinyFetch) may show more than the first response did
            elif verdict.extractable and extract_slots[0] > 0:
                extract_slots[0] -= 1
                to_extract.append((verdict.page, cand, verdict.claimed))
            elif verdict.extractable and verdict.claimed is not None:      # no AI slot left: the claim-based check decides
                verified.append(verdict.claimed)
            else:
                stats["rejected_unverified"] += 1          # also a list or home page that offered no matching job links

        async def run_stage(checks: list[tuple[Candidate, asyncio.Task]], follow_listing: bool, may_render: bool,
                            wait: float | None = None) -> list[tuple[Candidate, asyncio.Task]]:
            """Wait for the checks until the time box ends (and at most `wait` seconds) and settle the finished ones in
            lead order. Returns the checks that were still running."""
            if not checks:
                return []
            limits = [x for x in (left(), wait) if x is not None]
            try:
                done, late = await asyncio.wait([task for _, task in checks], timeout=min(limits) if limits else None)
            except asyncio.CancelledError:
                for _, task in checks:
                    task.cancel()
                raise
            for cand, task in checks:
                if task not in done:
                    continue
                if task.cancelled() or task.exception() is not None:
                    stats["rejected_unverified"] += 1
                else:
                    settle(cand, task.result(), follow_listing, may_render)
            return [(cand, task) for cand, task in checks if task in late]

        first = list(page_leads.values())
        # Page reads: every round may check its own leads (MAX_PAGE_VERIFICATIONS a round, what an earlier round left
        # unused included), up to the ceiling of the whole search. The limit protects the sites and the round's time.
        each = state.plan.page_verifications if state.plan else self.settings.max_page_verifications
        total = min(each * max(1, stats["round"]), max(each, self.settings.max_page_verifications_max))
        budget = max(0, total - getattr(state, "pages_checked", 0))
        state.pages_checked = getattr(state, "pages_checked", 0) + min(budget, len(first))
        # Pages without structured data are read by a model: one call with a paid model, several with the company
        # model (no bill; it answers about one page a second, so this is also a time budget of the round).
        paid_calls = max(1, getattr(self.settings, "ai_page_extraction_paid_calls", 1))
        extraction_limit = self.settings.max_ai_page_extractions * (
            max(1, getattr(self.settings, "ai_page_extraction_calls", 5)) if getattr(ai, "techsara", None) is not None else paid_calls)
        extract_slots = [extraction_limit]           # pages the AI page check may still read in this round
        # ... of which a model with a bill may read this many when the company model does not answer (the pages of
        # AI_PAGE_EXTRACTION_PAID_CALLS full calls; the check runs in several passes, so pages are counted, not calls)
        paid_pages = [paid_calls * max(1, self.settings.max_ai_page_extractions)]
        extract_gate = asyncio.Semaphore(max(1, getattr(self.settings, "ai_page_extraction_concurrency", 4)))
        if len(first) > budget:
            state.warn(f"Verification budget reached: {len(first) - budget} web lead(s) were not checked.")

        async def check_pages():
            # Cheap checks first: one plain request per page lead, in the order the leads arrived (the likeliest open
            # postings first). Whether a page verifies depends most of all on its site (see ai_orchestrator._lead_rank):
            # so every site is asked for a few of its leads first (PROBES_PER_SITE; all leads of a site whose pages
            # were verified before), and the rest follows where a page verified. A site that refused every one of its
            # first pages (HTTP 401 / 403 / 429, robots.txt) is not asked for the others: no request, no time.
            leads = first[:budget]
            trusted = getattr(ai, "_verified_site", None)
            taken: dict[str, int] = {}
            probes, rest = [], []
            for cand in leads:
                site = site_of(cand.url)
                taken[site] = taken.get(site, 0) + 1
                (probes if taken[site] <= PROBES_PER_SITE or (trusted is not None and trusted(site)) else rest).append(cand)
            late = await run_stage([(c, asyncio.create_task(plain(c))) for c in probes], True, True, PROBE_SECONDS if rest else None)
            more = []
            for cand in sorted(rest, key=lambda c: sites.get(site_of(c.url), [0, 0])[0] == 0):      # stable: sites with a verified page first
                ok, refused = sites.get(site_of(cand.url), [0, 0])
                if not ok and refused >= PROBES_PER_SITE:
                    stats["pages_site_refused"] += 1
                    stats["rejected_unverified"] += 1
                else:
                    more.append(cand)
            late = await run_stage([*late, *((c, asyncio.create_task(plain(c))) for c in more)], True, True)
            if crawl_queue and (left() is None or left() > 0):
                stats["crawled_links"] += len(crawl_queue)
                late += await run_stage([(c, asyncio.create_task(plain(c))) for c in list(crawl_queue.values())], False, True)
            if late:
                # The search moves on. A plain request that is still on its way may finish in the background: what it
                # verifies is remembered (source career_page), so the next search finds it in the index.
                stats["pages_late"] += len(late)
                self._finish_late([task for _, task in late], stats)

        async def check_workday_leads():
            checks = [asyncio.create_task(check_workday(b, l)) for b, l in workday_leads[:10]]
            if checks:                                   # paced source: bounded wait
                _, late = await asyncio.wait(checks, timeout=state.detail_wait if left() is None else min(state.detail_wait, left()))
                for task in late:
                    task.cancel()
                    stats["rejected_unverified"] += 1

        async def extract_pages(items: list[tuple[job_verifier.Page, Candidate, Job | None]]) -> None:
            """The AI page check of pages without structured data, within the time that is left. A page the model gave
            no answer for (the call failed, or the time ran out) goes back to the claim-based check."""
            if not items:
                return
            extracted: dict[str, dict] = {}
            cut = False
            timeout = left()
            if can_extract and (timeout is None or timeout >= MIN_EXTRACTION_SECONDS):
                pages = [(str(i), job_verifier.extraction_excerpt(page)) for i, (page, _, _) in enumerate(items)]
                size = max(1, self.settings.max_ai_page_extractions)           # one call per batch of this size

                async def ask(paid: bool, batch: list[tuple[str, str]]) -> None:
                    async with extract_gate:
                        # Only the first pages may fall back to a model with a bill when the company model does not
                        # answer; the others are its own, and their pages go back to the claim-based check.
                        extracted.update(await (ai.extract_pages(batch) if paid else ai.extract_pages(batch, paid=False)))
                asks = []
                for i in range(0, len(pages), size):
                    batch = pages[i:i + size]
                    asks.append(asyncio.create_task(ask(paid_pages[0] > 0, batch)))
                    paid_pages[0] -= len(batch)
                try:
                    _, unfinished = await asyncio.wait(asks, timeout=timeout)
                except asyncio.CancelledError:
                    for task in asks:
                        task.cancel()
                    raise
                for task in unfinished:
                    task.cancel()
                if unfinished:
                    await asyncio.gather(*unfinished, return_exceptions=True)
                cut = bool(unfinished)
            elif not can_extract:
                stats["rejected_unverified"] += len(items)
                return
            else:
                cut = True                                   # no time was left for the model at all
            for i, (page, cand, claimed) in enumerate(items):
                answer = extracted.get(str(i))
                job, _ = job_from_extraction(page, cand, answer or {})
                if job:
                    stats["ai_extracted"] += 1
                    verified.append(job)
                elif answer is None and claimed is not None and not cut:      # the model gave no answer for this page (call
                    verified.append(claimed)                                   # failed, page left out): the claim-based check decides
                else:
                    # ... but never because the time ran out: a page nobody read is not a job of this search.
                    if answer is None and cut:
                        stats["page_checks_cut"] += 1
                    stats["rejected_unverified"] += 1

        async def render_pages() -> None:
            """The browser, only while time is left. A render still running when the time ends is stopped: what a
            browser verifies is not remembered (see _remember_pages), so finishing it would help no search."""
            if not to_render:
                return
            if left() is not None and left() <= 0:
                stats["page_checks_cut"] += len(to_render)
                stats["rejected_unverified"] += len(to_render)
                return
            checks = {asyncio.create_task(rendered(c)): c for c in to_render}
            pending = set(checks)
            readers: list[asyncio.Task] = []
            size = max(1, self.settings.max_ai_page_extractions)
            try:
                while pending:
                    timeout = left()
                    if timeout is not None and timeout <= 0:
                        break
                    done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                    for task in [t for t in checks if t in done]:
                        if task.cancelled() or task.exception() is not None:
                            stats["rejected_unverified"] += 1
                        else:
                            settle(checks[task], task.result(), False, False)
                    # The model reads what the browser showed as soon as a batch is ready, not after the last render:
                    # a slow render must not cost the pages before it their check.
                    if len(to_extract) >= size or (not pending and to_extract):
                        batch = list(to_extract)
                        to_extract.clear()
                        readers.append(asyncio.create_task(extract_pages(batch)))
            except asyncio.CancelledError:
                for task in [*pending, *readers]:
                    task.cancel()
                raise
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                stats["page_checks_cut"] += len(pending)
                stats["rejected_unverified"] += len(pending)
            if to_extract:
                batch = list(to_extract)
                to_extract.clear()
                readers.append(asyncio.create_task(extract_pages(batch)))
            if readers:
                await asyncio.gather(*readers)

        await asyncio.gather(check_boards(), check_pages(), check_workday_leads())
        # Second stage, only while time is left: the browser for the pages a plain request could not show, and beside it
        # the AI page check, first of the pages that came complete and then of what the browser shows, batch by batch.
        waiting = list(to_extract)
        to_extract.clear()
        await asyncio.gather(render_pages(), extract_pages(waiting))
        stats["browser_pages"] += browser_budget.used
        stats["lead_check_seconds"] = round(stats["lead_check_seconds"] + time.monotonic() - box_start, 1)
        if hasattr(ai, "note_verified"):
            # These sites are read first by the next round; where the title fits, a follow-up call searches them again.
            ai.note_verified((job.url for job in verified), relevant=False)
            ai.note_verified((job.url for job in verified if best_title_relevance(job.title, req.titles, related) >= MIN_RELEVANCE),
                             relevant=True)
        return expected, verified

    def _finish_late(self, checks: list[asyncio.Task], stats: dict) -> None:
        """Let page checks (plain requests) that were still running when a search moved on finish in the background,
        for at most LEAD_VERIFICATION_BACKGROUND_SECONDS, and keep what they verify through _remember_pages: the next
        search finds those postings in the index. No browser and no model is used here (the search's orchestrator is
        closed by then), so a page that only bears out the lead's own claim is not kept. Stopped when the engine closes."""
        async def finish() -> None:
            try:
                done, late = await asyncio.wait(checks, timeout=max(1.0, self.settings.lead_verification_background_seconds))
            except asyncio.CancelledError:
                for task in checks:
                    task.cancel()
                raise
            for task in late:
                task.cancel()
            if late:
                await asyncio.gather(*late, return_exceptions=True)
            jobs = [task.result().job for task in done
                    if not task.cancelled() and task.exception() is None and task.result().job is not None]
            stats["pages_late_verified"] += len(jobs)
            if jobs:
                stats["pages_late_remembered"] += await self._remember_pages(jobs)

        task = asyncio.create_task(finish())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------ which boards a search reads
    async def _reading_plan(self, units: list[Unit], titles: list[str], related: list[str], limit: int, scope: Scope) -> list[Unit]:
        """The units this search asks to be fresh, in reading order.

        A search may read only so many boards (`limit`, its budget) and the index knows many more. The boards worth
        reading now are those whose last read, up to three days old, listed postings with the requested titles: they
        go first, most matching postings first, whatever their platform. Until 2026-11-21 the list was cut in registry
        order, so once the index knew more Greenhouse boards than the budget (production: 6,629 against 1,500) a
        search refreshed Greenhouse boards only and every other platform depended on the background reader.

        Units that are still fresh cost nothing and do not use up the budget. Of a source that allows about one
        request a second, at most PACED_UNITS_PER_SEARCH stale units are asked for: more would only queue behind its
        limiter. The few query and feed units are always kept (registry.within_budget); under the startup focus the
        startup platforms come first, as before."""
        try:
            states = await self.index.unit_states()
        except Exception:
            states = {}
        now = time.time()
        fresh, stale = [], []
        for unit in dict.fromkeys(units):
            st = states.get((unit.src, unit.key, unit.scope))
            (fresh if st and st[1] > now else stale).append(unit)
        hits = await self._title_hits(titles, related)
        if hits:
            stale.sort(key=lambda u: -hits.get((u.src, u.key), 0))        # stable: boards without a known match keep their order
        taken: dict[str, int] = {}
        chosen: list[Unit] = []
        pages: list[Unit] = []
        for unit in scope.order_units(stale):
            if unit.src == CAREER_PAGE:                 # one posting each: only those whose title fits, outside the board budget
                if hits.get((unit.src, unit.key)) and len(pages) < CAREER_PAGES_PER_SEARCH:
                    pages.append(unit)
                continue
            if paced_unit(unit):
                if taken.get(unit.src, 0) >= PACED_UNITS_PER_SEARCH:
                    continue
                taken[unit.src] = taken.get(unit.src, 0) + 1
            chosen.append(unit)
        # A remembered page that is still fresh is in the index anyway; listing it would only inflate the count of reads.
        return [*registry.within_budget(chosen, limit), *pages, *(u for u in fresh if u.src != CAREER_PAGE)]

    async def _title_hits(self, titles: list[str], related: list[str]) -> dict[tuple[str, str], int]:
        """Per board (source, key): how many postings with a requested title its last read listed. Only an estimate
        of where to look; what counts as verified is decided after the boards were read again."""
        lookup = getattr(self.index, "relevant_units", None)
        if lookup is None:
            return {}
        try:
            rows = await lookup(query_tokens([*titles, *related]), RELEVANT_READ_SECONDS)
        except Exception:
            return {}

        def count() -> dict[tuple[str, str], int]:
            relevance: dict[str, bool] = {}
            out: dict[tuple[str, str], int] = {}
            for src, key, title in rows:
                fits = relevance.get(title)
                if fits is None:
                    fits = relevance[title] = best_title_relevance(title, titles, related) >= MIN_RELEVANCE
                if fits:
                    out[(src, key)] = out.get((src, key), 0) + 1
            return out

        return await asyncio.to_thread(count) if rows else {}

    # ------------------------------------------------------------ scoring helpers
    def _prerank(self, ai, profile: Profile, req: SearchRequest, jobs: list[Job], related: list[str]) -> None:
        """The early pass of the reranker: hand it the postings known so far, best keyword matches first, and go on.
        The company reranker reads about 20 postings a second whatever the batch size (measured 2026-11-21), so 800
        postings cost about 40 s: started here, that time runs while sources are read and leads are verified, and the
        ranking stage only waits for what is new or still missing (AIOrchestrator.rerank_start / rerank_collect)."""
        if not jobs or not getattr(ai, "can_rerank", False) or not hasattr(ai, "rerank_start"):
            return
        self._score(jobs, profile, req, related)                # keyword scores: the order in which the reranker reads
        head = sorted(jobs, key=self._order)[: max(0, self.settings.techsara_rerank_max_jobs)]
        ai.rerank_start(profile, req, head)

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
        state.scope = scope = Scope.of(req.ats_platforms, req.startup_focus, self.specs)
        stats["platform_filter"], stats["startup_focus"] = [p for p in req.ats_platforms if p in scope.chosen], scope.focus
        stats["coverage_warnings"] = scope.coverage_warnings()       # shown while the search runs; warned once at the end
        if hasattr(ai, "configure"):
            ai.configure(plan)                    # AI budgets scale with the number of results requested
        if hasattr(ai, "set_scope"):              # AI web search: only hosts this search can use (platform filter, enabled readers)
            hosts = self._hosts(state)
            ai.set_scope(scope, hosts, self._paced_hosts(hosts))
        if hasattr(ai, "remember"):
            ai.remember(self.rank_cache, resume_text, req)       # same candidate, same posting text: the earlier judgement is used again
        state.providers = ai.status
        state.status = "running"
        titles = req.titles
        wanted = {t.lower() for t in titles}

        state.emit("analyze", "Analyzing resume")
        base_profile: Profile = heuristic_profile(resume_text)
        related0 = [t for t in static_related_titles(titles, base_profile.all_skills) if t.lower() not in wanted][:8]
        keys = await self._keys()
        if hasattr(ai, "note_verified"):
            # Sites on which earlier searches verified postings (the remembered career pages): their leads are checked first.
            ai.note_verified(getattr(self, "_pages", []))
        use_web = ai.available and req.deep_search
        if not ai.available:
            state.warn("No AI provider configured: using keyword-based resume analysis and scoring. No AI web search was performed.")
        elif not req.deep_search:
            state.warn("AI web search and browser crawl were switched off for this search (faster, narrower coverage).")

        # Everything slow starts now and runs side by side.
        profile_task = asyncio.create_task(ai.extract_profile(resume_text, req, base_profile)) if ai.available else None
        known_names = sorted({k.split("|")[0] for ks in keys.values() for k in ks})
        state.known_employers = asyncio.get_running_loop().create_future()     # set by the pipeline once the index was screened
        discover_task = asyncio.create_task(self._first_discovery(state, ai, base_profile, req, related0, known_names)) if use_web else None
        expand_task = asyncio.create_task(self._expand_titles(state, ai, titles, base_profile))
        # Platform filter: only the readers of the chosen platforms. Startup focus: their units first (they are read
        # first and are the last to fall outside the budget of boards).
        specs = {name: spec for name, spec in self.specs.items() if scope.allows_source(name)}
        accounts = self._account_units() if scope.allows_source("workable") else []
        # The budget cuts employer boards only: the few query and feed units are always kept (registry.within_budget).
        pages = self._page_units() if scope.allows_source(CAREER_PAGE) else []
        units = await self._reading_plan([*registry.plan_units(specs, keys, self._queries(titles, related0)), *accounts, *pages],
                                         titles, related0, plan.boards, scope)
        if hasattr(ai, "note_verified"):
            # Remembered postings whose title fits this search: their sites are searched again by a follow-up call.
            ai.note_verified([u.key for u in units if u.src == CAREER_PAGE], relevant=True)
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
            stats["rank_reused"] = {k: v for k, v in (getattr(ai, "reused", None) or {}).items() if v}
            if hasattr(ai, "usage"):
                state.usage = ai.usage()
            if hasattr(ai, "aclose"):
                await ai.aclose()
        state.mark("total")
        stats["total_seconds"] = state.timings["total"]
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

    # ---- AI web search: where it looks and what it is told
    def _paced_hosts(self, hosts: list[str]) -> list[str]:
        """Hosts of paced sources (Workday: one request every 1.5 s, a few leads checked per round). The AI web search
        asks them in its first pass only, so searches are not spent on leads the round cannot check."""
        return [h for h in hosts if (spec := SPECS.get(host_reader(h))) is not None and spec.wait]

    @staticmethod
    def _known_employers(jobs: list[Job], limit: int = 200) -> list[str]:
        """Employers whose matching postings this search already has, the ones with most postings first, as
        "platform:board key". The AI web search is told about them so that it brings other employers. Only board
        keys are passed (plain slugs); an employer NAME comes from a posting, is free text, and stays out of prompts."""
        counts: dict[str, int] = {}
        for job in jobs:
            platform = platform_of_job(job)
            if platform and job.board:
                name = f"{platform}:{job.board.split('|')[0]}"
                counts[name] = counts.get(name, 0) + 1
        return sorted(counts, key=lambda name: -counts[name])[:limit]

    async def _first_discovery(self, state: SearchState, ai, profile: Profile, req: SearchRequest, related: list[str],
                               known_names: list[str]) -> list[Candidate]:
        """Round 1 of the AI web search. It waits a moment for the employers whose matching postings the index already
        holds (the pipeline reports them as soon as it screened the index: no network, long before a model call
        returns), so even the first calls know whom not to bring again. Without them it is told the known boards."""
        try:
            relevant = await asyncio.wait_for(asyncio.shield(state.known_employers), timeout=KNOWN_EMPLOYERS_WAIT_SECONDS)
        except asyncio.TimeoutError:
            relevant = []
        return await ai.discover(profile, req, related, relevant or known_names)

    @staticmethod
    def _discovery_notes(state: SearchState, ai) -> None:
        """What the AI web search has to say about itself (a provider switched off after failing and who took over,
        calls dropped at the time limit) becomes a warning of the search, once."""
        notes = getattr(ai, "discovery_notes", None)
        while notes:
            state.warn(notes.pop(0))

    # ---- search engines: no query twice
    def _ledger(self, state: SearchState) -> QueryLedger:
        """What this search already asked a search engine (search_plan.QueryLedger), shared by SearXNG, the company
        model's web search, TinyFish and AWS."""
        ledger = getattr(state, "ledger", None)
        if ledger is None:
            ledger = state.ledger = QueryLedger()
        return ledger

    def _tsweb_usable(self, state: SearchState) -> bool:
        return self.tsweb_on and not getattr(state, "tsweb_down", False)

    def _tsweb_budget(self, state: SearchState, round_no: int | None = None) -> int:
        """Web searches the company model may run in a round (default: the current one): that round's share of its
        budget for this search (Plan.tsweb_searches, never beyond TECHSARA_WEB_SEARCHES_PER_SEARCH), minus what it used."""
        stats, plan = state.stats, state.plan
        if not self._tsweb_usable(state):
            return 0
        total = min(plan.tsweb_searches, max(0, self.settings.techsara_web_searches_per_search))
        return max(0, min(plan.per_round(total, round_no or stats["round"]), total - stats["tsweb_searches"]))

    async def _searx_round(self, state: SearchState, queries: list[str]) -> list[Candidate]:
        """One round of `site:` queries; result addresses become leads. Only addresses are used: a board address or a
        posting address on a board ATS becomes a lead that the normal lead check resolves (the board is registered and
        read through its API). Never raises.

        No query is sent twice (the search's ledger). SearXNG sends `queries`, its slice of the plan, paced by its
        client. The company model's web search runs beside it on queries SearXNG will NOT reach in this search (the
        plan beyond SearXNG's budget), and afterwards on the ones SearXNG failed on or got nothing usable for. When
        SearXNG is off or stopped answering, the company model takes SearXNG's slice, so that part of the plan is
        still asked."""
        stats = state.stats
        ledger = self._ledger(state)
        searx_ok = self.searx_on and not getattr(state, "searx_down", False)
        budget = self._tsweb_budget(state)
        if not queries and not budget:
            return []
        hosts = self._hosts(state)
        ctx = await self._hit_context(state)
        out: dict[str, Candidate] = {}
        tail = getattr(state, "query_tail", [])
        plan = getattr(state, "query_plan", None) or [*queries, *tail]
        mine = [q for q in queries if ledger.claim(SEARX, q)] if searx_ok else []
        if searx_ok and len(mine) < len(queries):
            mine += ledger.take(SEARX, tail, len(queries) - len(mine))      # another engine had asked some: SearXNG keeps its full slice
        # Beside SearXNG the model takes queries beyond SearXNG's budget and keeps a third of its round for second
        # chances. Without SearXNG it stands in for it: the plan from the top, whatever nobody answered yet.
        beside = (ledger.take(TECHSARA_WEB, tail, budget - budget // 3) if searx_ok
                  else ledger.take_or_retry(TECHSARA_WEB, plan, budget))
        model_task = asyncio.create_task(self._tsweb_round(state, beside, hosts, ctx)) if beside else None
        # Ollama's web search: its own part of the plan nobody else asks in this round (the plan's tail, then second
        # chances), never beyond its budget per search; without SearXNG the plan from the top.
        olla = self._ollama_left(state)
        if olla:
            want = min(olla, max(len(queries), 1) * 2)
            olla_q = ledger.take(OLLAMA, tail, want) if searx_ok else ledger.take_or_retry(OLLAMA, plan, want)
            if len(olla_q) < want:
                olla_q += ledger.retry_for(OLLAMA, plan, want - len(olla_q))
        else:
            olla_q = []
        olla_task = asyncio.create_task(self._ollama_round(state, olla_q, hosts, ctx)) if olla_q else None
        try:
            await self._searx_queries(state, mine, hosts, ctx, out)
        finally:
            for task in (model_task, olla_task):
                if task is not None:
                    for url, lead in (await task).items():
                        out.setdefault(url, lead)
        left = budget - len(beside)
        if left > 0 and self._tsweb_usable(state):
            if searx_ok and getattr(state, "searx_down", False):
                # SearXNG stopped answering in this round: its slice in plan order, failed and never sent alike.
                again = ledger.take_or_retry(TECHSARA_WEB, mine, left)
            else:
                # Second chances: queries of this or an earlier round that SearXNG failed on or got nothing usable for.
                again = ledger.retry_for(TECHSARA_WEB, plan, left)
            again += ledger.take(TECHSARA_WEB, tail, left - len(again))
            if again:
                for url, lead in (await self._tsweb_round(state, again, hosts, ctx)).items():
                    out.setdefault(url, lead)
        stats["searx_leads"] += len(out)
        stats["query_ledger"] = ledger.stats()
        return list(out.values())

    def _ollama_left(self, state: SearchState) -> int:
        stats = state.stats
        if not self.ollama_on or stats["ollama_skipped"]:
            return 0
        return max(0, self.settings.ollama_web_searches_per_search - stats["ollama_searches"])

    async def _ollama_query(self, state: SearchState, query: str) -> tuple[list[str], str]:
        """One query through Ollama's web search. A refusal that will not go away within this search (no credits, a
        wrong key) switches the engine off for the search with one warning; three other failures do too."""
        stats = state.stats
        urls, note = await self.ollama.search(query)
        stats["ollama_searches"] += 1
        if note:
            stats["ollama_failed"] += 1
            if note in ("HTTP 401", "HTTP 402", "HTTP 403") or stats["ollama_failed"] >= 3 and not stats["ollama_results"]:
                why = {"HTTP 402": "the Ollama account has no usage credits", "HTTP 401": "the Ollama API key was refused",
                       "HTTP 403": "the Ollama API key was refused"}.get(note, f"it did not answer ({note})")
                stats["ollama_skipped"] = why
                state.warn(f"Ollama web search was skipped: {why}.")
            return [], note
        stats["ollama_results"] += len(urls)
        return urls, ""

    async def _ollama_round(self, state: SearchState, queries: list[str], hosts: list[str], ctx: tuple) -> dict[str, Candidate]:
        """`site:` queries through Ollama's web search, side by side (OLLAMA_WEB_SEARCH_CONCURRENCY). The caller chose
        queries no other engine sends. Never raises."""
        ledger = self._ledger(state)
        out: dict[str, Candidate] = {}

        async def one(query: str) -> None:
            if state.stats["ollama_skipped"]:
                ledger.release(OLLAMA, query)
                return
            try:
                urls, note = await self._ollama_query(state, query)
            except asyncio.CancelledError:
                raise
            except Exception:
                urls, note = [], "request failed"
            if note:
                ledger.note(query, "failed")
                return
            before = len(out)
            self._hits_to_leads(state, urls, hosts, OLLAMA, ctx, out)
            ledger.note(query, "ok" if len(out) > before else "empty")

        await asyncio.gather(*(one(q) for q in queries))
        state.stats["ollama_leads"] += len(out)
        return out

    async def _tsweb_round(self, state: SearchState, queries: list[str], hosts: list[str], ctx: tuple) -> dict[str, Candidate]:
        """`site:` queries through the company model's web search tool, a few at a time
        (TECHSARA_WEB_SEARCH_CONCURRENCY). The caller chose queries no other engine sends. Never raises."""
        stats, s = state.stats, self.settings
        ledger = self._ledger(state)
        out: dict[str, Candidate] = {}

        async def one(query: str) -> None:
            if getattr(state, "tsweb_down", False):
                ledger.release(TECHSARA_WEB, query)                # never sent: another engine may still take it
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
                ledger.note(query, "failed")
                if stats["tsweb_failed"] >= 3 and not stats["tsweb_results"] and not getattr(state, "tsweb_down", False):
                    state.tsweb_down = True
                    state.warn(f"The company model's web search did not answer ({note}); the search went on without it.")
                return
            stats["tsweb_results"] += len(urls)
            before = len(out)
            self._hits_to_leads(state, urls, hosts, TECHSARA_WEB, ctx, out)
            ledger.note(query, "ok" if len(out) > before or any(extract(u, hosts) is not None for u in urls) else "empty")

        await asyncio.gather(*(one(q) for q in queries))
        stats["tsweb_leads"] += len(out)
        return out

    async def _searx_queries(self, state: SearchState, queries: list[str], hosts: list[str], ctx: tuple, out: dict) -> None:
        stats, scope = state.stats, _scope_of(state)
        ledger = self._ledger(state)
        if not self.searx_on or getattr(state, "searx_down", False):
            return
        for position, query in enumerate(queries):
            answer = await self.searx.search(query, self.settings.searxng_pages_per_query,
                                             useful=lambda u: extract(u, hosts) is not None or bool(scope.page_lead(u)))
            stats["searx_cached" if answer.cached else "searx_queries"] += 1
            if answer.error:
                stats["searx_failed"] += 1
                ledger.note(query, "failed")
                state.searx_failures = getattr(state, "searx_failures", 0) + 1
                if state.searx_failures >= 3:                          # slow or down: stop asking, the search goes on without it
                    state.searx_down = True
                    state.warn(f"Search-engine discovery (SearXNG) did not answer ({answer.error}); the search went on without it.")
                    for unsent in queries[position + 1:]:
                        ledger.release(SEARX, unsent)                  # never sent: another engine takes them
                    break
                continue
            state.searx_failures = 0
            if answer.unresponsive:
                state.__dict__.setdefault("searx_unresponsive", set()).update(answer.unresponsive)
            stats["searx_results"] += len(answer.urls)
            self._hits_to_leads(state, answer.urls, hosts, SEARX, ctx, out)
            usable = any(extract(u, hosts) is not None or scope.page_lead(u) for u in answer.urls)
            ledger.note(query, "ok" if usable else "empty")            # nothing usable: another engine may try it once
            state.emit(state.stage or "search", "Asking the search engine for employer job boards")

    def _hosts(self, state: SearchState) -> list[str]:
        """Hosts of this search's `site:` queries and of its AI web search (see Scope.hosts: only hosts whose results
        the engine can use, the platform filter, and the startup focus)."""
        return _scope_of(state).hosts(site_hosts(self.settings.searxng_sites))

    async def _hit_context(self, state: SearchState) -> tuple[set, set, set]:
        keys = await self._keys()
        known = {(src, key) for src, ks in keys.items() for key in ks} | {("workable", k) for k in getattr(self, "_accounts", [])}
        return known, await self.index.dead_boards(), state.__dict__.setdefault("searx_boards", set())

    def _hits_to_leads(self, state: SearchState, urls: list[str], hosts: list[str], provider: str, ctx: tuple, out: dict) -> None:
        """Result addresses of a search engine -> leads. Only well-formed addresses on the configured ATS hosts count;
        new boards are limited by the search's budget of new boards (shared by every search engine)."""
        known, dead, boards = ctx
        scope = _scope_of(state)

        def page(address: str) -> None:
            pages = state.__dict__.setdefault("searx_pages", set())
            if address in pages or len(pages) < self.settings.searxng_page_leads_per_search:
                pages.add(address)
                out.setdefault(address, Candidate(url=address, provider=provider, cited=True))

        for url in urls:
            hit = extract(url, hosts)
            if hit is None:
                # Platform filter: `hosts` holds only the chosen platforms, so an address of any other platform ends
                # here. A posting page of a chosen platform without a board reader is checked as a page.
                address = scope.page_lead(url)
                if address:
                    page(address)
                elif scope.filtered and platform_of_url(url) and not scope.allows(platform_of_url(url)):
                    state.stats["filtered_out"] += 1
                continue
            if hit.kind == "page":
                page(hit.url)
                continue
            pair = (hit.src, hit.board)
            if hit.src not in self.specs or pair in dead:
                continue
            if pair not in known and pair not in boards:
                if len(boards) >= state.plan.new_boards:      # budget of new boards for this search is used up
                    continue
                cap = scope.other_boards(state.plan.new_boards)
                if cap is not None and platform_of_url(hit.url) not in scope.preferred:
                    others = state.__dict__.setdefault("other_boards", set())      # startup focus: the smaller share
                    if len(others) >= cap:
                        continue
                    others.add(pair)
                boards.add(pair)
            elif pair in known and hit.kind == "board":
                continue                                # a board the index already reads: nothing to do
            out.setdefault(hit.url, Candidate(url=hit.url, provider=provider, cited=True))

    async def _open_engine_round(self, state: SearchState, req: SearchRequest, titles: list[str], after=None, ai=None,
                                 profile: Profile | None = None) -> list[Candidate]:
        """Open-web queries of the plain search engines (SearXNG, TinyFish): `"<title>" job opening <place>` without a
        `site:` operator, SEARCH_ENGINE_OPEN_WEB_QUERIES per search, in the first round. Never under a platform filter.

        A result becomes a lead only when its address reads like ONE posting on an employer's own site
        (_own_site_posting: job boards, aggregators, list and search pages and every host that is never fetched are
        dropped before anything is requested); the page then has to prove the posting like every web lead. A result on
        an ATS host is handled like the result of a `site:` query. Measured on 2026-11-21 through TinyFish (three
        profiles, two query forms, 6 queries): 31 of 60 results passed that test and 17 of them were verified as open
        postings, 2.8 a query. The company model's web search is NOT used for these queries: its answer carries no
        structured results, so an address would come from text a model wrote, and here no board API checks it.
        `after`: the first round of `site:` queries. These queries start when it has finished, so the SearXNG instance
        (which forwards to public engines) has one user at a time, and an instance that stopped answering is not asked.
        Never raises."""
        stats, scope, s = state.stats, _scope_of(state), self.settings
        budget = state.plan.open_engine_queries if state.plan else 0
        if after is not None:
            await asyncio.wait([after])
        searx_ok = self.searx_on and not getattr(state, "searx_down", False)
        tiny_ok = self.tiny_on and not stats["tiny_skipped"]
        olla_ok = self._ollama_left(state) > 0
        if budget <= 0 or scope.filtered or not (searx_ok or tiny_ok or olla_ok):
            return []
        places = [p for p in dict.fromkeys(" ".join(str(x).split()) for x in req.locations[:4]) if p] or ["United States"]
        if req.work_mode == "Remote":
            places = ["remote"]
        clean = lambda text: " ".join(re.sub(r'["\\]', " ", str(text or "")).split())
        queries = [form.format(title=clean(title), place=place)
                   for title in list(dict.fromkeys(t for t in titles if clean(t)))[:6] for place in places for form in OPEN_WEB_QUERY_FORMS]
        # A model's ideas for further plain queries (kinds of employers, other wordings), taking turns with the fixed
        # forms. Only the query text comes from the model; every result is checked like any other.
        ideas = []
        if ai is not None and hasattr(ai, "query_ideas") and self.settings.ollama_query_ideas > 0:
            try:
                ideas = await asyncio.wait_for(ai.query_ideas(titles, profile or Profile(), req, self.settings.ollama_query_ideas), timeout=45)
            except Exception:
                ideas = []
        stats["query_ideas"] = len(ideas)
        if ideas:
            mixed = []
            for pair in itertools.zip_longest(queries, ideas):
                mixed.extend(q for q in pair if q)
            queries = mixed
        ledger = self._ledger(state)
        hosts = self._hosts(state)
        ctx = await self._hit_context(state)
        out: dict[str, Candidate] = {}
        engines = [e for e, ok in ((SEARX, searx_ok), (OLLAMA, olla_ok), (TINYFISH, tiny_ok)) if ok]
        sent = 0
        for query in queries:
            if sent >= budget or not engines:
                break
            if SEARX in engines and getattr(state, "searx_down", False):
                engines.remove(SEARX)
                continue
            engine = engines[sent % len(engines)]            # both engines on: they take turns (two different indexes)
            if engine == TINYFISH and stats["tiny_searches"] >= state.plan.tiny_searches:
                engines.remove(TINYFISH)
                continue
            if engine == OLLAMA and not self._ollama_left(state):
                engines.remove(OLLAMA)
                continue
            if not ledger.claim(engine, query):
                continue
            sent += 1
            if engine == SEARX:
                answer = await self.searx.search(query, 1, useful=lambda u: self._own_site_posting(u, scope) or extract(u, hosts) is not None)
                stats["searx_cached" if answer.cached else "searx_queries"] += 1
                urls, note = list(answer.urls), answer.error or ""
                state.searx_failures = getattr(state, "searx_failures", 0) + 1 if note else 0
                if note:
                    stats["searx_failed"] += 1
                    if state.searx_failures >= 3:                          # slow or down: stop asking, like the `site:` rounds do
                        state.searx_down = True
                        state.warn(f"Search-engine discovery (SearXNG) did not answer ({note}); the search went on without it.")
                else:
                    stats["searx_results"] += len(urls)
            elif engine == OLLAMA:
                urls, note = await self._ollama_query(state, query)
                if stats["ollama_skipped"] and OLLAMA in engines:
                    engines.remove(OLLAMA)
            else:
                urls, note = await self.tinyfish.search(query)
                if note in ("daily spending ceiling reached", "wallet balance is at the reserve", "not configured"):
                    stats["tiny_skipped"] = note
                    ledger.release(TINYFISH, query)            # not sent: another engine may still take it
                    engines.remove(TINYFISH)
                    sent -= 1
                    continue
                stats["tiny_searches"] += 1
                stats["tiny_usd"] = round(self.tinyfish.spent, 4)
            stats["open_engine_queries"] += 1
            if note:
                ledger.note(query, "failed")
                continue
            stats["open_engine_results"] += len(urls)
            before = len(out)
            on_hosts = []
            for url in urls:
                if extract(url, hosts) is not None:
                    on_hosts.append(url)                       # a board or posting on an ATS host: the usual handling
                elif self._own_site_posting(url, scope):
                    pages = state.__dict__.setdefault("searx_pages", set())
                    if url in pages or len(pages) < s.searxng_page_leads_per_search:
                        pages.add(url)
                        out.setdefault(url, Candidate(url=url, provider=engine, cited=True))
            stats["open_engine_leads"] += len(out) - before
            self._hits_to_leads(state, on_hosts, hosts, engine, ctx, out)
            ledger.note(query, "ok" if len(out) > before else "empty")
            state.emit(state.stage or "search", "Asking the search engines for postings on employers' own sites")
        stats["query_ledger"] = ledger.stats()
        return list(out.values())

    async def _tiny_round(self, state: SearchState, count: int) -> list[Candidate]:
        """TinySearch (paid) as one more discovery engine: up to `count` `site:` queries that no engine asked in this
        search (the plan beyond SearXNG's budget) and, when none of those are left, queries another engine failed on
        or got nothing usable for. The same handling of results."""
        stats = state.stats
        if count <= 0 or not self.tiny_on or stats["tiny_skipped"]:
            return []
        ledger = self._ledger(state)
        count = min(count, state.plan.tiny_searches - stats["tiny_searches"])
        queries = ledger.take(TINYFISH, getattr(state, "query_tail", []), count)
        queries += ledger.retry_for(TINYFISH, getattr(state, "query_plan", []), count - len(queries))
        if not queries:
            return []
        hosts = self._hosts(state)
        ctx = await self._hit_context(state)
        out: dict[str, Candidate] = {}
        for position, query in enumerate(queries):
            urls, note = await self.tinyfish.search(query.replace('"', "") + " jobs")
            if note in ("daily spending ceiling reached", "wallet balance is at the reserve", "not configured"):
                stats["tiny_skipped"] = note
                state.warn(f"TinyFish was skipped: {note}.")
                for unsent in queries[position:]:
                    ledger.release(TINYFISH, unsent)                   # not sent: another engine may still take them
                break
            stats["tiny_searches"] += 1
            stats["tiny_usd"] = round(self.tinyfish.spent, 4)
            if note:
                ledger.note(query, "failed")
                state.tiny_failures = getattr(state, "tiny_failures", 0) + 1
                if state.tiny_failures >= 3:
                    stats["tiny_skipped"] = f"it did not answer ({note})"
                    state.warn(f"TinyFish did not answer ({note}); the search went on without it.")
                    for unsent in queries[position + 1:]:
                        ledger.release(TINYFISH, unsent)
                    break
                continue
            before = len(out)
            self._hits_to_leads(state, urls, hosts, TINYFISH, ctx, out)
            ledger.note(query, "ok" if len(out) > before or any(extract(u, hosts) is not None for u in urls) else "empty")
            state.emit(state.stage or "search", "Asking a second search engine for employer job boards")
        stats["query_ledger"] = ledger.stats()
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
        hosts, scope = self._hosts(state), _scope_of(state)
        # None: an "any site" query for postings on employers' own sites (AWS_WEB_SEARCH_OPEN_WEB_SHARE of the queries;
        # left out under a platform filter, where only the chosen platforms count). Such a query excludes the job boards
        # and aggregators, the career platforms nothing may read, and the ATS hosts the other queries are about.
        rotation: list = aws_web_search.rotation(hosts, 0.0 if scope.filtered else state.plan.aws_open_share)
        if not rotation:
            return []
        exclude = list(dict.fromkeys([*aggregator_hosts(s.aggregator_hosts), *scope.unreadable_hosts(), *hosts]))
        ctx = await self._hit_context(state)
        seen: set = state.__dict__.setdefault("aws_seen", set())          # in memory, this search only
        ledger = self._ledger(state)
        out: dict[str, Candidate] = {}
        made = 0
        while made < calls and state.aws_pairs and stats["aws_queries"] < stats["aws_budget"]:
            title, location = state.aws_pairs.pop(0)
            text = aws_web_search.query_text(title, location, state.aws_mode)
            # The next host group in turn on which no engine asked this query yet (the search's ledger). A query that
            # was asked on every group already is left out: no unit of the allowance is spent on it.
            group, where = None, ""
            for step in range(len(rotation)):
                group = rotation[(state.aws_index + step) % len(rotation)]
                where = group[0] if group else "*"
                if ledger.claim(AWS, text, where):
                    state.aws_index += step + 1
                    break
            else:
                continue
            made += 1
            try:
                answer = await self.aws.search(text, include=group, exclude=None if group else exclude)
            except AwsWebSearchError as err:
                ledger.note(text, "failed", where)
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
            stats["aws_open_queries"] += group is None
            urls: list[str] = []
            pages_before = len(out)
            # Freshness: of an "any site" query the results published last come first (only their order is used, for
            # the life of this loop), so the page leads this search may still take are the newest ones.
            results = answer.results if group is not None else sorted(
                answer.results, key=lambda r: -(r.published_at.timestamp() if r.published_at is not None else 0.0))
            for result in results:
                if result.url in seen:
                    stats["aws_duplicates"] += 1
                    continue
                seen.add(result.url)
                if extract(result.url, hosts) is not None or scope.page_lead(result.url):
                    urls.append(result.url)
                elif group is None and self._own_site_posting(result.url, scope):
                    pages = state.__dict__.setdefault("searx_pages", set())
                    if len(pages) < s.searxng_page_leads_per_search:
                        pages.add(result.url)
                        out.setdefault(result.url, Candidate(url=result.url, provider=AWS, cited=True))
                else:
                    stats["aws_generic"] += 1                 # a search, category or aggregator page: never a vacancy
            stats["aws_open_leads"] += len(out) - pages_before
            self._hits_to_leads(state, urls, hosts, AWS, ctx, out)
            ledger.note(text, "ok" if urls or len(out) > pages_before else "empty", where)   # an outcome only: nothing of the result is kept
            del answer                                        # nothing of the response outlives this loop
            state.emit(state.stage or "search", "Asking AWS web search for employer job postings")
        stats["aws_leads"] += len(out)
        stats["query_ledger"] = ledger.stats()
        return list(out.values())

    def _own_site_posting(self, url: str, scope: Scope) -> bool:
        """Whether a result address of a search that was NOT held to hosts may be checked as ONE posting on an
        employer's own site. Strict, and decided before anything is requested: the address reads like one posting
        (aws_web_search.employer_posting_address: no list, search, category or home page), it is no job board or
        aggregator, and it is on no host that is never fetched or that this engine cannot read (iCIMS, SAP
        SuccessFactors, BambooHR, a reader that is switched off). The page then still has to prove the posting."""
        return bool(employer_posting_address(url, aggregator_hosts(self.settings.aggregator_hosts))
                    and not job_verifier.never_fetched(url) and scope.readable_url(url))

    def _more_discovery(self, state: SearchState, ai, use_web: bool, next_round: int, queries_left: int) -> bool:
        plan, stats = state.plan, state.stats
        ai_more = (use_web and hasattr(ai, "discover_round") and any(
            getattr(ai, name.lower(), None) is not None and sum(plan.ai_round(name, next_round)) > 0
            for name in ("OpenAI", "Claude"))
            # ... and a provider that was not switched off still has somewhere to search (ai_orchestrator.discovery_left)
            and (not hasattr(ai, "discovery_left") or ai.discovery_left(next_round)))
        unasked = self._ledger(state).free(getattr(state, "query_tail", []))      # of the plan beyond SearXNG's budget
        model_more = self._tsweb_budget(state, next_round) > 0 and (queries_left > 0 or unasked > 0)
        searx_more = (queries_left > 0 and self.searx_on and not getattr(state, "searx_down", False)) or model_more
        tiny_more = self.tiny_on and not stats["tiny_skipped"] and stats["tiny_searches"] < plan.tiny_searches and unasked > 0
        serp_more = self.serp_on and not stats["serp_skipped"] and stats["serp_calls"] < plan.serp_calls and getattr(state, "serp_left", 0) > 0
        aws_more = (self.aws_on and not stats["aws_skipped"] and stats["aws_queries"] < stats["aws_budget"]
                    and bool(getattr(state, "aws_pairs", None)))
        return bool(ai_more or searx_more or tiny_more or serp_more or aws_more)

    # ------------------------------------------------------------ pipeline
    async def _pipeline(self, state, req, resume_text, ai, target, base_profile, related0,
                        profile_task, discover_task, refresh_task, expand_task=None, known_names=()) -> None:
        stats, plan = state.stats, state.plan
        scope = _scope_of(state)
        wanted = {t.lower() for t in req.titles}
        use_web = ai.available and req.deep_search
        preview_size = max(300, target * 3)             # previews only need the head of the list (memory)

        def first_verified() -> None:
            # The first moment postings confirmed by a current read of their source (and screened) existed.
            if stats["seconds_to_first_verified"] is None:
                stats["seconds_to_first_verified"] = round(time.time() - state.created, 1)

        # 1. Preliminary results from what the index already holds (no network).
        state.emit("titles", "Identifying relevant job titles")
        early = await self._collect(req, related0, {}, max_age=self.settings.index_stale_ok_seconds, details=False, scope=scope)
        indexed = list(early) if hasattr(ai, "rerank_start") else []     # what the index holds, for the reranker's early pass
        known_ready = getattr(state, "known_employers", None)
        if known_ready is not None and not known_ready.done():
            known_ready.set_result(self._known_employers(early))     # the first AI web search waits for exactly this
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
        # The reranker starts on the postings the index already holds (up to a day old; most are read again unchanged
        # below, and an unchanged text is the same question). Its query needs the profile, hence not earlier.
        self._prerank(ai, profile, req, indexed, related)
        del indexed
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
        hosts = self._hosts(state)                      # chosen platforms only; startup platforms first under the focus
        all_queries = scope.order_queries(build_queries([*req.titles, *variants, *related[:8]], hosts, locations))
        queries = all_queries[: plan.searx_queries] if self.engines_on else []
        # Every engine takes a different part of this plan (see _searx_round and the search's ledger): SearXNG its
        # budget from the top; the company model's web search and TinySearch what SearXNG will not reach in this
        # search (everything, when SearXNG is off); AWS its own (title, place) pairs on hosts nobody asked them on.
        state.query_plan = all_queries
        state.query_tail = all_queries[plan.searx_queries:] if self.searx_on else all_queries
        used = plan.per_round(plan.searx_queries, 1)
        searx_task = asyncio.create_task(self._searx_round(state, queries[:used])) if queries else None
        if searx_task:
            state.side_tasks.append(searx_task)
        queries = queries[used:]
        # The same engines are also asked a few queries that are not held to a host: postings on employers' own sites.
        open_task = None
        if use_web and plan.open_engine_queries > 0 and not scope.filtered and (self.searx_on or self.tiny_on or self.ollama_on):
            open_task = asyncio.create_task(self._open_engine_round(state, req, [*req.titles, *variants], after=searx_task,
                                                                    ai=ai, profile=profile))
            state.side_tasks.append(open_task)
        # Paid sources wait for round 2: they are spent only when the free ones left the search short.
        # SerpApi gets title x location pairs.
        places = [f"{p}, United States" for p in req.locations[:6]] or ["United States"]
        # Google Jobs listings are not postings of a chosen platform: left out under a platform filter.
        serp_pairs = [(t, p) for p in places for t in [*req.titles, *variants]] if self.serp_on and not scope.filtered else []
        state.serp_left = len(serp_pairs)
        # AWS web search runs from round 1 (fast, own monthly allowance): title variants x US locations, paced.
        aws_task = None
        if self.aws_on and (hosts or not scope.filtered):
            await self._aws_prepare(state, req, [*req.titles, *variants], locations)
            # Like the AI web search: the base budget at once in the first round (a query takes under a second and the
            # round runs beside the reading of the sources), what a larger request adds in the later rounds.
            first = plan.first_round(stats["aws_budget"], self.settings.aws_web_search_max_calls_per_search, 1)
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
        kept = await self._collect(req, related, {}, counts=counts, state=state, scope=scope)
        stats["unique_candidates"] = stats["verified_active"] = len(kept)      # so far; final numbers follow deduplication
        if kept:
            first_verified()
            head = sorted(kept, key=lambda j: -j.title_relevance)[:preview_size]
            preview, _ = await asyncio.to_thread(deduplicate, [j.model_copy(deep=True) for j in head])
            self._score(preview, profile, req, related)
            preview.sort(key=self._order)
            state.publish(self._select(preview, target), partial=True)
            if "first_results" not in state.timings:
                state.mark("first_results")
            state.emit("boards", "Preliminary matches updated")
            del head, preview
        # The company reranker reads about 20 postings a second whatever the batch size (measured 2026-11-21), so 800
        # postings cost about 40 s. The verified postings are known now and the lead checks below take about as long:
        # their relevance is computed meanwhile and remembered, and the ranking stage only sends what is new by then.
        self._prerank(ai, profile, req, kept, related)

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
            self._discovery_notes(state, ai)
        searx_leads: list[Candidate] = (await searx_task) if searx_task else []
        if aws_task:
            searx_leads = [*searx_leads, *(await aws_task)]
        if open_task:
            searx_leads = [*searx_leads, *(await open_task)]
        expected: dict[str, set[str]] = {}
        lead_jobs: list[Job] = []
        seen_leads: set[str] = set()
        lead_counts = dict.fromkeys(REJECT_KEYS, 0)
        lead_kept: list[Job] = []
        lead_by: dict = {}                              # per platform: postings read from web leads, and why some were screened out
        platform_leads: dict[str, int] = {}             # per platform: addresses named by a search engine or an AI provider
        provider_leads: dict[str, int] = {}             # per provider: the same addresses
        duplicates_by: dict = {}
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
                    platform = platform_of_url(lead.url)
                    if not scope.allows(platform):          # platform filter: another platform, or no ATS platform at all
                        stats["filtered_out"] += 1
                        continue
                    # A career platform that forbids automated reading, or whose reader is switched off (iCIMS, SAP
                    # SuccessFactors, BambooHR, Rippling, Pinpoint): the lead goes nowhere, not to a board reader and
                    # not to the page verifier, whoever named it.
                    if not scope.readable_url(lead.url):
                        stats["leads_not_readable"] += 1
                        continue
                    platform_leads[platform or OTHER] = platform_leads.get(platform or OTHER, 0) + 1
                    provider_leads[lead.provider] = provider_leads.get(lead.provider, 0) + 1
                    fresh.append(lead)
            boards_before, postings_before = stats["new_boards"], stats["new_board_postings"]
            found, jobs = await self._resolve_leads(state, req, related, fresh, ai)
            stats["pages_remembered"] += await self._remember_pages(jobs)      # company-site postings stay in the index
            for key, providers in found.items():
                expected.setdefault(key, set()).update(providers)
            if scope.filtered:                              # a page that turned out to belong to another platform
                allowed = [j for j in jobs if scope.allows(platform_of_job(j))]
                stats["filtered_out"] += len(jobs) - len(allowed)
                jobs = allowed
            lead_jobs.extend(jobs)
            lead_jobs.extend(listed_new)                    # Google Jobs listings without an employer link (marked as such)
            listed_new = []
            lead_counts = dict.fromkeys(REJECT_KEYS, 0)
            lead_by = {}
            if lead_jobs:
                relevant = []
                for job in lead_jobs:
                    bump(lead_by, job, "considered")
                    job.title_relevance = best_title_relevance(job.title, req.titles, related)
                    job.matched_query = best_matching_title(job.title, req.titles, related)
                    if job.ats == "workday" and AI_PROVIDERS & {s.replace(" web search", "") for s in job.sources}:
                        job.title_relevance = max(job.title_relevance, AI_LEAD_RELEVANCE_FLOOR)
                    if job.title_relevance < MIN_RELEVANCE:
                        lead_counts["rejected_not_relevant"] += 1
                        bump(lead_by, job, "not_relevant")
                    else:
                        relevant.append(job)
                lead_kept, screen_counts, _ = await asyncio.to_thread(self._screen, relevant, req, lead_by)
                for key, value in screen_counts.items():
                    lead_counts[key] += value
            if fresh:
                state.detail_wait = min(state.detail_wait, 6.0)      # second pass: do not wait long for paced details again
                kept = await self._collect(req, related, expected, counts=counts, state=state, scope=scope)   # lead boards are in the index now
                await self._source_report(state, failed, pending)
            unique, removed = await asyncio.to_thread(deduplicate, [*kept, *lead_kept])
            # What counts towards the target: verified jobs whose keyword score reaches the minimum match score. A job
            # that only shares a title word is not progress (a loose title variant can match hundreds of those).
            self._score(unique, profile, req, related)
            bar = self.settings.default_min_match_score
            matches = sum(1 for j in unique if j.score >= bar)
            gained = matches - (previous or 0)
            stats["unique_candidates"] = stats["verified_active"] = len(unique)
            if unique:
                first_verified()
            unique_by = count_platforms(unique)
            duplicates_by = count_platforms([*kept, *lead_kept]) - unique_by
            stats["platforms"] = platform_stats(scope, [counts.get("platforms") or {}, lead_by], unique_by, duplicates_by, {}, platform_leads)
            stats["providers"] = self._provider_stats(state, provider_leads, unique, [])
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
            # Whom the AI web search need not bring again: the employers this search already has matching postings of.
            names = self._known_employers(unique) or sorted({k.split("|")[0] for ks in (await self._keys()).values() for k in ks})

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
            aws_n = plan.first_round(stats["aws_budget"], self.settings.aws_web_search_max_calls_per_search, round_no) if self.aws_on else 0
            ai_leads, searx_leads, tiny_leads, (serp_leads, listed_new), aws_leads = await asyncio.gather(
                more_ai(), self._searx_round(state, queries[:used]), self._tiny_round(state, tiny_n),
                self._serp_round(state, serp_pairs[: max(1, serp_n)], serp_n), self._aws_round(state, aws_n))
            queries, serp_pairs = queries[used:], serp_pairs[max(1, serp_n):]
            state.serp_left = len(serp_pairs)
            self._discovery_notes(state, ai)
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
        fresh_postings = self._fresh_postings(index, scope)
        stats["total_discovered"] = fresh_postings + stats["ai_leads"] + stats["searx_leads"] + stats["crawled_links"]
        stats["rejected_not_relevant"] = max(0, fresh_postings - counts.get("relevant", 0)) + lead_counts["rejected_not_relevant"]
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
        before_rank = count_platforms(unique)
        if (ai.available or getattr(ai, "can_rerank", False)) and unique:
            unique = await self._ai_rank(state, req, resume_text, ai, profile, unique, target, order=scope.order_key(self._order))
        audited = before_rank - count_platforms(unique)      # dropped by the AI audit (not relevant, or not eligible)
        for name, st in ai.status.items():
            if st["configured"] and st["failed_calls"]:
                state.warn(f"{name} degraded: {st['failed_calls']} call(s) failed ({st['last_error']}).")
        state.mark("ranked")
        stats["rank_seconds"] = round(state.timings["ranked"] - state.timings.get("leads_verified", state.timings["ranked"]), 1)

        state.emit("prepare", "Preparing results")
        before_second = count_platforms(unique)
        unique, removed = deduplicate(unique)        # second pass: nothing may be duplicated after ranking
        stats["rejected_duplicates"] += removed
        # Startup focus: at an equal score a job of a startup platform comes first, also at the cut of the list.
        order = scope.order_key(self._order)
        unique.sort(key=order)
        final = self._select(unique, target)
        final.sort(key=order)
        stats["final_matches"] = len(final)
        unique_by = count_platforms(unique)
        stats["platforms"] = platform_stats(scope, [counts.get("platforms") or {}, lead_by], unique_by,
                                            duplicates_by + (before_second - unique_by), count_platforms(final), platform_leads, audited)
        stats["providers"] = self._provider_stats(state, provider_leads, unique, final)
        # Coverage: every chosen platform without a board reader, and whether anything was found for it.
        stats["coverage_warnings"] = scope.coverage_warnings(stats["platforms"])
        for text in stats["coverage_warnings"]:
            state.warn(text)
        state.message = self._outcome(stats, target, len(unique), ai)
        if round_no > 1:
            state.message += f" The search ran {round_no} rounds and stopped because {stats['stop_reason']}."
        state.status = "completed"
        state.publish(final, partial=False)

    def _provider_stats(self, state: SearchState, leads: dict[str, int], unique: list[Job], final: list[Job]) -> dict:
        """What each search engine and AI provider contributed, as counts only (no result text is kept anywhere):
        queries sent, result addresses returned, leads (addresses passed to verification), employer boards it was the
        first to name and their postings, and verified / matched postings whose provenance names the provider.
        Postings of a board that a provider found count under `new_board_postings`, not under `verified`."""
        stats = state.stats
        sent = {SEARX: (stats["searx_queries"] + stats["searx_cached"], stats["searx_results"]),
                TECHSARA_WEB: (stats["tsweb_searches"], stats["tsweb_results"]),
                TINYFISH: (stats["tiny_searches"], None), OLLAMA: (stats["ollama_searches"], stats["ollama_results"]), GOOGLE_JOBS: (stats["serp_calls"], stats["serp_jobs"]),
                AWS: (stats["aws_queries"], stats["aws_results"])}
        boards = getattr(state, "provider_boards", {})
        out: dict[str, dict] = {}
        for name in dict.fromkeys([*sent, *sorted(AI_PROVIDERS), *leads, *boards]):
            if not name:
                continue
            queries, results = sent.get(name, (None, None))
            labels = {name, f"{name} web search"}
            row = {"kind": "ai" if name in AI_PROVIDERS else "search_engine", "queries": queries, "results": results,
                   "leads": leads.get(name, 0), "new_boards": boards.get(name, [0, 0])[0],
                   "new_board_postings": boards.get(name, [0, 0])[1],
                   "verified": sum(1 for j in unique if labels & set(j.sources)),
                   "matched": sum(1 for j in final if labels & set(j.sources))}
            if queries or row["leads"] or row["new_boards"] or row["verified"]:
                out[name] = row
        return out

    async def _ai_rank(self, state: SearchState, req: SearchRequest, resume_text: str, ai: AIOrchestrator,
                       profile: Profile, unique: list[Job], target: int, order=None) -> list[Job]:
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
            # The reranker has been reading since the first postings were known (_prerank), best keyword matches
            # first. The stage waits a bounded time for what is still missing and goes on with what is scored: the
            # postings it did not reach are the weakest keyword matches, and they keep their keyword score. No keyword
            # shortlist cuts the list beforehand: of the postings the reranker rates as good, a shortlist of 600 lost 4
            # of 25 in one measured search and 1 of 99 in another; reading on while there is time loses fewer.
            sent = unique[: max(0, s.techsara_rerank_max_jobs)]
            stats["rerank_sent"] = len(sent)
            if hasattr(ai, "rerank_collect"):
                raw = await ai.rerank_collect(profile, req, sent, seconds=max(1.0, s.techsara_rerank_wait_seconds))
            else:
                raw = await ai.rerank(profile, req, sent)
            stats["reranked"] = len(raw)
            if len(raw) < MIN_ANCHORS:
                if unique[:1] and len(unique) >= MIN_ANCHORS:
                    state.warn("The company model did not answer; ranking used OpenAI / Claude for the top jobs and keyword scores for the rest.")
                raw = {}
            elif len(raw) < len(sent):
                state.warn(f"The company model scored the {len(raw)} best keyword matches of {len(sent)} jobs in the time it had; "
                           "the others keep their keyword score.")
        state.mark("reranked")
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
        # A posting OpenAI / Claude already judged for this candidate, with unchanged text, keeps that judgement: the
        # same pair gets the same score in every search, and no call is spent on it.
        known = await ai.recall_rankings(profile, req, pool) if pool and hasattr(ai, "recall_rankings") else {}
        for job in pool:
            if job.id in known:
                apply_ai_ranking(job, known[job.id], profile, resume_text)
                if job.score_source == "ai":
                    stats["ai_ranked"] += 1
                    stats["ai_ranked_reused"] += 1
        to_judge = [j for j in pool if j.score_source != "ai"]
        batches = [to_judge[i:i + size] for i in range(0, len(to_judge), size)]
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

        limit = time.monotonic() + max(1.0, s.ai_rank_seconds)
        try:
            try:
                await asyncio.wait_for(asyncio.gather(*(rank(b) for b in batches)), timeout=max(1.0, s.ai_rank_seconds))
            except asyncio.TimeoutError:
                state.warn(f"AI ranking did not finish within its time limit ({s.ai_rank_seconds:g} s); it stopped there.")
            try:
                audits = await asyncio.wait_for(audit_task, timeout=max(15.0, limit - time.monotonic()))
            except asyncio.TimeoutError:
                audits = {}
                state.warn("The AI audit of the shortlist did not answer in time and was skipped.")
        except BaseException:
            audit_task.cancel()
            raise
        state.mark("judged")
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
            await self._company_scores(state, req, resume_text, ai, profile, unique, raw, target, order or self._order)
        elif ai.available and len(unique) > stats["ai_ranked"]:
            note = f" Keyword-based score: this job was not among the {stats['ai_ranked']} jobs judged by AI in this search."
            for job in unique:
                if job.score_source == "heuristic" and note not in job.reason:
                    job.reason = (job.reason + note)[:500]
        unique.sort(key=self._order)
        return unique

    async def _company_scores(self, state: SearchState, req: SearchRequest, resume_text: str, ai, profile: Profile,
                              unique: list[Job], raw: dict[str, float], target: int = 0, order=None) -> None:
        """Put the reranker's relevance on the 0-100 scale, and give a written explanation to those of its jobs that
        can be delivered. Until 2026-11-21 the best 400 were explained whatever the request: for a request of 25 that
        was up to three minutes for text nobody saw, because all 25 delivered jobs were judged by OpenAI / Claude."""
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
        # The jobs that can be delivered: the selection the final step will make (same order, same spread across
        # employers), plus a margin for what the last duplicate pass may take out.
        order = order or self._order
        target = target or len(unique)
        ranked = sorted(unique, key=order)
        chosen = self._select(ranked, target)
        taken = {j.id for j in chosen}
        chosen.extend([j for j in ranked if j.id not in taken][: max(5, target // 5)])
        mine = {j.id for j in scored}
        todo = [j for j in chosen if j.id in mine][: s.techsara_explain_max_jobs]
        if todo and hasattr(ai, "recall_explanations"):          # written before for this candidate and this posting text
            known = await ai.recall_explanations(profile, req, todo)
            for job in todo:
                if job.id in known:
                    apply_explanation(job, known[job.id], profile, resume_text)
                    stats["company_model_explained"] += 1
            todo = [j for j in todo if j.id not in known]
        if not todo:
            return
        state.emit("rank", "Writing explanations with the company model")
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
