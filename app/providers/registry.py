"""Every job source in one place.

A source is read in "units": one employer board, one search query, or one whole feed. The engine
decides which units a search needs; this module only describes the sources."""
from dataclasses import dataclass, replace
from typing import Awaitable, Callable

from ..config import Settings
from ..schemas import Job
from . import adzuna, amazon, ashby, greenhouse, lever, remote_boards, workable, workday
from .base import Ctx


@dataclass(frozen=True)
class Unit:
    src: str
    key: str
    scope: str = ""


@dataclass(frozen=True)
class Spec:
    name: str
    label: str                    # shown to users and stored as Job.source
    kind: str                     # "board": one employer, all postings / "search": query / "feed": whole feed
    fetch: Callable[[Ctx, str, str], Awaitable[list[Job]]]
    keyed: bool = False           # True: one unit per known employer key (seed list + discovered)
    default_key: str = "*"
    detail: Callable[[Ctx, Job], Awaitable[Job | None]] | None = None
    min_ttl: int = 0              # floor for the refresh interval (API etiquette of the source)
    max_queries: int = 0          # cap on title queries per search for this source (0 = no cap)
    wait: float = 0.0             # longest a search waits for this source (0 = the normal deadline); it keeps loading after
    needs: Callable[[Settings], bool] = lambda s: True
    note: str = ""


async def _greenhouse(ctx: Ctx, key: str, scope: str) -> list[Job]:
    async with ctx.limiter.slot("greenhouse"):
        return await greenhouse.fetch_board(ctx.client, key, timeout=ctx.timeout)


async def _lever(ctx: Ctx, key: str, scope: str) -> list[Job]:
    async with ctx.limiter.slot("lever"):
        return await lever.fetch_board(ctx.client, key, timeout=ctx.timeout)


async def _ashby(ctx: Ctx, key: str, scope: str) -> list[Job]:
    async with ctx.limiter.slot("ashby"):
        return await ashby.fetch_board(ctx.client, key, timeout=max(ctx.timeout, 60.0))


async def _workday(ctx: Ctx, key: str, scope: str) -> list[Job]:
    return await workday.fetch(ctx, key, scope, pages=int(ctx.options.get("workday_pages", 1)))


SPECS: dict[str, Spec] = {s.name: s for s in [
    Spec("greenhouse", "Greenhouse", "board", _greenhouse, keyed=True, note="Employer boards, official Job Board API"),
    Spec("ashby", "Ashby", "board", _ashby, keyed=True, note="Employer boards, official posting API"),
    Spec("lever", "Lever", "board", _lever, keyed=True, note="Employer boards, official postings API"),
    Spec("workday", "Workday", "search", _workday, keyed=True, detail=workday.detail, min_ttl=3 * 3600, max_queries=1, wait=20.0,
         note="Large-employer career sites, searched per employer (Workday allows about one request per second)"),
    # Workable is read two ways by one entry (providers/workable.py): key "*" = the cross-company search, any other key =
    # one employer account through the documented public endpoint (catalogue accounts, read by board_catalog).
    Spec("workable", "Workable", "search", workable.fetch,
         note="Employer accounts through Workable's documented public jobs endpoint, plus its cross-company job search"),
    Spec("amazon", "Amazon Jobs", "search", amazon.fetch, note="Amazon's own job search"),
    Spec("himalayas", "Himalayas", "search", remote_boards.fetch_himalayas, min_ttl=6 * 3600, note="Remote job board API"),
    Spec("remoteok", "Remote OK", "feed", remote_boards.fetch_remoteok, min_ttl=2 * 3600, note="Remote job board API"),
    Spec("jobicy", "Jobicy", "feed", remote_boards.fetch_jobicy, default_key="usa", min_ttl=2 * 3600, note="Remote job board API"),
    Spec("adzuna", "Adzuna", "search", adzuna.fetch, default_key="us", min_ttl=2 * 3600,
         needs=lambda s: bool(s.adzuna_app_id and s.adzuna_app_key), note="Aggregator API (optional, needs free key)"),
]}
# Additional board readers (SmartRecruiters, BambooHR): returned only when switched on, both are off by default
# (docs/board-sources.md explains why).
from . import boards as _boards  # noqa: E402

