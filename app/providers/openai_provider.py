"""OpenAI via the official SDK and the Responses API.

Live discovery uses the hosted `web_search` tool (https://developers.openai.com/api/docs/guides/tools-web-search):
- `tool_choice="required"`: with "auto" a search is optional, and an answer without a search is worthless here;
- `filters.allowed_domains` / `blocked_domains` (up to 100 each, subdomains included) keep a call on the hosts the
  engine can read; `user_location` is the United States, with a state when the search is about one;
- `include=["web_search_call.action.sources"]` returns every address the tool retrieved. ONLY these (and
  `url_citation` annotations) become leads: an address the model merely wrote is dropped (`build_candidates`);
- one `web_search_call` of type `search` is one billed search (it returned 12 sources in every probe on
  2026-11-21, however many queries the model packed into it), so the prompt asks for one call per query;
  `open_page` / `find_in_page` actions are not searches, are not asked for, and are counted apart;
- the model's own list of leads is asked for as a strict JSON schema (`text.format`); it only adds title, employer
  and place to addresses the tool returned, and says which of them the model picked (`Leads.picks`);
- a call that composes its own searches (employers' own career sites) runs at a medium reasoning effort: at a low
  effort gpt-6-luna sent all its queries as ONE search and got one page of results for five employers (three of
  four probes on 2026-11-21); at medium it ran them one by one (two of two);
- a `site:` operator in a query is not a filter for this tool (`site_list_needed`): in two 14-call searches on
  2026-11-21 (model gpt-6-luna), 393 of the 518 addresses returned for 80 `site:<employer career site> "title"`
  queries were on other sites (job boards, resume examples). Only `filters.allowed_domains` holds a call to its
  sites, so the sites of a call have to be known before the request is sent.
A request the model or account rejects (HTTP 400) is repeated once the plain way, so discovery keeps working on a
model without one of these settings."""
import json
import re

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, BadRequestError

from ..job_normalizer import parse_ats
from ..schemas import Candidate

# Hosts of a call that names none itself (the orchestrator always passes the hosts of the search's scope).
ATS_DOMAINS = ["boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
               "myworkdayjobs.com", "apply.workable.com", "jobs.smartrecruiters.com"]
MAX_FILTER_DOMAINS = 100               # the web search tool takes up to 100 allowed or 100 blocked domains
SEARCH_MAX_OUTPUT_TOKENS = 8000        # reasoning and the list of leads of one web-search call (a list cut short loses only details: the addresses come from the tool)
LEADS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["leads"],
    "properties": {"leads": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["url", "title", "company", "location"],
        "properties": {"url": {"type": "string"}, "title": {"type": "string"}, "company": {"type": "string"},
                       "location": {"type": "string"}}}}},
}
SEARCH_HOW = ("\n\nHow to run the searches: send every query as its own web search call, one query per call. "
              "Do not open or read result pages.")
_SECRET = re.compile(r"sk-[A-Za-z0-9_\-*\.]{6,}")
_QUOTA_CODES = {"insufficient_quota", "billing_hard_limit_reached", "billing_not_active", "account_deactivated"}


class ProviderError(Exception):
    """Sanitized provider failure (never contains keys or resume text). `fatal`: asking this provider again in the
    same search is pointless (wrong key, no access, unknown model, quota or billing limit)."""
    fatal = False


def is_fatal(exc: Exception) -> bool:
    if not isinstance(exc, APIStatusError):
        return False
    if exc.status_code in (401, 403, 404):
        return True
    # "Billing, spend, and quota errors require action before retrying; do not treat every 429 as a temporary rate limit."
    return exc.status_code == 429 and str(getattr(exc, "code", "") or getattr(exc, "type", "") or "") in _QUOTA_CODES


def sanitize_error(exc: Exception) -> str:
    if isinstance(exc, APIStatusError):
        code = exc.status_code
        hint = {401: "authentication failed (check API key)", 403: "access denied", 404: "model or endpoint not found (check model name)",
                429: "rate limited or quota exceeded"}.get(code, "request failed")
        return f"HTTP {code}: {hint}"
    if isinstance(exc, APITimeoutError):
        return "request timed out"
    if isinstance(exc, APIConnectionError):
        return "connection error"
    return _SECRET.sub("[redacted]", f"{type(exc).__name__}")


def parse_json_array(text: str) -> list[dict]:
    """Leniently pull a JSON array of objects out of model text."""
    if not text:
        return []
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def parse_leads(text: str) -> list[dict]:
    """The model's list of leads: {"leads": [...]} as the schema asks, or, from a model that was not held to the
    schema, any JSON array of objects in its text. Never raises; a list that cannot be read is an empty list (the
    addresses the search tool returned are leads without it)."""
    text = (text or "").strip()
    if not text:
        return []
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""):
        try:
            data = json.loads(candidate) if candidate else None
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("leads"), list):
            return [d for d in data["leads"] if isinstance(d, dict)]
    return parse_json_array(text)


