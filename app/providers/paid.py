"""Paid sources, each with its own spending guard. They are used only after the free sources (the index,
SearXNG, the company model) left a search short of its target.

SerpApi (Google Jobs): a licensed API that returns job listings Google aggregated. The plan's monthly allowance
is the binding limit, so every call that is not answered from our own cache is counted per calendar month in
the index, the account endpoint (free) is read every few minutes for the real remaining number, and a reserve
is kept back. What comes back is handled by three rules:
1. an apply link on an ATS this app reads, or on an employer's own careers site, is a lead and is verified like
   every other lead (board API or the page itself);
2. pages of job boards and aggregators (LinkedIn, Indeed, Dice, ...) are never fetched;
3. a job that only has job-board links may be returned from the data the API gave, marked plainly as
   "listed on Google Jobs, not checked on the employer's site" (setting SERPAPI_INCLUDE_LISTED_ONLY).

TinyFish: TinySearch as a second discovery engine beside SearXNG (results are addresses, handled exactly like
SearXNG results) and TinyFetch to read a page our own browser could not render. Spending is computed from the
per-call prices, limited per UTC day, and stopped when the wallet falls below a reserve. Its agent, research,
browser and vault endpoints are never called."""
import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from ..job_normalizer import detect_work_mode, parse_ats
from ..job_verifier import host_blocked
from ..schemas import Candidate, Job
from .base import now_iso
from .searxng import BOARD_SOURCES, valid_board

SERPAPI_URL = "https://serpapi.com/search"
SERPAPI_ACCOUNT_URL = "https://serpapi.com/account.json"
TINYSEARCH_URL = "https://api.search.tinyfish.ai/"
TINYFETCH_URL = "https://api.fetch.tinyfish.ai/"
TINYFISH_WALLET_URL = "https://agent.tinyfish.ai/v1/wallet"
GOOGLE_JOBS = "Google Jobs"
TINYFISH = "TinyFish"


async def _meta_get(index, key: str) -> str:
    try:
        return (await index.meta_prefix(key)).get("", "")
    except Exception:
        return ""


def aggregator_hosts(setting: str) -> tuple[str, ...]:
    return tuple(h.strip().lower().removeprefix("www.") for h in (setting or "").split(",") if h.strip())


