"""Search-engine discovery through a SearXNG meta-search instance (JSON API).

What it is used for: finding employer job boards on applicant-tracking systems this app can already read
through their official API or public page. For every title variant and ATS host one paced `site:` query is
sent. Results are only *addresses*; nothing is taken from a result's title or snippet:

- an address of an employer board (Greenhouse, Lever, Ashby, Workable account, Workday tenant, Recruitee,
  Pinpoint, Rippling) names a board.
  A board the index does not know yet is registered and then read through its API like every other board;
- an address of a single posting is a lead and goes through the same verification as an AI web-search lead.

Hosts outside the configured ATS list are ignored, and hosts that forbid automated access (LinkedIn,
Indeed, Glassdoor, ZipRecruiter, ...) are never followed, whatever a result says.

The instance is infrastructure chosen by the operator (SEARXNG_URL), like the database: it may live on a
private address and is contacted directly. Every address it *returns* is untrusted and is only ever fetched
through the SSRF guard by the existing source and verification code."""
import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from ..job_normalizer import canonical_url, parse_ats
from ..job_verifier import host_blocked
from . import ats_pages, ats_startup

CACHE_PREFIX = "searx:"
MAX_RESULTS_KEPT = 40
TIME_RANGES = ("day", "week", "month", "year")      # SearXNG's `time_range` values
BOARD_SOURCES = ("greenhouse", "lever", "ashby", "workable", "workday", "recruitee", "pinpoint", "rippling")
_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
_WD_TENANT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_WD_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_WD_DC = re.compile(r"wd\d{1,3}")
# Path words on ATS hosts that are pages of the ATS itself, not an employer.
_NOT_A_BOARD = {"embed", "api", "j", "jobs", "job", "search", "login", "signin", "signup", "privacy", "terms", "about",
                "robots.txt", "sitemap.xml", "favicon.ico", "static", "assets", "careers", "undefined", "null"}
# Hosts whose results are single postings to verify by page (no board API in this app).
PAGE_LEAD_HOSTS = ("jobs.smartrecruiters.com",)


@dataclass(frozen=True)
class Hit:
    kind: str            # "board" (an employer board), "posting" (one posting on a board ATS), "page" (posting page to verify)
    src: str             # one of BOARD_SOURCES / "" for page leads
    board: str
    job_id: str
    url: str


def valid_board(src: str, key: str) -> bool:
    """Whether `key` is a well-formed employer key for `src`. Keys are placed into API addresses, so nothing but
    plain slug characters is accepted (no dots-only names, no separators, no percent-encoding)."""
    if src == "workday":
        parts = key.split("|")
        return (len(parts) == 3 and bool(_WD_TENANT.fullmatch(parts[0])) and bool(_WD_DC.fullmatch(parts[1]))
                and bool(_WD_SITE.fullmatch(parts[2])))
    if src in ("greenhouse", "lever", "ashby", "workable"):
        return bool(_SLUG.fullmatch(key)) and key not in _NOT_A_BOARD and ".." not in key and not key.strip(".-_") == ""
    if src in ats_startup.SLUGS:                     # Recruitee / Pinpoint subdomain, Rippling board slug
        return ats_startup.valid_key(src, key)       # the same rule the catalogue import applies
    return False


def site_hosts(setting: str) -> list[str]:
    hosts = []
    for raw in (setting or "").split(","):
        host = raw.strip().lower().removeprefix("www.")
        if host and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) and not host_blocked("https://" + host + "/"):
            hosts.append(host)
    return list(dict.fromkeys(hosts))