class OpenAIProvider:
    name = "OpenAI"
    site_list_needed = True           # a call is held to sites only by `filters.allowed_domains` (see the module text)

    def __init__(self, api_key: str, model: str, timeout: float = 180.0, reasoning_effort: str = "low", search_model: str = ""):
        self.model = model
        self.search_model = search_model or model         # web-search discovery may run on another model (OPENAI_SEARCH_MODEL)
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=2)
        # Extraction and scoring do not need deep reasoning; a low effort makes these calls much faster.
        self._reasoning = {"reasoning": {"effort": reasoning_effort}} if reasoning_effort else {}
        self._rich_search = True          # False once this model or account refused the finer web-search settings
        # Tokens and web searches of this search, for the cost report (counts only).
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "web_searches": 0, "page_opens": 0,
                      "ungrounded_addresses": 0}

    def _count(self, resp) -> None:
        usage = getattr(resp, "usage", None)
        for name in ("input_tokens", "output_tokens"):
            value = getattr(usage, name, 0)
            if isinstance(value, int):
                self.usage[name] += value
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0)
        if isinstance(cached, int):
            self.usage["cached_input_tokens"] += cached

    async def json_call(self, system: str, user: str, schema: dict, schema_name: str) -> dict:
        request = dict(model=self.model, instructions=system, input=user, store=False,
                       text={"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}})
        try:
            try:
                resp = await self.client.responses.create(**request, **self._reasoning)
            except BadRequestError:
                if not self._reasoning:
                    raise
                self._reasoning = {}          # this model has no reasoning-effort setting
                resp = await self.client.responses.create(**request)
            self._count(resp)
            return json.loads(resp.output_text)
        except ValueError as exc:
            raise ProviderError("model returned invalid JSON") from exc
        except Exception as exc:
            raise ProviderError(sanitize_error(exc)) from None

    async def web_discover(self, system: str, prompt: str, max_searches: int, restrict_to_ats: bool, *,
                           allowed_domains: list[str] | None = None, blocked_domains: list[str] | None = None,
                           region: str = "", own_searches: bool = False, timeout: float | None = None) -> tuple["Leads", int]:
        """Run live web search. Returns (leads, web searches actually run and billed); the leads are every address the
        search tool returned, the model's own picks first (`Leads.picks` of them).
        `allowed_domains` (the hosts of the call) wins over `restrict_to_ats`; `blocked_domains` keeps hosts out of a
        search that is not limited to hosts (job boards, and the ATS hosts other calls search); `region` is a US state
        for the approximate user location; `own_searches`: the call composes its searches itself (see the module
        text); `timeout` bounds this one call (one retry at most)."""
        location = {"type": "approximate", "country": "US"}
        if region:
            location["region"] = region
        tool: dict = {"type": "web_search", "user_location": location}
        if allowed_domains:
            tool["filters"] = {"allowed_domains": list(allowed_domains)[:MAX_FILTER_DOMAINS]}
        elif restrict_to_ats:
            tool["filters"] = {"allowed_domains": ATS_DOMAINS}
        elif blocked_domains:
            tool["filters"] = {"blocked_domains": list(blocked_domains)[:MAX_FILTER_DOMAINS]}
        client = self.client.with_options(timeout=timeout, max_retries=1) if timeout else self.client
        reasoning = self._reasoning
        if own_searches and (reasoning.get("reasoning") or {}).get("effort") in ("minimal", "low"):
            reasoning = {"reasoning": {"effort": "medium"}}
        rich = dict(tool_choice="required", max_output_tokens=SEARCH_MAX_OUTPUT_TOKENS, **reasoning,
                    text={"format": {"type": "json_schema", "name": "job_leads", "schema": LEADS_SCHEMA, "strict": True}})

        async def call(t: dict, **settings):
            return await client.responses.create(
                model=self.search_model, instructions=system, input=prompt + SEARCH_HOW, tools=[t],
                include=["web_search_call.action.sources"], max_tool_calls=max_searches, store=False, **settings)

        try:
            try:
                resp = await (call(tool, **rich) if self._rich_search else call(tool, tool_choice="auto"))
            except BadRequestError:
                # This model or account does not take one of the finer settings: ask again the long-standing way
                # (search optional, free-text answer), and with the plain tool when that is refused as well.
                try:
                    if not self._rich_search:
                        raise
                    resp = await call(tool, tool_choice="auto")
                except BadRequestError:
                    if not (blocked_domains or region):
                        raise
                    plain: dict = {"type": "web_search", "user_location": {"type": "approximate", "country": "US"}}
                    if "allowed_domains" in tool.get("filters", {}):
                        plain["filters"] = tool["filters"]
                    resp = await call(plain, tool_choice="auto")
                self._rich_search = False
        except Exception as exc:
            error = ProviderError(sanitize_error(exc))
            error.fatal = is_fatal(exc)
            raise error from None
        self._count(resp)
        sourced: dict[str, str] = {}
        searches = opened = 0
        text_parts: list[str] = []
        queries: list[str] = []
        for item in resp.output or []:
            data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            if data.get("type") == "web_search_call":
                action = data.get("action") or {}
                if (action.get("type") or "search") != "search":
                    opened += 1            # open_page / find_in_page: the model read a page; nothing is taken from that
                    continue
                searches += 1
                queries.extend(str(q) for q in (action.get("queries") or [action.get("query")]) if q)
                for src in (action.get("sources") or []):
                    if isinstance(src, dict) and src.get("url"):
                        sourced.setdefault(src["url"], src.get("title") or "")
            elif data.get("type") == "message":
                for part in data.get("content") or []:
                    if part.get("type") == "output_text":
                        text_parts.append(part.get("text") or "")
                        for ann in part.get("annotations") or []:
                            if ann.get("type") == "url_citation" and ann.get("url"):
                                sourced.setdefault(ann["url"], ann.get("title") or "")
        self.usage["web_searches"] += searches
        self.usage["page_opens"] += opened
        if searches == 0:
            # No live search happened: nothing in this response may be presented as a web result.
            return Leads(), 0
        leads, ungrounded = build_candidates(self.name, parse_leads("\n".join(text_parts)), sourced)
        leads.sites = searched_sites(queries)
        self.usage["ungrounded_addresses"] += ungrounded
        return leads, searches


def url_key(url: str) -> str:
    return re.sub(r"[?#].*$", "", (url or "").strip()).rstrip("/").lower()


class Leads(list):
    """The leads of one web-search call, the model's own picks first. `picks`: how many of them the model picked; the
    rest are addresses the search tool returned and the model did not list (the caller decides what they are worth:
    on an ATS host every one is an employer board or a posting, on the open web most are not). `sites`: the sites
    the executed searches named with a `site:` operator, as the search tool reports the queries it ran. `ages`:
    address -> days since the page was last updated, where the search tool says so (Claude's `page_age`)."""
    picks: int = 0
    sites: frozenset = frozenset()
    ages: dict = {}


_SITE_OPERATOR = re.compile(r"(?i)\bsite:([a-z0-9][a-z0-9.-]*\.[a-z]{2,})")


_AGO = re.compile(r"(?i)^\s*(\d{1,5})\s+(day|week|month|year)s?\s+ago\s*$")


def page_age_days(text, today=None) -> int | None:
    """Days since a page was last updated, from a search tool's "50 days ago" (the only form seen in 338 results on
    2026-11-21) or a plain date ("April 30, 2025", as the documentation's example shows). None when it says neither."""
    from datetime import date, datetime
    text = str(text or "").strip()
    found = _AGO.match(text)
    if found:
        return int(found.group(1)) * {"day": 1, "week": 7, "month": 30, "year": 365}[found.group(2).lower()]
    for form in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return max(0, ((today or date.today()) - datetime.strptime(text, form).date()).days)
        except ValueError:
            continue
    return None


def searched_sites(queries) -> frozenset:
    """Hosts named by a `site:` operator in the queries a search tool ran (without www.; a path after the host is dropped)."""
    return frozenset(m.group(1).lower().removeprefix("www.") for q in queries or [] for m in _SITE_OPERATOR.finditer(str(q)))


def build_candidates(provider: str, rows: list[dict], sourced: dict[str, str]) -> tuple[Leads, int]:
    """Leads of one web-search call. Returns (leads, addresses the model wrote that the search tool never returned).

    An address becomes a lead only when the search tool itself returned it (`sourced`: tool sources, result blocks,
    citations). What the model wrote (`rows`) adds title, employer and place to a returned address and puts the
    model's own picks first; an address that is in `rows` only is dropped and counted, so an invented, completed or
    repaired address can never become a lead. One exception keeps employer boards coming: the board root of an
    employer whose posting the tool returned (same ATS, same employer) is accepted, because the tool found that
    employer."""
    returned = {url_key(u): u for u in sourced if isinstance(u, str) and u.startswith(("http://", "https://"))}
    boards = {parsed[:2] for parsed in (parse_ats(u) for u in returned.values()) if parsed and parsed[1]}
    out: dict[str, Candidate] = {}
    ungrounded = 0
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        key = url_key(url)
        if key in out:
            continue
        details = dict(title=str(row.get("title") or "")[:200], company=str(row.get("company") or "")[:120],
                       location=str(row.get("location") or "")[:160], provider=provider)
        if key in returned:
            out[key] = Candidate(url=returned[key], cited=True, **details)
            continue
        parsed = parse_ats(url)
        if parsed and parsed[1] and not parsed[2] and parsed[:2] in boards:
            out[key] = Candidate(url=url, cited=False, **details)        # the board of an employer the tool returned
        else:
            ungrounded += 1
    leads = Leads(out.values())
    leads.picks = len(leads)
    for key, url in returned.items():
        if key not in out:
            leads.append(Candidate(url=url, title=(sourced.get(url) or "")[:200], provider=provider, cited=True))
    return leads, ungrounded
