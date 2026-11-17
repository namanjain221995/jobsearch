"""Every job source in one place.

A source is read in "units": one employer board, one search query, or one whole feed. The engine
decides which units a search needs; this module only describes the sources."""
from dataclasses import dataclass
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
    Spec("workable", "Workable", "search", workable.fetch, note="Cross-company search of Workable-hosted jobs"),
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

# Not a posting source: it only finds employer boards and posting addresses, which are then read through the
# sources above (see providers/searxng.py). Listed here so it appears with the other sources and can be switched off.
async def _no_fetch(ctx: Ctx, key: str, scope: str) -> list[Job]:
    return []


SPECS["searxng"] = Spec("searxng", "Web search (SearXNG)", "discovery", _no_fetch, needs=lambda s: bool(s.searxng_url.strip()),
                        note="Search-engine discovery of employer job boards on the ATS hosts above; found boards are read through their own API")
SPECS["tinyfish"] = Spec("tinyfish", "Web search (TinyFish)", "discovery", _no_fetch, needs=lambda s: bool(s.tinyfish_api_key),
                         note="Paid second search engine for employer job boards, used after the free sources; daily spending ceiling")
SPECS["serpapi"] = Spec("serpapi", "Google Jobs (SerpApi)", "discovery", _no_fetch, needs=lambda s: bool(s.serpapi_api_key),
                        note="Paid job-listing API with a monthly allowance, used after the free sources; job-board pages are never fetched")
BOARD_LEAD_SOURCES = ("greenhouse", "lever", "ashby")      # AI leads on these are checked against the employer's full board


def enabled_specs(settings: Settings) -> dict[str, Spec]:
    off = {s.strip().lower() for s in settings.disabled_sources.split(",") if s.strip()}
    return {name: spec for name, spec in SPECS.items() if name not in off and spec.needs(settings)}


def ttl(spec: Spec, settings: Settings) -> int:
    return max(settings.index_ttl_seconds, spec.min_ttl)


def plan_units(specs: dict[str, Spec], keys: dict[str, list[str]], queries: list[str]) -> list[Unit]:
    """Units one search wants fresh: every known board, each feed, and each query against the search sources."""
    units: list[Unit] = []
    for name, spec in specs.items():
        if spec.kind == "discovery":
            continue
        targets = keys.get(name, []) if spec.keyed else [spec.default_key]
        wanted = queries[: spec.max_queries] if spec.max_queries else queries
        for key in targets:
            if spec.kind == "search":
                units.extend(Unit(name, key, q) for q in wanted)
            else:
                units.append(Unit(name, key, ""))
    return units
