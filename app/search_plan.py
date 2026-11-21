"""What one search may spend, and how a requested title is widened.

Budgets: the base settings (OPENAI_SEARCH_CALLS, CLAUDE_SEARCH_CALLS, AI_WEB_SEARCHES_PER_CALL, ...) describe a
request of 50 results or fewer. A larger request scales them linearly (a request of 300 gets six times the
base) up to the hard ceiling of each setting (`*_MAX`). Discovery budgets are totals for the whole search and
are handed out round by round, so a search that reaches its target early does not spend the rest.

Title expansion: variants proposed by a model are only ever used to *find* postings (index matching and
discovery queries). Every posting found is still screened and scored against the candidate.

No request twice: `QueryLedger` is the record of what one search already asked a search engine (normalised query
and host). SearXNG, the company model's web search, TinyFish and AWS claim a query before they send it, so each
gets a different part of the query plan. `ai_cells` is the same idea for the AI web search: every (title, host,
place) cell is handed to one call of one provider only."""
import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Iterator

from .config import Settings
from .job_ranker import _ROLE_FAMILY, title_tokens

BASE_REQUEST = 50
EXPANSION_PREFIX = "titlex:"
_TITLE_OK = re.compile(r"[A-Za-z][A-Za-z0-9 .,&/+#()'-]{1,58}[A-Za-z0-9+#)]")
_NOT_A_TITLE = re.compile(r"(?i)(https?:|www\.|\b(ignore|instructions?|prompt|salary|hiring|apply|jobs?|careers?|vacanc(y|ies))\b)")
_GENERIC = {"platform", "software", "application", "applications", "systems", "system", "solutions", "solution", "technical",
            "technology", "it", "product", "products", "web", "digital", "enterprise", "business", "services", "service"}
# Used to vary discovery queries when the candidate named no preferred state.
DEFAULT_LOCATIONS = ["remote", "California", "Texas", "New York", "Washington", "Massachusetts", "Illinois", "Virginia",
                     "Georgia", "Florida", "North Carolina", "Colorado", "Pennsylvania", "New Jersey", "Arizona", "Ohio"]


@dataclass(frozen=True)
class Plan:
    target: int
    rounds: int
    openai_calls: int           # web-search discovery calls over the whole search
    claude_calls: int
    web_searches: int           # live web searches one discovery call may run
    ai_calls: int               # every OpenAI / Claude call of the search together (hard stop)
    ai_ranked: int              # jobs judged by OpenAI / Claude
    searx_queries: int
    new_boards: int
    page_verifications: int
    detail_fetches: int
    audit_jobs: int = 40
    boards: int = 1500
    serp_calls: int = 0         # SerpApi result pages (also limited by the month's allowance)
    tiny_searches: int = 0      # TinySearch queries (also limited by the daily spending ceiling and the wallet)
    tiny_fetches: int = 0
    aws_calls: int = 0          # AWS web search queries (also paced by the month's remaining allowance)
    tsweb_searches: int = 0     # web searches through the company model (no bill; its queue is shared with the chat app)

    def per_round(self, total: int, round_no: int) -> int:
        """How much of a search-wide budget round `round_no` (1-based) may use."""
        share = math.ceil(total / max(1, self.rounds))
        return max(0, min(share, total - share * (round_no - 1)))


def _scaled(base: int, ceiling: int, target: int, scaling: bool) -> int:
    if not scaling or target <= BASE_REQUEST:
        return max(0, base)
    return max(0, min(max(base, ceiling), math.ceil(base * target / BASE_REQUEST)))


