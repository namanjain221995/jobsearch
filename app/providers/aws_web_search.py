"""AWS Bedrock AgentCore Web Search (boto3, service `bedrock-agentcore`, operation `InvokeWebSearch`).

A discovery source: like SearXNG, only the result *addresses* are used. An address on an ATS this app reads (or a
board root on one) becomes a lead that the normal verification path resolves against the employer's own live feed;
nothing is a job until that feed lists it. Generic pages (search, category and aggregator pages) are never jobs.

Data use (AWS Web Search terms): result titles, text extracts and rankings are used inside one request / one search
only, in memory. They are never written to the database, the job index, a cache or a log. What the engine may store
is what it stores from every other source: the employer's own posting, read from the employer's feed, with the
employer address as source URL (its provenance may say "AWS web search").

Allowance: one InvokeWebSearch call is one query against the monthly allowance (AWS_WEB_SEARCH_MONTHLY_LIMIT,
4,000). A unit is reserved atomically in PostgreSQL *before* each attempt (see `PgQuota`), across the API and
worker processes. botocore's own retries are switched off (total_max_attempts=1) so every request that reaches
AWS has a reserved unit; this module retries throttling / 5xx / timeouts itself, reserving a unit per attempt.

Credentials come from boto3's default chain (on EC2: the instance profile through IMDSv2). Nothing here reads,
logs or returns credentials, and log lines never contain the query text."""
import asyncio
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("jobfinder.aws_web_search")

AWS = "AWS"                                   # Candidate.provider; shown as "AWS web search" in a job's provenance
DISCOVERED_VIA = "aws_web_search"
# Limits of the official schema (botocore 1.44.5, bedrock-agentcore InvokeWebSearch).
MAX_QUERY_CHARS = 200
MAX_RESULTS = 25
MAX_DOMAINS = 100
_HOSTNAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
_WEB_SEARCH_ID = re.compile(r"[a-z0-9]+([.-][a-z0-9]+)*")
RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "InternalServerException", "timeout", "unavailable"}


