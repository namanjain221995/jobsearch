"""What one search may spend, and how a requested title is widened.

Budgets: the base settings (OPENAI_SEARCH_CALLS, CLAUDE_SEARCH_CALLS, AI_WEB_SEARCHES_PER_CALL, ...) describe a
request of 50 results or fewer. A larger request scales them linearly (a request of 300 gets six times the
base) up to the hard ceiling of each setting (`*_MAX`). Discovery budgets are totals for the whole search and
are handed out round by round, so a search that reaches its target early does not spend the rest.

Title expansion: variants proposed by a model are only ever used to *find* postings (index matching and
discovery queries). Every posting found is still screened and scored against the candidate."""
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass

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
    needed = openai_calls + claude_calls + rank_calls + 12        # + profile, expansion, audit, page extraction, retries on the other provider
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
    )


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
