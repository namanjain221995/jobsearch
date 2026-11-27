"""Shared plumbing for every source: polite HTTP (retries, backoff, per-host limits) and robots.txt."""
import asyncio
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

USER_AGENT = "usa-ai-job-finder/1.0 (local job search tool)"
ROBOTS_TOKEN = "usa-ai-job-finder"


class SourceError(Exception):
    """A source could not be read. Carries a short, secret-free message."""


class BotChallenge(SourceError):
    """The site answered with a bot check instead of content (Cloudflare `cf-mitigated: challenge`). That is a refusal
    of automated clients, not a "slow down": asking again sooner or later gets the same answer, and the check is
    never solved or worked around. HostLimiter.challenged() leaves the whole source alone for hours after a few."""


def bot_challenge(response) -> bool:
    return (response.headers.get("cf-mitigated") or "").strip().lower() == "challenge"


class BoardNotFound(SourceError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_date(value) -> str:
    """Best-effort YYYY-MM-DD from ISO strings, epoch seconds/ms or 'November 6, 2026'. '' when unknown."""
    if value in (None, "", 0):
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        n = float(value)
        try:
            return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return ""
    text = str(value).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(re.sub(r"\s+", " ", text), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


class HostLimiter:
    """Bounded concurrency per host group, plus an optional minimum gap between request starts,
    so one busy source cannot flood a provider (Workday answers HTTP 429 above a few requests per second)."""

    def __init__(self, default: int = 6, limits: dict[str, int] | None = None, intervals: dict[str, float] | None = None):
        self.default = default
        self.limits = limits or {}
        self.intervals = intervals or {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._gates: dict[str, asyncio.Lock] = {}
        self._next: dict[str, float] = {}
        self._challenges: dict[str, int] = {}                 # bot checks in a row, per group
        self._blocked: dict[str, tuple[float, str]] = {}      # group -> (until, reason)

    @asynccontextmanager
    async def slot(self, group: str):
        sem = self._sems.get(group)
        if sem is None:
            sem = self._sems[group] = asyncio.Semaphore(self.limits.get(group, self.default))
        async with sem:
            interval = self.intervals.get(group, 0.0)
            if interval > 0:
                async with self._gates.setdefault(group, asyncio.Lock()):
                    loop = asyncio.get_running_loop()
                    wait = self._next.get(group, 0.0) - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._next[group] = max(self._next.get(group, 0.0), loop.time()) + interval
            yield

    def blocked(self, group: str) -> str:
        """Why no request of this group is sent right now ("" = requests go out)."""
        hit = self._blocked.get(group)
        if hit and time.time() < hit[0]:
            return hit[1]
        self._blocked.pop(group, None)
        return ""

    def challenged(self, group: str, host: str = "") -> None:
        """One more bot check from the group. After CHALLENGES_BEFORE_BLOCK in a row the source is left alone for
        CHALLENGE_BLOCK_SECONDS: one request is tried again after that, and a single answer with content lifts it."""
        self._challenges[group] = self._challenges.get(group, 0) + 1
        if self._challenges[group] >= CHALLENGES_BEFORE_BLOCK or group in self._blocked:
            self._blocked[group] = (time.time() + CHALLENGE_BLOCK_SECONDS,
                                    f"{host or group} answers this server with a bot check; it is not read and the check is not worked around")

    def answered(self, group: str) -> None:
        """The group answered with content: it is not refusing this server."""
        if self._challenges.pop(group, None) or group in self._blocked:
            self._blocked.pop(group, None)

    def penalize(self, group: str, seconds: float) -> None:
        """The source said "slow down" (HTTP 429): hold every request of the group for a while."""
        if self.intervals.get(group, 0.0) > 0:
            loop = asyncio.get_running_loop()
            self._next[group] = max(self._next.get(group, 0.0), loop.time() + seconds)


CHALLENGES_BEFORE_BLOCK = 3
CHALLENGE_BLOCK_SECONDS = 6 * 3600.0


async def request_json(client: httpx.AsyncClient, method: str, url: str, *, params: dict | None = None,
                       json_body: dict | None = None, headers: dict | None = None,
                       retries: int = 2, timeout: float = 45.0):
    """GET/POST returning parsed JSON. Retries 429/5xx/timeouts with exponential backoff; 404 means 'not there'."""
    last = "unknown error"
    host = httpx.URL(url).host
    for attempt in range(retries + 1):
        delay = 0.6 * 2 ** attempt + random.random() * 0.3
        try:
            r = await client.request(method, url, params=params, json=json_body, timeout=timeout,
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
            if r.status_code in (404, 410, 422):
                raise BoardNotFound(f"HTTP {r.status_code} from {host}")
            if bot_challenge(r):
                raise BotChallenge(f"{host} answered with a bot check (HTTP {r.status_code}); not read")
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                retry_after = r.headers.get("retry-after", "")
                if retry_after.isdigit():
                    delay = min(float(retry_after), 15.0)
                elif r.status_code == 429:
                    delay = max(delay, 2.0 * (attempt + 1))        # throttled: back off properly
            elif r.status_code >= 400:
                raise SourceError(f"HTTP {r.status_code} from {host}")
            else:
                return r.json()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = type(exc).__name__
        except ValueError as exc:  # invalid JSON
            raise SourceError(f"Invalid JSON from {host}") from exc
        if attempt < retries:
            await asyncio.sleep(delay)
    raise SourceError(f"{host}: {last} after {retries + 1} attempts")


async def get_json(client: httpx.AsyncClient, url: str, *, params: dict | None = None,
                   retries: int = 2, timeout: float = 45.0):
    return await request_json(client, "GET", url, params=params, retries=retries, timeout=timeout)


# ---------------------------------------------------------------- robots.txt (RFC 9309)
class RobotsRules:
    """Rules for our user agent from one robots.txt. Supports '*' and '$'; the longest matching rule wins."""

    def __init__(self, text: str = "", allow_all: bool = True, deny_all: bool = False, transient: bool = False):
        self.allow_all, self.deny_all = allow_all, deny_all
        self.transient = transient      # robots.txt could not be read right now (throttled / server error)
        self.rules: list[tuple[int, bool, re.Pattern]] = []
        if text:
            self._parse(text)

    def _parse(self, text: str) -> None:
        groups: dict[str, list[tuple[bool, str]]] = {}
        agents: list[str] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, value = (p.strip() for p in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if not last_was_agent:
                    agents = []
                agents.append(value.lower())
                groups.setdefault(value.lower(), [])
                last_was_agent = True
            elif field in ("allow", "disallow"):
                last_was_agent = False
                for agent in agents:
                    if value:
                        groups[agent].append((field == "allow", value))
            else:
                last_was_agent = False
        chosen = next((rules for agent, rules in groups.items() if agent == ROBOTS_TOKEN), None)
        if chosen is None:
            chosen = groups.get("*", [])
        for allow, pattern in chosen:
            anchored = pattern.endswith("$")
            body = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern.rstrip("$"))
            self.rules.append((len(pattern), allow, re.compile(body + ("$" if anchored else ""))))
        self.allow_all = not self.rules

    def allows(self, url: str) -> bool:
        if self.deny_all:
            return False
        if self.allow_all:
            return True
        p = urlsplit(url)
        target = (p.path or "/") + (f"?{p.query}" if p.query else "")
        best: tuple[int, bool] | None = None
        for length, allow, rx in self.rules:
            if rx.match(target) and (best is None or length > best[0] or (length == best[0] and allow)):
                best = (length, allow)
        return True if best is None else best[1]


class RobotsCache:
    """Fetches and caches robots.txt per origin. 4xx = no restrictions. 429 / 5xx / network failure = do not
    crawl for now (RFC 9309) and look again in a couple of minutes."""

    def __init__(self, ttl: float = 6 * 3600, fetcher=None, retry_after: float = 120.0):
        self.ttl = ttl
        self.retry_after = retry_after
        self._cache: dict[str, tuple[float, RobotsRules]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._fetcher = fetcher      # async (url) -> (status, text); defaults to a plain GET with the shared client

    def _fresh(self, origin: str) -> RobotsRules | None:
        hit = self._cache.get(origin)
        if hit and time.time() - hit[0] < (self.retry_after if hit[1].transient else self.ttl):
            return hit[1]
        return None

    def peek(self, url: str) -> RobotsRules | None:
        """Cached rules for the URL's origin, or None when robots.txt still has to be fetched."""
        p = urlsplit(url)
        return self._fresh(f"{p.scheme}://{p.netloc}")

    async def rules(self, client: httpx.AsyncClient, url: str) -> RobotsRules:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        rules = self._fresh(origin)
        if rules is None:
            async with self._locks.setdefault(origin, asyncio.Lock()):
                rules = self._fresh(origin)
                if rules is None:
                    rules = await self._load(client, origin)
                    self._cache[origin] = (time.time(), rules)
        return rules

    async def allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        return (await self.rules(client, url)).allows(url)

    async def _load(self, client: httpx.AsyncClient, origin: str) -> RobotsRules:
        try:
            if self._fetcher:
                status, text = await self._fetcher(origin + "/robots.txt")
            else:
                r = await client.get(origin + "/robots.txt", timeout=12.0, headers={"User-Agent": USER_AGENT},
                                     follow_redirects=True)
                status, text = r.status_code, r.text[:500_000]
                if bot_challenge(r):
                    rules = RobotsRules(deny_all=True, transient=True)
                    rules.challenged = True
                    return rules
        except Exception:
            return RobotsRules(deny_all=True, transient=True)
        if status == 200:
            return RobotsRules(text)
        if 400 <= status < 500 and status != 429:
            return RobotsRules()
        return RobotsRules(deny_all=True, transient=True)


THROTTLE_PAUSE_SECONDS = 600.0      # after "429 Too Many Requests" a paced source is left alone this long


class Ctx:
    """What a source needs to talk to the network politely."""

    def __init__(self, client: httpx.AsyncClient, limiter: HostLimiter | None = None,
                 robots: RobotsCache | None = None, timeout: float = 45.0, options: dict | None = None):
        self.client = client
        self.limiter = limiter or HostLimiter()
        self.robots = robots or RobotsCache()
        self.timeout = timeout
        self.options = options or {}      # per-source knobs from settings (page counts, API keys)

    async def json(self, group: str, method: str, url: str, *, check_robots: bool = False, **kwargs):
        """Rate-limited JSON request. With check_robots the host's robots.txt must allow the URL."""
        refusal = self.limiter.blocked(group)
        if refusal:
            raise BotChallenge(refusal)                       # no request: the source refused this server a short while ago
        if check_robots:
            rules = self.robots.peek(url)
            if rules is None:
                async with self.limiter.slot(group):          # robots.txt is fetched at the same polite pace
                    rules = await self.robots.rules(self.client, url)
            if getattr(rules, "challenged", False):
                self.limiter.challenged(group, httpx.URL(url).host)
                self.limiter.penalize(group, THROTTLE_PAUSE_SECONDS)
                raise BotChallenge(f"{httpx.URL(url).host} answered with a bot check; not read")
            if rules.transient:
                self.limiter.penalize(group, THROTTLE_PAUSE_SECONDS)
                raise SourceError(f"robots.txt of {httpx.URL(url).host} is unavailable right now (the site is throttling or down)")
            if not rules.allows(url):
                raise SourceError(f"robots.txt disallows {httpx.URL(url).host}{httpx.URL(url).path}")
        async with self.limiter.slot(group):
            try:
                data = await request_json(self.client, method, url, timeout=kwargs.pop("timeout", self.timeout), **kwargs)
            except BotChallenge:
                self.limiter.challenged(group, httpx.URL(url).host)
                self.limiter.penalize(group, THROTTLE_PAUSE_SECONDS)
                raise
            except SourceError as exc:
                if "HTTP 429" in str(exc):
                    self.limiter.penalize(group, THROTTLE_PAUSE_SECONDS)
                raise
            self.limiter.answered(group)
            return data