def plan_for(settings: Settings, target: int, deep: bool = True) -> Plan:
    s = settings
    on = bool(s.ai_budget_scaling)
    openai_calls = _scaled(s.openai_search_calls, s.openai_search_calls_max, target, on)
    claude_calls = _scaled(s.claude_search_calls, s.claude_search_calls_max, target, on)
    # The judged slice: 40 % of the request, never below the old fixed size, within the ceiling.
    slice_cap = max(target * 2, target + 30)
    ai_ranked = s.max_ai_ranked_jobs
    if on and target > BASE_REQUEST:
        ai_ranked = min(max(s.max_ai_ranked_jobs, s.max_ai_ranked_jobs_max), max(s.max_ai_ranked_jobs, math.ceil(target * 0.4)))
    ai_ranked = min(ai_ranked, slice_cap)
    rank_calls = math.ceil(ai_ranked / max(1, s.ai_rank_batch_size))
    # + one model call for each open-web call of a provider whose search tool needs its career sites named beforehand
    # (ai_orchestrator._site_slice), + profile, expansion, audit, page extraction, retries on the other provider
    site_plans = math.ceil(max(openai_calls, claude_calls) * min(1.0, max(0.0, s.ai_open_web_share)))
    needed = openai_calls + claude_calls + site_plans + rank_calls + 12
    ai_calls = s.max_ai_calls_per_search
    if on and target > BASE_REQUEST:
        ai_calls = min(max(s.max_ai_calls_per_search, s.max_ai_calls_per_search_max), max(s.max_ai_calls_per_search, needed))
    return Plan(
        target=target, rounds=1 if s.search_max_rounds <= 1 else max(1, s.search_budget_rounds),
        openai_calls=openai_calls if deep else 0, claude_calls=claude_calls if deep else 0,
        web_searches=_scaled(s.ai_web_searches_per_call, s.ai_web_searches_per_call_max, target, on),
        ai_calls=ai_calls, ai_ranked=ai_ranked,
        searx_queries=_scaled(s.searxng_queries_per_search, s.searxng_queries_per_search_max, target, on),
        new_boards=_scaled(s.searxng_new_boards_per_search, s.searxng_new_boards_per_search_max, target, on),
        page_verifications=_scaled(s.max_page_verifications, s.max_page_verifications_max, target, on),
        detail_fetches=_scaled(s.max_detail_fetches, s.max_detail_fetches_max, target, on),
        audit_jobs=_scaled(s.max_audit_jobs, s.max_audit_jobs_max, target, on),
        boards=_scaled(s.max_boards_per_search, s.max_boards_per_search_max, target, on),
        serp_calls=max(0, s.serpapi_max_calls_per_search) if s.serpapi_api_key else 0,
        tiny_searches=max(0, s.tinyfish_search_queries_per_search) if s.tinyfish_api_key else 0,
        tiny_fetches=max(0, s.tinyfish_fetches_per_search) if s.tinyfish_api_key else 0,
        aws_calls=_scaled(s.aws_web_search_max_calls_per_search, s.aws_web_search_max_calls_per_search_max, target, on)
        if s.aws_web_search_enabled else 0,
        tsweb_searches=min(max(0, s.techsara_web_searches_per_search),
                           _scaled(s.techsara_web_searches_base, s.techsara_web_searches_per_search, target, on))
        if s.techsara_configured and s.techsara_web_search_enabled else 0,
    )


# ---------------------------------------------------------------- no request twice
_SITE = re.compile(r"(?i)^\s*site:(\S+)\s*(.*)$")
_NOT_A_QUERY_WORD = {"job", "jobs"}                  # AWS and TinyFish add them; they do not make a different question
_WHOLE_COUNTRY = ("united states", "usa")            # a query without a place already means the whole country


def query_key(query: str, host: str = "") -> tuple[str, str]:
    """(normalised query, host): what counts as the same request whichever engine sends it. A `site:host` query
    carries its host; an engine that restricts a plain query to one host (AWS) passes that host. Quotes, case,
    spacing, the word "jobs" and a bare "United States" do not make a different request."""
    found = _SITE.match(query or "")
    if found:
        host, query = found.group(1), found.group(2)
    text = " ".join(re.sub(r'["\\]', " ", str(query or "").lower()).split())
    for country in _WHOLE_COUNTRY:
        text = re.sub(rf"\b{country}\b", " ", text)
    words = [w for w in text.split() if w not in _NOT_A_QUERY_WORD]
    return " ".join(words), (host or "").strip().lower().removeprefix("www.")