SPECS.update(_boards.extra_specs())

# Startup-focused ATS boards (Recruitee, Pinpoint, Rippling): anonymous career-site endpoints, one unit per employer board.
# The findings on robots.txt and terms are in the docstring of providers/ats_startup.py.
from . import ats_startup as _startup  # noqa: E402

SPECS.update({s.name: s for s in [
    Spec("recruitee", "Recruitee", "board", _startup.fetch_recruitee, keyed=True, min_ttl=3 * 3600,
         needs=lambda s: bool(getattr(s, "recruitee_enabled", True)), note="Employer boards, official Careers Site API"),
    Spec("pinpoint", "Pinpoint", "board", _startup.fetch_pinpoint, keyed=True, min_ttl=3 * 3600,
         needs=lambda s: bool(getattr(s, "pinpoint_enabled", False)),
         note="Employer boards, official job postings JSON feed (off by default: most tenants disallow robots)"),
    Spec("rippling", "Rippling", "board", _startup.fetch_rippling, keyed=True, detail=_startup.detail_rippling, min_ttl=3 * 3600,
         needs=lambda s: bool(getattr(s, "rippling_enabled", False)),
         note="Employer boards, the job list of Rippling's hosted career pages (off by default until Rippling's terms are "
              "reviewed: the endpoints are undocumented)"),
]})

# Oracle Recruiting Cloud career sites: built and verified, but OFF unless BOARD_SOURCES_ORACLE=1, because Oracle documents
# the endpoint as "only for Oracle internal use" (providers/ats_oracle.py, docs/ats-platforms.md).
from . import ats_oracle as _oracle  # noqa: E402

if _oracle.ENABLED:
    SPECS["oracle"] = Spec("oracle", "Oracle Recruiting Cloud", "search", _oracle.fetch, keyed=True, detail=_oracle.detail,
                           min_ttl=3 * 3600, max_queries=1, wait=20.0,
                           note="Employer career sites on Oracle Recruiting Cloud, searched per employer site")

# Not a posting source: it only finds employer boards and posting addresses, which are then read through the
# sources above (see providers/searxng.py). Listed here so it appears with the other sources and can be switched off.
async def _no_fetch(ctx: Ctx, key: str, scope: str) -> list[Job]:
    return []


SPECS["searxng"] = Spec("searxng", "Web search (SearXNG)", "discovery", _no_fetch, needs=lambda s: bool(s.searxng_url.strip()),
                        note="Search-engine discovery of employer job boards on the ATS hosts above; found boards are read through their own API")
SPECS["ollama_web"] = Spec("ollama_web", "Web search (Ollama)", "discovery", _no_fetch, needs=lambda s: bool(getattr(s, "ollama_api_key", "")),
                           note="Ollama's web search API: the same site: and open-web queries as the other engines; only result addresses are used")
SPECS["tinyfish"] = Spec("tinyfish", "Web search (TinyFish)", "discovery", _no_fetch, needs=lambda s: bool(s.tinyfish_api_key),
                         note="Paid second search engine for employer job boards, used after the free sources; daily spending ceiling")
SPECS["techsara_web"] = Spec("techsara_web", "Web search (TechSara model)", "discovery", _no_fetch,
                             needs=lambda s: bool(s.techsara_configured and s.techsara_web_search_enabled),
                             note="The company model's own web search tool, asked the same `site:` queries; only result addresses are used")
SPECS["serpapi"] = Spec("serpapi", "Google Jobs (SerpApi)", "discovery", _no_fetch, needs=lambda s: bool(s.serpapi_api_key),
                        note="Paid job-listing API with a monthly allowance, used after the free sources; job-board pages are never fetched")
