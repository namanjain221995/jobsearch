"""What one search may spend, and how a requested title is widened.

Budgets: the base settings (OPENAI_SEARCH_CALLS, CLAUDE_SEARCH_CALLS, AI_WEB_SEARCHES_PER_CALL, ...) describe a
request of 50 results or fewer. A larger request scales them linearly (a request of 300 gets six times the
base) up to the hard ceiling of each setting (`*_MAX`). Discovery budgets are totals for the whole search and
are handed out round by round, so a search that reaches its target early does not spend the rest.

The AI web search is not held back by cost. It has two kinds of calls per provider that do not take from each other:
calls on the ATS hosts (OPENAI_SEARCH_CALLS, CLAUDE_SEARCH_CALLS) and, on top of them, calls on employers' own career
sites (OPENAI_OPEN_WEB_CALLS, CLAUDE_OPEN_WEB_CALLS). These settings are what ONE ROUND makes, side by side, for a
request of 50 or fewer (a larger request makes up to twice as many a round). Every further round makes them again with
new searches (`Plan.ai_round`), for as long as the engine's round loop goes on: until the requested number of verified
matching jobs is reached, two rounds in a row brought nothing new, or the time ceiling of a search is reached. The
`*_MAX` settings are safety ceilings of one whole search, far above what a normal search uses; they are not budgets.
AWS web search spends its paced share of the month's allowance in the first round (`Plan.first_round`).

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
ROUND_GROWTH = 2.0                 # a round of a large request makes at most this many times the calls of a round of a small one
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
    openai_open_calls: int = 0  # web-search calls on employers' own career sites, on top of the calls on ATS hosts
    claude_open_calls: int = 0
    open_searches: int = 0      # live web searches one open-web call may run (0: as many as a call on ATS hosts)
    followup_calls: int = 0     # further open-web calls of the first round, started when the first ones returned (see ai_orchestrator)
    open_engine_queries: int = 0   # open-web queries (no `site:`) of the plain search engines, first round
    aws_open_share: float = 0.0    # share of the AWS web search queries that ask "any site" for employers' own posting pages
    ai_reserve: int = 0         # OpenAI / Claude calls the AI web search leaves for the rest of the search (judging, audit, ...)
    lead_seconds: float = 0.0   # time box of one round's lead check (0 = none); grows with a round, up to twice
    base: tuple = ()            # calls of ONE round: (OpenAI on ATS hosts, Claude on ATS hosts, OpenAI open web, Claude open web)

    def per_round(self, total: int, round_no: int) -> int:
        """How much of a search-wide budget round `round_no` (1-based) may use."""
        share = math.ceil(total / max(1, self.rounds))
        return max(0, min(share, total - share * (round_no - 1)))

    def first_round(self, total: int, base: int, round_no: int) -> int:
        """How much of a budget round `round_no` may use when its base part (`base`: what a request of 50 or fewer
        gets) is spent at once in the first round and the rest, which only a larger request has, over the later
        rounds. A single-round plan spends everything in its one round."""
        first = total if self.rounds <= 1 else max(0, min(total, base))
        if round_no <= 1:
            return first
        rest = total - first
        share = math.ceil(rest / max(1, self.rounds - 1))
        return max(0, min(share, rest - share * (round_no - 2)))

    def ai_round(self, provider: str, round_no: int) -> tuple[int, int]:
        """(calls on ATS hosts, calls on employers' own career sites) of `provider` ("OpenAI" / "Claude") in a round:
        the round's calls (`base`) in every round, until the ceiling of the whole search is reached. A plan without
        base values (built by hand) spreads the totals evenly over the rounds, as before."""
        i = 0 if provider == "OpenAI" else 1
        ats, own = (self.openai_calls, self.openai_open_calls) if i == 0 else (self.claude_calls, self.claude_open_calls)
        if len(self.base) != 4:
            return self.per_round(ats, round_no), self.per_round(own, round_no)
        take = lambda total, each: max(0, min(each, total - each * (max(1, round_no) - 1)))
        return take(ats, self.base[i]), take(own, self.base[2 + i])


def _scaled(base: int, ceiling: int, target: int, scaling: bool) -> int:
    if not scaling or target <= BASE_REQUEST:
        return max(0, base)
    return max(0, min(max(base, ceiling), math.ceil(base * target / BASE_REQUEST)))


def open_web_on(settings: Settings) -> bool:
    """Whether a search looks at employers' own career sites at all. AI_OPEN_WEB_SHARE used to be the share of the
    web-search calls taken away from the ATS hosts; the open-web calls now come on top (budgets of their own), and
    the old setting is kept as the switch it also was: 0 = no open-web call or query, anything else = on."""
    return float(settings.ai_open_web_share) > 0


def plan_for(settings: Settings, target: int, deep: bool = True) -> Plan:
    s = settings
    on = bool(s.ai_budget_scaling)
    own = open_web_on(s)
    single = s.search_max_rounds <= 1
    # One round: the settings for a request of 50 or fewer, up to twice as many for a larger one (more would only queue
    # behind the per-provider concurrency and run into the round's time limit; a larger request gets more rounds instead).
    grow = min(ROUND_GROWTH, max(1.0, target / BASE_REQUEST)) if on else 1.0
    each = lambda value: math.ceil(max(0, value) * grow)
    base = (each(s.openai_search_calls), each(s.claude_search_calls),
            each(s.openai_open_web_calls) if own else 0, each(s.claude_open_web_calls) if own else 0)
    # The whole search: the round's calls again in every round, up to the safety ceilings (`*_MAX`). Without scaling,
    # or with a single round, a search makes one round's calls and no more.
    whole = lambda one, ceiling: max(one, ceiling) if one and on and not single else one
    openai_calls, claude_calls = whole(base[0], s.openai_search_calls_max), whole(base[1], s.claude_search_calls_max)
    openai_open, claude_open = whole(base[2], s.openai_open_web_calls_max), whole(base[3], s.claude_open_web_calls_max)
    followups = each(s.ai_open_web_followup_calls) if own and base[2] + base[3] > 0 else 0
    rounds_cap = max(1, s.search_max_rounds)
    # The judged slice: 40 % of the request, never below the old fixed size, within the ceiling.
    slice_cap = max(target * 2, target + 30)
    ai_ranked = s.max_ai_ranked_jobs
    if on and target > BASE_REQUEST:
        ai_ranked = min(max(s.max_ai_ranked_jobs, s.max_ai_ranked_jobs_max), max(s.max_ai_ranked_jobs, math.ceil(target * 0.4)))
    ai_ranked = min(ai_ranked, slice_cap)
    rank_calls = math.ceil(ai_ranked / max(1, s.ai_rank_batch_size))
    # + one model call for each open-web call of a provider whose search tool needs its career sites named beforehand
    # (ai_orchestrator._site_slice), + profile, expansion, audit, page extraction, retries on the other provider
    reserve = rank_calls + 12
    needed = openai_calls + claude_calls + 2 * openai_open + claude_open + followups * rounds_cap + reserve
    # The ceiling of all OpenAI / Claude calls follows what these budgets need (within its own ceiling), for a small
    # request too; and the web search never takes the calls the rest of the search needs (Plan.ai_reserve).
    ai_calls = s.max_ai_calls_per_search
    if on:
        ai_calls = min(max(s.max_ai_calls_per_search, s.max_ai_calls_per_search_max), max(s.max_ai_calls_per_search, needed))
    return Plan(
        target=target, rounds=1 if single else max(1, s.search_budget_rounds),
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
        openai_open_calls=openai_open if deep else 0, claude_open_calls=claude_open if deep else 0,
        open_searches=max(1, s.ai_open_web_searches_per_call), followup_calls=followups if deep else 0,
        open_engine_queries=max(0, s.search_engine_open_web_queries) if deep and own else 0,
        aws_open_share=min(1.0, max(0.0, float(s.aws_web_search_open_web_share))) if own else 0.0,
        ai_reserve=min(reserve, max(0, ai_calls - 1)), base=base,
        lead_seconds=max(0.0, float(s.lead_verification_seconds)) * grow,
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