class QueryLedger:
    """What one search already asked a search engine. An engine claims a query before sending it; a query somebody
    claimed is not sent again by another engine. One exception: a query that failed or brought no usable address may
    be given ONCE to a different engine (`retry_for`), because the first request found nothing.
    Counts only; no result of any engine is kept here."""

    def __init__(self) -> None:
        self.asked: dict[tuple[str, str], str] = {}        # key -> the engine that sent it
        self.outcome: dict[tuple[str, str], str] = {}      # key -> "ok" | "empty" | "failed"
        self.retried: set[tuple[str, str]] = set()
        self.sent: Counter = Counter()                      # engine -> requests claimed
        self.avoided: Counter = Counter()                   # engine -> requests it did not send because another engine had
        self.retries: Counter = Counter()                   # engine -> second chances it took over from another engine
        self._passed: set[tuple[str, tuple[str, str]]] = set()

    def claim(self, engine: str, query: str, host: str = "") -> bool:
        key = query_key(query, host)
        if not key[0]:
            return False
        if key in self.asked:
            if self.asked[key] != engine and (engine, key) not in self._passed:
                self._passed.add((engine, key))             # counted once, however often the engine comes past it
                self.avoided[engine] += 1
            return False
        self.asked[key] = engine
        self.sent[engine] += 1
        return True

    def release(self, engine: str, query: str, host: str = "") -> None:
        """Give back a claim that was never sent (the engine stopped answering before it got there)."""
        key = query_key(query, host)
        if self.asked.get(key) == engine and key not in self.outcome:
            del self.asked[key]
            self.sent[engine] -= 1

    def note(self, query: str, outcome: str, host: str = "") -> None:
        self.outcome[query_key(query, host)] = outcome

    def take(self, engine: str, queries: Iterable[str], count: int) -> list[str]:
        """The next `count` queries of `queries` that nobody asked yet, claimed for `engine`."""
        out: list[str] = []
        if count <= 0:
            return out
        for query in queries:
            if self.claim(engine, query):
                out.append(query)
                if len(out) >= count:
                    break
        return out

    def free(self, queries: Iterable[str]) -> int:
        """How many of `queries` nobody asked yet."""
        return sum(1 for q in dict.fromkeys(queries) if query_key(q) not in self.asked and query_key(q)[0])

    def _second_chance(self, engine: str, key: tuple[str, str]) -> bool:
        """Take the one second chance of a query ANOTHER engine failed on or got nothing usable for."""
        if (self.outcome.get(key) in ("failed", "empty") and self.asked.get(key) not in (None, engine)
                and key not in self.retried):
            self.retried.add(key)
            self.retries[engine] += 1
            return True
        return False

    def retry_for(self, engine: str, queries: Iterable[str], count: int) -> list[str]:
        """Up to `count` of `queries` that ANOTHER engine failed on or got nothing usable for, not yet retried."""
        out: list[str] = []
        for query in queries:
            if len(out) >= count:
                break
            if self._second_chance(engine, query_key(query)):
                out.append(query)
        return out

    def take_or_retry(self, engine: str, queries: Iterable[str], count: int) -> list[str]:
        """Up to `count` of `queries`, in their order, for an engine that stands in for another one: a query nobody
        asked is claimed, a query the other engine failed on or got nothing for gets its second chance."""
        out: list[str] = []
        for query in queries:
            if len(out) >= count:
                break
            key = query_key(query)
            if (self._second_chance(engine, key) if key in self.asked else self.claim(engine, query)):
                out.append(query)
        return out

    def stats(self) -> dict:
        return {"asked": len(self.asked), "by_engine": {k: v for k, v in sorted(self.sent.items()) if v},
                "duplicates_avoided": int(sum(self.avoided.values())), "retried_on_another_engine": int(sum(self.retries.values()))}


def site_query(title: str, host: str, place: str = "") -> str:
    """One `site:` query, in the form providers/searxng.build_queries writes them."""
    clean = lambda text: " ".join(re.sub(r'["\\]', " ", str(text or "")).split())
    return f'site:{host} "{clean(title)}"' + (f" {clean(place)}" if clean(place) else "")