SPECS["aws_web_search"] = Spec("aws_web_search", "Web search (AWS)", "discovery", _no_fetch, needs=lambda s: bool(s.aws_web_search_enabled),
                               note="AWS Bedrock AgentCore Web Search: finds employer postings on the ATS hosts above, verified against the "
                                    "employer's live feed; monthly allowance; result text is never stored")
# Postings on employers' OWN career sites that a search verified on their page (search_engine._remember_pages). Each is
# one unit, keyed by the posting's address, and "reading" it means checking that page again (plain request, no browser,
# no model). So jobs the AI web search found once stay in the index, are kept current by the background reader and are
# found by later searches without any web search: the index grows beyond the ATS platforms. The engine supplies the
# reader, because the page verifier is its own (Ctx.options["career_page_reader"]).
# The career-site crawler (app/career_sites.py) stores what it finds on a company's site as units of this same source.
CAREER_PAGE = "career_page"


async def _career_page(ctx: Ctx, key: str, scope: str) -> list[Job]:
    reader = ctx.options.get("career_page_reader")
    if reader is None:
        from .base import SourceError
        raise SourceError("the page reader is not available")
    return await reader(key)


SPECS[CAREER_PAGE] = Spec(CAREER_PAGE, "Company career sites", "board", _career_page, keyed=True, min_ttl=3 * 3600,
                          needs=lambda s: bool(getattr(s, "career_page_memory", True)),
                          note="Postings on employers' own career sites that a search verified or the career-site crawler found; each is checked again on its own page")
BOARD_LEAD_SOURCES = ("greenhouse", "lever", "ashby", "recruitee", "pinpoint", "rippling")      # AI leads on these are checked against the employer's full board


def enabled_specs(settings: Settings) -> dict[str, Spec]:
    off = {s.strip().lower() for s in settings.disabled_sources.split(",") if s.strip()}
    specs = {name: spec for name, spec in SPECS.items() if name not in off and spec.needs(settings)}
    if "workable" in specs and not getattr(settings, "workable_search_enabled", True):
        specs["workable"] = replace(specs["workable"], default_key="")      # accounts only: no cross-company query is planned
    return specs


def ttl(spec: Spec, settings: Settings) -> int:
    return max(settings.index_ttl_seconds, spec.min_ttl)


def plan_units(specs: dict[str, Spec], keys: dict[str, list[str]], queries: list[str]) -> list[Unit]:
    """Units one search wants fresh: every known board, each feed, and each query against the search sources."""
    units: list[Unit] = []
    for name, spec in specs.items():
        if spec.kind == "discovery":
            continue
        targets = keys.get(name, []) if spec.keyed else [spec.default_key] if spec.default_key else []
        wanted = queries[: spec.max_queries] if spec.max_queries else queries
        for key in targets:
            if spec.kind == "search":
                units.extend(Unit(name, key, q) for q in wanted)
            else:
                units.append(Unit(name, key, ""))
    return units


def within_budget(units: list[Unit], limit: int) -> list[Unit]:
    """The units a search keeps when it planned more than its budget of `limit`.

    Query and feed units (one per title and source: a handful) are kept first, so the cut only ever falls on employer
    boards. Without this the list was cut in registry order, and once the index knew more Greenhouse boards than the
    budget (production, 2026-11-20: 6,629 boards against a budget of 1,500) every source registered after Greenhouse,
    among them the Workable, Amazon and Himalayas searches and the Remote OK and Jobicy feeds, was never asked.
    The order inside each of the two groups is unchanged."""
    if len(units) <= limit:
        return list(units)

    def board(unit: Unit) -> bool:
        spec = SPECS.get(unit.src)
        return bool(spec and spec.keyed) or (unit.src == "workable" and unit.key != "*")
    return [*(u for u in units if not board(u)), *(u for u in units if board(u))][: max(0, limit)]