class AwsWebSearchError(Exception):
    """A clean, internal error. `kind`: budget | throttled | access_denied | validation | not_found | unavailable |
    timeout | credentials | disabled | error. `message` never contains query text or credentials."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind
        self.message = message or kind


@dataclass
class WebResult:
    """One result, held in memory only for the life of one request / search."""
    url: str
    title: str = ""
    text: str = ""
    published_at: datetime | None = None


@dataclass
class CallResult:
    results: list[WebResult] = field(default_factory=list)
    queries: int = 0                  # units reserved (attempts that may have reached AWS)


def month_key(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).strftime("%Y-%m")


def valid_domains(hosts) -> list[str]:
    out = []
    for raw in hosts or []:
        host = str(raw).strip().lower().removeprefix("www.")
        if host and len(host) <= 253 and _HOSTNAME.fullmatch(host):
            out.append(host)
    return list(dict.fromkeys(out))[:MAX_DOMAINS]


def build_request(web_search_id: str, query: str, max_results: int, include=None, exclude=None,
                  published_after: datetime | None = None) -> dict:
    """The InvokeWebSearch parameters. Include and exclude are never sent together: the API documents both under
    domainFilter but not their combination, so this module uses one or the other (include wins)."""
    query = " ".join(str(query or "").split())[:MAX_QUERY_CHARS].strip()
    if not query:
        raise AwsWebSearchError("validation", "empty query")
    if not _WEB_SEARCH_ID.fullmatch(web_search_id or "") or len(web_search_id) > 100:
        raise AwsWebSearchError("validation", "invalid web search id")
    params: dict[str, Any] = {"webSearchId": web_search_id, "searchQuery": query,
                              "maxResults": max(1, min(MAX_RESULTS, int(MAX_RESULTS if max_results is None else max_results)))}
    filters: dict[str, Any] = {}
    inc, exc = valid_domains(include), valid_domains(exclude)
    if inc:
        filters["domainFilter"] = {"include": inc}
    elif exc:
        filters["domainFilter"] = {"exclude": exc}
    if published_after is not None:
        filters["publishedDateFilter"] = {"publishedAfter": published_after}
    if filters:
        params["filters"] = filters
    return params


# ---------------------------------------------------------------- monthly allowance
class Quota:
    """Monthly allowance. `reserve(interactive)` takes one unit or returns False. Background searches may use the
    allowance up to limit - reserve; the interactive endpoint may use all of it."""
    limit: int
    reserve_units: int

    async def reserve(self, interactive: bool = False) -> bool: ...
    async def release(self) -> None: ...
    async def used(self) -> int: ...

    def ceiling(self, interactive: bool) -> int:
        return max(0, self.limit if interactive else self.limit - self.reserve_units)

    async def remaining(self, interactive: bool = False) -> int:
        return max(0, self.ceiling(interactive) - await self.used())

    async def usage(self) -> dict:
        used = await self.used()
        return {"month": self._month(), "used": used, "limit": self.limit, "reserve": self.reserve_units,
                "remaining": max(0, self.limit - used), "remaining_for_searches": max(0, self.ceiling(False) - used)}

    def _month(self) -> str:
        return month_key(self.clock())


class MemoryQuota(Quota):
    """One process only (tests, a single-process tool). Production uses PgQuota."""

    def __init__(self, limit: int = 4000, reserve: int = 0, clock=time.time):
        self.limit, self.reserve_units, self.clock = max(0, limit), max(0, reserve), clock
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()

    async def reserve(self, interactive: bool = False) -> bool:
        with self._lock:
            month = self._month()
            if self._used.get(month, 0) >= self.ceiling(interactive):
                return False
            self._used[month] = self._used.get(month, 0) + 1
            return True

    async def release(self) -> None:
        with self._lock:
            month = self._month()
            self._used[month] = max(0, self._used.get(month, 0) - 1)

    async def used(self) -> int:
        return self._used.get(self._month(), 0)


RESERVE_SQL = ("UPDATE aws_web_search_usage SET used = used + 1, updated_at = now() "
               "WHERE month = $1 AND used < $2 RETURNING used")
ENSURE_SQL = "INSERT INTO aws_web_search_usage (month, used) VALUES ($1, 0) ON CONFLICT (month) DO NOTHING"
RELEASE_SQL = "UPDATE aws_web_search_usage SET used = used - 1, updated_at = now() WHERE month = $1 AND used > 0"
USED_SQL = "SELECT used FROM aws_web_search_usage WHERE month = $1"


class PgQuota(Quota):
    """The month's counter is one row of aws_web_search_usage (Alembic revision b5e8d2c47a19). Reserving is a single
    conditional UPDATE: PostgreSQL takes the row lock, re-checks `used < ceiling` on the latest row version and
    increments, so any number of coroutines in the API and worker processes together can never pass the ceiling.
    The table holds only (month, used, updated_at): no query, no result. `db` is an asyncpg pool or a SQLAlchemy
    AsyncEngine (the API's)."""

    def __init__(self, db, limit: int = 4000, reserve: int = 0, clock=time.time):
        self.db, self.limit, self.reserve_units, self.clock = db, max(0, limit), max(0, reserve), clock
        self._ensured: set[str] = set()

    async def _run(self, sql: str, *args, fetch: bool = True):
        db = self.db
        if hasattr(db, "fetchval"):                              # asyncpg pool
            return await (db.fetchval(sql, *args) if fetch else db.execute(sql, *args))
        from sqlalchemy import text                               # SQLAlchemy AsyncEngine
        names = {f"p{i + 1}": a for i, a in enumerate(args)}
        stmt = re.sub(r"\$(\d+)", lambda m: f":p{m.group(1)}", sql)
        async with db.begin() as conn:
            result = await conn.execute(text(stmt), names)
            return result.scalar() if fetch and result.returns_rows else None

    async def _ensure(self, month: str) -> None:
        if month not in self._ensured:
            await self._run(ENSURE_SQL, month, fetch=False)
            self._ensured.add(month)

    async def reserve(self, interactive: bool = False) -> bool:
        month = self._month()
        await self._ensure(month)
        return await self._run(RESERVE_SQL, month, self.ceiling(interactive)) is not None

    async def release(self) -> None:
        await self._run(RELEASE_SQL, self._month(), fetch=False)

    async def used(self) -> int:
        value = await self._run(USED_SQL, self._month())
        return int(value or 0)


def quota_for(db, settings) -> Quota | None:
    """PgQuota over the given pool / engine, or None when there is no PostgreSQL (the legacy SQLite app: the
    allowance could not be enforced across restarts and processes, so the source stays off there)."""
    if db is None:
        return None
    if hasattr(db, "fetchval") or type(db).__name__ == "AsyncEngine":
        return PgQuota(db, settings.aws_web_search_monthly_limit, settings.aws_web_search_monthly_reserve)
    return None


def working_days_left(now: datetime | None = None) -> int:
    """Monday-Friday days from today to the end of the month (today included, at least 1)."""
    now = now or datetime.now(timezone.utc)
    day, count = now.date(), 0
    while day.month == now.month:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return max(1, count)


def paced_calls(cap: int, remaining: int, searches_per_day: int, now: datetime | None = None) -> int:
    """Calls one candidate search may make: the per-search cap, scaled down when the month's remaining allowance
    would not last at the expected pace (searches per working day x working days left). Never more than remaining;
    at least 1 while anything remains, so the source thins out instead of stopping abruptly."""
    if cap <= 0 or remaining <= 0:
        return 0
    expected = max(1, searches_per_day) * working_days_left(now)
    return max(1, min(cap, remaining, -(-remaining // expected)))


# ---------------------------------------------------------------- client
_CLIENTS: dict[tuple, Any] = {}
_CLIENT_LOCK = threading.Lock()


def _boto_client(region: str, connect_timeout: float, read_timeout: float):
    """One client per (region, timeouts) for the process. boto3 clients are thread safe."""
    key = (region, connect_timeout, read_timeout)
    with _CLIENT_LOCK:
        if key not in _CLIENTS:
            import boto3
            from botocore.config import Config
            config = Config(region_name=region, connect_timeout=connect_timeout, read_timeout=read_timeout,
                            retries={"mode": "standard", "total_max_attempts": 1},     # retried here, one unit per attempt
                            user_agent_extra="jobfinder-discovery")
            _CLIENTS[key] = boto3.session.Session().client("bedrock-agentcore", region_name=region, config=config)
        return _CLIENTS[key]


def map_error(exc: Exception) -> AwsWebSearchError:
    """botocore / network exceptions -> AwsWebSearchError. Messages are fixed texts (no AWS message, which could
    echo the request)."""
    from botocore.exceptions import (ClientError, ConnectTimeoutError, EndpointConnectionError, NoCredentialsError,
                                     NoRegionError, ParamValidationError, PartialCredentialsError, ReadTimeoutError)
    if isinstance(exc, AwsWebSearchError):
        return exc
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code") or "")
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
        if code in ("ThrottlingException", "TooManyRequestsException") or status == 429:
            return AwsWebSearchError("throttled", "AWS web search is throttling requests")
        if code in ("AccessDeniedException", "UnrecognizedClientException", "ExpiredTokenException") or status == 403:
            return AwsWebSearchError("access_denied", "AWS web search refused access (IAM permission bedrock-agentcore:InvokeWebSearch)")
        if code == "ValidationException" or status == 400:
            return AwsWebSearchError("validation", "AWS web search rejected the request parameters")
        if code == "ResourceNotFoundException" or status == 404:
            return AwsWebSearchError("not_found", "AWS web search tool not found (AWS_WEB_SEARCH_ID / region)")
        if code in ("ServiceUnavailableException", "InternalServerException") or status >= 500:
            return AwsWebSearchError("unavailable", "AWS web search is unavailable")
        return AwsWebSearchError("error", f"AWS web search error ({code or status})")
    if isinstance(exc, (ConnectTimeoutError, ReadTimeoutError, asyncio.TimeoutError, TimeoutError)):
        return AwsWebSearchError("timeout", "AWS web search timed out")
    if isinstance(exc, EndpointConnectionError):
        return AwsWebSearchError("unavailable", "AWS web search endpoint is not reachable")
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError, NoRegionError)):
        return AwsWebSearchError("credentials", "no AWS credentials or region available to the server")
    if isinstance(exc, ParamValidationError):
        return AwsWebSearchError("validation", "the request did not match the InvokeWebSearch schema")
    return AwsWebSearchError("error", f"AWS web search failed ({type(exc).__name__})")


NOT_SENT = {"credentials"}           # raised before anything reached AWS: the reserved unit is given back
_CLIENT_SIDE_VALIDATION = "ParamValidationError"


class AwsWebSearch:
    """Async wrapper. The boto3 call runs in a thread; each attempt first reserves a unit from `quota`."""

    def __init__(self, settings, quota: Quota | None, client=None, sleep=asyncio.sleep):
        self.settings = settings
        self.quota = quota
        self._client = client
        self._sleep = sleep
        self.calls = 0                            # units reserved by this process

    @property
    def enabled(self) -> bool:
        return bool(self.settings.aws_web_search_enabled) and self.quota is not None

    def client(self):
        if self._client is None:
            s = self.settings
            self._client = _boto_client(s.aws_web_search_region, s.aws_web_search_connect_timeout_seconds,
                                        s.aws_web_search_read_timeout_seconds)
        return self._client

    def published_after(self) -> datetime | None:
        days = self.settings.aws_web_search_published_days
        return datetime.now(timezone.utc) - timedelta(days=days) if days and days > 0 else None

    async def search(self, query: str, *, include=None, exclude=None, max_results: int | None = None,
                     interactive: bool = False) -> CallResult:
        """One query (retried on throttling / 5xx / timeouts). Raises AwsWebSearchError."""
        s = self.settings
        if not self.enabled:
            raise AwsWebSearchError("disabled", "AWS web search is switched off")
        params = build_request(s.aws_web_search_id, query, s.aws_web_search_max_results if max_results is None else max_results, include, exclude,
                               self.published_after())
        out = CallResult()
        attempts = max(1, s.aws_web_search_max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                granted = await self.quota.reserve(interactive)
            except Exception as exc:                              # table missing / database down: never call uncounted
                raise AwsWebSearchError("counter", f"the monthly usage counter is unavailable ({type(exc).__name__})") from None
            if not granted:
                raise AwsWebSearchError("budget", "this month's AWS web search allowance is used up")
            out.queries += 1
            self.calls += 1
            started = time.monotonic()
            try:
                raw = await asyncio.wait_for(asyncio.to_thread(self._invoke, params),
                                             timeout=s.aws_web_search_connect_timeout_seconds + s.aws_web_search_read_timeout_seconds + 5)
            except Exception as exc:                              # noqa: BLE001 - mapped below
                err = map_error(exc)
                if err.kind in NOT_SENT or type(exc).__name__ == _CLIENT_SIDE_VALIDATION:
                    await self.quota.release()                    # nothing was sent: the unit is not spent
                    out.queries -= 1
                    self.calls -= 1
                log.warning("aws web search call failed", extra={"kind": err.kind, "attempt": attempt,
                                                                  "ms": round((time.monotonic() - started) * 1000)})
                if err.kind in ("throttled", "unavailable", "timeout") and attempt < attempts:
                    await self._sleep(min(8.0, 0.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.25))
                    continue
                err.queries = out.queries
                raise err
            out.results = parse_results(raw)
            log.info("aws web search call", extra={"results": len(out.results), "attempt": attempt,
                                                    "ms": round((time.monotonic() - started) * 1000)})
            return out
        raise AwsWebSearchError("error", "no attempt was made")       # pragma: no cover

    def _invoke(self, params: dict) -> dict:
        return self.client().invoke_web_search(**params)


def parse_results(raw: Any) -> list[WebResult]:
    out: list[WebResult] = []
    for row in (raw or {}).get("results") or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url.startswith("https://") or len(url) > 600:
            continue
        published = row.get("publishedAt")
        out.append(WebResult(url=url, title=str(row.get("title") or "")[:300], text=str(row.get("text") or "")[:2000],
                             published_at=published if isinstance(published, datetime) else None))
    return out


def work_mode_words(mode: str) -> str:
    return {"remote": "remote", "hybrid": "hybrid", "onsite": "on-site", "on-site": "on-site"}.get((mode or "").lower(), "")


def query_text(title: str, location: str, mode: str) -> str:
    """'<title> jobs <location> [remote|hybrid|on-site]'. A 'remote' location is not repeated as a mode."""
    loc = " ".join(str(location or "").split())
    words = work_mode_words(mode)
    us_only = not loc or loc.lower() in ("united states", "usa", "us")
    parts = [" ".join(str(title).split()), "jobs", "United States" if us_only else loc]
    if words and words not in loc.lower():
        parts.append(words)
    return " ".join(parts)[:MAX_QUERY_CHARS]


_POSTING_PATH = re.compile(r"(?i)/(jobs?|careers?|positions?|openings?|vacanc(y|ies)|requisitions?|job-details?)/[^/?#]*[A-Za-z0-9][^/?#]*")
_LISTING_PATH = re.compile(r"(?i)(/search|/results|/category|/categories|/locations?/?$|/teams?/?$|/departments?/?$)")
_SEARCH_PARAMS = {"q", "query", "keyword", "keywords", "search", "k", "kw", "location", "l", "page", "category"}


def employer_posting_address(url: str, aggregators: tuple[str, ...]) -> bool:
    """Whether an address off the ATS hosts may be *checked* as one employer posting (the page then has to prove it
    with structured posting data). Search pages, category / location pages and job boards are refused."""
    from urllib.parse import parse_qsl, urlsplit
    from .paid import is_aggregator
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    if p.scheme != "https" or not p.hostname or p.username or p.password or is_aggregator(url, aggregators):
        return False
    if _LISTING_PATH.search(p.path) or {k.lower() for k, _ in parse_qsl(p.query)} & _SEARCH_PARAMS:
        return False
    return bool(_POSTING_PATH.search(p.path))


def rotation(site_hosts: list[str], open_share: float) -> list:
    """The order in which the queries of one search are sent: one job-board host per query (`host_groups`), and
    between them "any site" queries (None), which ask for postings on employers' own sites. `open_share` is the share
    of the queries that are of the second kind (0 = none, 1 = all); with any share above 0 the rotation holds at
    least one of them, and they are spread evenly so a budget that ends early has asked both kinds."""
    groups = host_groups(site_hosts)
    share = min(1.0, max(0.0, float(open_share or 0.0)))
    if share <= 0:
        return list(groups)
    if not groups or share >= 1:
        return [None]
    out: list = []
    owed = 0.0
    for group in groups:
        out.append(group)
        owed += share / (1.0 - share)                 # "any site" queries owed per job-board query
        while owed >= 1.0 - 1e-9:
            out.append(None)
            owed -= 1.0
    if None not in out:
        out.append(None)
    return out


# ATS hosts, rotated: a query asks one group at a time (results are ranked; mixing every host lets one dominate).
def host_groups(site_hosts: list[str]) -> list[list[str]]:
    """One job-board host per query, the hosts with the most postings first. Measured on 2026-11-20: a query limited
    to one host returns about 20 postings of that host, while one query over several hosts returned 1 to 14."""
    hosts = valid_domains(site_hosts)
    order = ("job-boards.greenhouse.io", "jobs.lever.co", "myworkdayjobs.com", "jobs.ashbyhq.com", "apply.workable.com")
    ranked = [h for h in order if h in hosts] + [h for h in hosts if h not in order]
    return [[h] for h in ranked]