def _host_allowed(host: str, allowed: list[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in allowed)


def extract(url: str, allowed_hosts: list[str]) -> Hit | None:
    """Classify one result address. None = ignore it (wrong host, forbidden host, malformed, not a board)."""
    if not isinstance(url, str) or len(url) > 600 or "\n" in url or "\r" in url or "\\" in url:
        return None
    try:
        p = urlsplit(url.strip())
        port = p.port
    except ValueError:
        return None
    host = (p.hostname or "").lower().removeprefix("www.")
    if p.scheme != "https" or not host or p.username or p.password or port not in (None, 443):
        return None
    if host_blocked(url) or not _host_allowed(host, allowed_hosts):
        return None
    if any(host == h for h in PAGE_LEAD_HOSTS):
        parts = [s for s in p.path.split("/") if s]
        if len(parts) == 2 and re.fullmatch(r"\d{6,}[A-Za-z0-9%._-]*", parts[1]) and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", parts[0]):
            return Hit("page", "", "", "", canonical_url(url))
        return None
    if ats_pages.is_page_host(host):                  # public career pages without a board reader (providers/ats_pages.py)
        found = ats_pages.posting(url)
        return Hit("page", "", "", "", found[1]) if found else None
    parsed = parse_ats(url)
    if not parsed:
        return None
    src, board, job_id = parsed
    if src not in BOARD_SOURCES or not board or not valid_board(src, board):
        return None
    if job_id and not re.fullmatch(r"[A-Za-z0-9:_-]{1,120}", job_id):
        return None
    return Hit("posting" if job_id else "board", src, board, job_id, url.strip())


def build_queries(titles: list[str], hosts: list[str], locations: list[str]) -> list[str]:
    """The whole query plan of one search, most valuable first: every title on every ATS host, then the same
    with a location word. One `site:` per query (several in one query return unrelated pages)."""
    out: list[str] = []
    clean = [" ".join(re.sub(r'["\\]', " ", t).split()) for t in titles]
    clean = [t for t in dict.fromkeys(clean) if t]
    for title in clean:
        out.extend(f'site:{host} "{title}"' for host in hosts)
    for location in locations:
        loc = " ".join(re.sub(r'["\\]', " ", location).split())
        if not loc:
            continue
        for title in clean:
            out.extend(f'site:{host} "{title}" {loc}' for host in hosts)
    return list(dict.fromkeys(out))


@dataclass
class SearxAnswer:
    urls: list[str] = field(default_factory=list)
    cached: bool = False
    error: str = ""
    unresponsive: list[str] = field(default_factory=list)
    pages: int = 0            # result pages actually requested from the instance
    ranged: bool = False      # the query was asked with a time range
    widened: bool = False     # ... it brought nothing usable and was asked again without one (these are that answer's results)


class SearxClient:
    """One per engine process, shared by every search it runs: the rate limit is per instance."""

    def __init__(self, base_url: str, client: httpx.AsyncClient, index=None, timeout: float = 12.0,
                 min_interval: float = 2.0, cache_hours: float = 12.0, sleep=asyncio.sleep, clock=time.monotonic):
        self.base_url = (base_url or "").rstrip("/")
        self.client = client
        self.index = index
        self.timeout = timeout
        self.min_interval = max(0.0, min_interval)
        self.cache_seconds = max(0.0, cache_hours) * 3600.0
        self._sleep = sleep
        self._clock = clock
        self._gate = asyncio.Lock()
        self._next = 0.0
        self._memory: dict[str, tuple[float, list[str]]] = {}
        self.requests = 0

    @property
    def enabled(self) -> bool:
        return self.base_url.startswith(("http://", "https://"))

    @staticmethod
    def _key(query: str) -> str:
        return CACHE_PREFIX + hashlib.sha1(query.encode()).hexdigest()[:24]

    async def _cached(self, query: str) -> list[str] | None:
        if self.cache_seconds <= 0:
            return None
        key = self._key(query)
        hit = self._memory.get(key)
        if hit is None and self.index is not None:
            try:
                raw = (await self.index.meta_prefix(key)).get("")
                data = json.loads(raw) if raw else None
                if isinstance(data, dict) and isinstance(data.get("u"), list):
                    hit = (float(data.get("t") or 0), [str(u) for u in data["u"]])
                    self._memory[key] = hit
            except Exception:
                hit = None
        if hit and time.time() - hit[0] < self.cache_seconds:
            return hit[1]
        return None

    async def _remember(self, query: str, urls: list[str]) -> None:
        if self.cache_seconds <= 0:
            return
        key, now = self._key(query), time.time()
        if len(self._memory) > 5000:
            self._memory.clear()
        self._memory[key] = (now, urls)
        if self.index is not None:
            try:
                await self.index.meta_set(key, json.dumps({"t": now, "u": urls}))
            except Exception:
                pass

    async def _page(self, query: str, pageno: int, time_range: str = "") -> tuple[list[str], list[str]]:
        async with self._gate:                                # one request at a time, spaced by min_interval
            wait = self._next - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._next = self._clock() + self.min_interval
            self.requests += 1
            params = {"q": query, "format": "json", "pageno": pageno, "safesearch": 0,
                      "categories": "general"}                                  # web engines only: no image/news engines
            if time_range in TIME_RANGES:
                params["time_range"] = time_range                               # results the engines date within that span
            r = await self.client.get(self.base_url + "/search", timeout=self.timeout, follow_redirects=False,
                                      params=params, headers={"Accept": "application/json"})
        if r.status_code == 429:
            self._next = max(self._next, self._clock() + 30.0)       # the instance asks for a pause: give it one
            raise httpx.HTTPStatusError("429", request=r.request, response=r)
        if r.status_code != 200:
            raise httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("unexpected answer")
        urls = [str(row.get("url")) for row in data.get("results") or [] if isinstance(row, dict) and row.get("url")]
        down = [str(e[0]) if isinstance(e, (list, tuple)) and e else str(e) for e in data.get("unresponsive_engines") or []]
        return urls, down

    async def search(self, query: str, pages: int = 1, useful=None, time_range: str = "") -> SearxAnswer:
        """Result addresses for one query. Never raises: a slow, empty or unreachable instance is an answer
        with `error` set (or no addresses). Page n+1 is read only while page n brought something `useful`.

        `time_range` ("day", "week", "month", "year"): ask for results the engines date within that span, for a
        search with a posting-age limit. Public engines date few pages, so a restricted query can come back with
        nothing although fresh postings exist: when it brought nothing `useful`, the same query is asked once more
        without the restriction and that answer is returned (`widened`). A restriction never hides a result."""
        if not self.enabled:
            return SearxAnswer(error="not configured")
        time_range = time_range if time_range in TIME_RANGES else ""
        if time_range:
            ranged = await self._search(f"{query}\x1ftime_range={time_range}", query, pages, useful, time_range)
            found = any(useful(u) for u in ranged.urls) if useful is not None else bool(ranged.urls)
            if ranged.error or found:
                ranged.ranged = True
                return ranged
            answer = await self._search(query, query, pages, useful, "")
            answer.ranged = answer.widened = True
            answer.pages += ranged.pages
            answer.cached = answer.cached and ranged.cached
            return answer
        return await self._search(query, query, pages, useful, "")

    async def _search(self, cache_key: str, query: str, pages: int, useful, time_range: str) -> SearxAnswer:
        cached = await self._cached(cache_key)
        if cached is not None:
            return SearxAnswer(urls=list(cached), cached=True)
        answer = SearxAnswer()
        seen: dict[str, None] = {}
        for pageno in range(1, max(1, pages) + 1):
            try:
                urls, down = await self._page(query, pageno, time_range)
            except httpx.TimeoutException:
                answer.error = "timed out"
                break
            except httpx.HTTPStatusError as exc:
                answer.error = f"HTTP {exc.response.status_code}"
                break
            except (httpx.HTTPError, ValueError):
                answer.error = "unreachable or invalid answer"
                break
            answer.pages += 1
            answer.unresponsive = list(dict.fromkeys([*answer.unresponsive, *down]))
            fresh = [u for u in urls if u not in seen]
            for u in fresh:
                seen[u] = None
            if not fresh or (useful is not None and not any(useful(u) for u in fresh)):
                break
        answer.urls = list(seen)[:MAX_RESULTS_KEPT]
        if answer.pages and not answer.error:
            await self._remember(cache_key, answer.urls)         # an empty answer is cached too: do not ask again for hours
        return answer