def ai_cells(requested: list[str], variants: list[str], hosts: list[str], home: list[str], elsewhere: list[str],
             once: Iterable[str] = (), everywhere: str = "", keywords: Iterable[str] = ()) -> Iterator[tuple[str, str, str]]:
    """(title, host, word) cells for the AI web search of one search, the most valuable first. One cell is one web
    search (`site:host "title" word`); the orchestrator hands every cell to one call of one provider only.

    Tiers: the requested titles on every host (`everywhere` is the word of a query without a place: "" or, for a
    remote-only candidate, "remote"); the requested titles with the candidate's own places (`home`: preferred
    states, "remote"); the title variants; the requested titles in other places; the variants with places; last,
    the requested titles with one of the candidate's core skills (`keywords`), which reaches postings the plain
    title query did not show. Inside a tier the (title, word) pairs take turns and each starts on another host, so a
    budget that ends in the middle of a tier has still asked every title and every place on some hosts.
    `once` are hosts searched in the first tier only (paced sources whose leads the engine checks a few at a time)."""
    slow = set(once)
    every = [h for h in hosts if h not in slow]
    tiers = [([(title, everywhere) for title in requested], hosts),
             ([(title, place) for place in home for title in requested], every),
             ([(title, everywhere) for title in variants], every),
             ([(title, place) for place in elsewhere for title in requested], every),
             ([(title, place) for place in home for title in variants], every),
             ([(title, place) for place in elsewhere for title in variants], every),
             ([(title, word) for word in keywords for title in requested], every)]
    for pairs, where in tiers:
        for step in range(len(where)):
            for turn, (title, word) in enumerate(pairs):
                yield title, where[(step + turn) % len(where)], word


# ---------------------------------------------------------------- title expansion
def clean_variants(raw, wanted: list[str], cap: int) -> list[str]:
    """Validate what a model proposed. Kept: short plain job titles that are new. Dropped: anything that is not
    a string, is too long, carries markup / addresses / instructions, or only repeats a requested title
    (seniority words do not make a new title: matching ignores them)."""
    seen = {title_tokens(t) for t in wanted}
    seen.add(())
    out: list[str] = []
    if cap <= 0:
        return out
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        title = " ".join(item.split()).strip(" .,;:-")
        if not _TITLE_OK.fullmatch(title) or len(title.split()) > 6 or _NOT_A_TITLE.search(title):
            continue
        tokens = title_tokens(title)
        if tokens in seen or not [t for t in tokens if t not in _ROLE_FAMILY and t not in _GENERIC]:
            continue                 # a repeat, or too generic to search with ("Platform Developer", "Software Engineer")
        seen.add(tokens)
        out.append(title)
        if len(out) >= max(0, cap):
            break
    return out


def expansion_key(titles: list[str]) -> str:
    norm = "|".join(sorted(" ".join(t.lower().split()) for t in titles))
    return EXPANSION_PREFIX + hashlib.sha1(norm.encode()).hexdigest()[:24]


async def cached_variants(index, titles: list[str], max_age_seconds: float) -> list[str] | None:
    try:
        raw = (await index.meta_prefix(expansion_key(titles))).get("")
        data = json.loads(raw) if raw else None
    except Exception:
        return None
    if isinstance(data, dict) and time.time() - float(data.get("t") or 0) < max_age_seconds and isinstance(data.get("v"), list):
        return [str(v) for v in data["v"]]
    return None


async def store_variants(index, titles: list[str], variants: list[str]) -> None:
    try:
        await index.meta_set(expansion_key(titles), json.dumps({"t": time.time(), "v": variants}))
    except Exception:
        pass


def discovery_locations(preferred: list[str]) -> list[str]:
    """Location words for discovery queries: the candidate's preferred states first, then a fixed list."""
    out = [" ".join(str(p).split()) for p in preferred or [] if str(p).strip()]
    return list(dict.fromkeys([*out, *DEFAULT_LOCATIONS]))