def is_aggregator(url: str, hosts: tuple[str, ...]) -> bool:
    """A job board / aggregator page, or any host that forbids automated access: never fetched."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return True
    return not host or host_blocked(url) or any(host == h or host.endswith("." + h) for h in hosts)


# ---------------------------------------------------------------- SerpApi
class SerpApiClient:
    def __init__(self, api_key: str, client: httpx.AsyncClient, index, monthly_limit: int = 250, reserve: int = 25,
                 cache_hours: float = 6.0, account_check_seconds: float = 300.0, timeout: float = 30.0, clock=time.time):
        self.api_key = api_key
        self.client = client
        self.index = index
        self.monthly_limit = max(0, monthly_limit)
        self.reserve = max(0, reserve)
        self.cache_seconds = max(0.0, cache_hours) * 3600.0
        self.account_check_seconds = account_check_seconds
        self.timeout = timeout
        self._clock = clock
        self._account_left: int | None = None
        self._account_at = 0.0
        self.calls = 0                  # billed calls made by this process

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _month_key(self) -> str:
        return "serpapi:used:" + datetime.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y-%m")

    async def used_this_month(self) -> int:
        raw = await _meta_get(self.index, self._month_key())
        return int(raw) if raw.isdigit() else 0

    async def _account(self) -> int | None:
        """Searches the account has left, from the account endpoint (which costs no search). None = unknown."""
        if self._clock() - self._account_at < self.account_check_seconds and self._account_at:
            return self._account_left
        self._account_at = self._clock()
        try:
            r = await self.client.get(SERPAPI_ACCOUNT_URL, params={"api_key": self.api_key}, timeout=self.timeout)
            data = r.json() if r.status_code == 200 else {}
            left = data.get("total_searches_left")
            self._account_left = int(left) if isinstance(left, (int, float)) else None
        except (httpx.HTTPError, ValueError):
            self._account_left = None
        return self._account_left

    async def remaining(self) -> int:
        """Calls this installation may still make this month, after the reserve. The smaller of our own count and
        what the account reports (other users of the same key spend from the same allowance)."""
        if not self.enabled:
            return 0
        left = self.monthly_limit - await self.used_this_month()
        account = await self._account()
        if account is not None:
            left = min(left, account)
        return max(0, left - self.reserve)

    async def search(self, query: str, location: str, page_token: str = "") -> tuple[list[dict], str, str]:
        """One result page. Returns (items, next page token, note). note: "" billed call, "cached" (no call),
        "budget" (not sent: monthly allowance used up), or an error text. Never raises."""
        key = "serpapi:q:" + hashlib.sha1(f"{query}|{location}|{page_token}".encode()).hexdigest()[:24]
        if self.cache_seconds > 0:
            try:
                cached = json.loads(await _meta_get(self.index, key) or "null")
            except ValueError:
                cached = None
            if isinstance(cached, dict) and time.time() - float(cached.get("t") or 0) < self.cache_seconds:
                return list(cached.get("jobs") or []), str(cached.get("next") or ""), "cached"
        if await self.remaining() <= 0:
            return [], "", "budget"
        # Counted before it is sent: a call whose answer is lost was still spent.
        await self.index.meta_set(self._month_key(), str(await self.used_this_month() + 1))
        self.calls += 1
        if self._account_left is not None:
            self._account_left = max(0, self._account_left - 1)
        params = {"engine": "google_jobs", "q": query, "location": location or "United States", "gl": "us", "hl": "en",
                  "api_key": self.api_key}
        if page_token:
            params["next_page_token"] = page_token
        try:
            r = await self.client.get(SERPAPI_URL, params=params, timeout=self.timeout)
            if r.status_code != 200:
                return [], "", f"HTTP {r.status_code}"
            data = r.json()
        except httpx.TimeoutException:
            return [], "", "timed out"
        except (httpx.HTTPError, ValueError):
            return [], "", "unreachable or invalid answer"
        jobs = [_trim(item) for item in data.get("jobs_results") or [] if isinstance(item, dict)]
        token = str((data.get("serpapi_pagination") or {}).get("next_page_token") or "")
        if self.cache_seconds > 0:
            try:
                await self.index.meta_set(key, json.dumps({"t": time.time(), "jobs": jobs, "next": token}))
            except Exception:
                pass
        return jobs, token, ""


def _trim(item: dict) -> dict:
    """The fields of one Google Jobs item this app uses (kept small: it is cached)."""
    highlights = [{"title": str(h.get("title") or "")[:80], "items": [str(i)[:400] for i in (h.get("items") or [])[:12]]}
                  for h in item.get("job_highlights") or [] if isinstance(h, dict)]
    options = [{"title": str(o.get("title") or "")[:80], "link": str(o.get("link") or "")[:600]}
               for o in item.get("apply_options") or [] if isinstance(o, dict)]
    ext = item.get("detected_extensions") if isinstance(item.get("detected_extensions"), dict) else {}
    return {"title": str(item.get("title") or "")[:200], "company_name": str(item.get("company_name") or "")[:120],
            "location": str(item.get("location") or "")[:160], "via": str(item.get("via") or "")[:80],
            "description": str(item.get("description") or "")[:8000], "job_highlights": highlights, "apply_options": options,
            "detected_extensions": {k: ext[k] for k in ("salary", "schedule_type", "work_from_home") if k in ext}}


def serp_item(item: dict, hosts: tuple[str, ...], include_listed: bool) -> tuple[Candidate | None, Job | None]:
    """Turn one Google Jobs item into (a lead to verify, or a listed-only job). At most one of the two is set."""
    title, company = str(item.get("title") or "").strip(), str(item.get("company_name") or "").strip()
    location = str(item.get("location") or "").strip()
    links = [str(o.get("link") or "").strip() for o in item.get("apply_options") or [] if isinstance(o, dict)]
    links = [l for l in links if l.startswith("https://") and len(l) < 600 and "\n" not in l]
    if not title or not company or not links:
        return None, None
    for link in links:                                    # 1. an ATS this app reads: the board API decides
        parsed = parse_ats(link)
        if parsed and parsed[0] in BOARD_SOURCES and parsed[1] and valid_board(parsed[0], parsed[1]):
            return Candidate(url=link, title=title, company=company, location=location, provider=GOOGLE_JOBS, cited=True), None
    for link in links:                                    # 2. the employer's own site: the page decides
        if not is_aggregator(link, hosts):
            return Candidate(url=link, title=title, company=company, location=location, provider=GOOGLE_JOBS, cited=True), None
    if not include_listed:
        return None, None
    # 3. only job-board links: nothing of theirs is fetched. The job is what the API returned, and says so.
    via = str(item.get("via") or "a job board").strip()
    highlights = " ".join(f"{h.get('title', '')}: " + " ".join(h.get("items") or []) for h in item.get("job_highlights") or [])
    description = " ".join(p for p in (str(item.get("description") or ""), highlights) if p).strip()
    ext = item.get("detected_extensions") or {}
    mode = "Remote" if ext.get("work_from_home") else detect_work_mode(location, description[:600])
    label = f"{GOOGLE_JOBS} (via {via})"
    return None, Job(
        title=title, company=company, location=location or "Not listed", work_mode=mode,
        salary=str(ext.get("salary") or "Not listed")[:160], description=description[:20000], url=links[0], apply_url=links[0],
        source=GOOGLE_JOBS, sources=[label], aggregator=True, verified_at=now_iso(), verification_level="listed",
        verification=f"Listed on Google Jobs via {via} (SerpApi API); not checked on the employer's site",
    )


# ---------------------------------------------------------------- TinyFish
class TinyFishClient:
    def __init__(self, api_key: str, client: httpx.AsyncClient, index, search_usd: float = 0.005, fetch_usd: float = 0.001,
                 daily_usd: float = 1.0, wallet_reserve_usd: float = 2.0, wallet_check_seconds: float = 300.0,
                 timeout: float = 30.0, clock=time.time):
        self.api_key = api_key
        self.client = client
        self.index = index
        self.search_usd, self.fetch_usd = search_usd, fetch_usd
        self.daily_usd, self.wallet_reserve_usd = daily_usd, wallet_reserve_usd
        self.wallet_check_seconds = wallet_check_seconds
        self.timeout = timeout
        self._clock = clock
        self._balance: float | None = None
        self._balance_at = 0.0
        self.spent = 0.0                # USD spent by this process (computed from the prices)
        self.searches = self.fetches = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key, "Accept": "application/json"}

    def _day_key(self) -> str:
        return "tinyfish:spend:" + datetime.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y-%m-%d")

    async def spent_today(self) -> float:
        try:
            return float(await _meta_get(self.index, self._day_key()) or 0)
        except ValueError:
            return 0.0

    async def _wallet(self) -> float | None:
        if self._clock() - self._balance_at < self.wallet_check_seconds and self._balance_at:
            return self._balance
        self._balance_at = self._clock()
        try:
            r = await self.client.get(TINYFISH_WALLET_URL, headers=self._headers, timeout=self.timeout)
            self._balance = float(r.json().get("available_balance")) if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError, TypeError):
            self._balance = None
        return self._balance

    async def allowed(self, cost: float) -> str:
        """"" when `cost` USD may be spent now, otherwise the reason it may not."""
        if not self.enabled:
            return "not configured"
        if await self.spent_today() + cost > self.daily_usd + 1e-9:
            return "daily spending ceiling reached"
        balance = await self._wallet()
        if balance is not None and balance - cost < self.wallet_reserve_usd:
            return "wallet balance is at the reserve"
        return ""

    async def _charge(self, cost: float) -> None:
        self.spent += cost
        await self.index.meta_set(self._day_key(), f"{await self.spent_today() + cost:.4f}")
        if self._balance is not None:
            self._balance -= cost

    async def search(self, query: str) -> tuple[list[str], str]:
        """Result addresses of one TinySearch query. Returns (urls, note); note is "" or why nothing came. Never raises."""
        reason = await self.allowed(self.search_usd)
        if reason:
            return [], reason
        await self._charge(self.search_usd)               # counted before it is sent
        self.searches += 1
        try:
            r = await self.client.get(TINYSEARCH_URL, headers=self._headers, timeout=self.timeout,
                                      params={"query": query, "location": "US", "language": "en"})
            if r.status_code != 200:
                return [], f"HTTP {r.status_code}"
            data = r.json()
        except httpx.TimeoutException:
            return [], "timed out"
        except (httpx.HTTPError, ValueError):
            return [], "unreachable or invalid answer"
        return [str(row.get("url")) for row in data.get("results") or [] if isinstance(row, dict) and row.get("url")][:40], ""

    async def fetch_html(self, url: str) -> tuple[str, str]:
        """The page as HTML through TinyFetch. Returns (html, note). The caller has already checked the address
        (SSRF guard, robots.txt, not a forbidden host) and that our own browser was not *blocked* by the site."""
        reason = await self.allowed(self.fetch_usd)
        if reason:
            return "", reason
        await self._charge(self.fetch_usd)
        self.fetches += 1
        try:
            r = await self.client.post(TINYFETCH_URL, headers=self._headers, timeout=self.timeout + 30,
                                       json={"urls": [url], "format": "html", "purpose": "Check whether this is an open job posting"})
            if r.status_code != 200:
                return "", f"HTTP {r.status_code}"
            results = r.json().get("results") or []
        except httpx.TimeoutException:
            return "", "timed out"
        except (httpx.HTTPError, ValueError):
            return "", "unreachable or invalid answer"
        text = results[0].get("text") if results and isinstance(results[0], dict) else None
        if not isinstance(text, str) or not text.strip():
            return "", "no content"
        title = str(results[0].get("title") or "")
        return (f"<html><head><title>{title}</title></head><body>{text}</body></html>" if "<html" not in text[:500].lower() else text)[:1_500_000], ""
